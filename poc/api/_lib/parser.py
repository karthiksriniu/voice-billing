"""Utterance -> line items. Deterministic grammar + fuzzy catalog match (DECISIONS.md D3).

No LLM anywhere. The point of a grammar is that its failures are legible: when it can't
parse, it knows it can't, and that feeds the confidence gate. A model that always returns
something confident is the exact failure mode Principle 2 is about.

Language data lives in lang/*.json, never here, so a second language is configuration.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

LANG_DIR = Path(__file__).parent / "lang"

# Outcome bands from PLAN.md. A CONFIRM costs the shopkeeper ~2s; a silent wrong line costs
# the relationship. The gate is deliberately pessimistic — we would rather ask twice.
ACCEPT_THRESHOLD = 0.72
CONFIRM_THRESHOLD = 0.42

# In billing, a fuzzy match is a feature — find the nearest thing the shop actually sells.
# In admin, the same behaviour is destructive: an unrecognised name means "create this",
# and quietly resolving it to a neighbour edits the wrong product's price. "maida" scores
# 0.857 against Wheat Flour, so the bar for "this is the existing SKU" sits above that.
ADMIN_SAME_ITEM_THRESHOLD = 0.90


@dataclass
class LineItem:
    product_id: str | None
    name: str
    qty: float
    unit: str
    unit_price: float          # rupees
    amount: float              # rupees
    confidence: float
    verdict: str               # accept | confirm | reject
    matched_on: str = ""       # which catalog form matched, for debugging
    price_led: bool = False    # "ten rupees of coriander" — amount was spoken, not derived
    match_score: float = 0.0   # catalog match strength, before ASR confidence is applied
    spoken_name: str = ""      # the item words as heard, before matching — admin needs this
                               # to create a new SKU rather than reprice a near neighbour
    needs_price: bool = False  # in the catalog by name but no price known yet (D4)
    combo: list[dict] = field(default_factory=list)   # parts of a "A plus B" line
    spoken_qty: float | None = None   # quantity as actually said. A price-led line derives
                                      # qty from the amount and the current price, which
                                      # overwrites it — admin needs the original to work
                                      # out a per-UOM rate rather than echoing the old one.
    raw: str = ""


@dataclass
class ParseResult:
    items: list[LineItem] = field(default_factory=list)
    command: str | None = None      # cancel_last | clear_all | total | None
    mode_switch: str | None = None  # admin | billing | None
    unparsed: list[str] = field(default_factory=list)
    transcript: str = ""
    number: float | None = None     # utterance was just a number — an answer to "what price?"
    match_score: float = 0.0        # strength of the best catalog match, for admin decisions
    unmatched: list[dict] = field(default_factory=list)   # named, priced, but not in catalog
    woke: bool = False              # the utterance was addressed to us by name
    customer_mobile: str = ""       # "phone number 98400 12345" — opens the bill against a
                                    # customer so their history can be pulled up


def _loose(key: str) -> str:
    """Fold vowel length out of a phonetic key: long and short are one sound here."""
    return key.replace("i", "e").replace("u", "o")


def _keys(aliases) -> set:
    """Normalised alias keys, with anything that normalises to nothing thrown away.

    Every language pack lists "+" as a join word, and norm() strips punctuation, so "+"
    arrived here as the empty string. An empty key in a lookup set is not inert: the
    phrase tests below pad with spaces, so `" " + "" + " "` matched any run of two
    spaces — which every utterance has, because ASR ends sentences with a full stop and
    norm() turns that into a padded separator. The effect was that _has_join answered
    True for every utterance in every language, so every multi-item dictation was treated
    as a single blended line: three items spoken, one item billed, at the last quantity
    heard. Nothing empty gets in here again.
    """
    return {k for a in aliases for k in (norm(a),) if k}


class Lang:
    """A loaded language pack. Builds reverse lookup tables once."""

    def __init__(self, code: str = "ta-en"):
        self.data = json.loads((LANG_DIR / f"{code}.json").read_text(encoding="utf-8"))
        self.digits = self._reverse(self.data["digits"], float)
        self.fractions = self._reverse(self.data["fractions"], float)
        self.money = _keys(self.data["money"]["aliases"])
        self.fillers = _keys(self.data["fillers"])
        self.rules = self.data["phonetic_rules"]
        self.script = self.data["script"]
        self._script_chars = (set(self.script["consonants"])
                              | set(self.script["vowels"])
                              | set(self.script["vowel_signs"])
                              | {self.script["virama"]})

        self.units = {}
        for canon, spec in self.data["units"].items():
            for a in spec["aliases"]:
                if norm(a):
                    self.units[norm(a)] = canon
        self.unit_spec = self.data["units"]

        # Multi-word measures are matched on the raw string before tokenising, since
        # "arai kilo" must not be read as the number 0.5 followed by the unit kg.
        self.measures = {
            k: m for m in self.data["measures"].values() for a in m["aliases"]
            for k in (norm(a),) if k
        }
        self.modes = {
            k: mode for mode, al in self.data["modes"].items() for a in al
            for k in (norm(a),) if k
        }
        self.commands = {
            k: cmd for cmd, al in self.data["commands"].items() for a in al
            for k in (norm(a),) if k
        }
        # Numerals get the same phonetic fallback as item names. An exact-string table
        # cannot keep up with how an ASR chooses to spell a spoken number: Sarvam writes
        # 300 as "முன்னூறு" where the table said "முந்நூறு", so the word was not a number
        # at all, the quantity silently fell back to 1 and 300 g of onion billed as 7
        # paise. Keys that two different values share are left out rather than guessed —
        # Hindi "saat" (7) and "saath" (60) collapse to the same sound, and inventing an
        # answer there is exactly the silent error the confidence gate exists to prevent.
        # Two layers. The strict one is the ordinary phonetic key. The loose one also
        # folds vowel length (i/e, u/o), because that is the distinction an ASR flips most
        # readily: Sarvam wrote seventy as "ஏழுபது" where the table has "எழுபது", one
        # vowel apart, and the price silently fell back to whatever the catalog already
        # held. A key that two values share is dropped from both layers — Kannada twenty
        # and seventy are near-homophones, and guessing between them would put the wrong
        # number on a bill without anyone noticing.
        strict: dict[str, set] = {}
        loose: dict[str, set] = {}
        for table in (self.digits, self.fractions):
            for alias, val in table.items():
                key = self.phonetic(alias)
                strict.setdefault(key, set()).add(val)
                loose.setdefault(_loose(key), set()).add(val)
        self.numeral_sounds = {k: v.pop() for k, v in strict.items() if len(v) == 1}
        self.numeral_sounds_loose = {k: v.pop() for k, v in loose.items() if len(v) == 1}

        self.wake = sorted(_keys(self.data.get("wake", [])), key=len, reverse=True)
        self.join = _keys(self.data.get("join", []))
        self.customer_trigger = sorted(_keys(self.data.get("customer_trigger", [])),
                                       key=len, reverse=True)

    def canonical_unit(self, unit: str) -> str:
        """Any spelling of a unit -> the canonical key ("Kg", "KILO", "கிலோ" -> "kg").

        Units reach the catalog from a free-text box in the SKU editor, so what is stored
        is whatever was typed. An uncanonicalised unit is not a cosmetic problem: a shop
        holding "Kg" made _to_canonical find no conversion for "500 gram", leave the 500
        alone and bill 500 x the per-kilo price. Resolving the stored unit through the
        same alias table as the spoken one is what stops that.
        """
        return self.units.get(norm(unit or ""), (unit or "").strip())

    @staticmethod
    def _reverse(table: dict, cast) -> dict:
        return {k: cast(v) for v, aliases in table.items() for a in aliases
                for k in (norm(a),) if k}

    def translit(self, s: str) -> str:
        """Tamil script -> Latin. Tamil is an abugida: a bare consonant carries an inherent
        'a', which a vowel sign replaces and the virama removes."""
        sc = self.script
        out, i = [], 0
        while i < len(s):
            c = s[i]
            if c in sc["consonants"]:
                nxt = s[i + 1] if i + 1 < len(s) else ""
                if nxt == sc["virama"]:
                    out.append(sc["consonants"][c])
                    i += 2
                elif nxt in sc["vowel_signs"]:
                    out.append(sc["consonants"][c] + sc["vowel_signs"][nxt])
                    i += 2
                else:
                    out.append(sc["consonants"][c] + "a")
                    i += 1
            elif c in sc["vowels"]:
                out.append(sc["vowels"][c])
                i += 1
            else:
                out.append(c)
                i += 1
        return "".join(out)

    def phonetic(self, s: str) -> str:
        """Fold both scripts into one key space.

        Two kinds of variance to absorb. Romanised Tamil spells the same word many ways
        (thuvaram/tuvaram, chakkarai/sakkarai). And a code-mixed ASR returns English words
        in Tamil script — 'sugar' comes back as 'சுகர்', matching neither the Latin catalog
        name nor the Tamil one. Transliterating first puts both on the same footing.

        Soundex and Metaphone are English-only and actively mislead on romanised Tamil, so
        the collapse is an ordered rule list from the language pack instead.
        """
        out = self.translit(s) if any(c in self._script_chars for c in s) else s
        for src, dst in self.rules:
            out = out.replace(src, dst)
        return re.sub(r"(.)\1+", r"\1", out)


# ASR returns amounts as symbols ("₹10", "Rs.10"), and the symbol is punctuation that the
# strip below would discard — taking the price-led signal with it and leaving a bare number
# the grammar reads as a quantity. Rewrite to the word form, in the order the grammar wants.
_CURRENCY_RE = re.compile(r"(?:₹|\brs\.?)\s*(\d+(?:\.\d+)?)")

# Item boundary marker. Carries no letters or digits, so it never reaches an item name.
SEP = "¶"


def norm(s: str) -> str:
    """Lowercase, strip punctuation and accents, collapse whitespace."""
    s = unicodedata.normalize("NFKC", s).lower().strip()
    s = _CURRENCY_RE.sub(r"\1 rupees ", s)
    # Commas, semicolons and the Devanagari danda are how the ASR marks where one
    # dictated item ends and the next begins — by far the most reliable boundary signal
    # we get. The strip below would flatten them to spaces, so they become separators
    # first. (SEP itself must survive the strip, hence its place in the allowed set.)
    s = re.sub(r"[,;:!?\u0964\u0965]+", f" {SEP} ", s)
    s = re.sub(rf"[^\w\s\u0900-\u0D7F\u200c\u200d.{SEP}]", " ", s)
    # Keep decimal points; turn every other dot into an explicit separator. A trailing dot
    # made "ரூபாய்." match no money word, but simply deleting it also threw away the
    # sentence boundary — which is the clearest signal that one dictated item has ended.
    s = re.sub(r"(?<!\d)\.|\.(?!\d)", f" {SEP} ", s)
    return re.sub(r"\s+", " ", s).strip()


def find_phrase(text: str, phrase: str) -> bool:
    """Whole-phrase containment. Deliberately not a `\\b` regex: Tamil words routinely end
    in a virama (U+0BCD), a combining mark that isn't a word character, so `\\b` fails to
    fire where you'd expect. Space padding is dumber and correct for both scripts."""
    return f" {phrase} " in f" {text} "


def cut_phrase(text: str, phrase: str, repl: str = " ") -> str:
    """Remove or replace a whole phrase. Space-delimited, so 'kaal kilo' does not fire
    inside 'mukkaal kilo' — a silent quantity error if it did (0.25 instead of 0.75)."""
    return f" {text} ".replace(f" {phrase} ", f" {repl} ").strip()


# How close a spoken phrase has to sound to a command before we act on it. Commands are a
# tiny closed set and acting on the wrong one is cheap to undo, so this sits below the
# catalog's bar — but not so low that an item name can trip a command.
PHRASE_THRESHOLD = 0.84


class Parser:
    def __init__(self, lang: Lang, catalog):
        self.lang = lang
        self.catalog = catalog

    def _find_spoken(self, text: str, phrases: dict) -> tuple[str, object] | None:
        """Locate a fixed phrase in the utterance by how it SOUNDS, not how it is spelt.

        Item names have been matched phonetically from the start; commands, modes and the
        wake word were compared as exact strings, and in a regional shop that made every
        one of them dead. Sarvam is told the shop's language, so an English command spoken
        in a Tamil shop comes back in Tamil script: "cash received" arrives as
        "கேஷ் ரிசீவ்ட்" and "finalize the bill" as "ஃபைனலைஸ் த பில்". Neither can ever
        equal its Latin alias. Folding both sides through the same phonetic key is what
        the catalog already does; this brings the fixed phrases up to the same footing.

        Longest phrase first, so "billing mode" is not shadowed by "bill".
        """
        toks = text.split()
        if not toks:
            return None
        scored = []
        for phrase in sorted(phrases, key=len, reverse=True):
            if find_phrase(text, phrase):                 # exact wins, and costs nothing
                return phrase, phrases[phrase]
            want = self.lang.phonetic(phrase)
            n = len(phrase.split())
            for i in range(len(toks) - n + 1):
                window = " ".join(toks[i:i + n])
                score = SequenceMatcher(None, want, self.lang.phonetic(window)).ratio()
                if score >= PHRASE_THRESHOLD:
                    scored.append((score, window, phrases[phrase]))
        if not scored:
            return None
        scored.sort(key=lambda x: -x[0])
        best = scored[0]
        # Two different commands that sound equally like what was said is not a close call
        # to be broken by a hundredth of a similarity score — "bill me" and "bill mudi"
        # (close the bill) are one vowel apart and mean opposite things. Refuse instead.
        rival = next((s for s in scored if s[2] != best[2]), None)
        if rival and best[0] - rival[0] < 0.06:
            return None
        return best[1], best[2]

    # -- number handling ----------------------------------------------------

    def _value(self, tok: str) -> float | None:
        if re.fullmatch(r"\d+(\.\d+)?", tok):
            return float(tok)
        exact = self.lang.digits.get(tok, self.lang.fractions.get(tok))
        if exact is not None:
            return exact
        # Spelling the word differently must not stop it being a number (see
        # Lang.numeral_sounds). Only for tokens that carry letters — punctuation and the
        # measure markers have no business here.
        if len(tok) > 2 and any(c.isalpha() for c in tok):
            key = self.lang.phonetic(tok)
            hit = self.lang.numeral_sounds.get(key)
            return hit if hit is not None else self.lang.numeral_sounds_loose.get(_loose(key))
        return None

    def _read_number(self, toks: list[str], i: int) -> tuple[float | None, int]:
        """Read a possibly-compound number starting at i. Returns (value, next_index).

        Tamil composes numerals multiplicatively then additively, same as English:
          rendu nooru        -> 2 * 100      (multiplier before a larger scale)
          nooru pathu        -> 100 + 10     (anything smaller is added)
          irubathi anju      -> 20 + 5
          rendu arai         -> 2 + 0.5      (fractions are just smaller)

        The additive arm previously required the follower to be under 10, so hundreds and
        tens never combined and "nooru pathu rubai" billed as Rs10 instead of Rs110.
        """
        val = self._value(toks[i])
        if val is None:
            return None, i
        j = i + 1
        while j < len(toks):
            nxt = self._value(toks[j])
            if nxt is None:
                break
            if nxt >= 100 and val < nxt:          # rendu nooru = 200 (multiplier)
                val *= nxt
            elif nxt < val:                       # anything smaller is additive:
                val += nxt                        # nooru pathu = 110, irubathi anju = 25,
            else:                                 # rendu arai = 2.5
                break
            j += 1
        return val, j

    # -- main entry ---------------------------------------------------------

    def parse(self, transcript: str, asr_confidence: float = 1.0,
              mode: str = "billing") -> ParseResult:
        res = ParseResult(transcript=transcript)
        text = norm(transcript)
        if not text:
            return res

        # "Chitti, two kilo sugar" — the name is how the shopkeeper gets the phone's
        # attention with both hands full. It carries no meaning past that, and left in
        # place it would be matched against the catalog like any other word, so it comes
        # out first. Anywhere in the utterance, not just the front: the recorder often
        # opens mid-word and catches the tail of it.
        wake_table = {w: True for w in self.lang.wake}
        while True:
            hit = self._find_spoken(text, wake_table)
            if not hit:
                break
            res.woke = True
            text = cut_phrase(text, hit[0])
        text = text.strip(f" {SEP}").strip()
        if not text:
            return res

        # Longest phrase first, so "billing mode" is not shadowed by a shorter alias.
        # A customer number is read before anything else: it is not an item, and leaving
        # its digits in the text would have the grammar bill ten kilos of something.
        text, res.customer_mobile = self._take_customer(text)
        if res.customer_mobile and not text.strip():
            return res

        # Modes and commands compete in ONE contest rather than one after the other.
        # Run separately, a fuzzy mode match consumed the words before an EXACT command
        # ever got to look at them: "bill me" scored 0.857 against the mode "bill mode"
        # and was swallowed, so the command that starts a bill did nothing at all. Which
        # table a phrase came from is not evidence about what was said.
        table = {p: ("mode", v) for p, v in self.lang.modes.items()}
        table |= {p: ("command", v) for p, v in self.lang.commands.items()}
        hit = self._find_spoken(text, table)
        if hit:
            kind, value = hit[1]
            if kind == "mode":
                res.mode_switch = value
            else:
                res.command = value
            text = cut_phrase(text, hit[0])

        # "add item lemonade 50 rupees" names the thing before its price, which is admin
        # word order regardless of which screen is open. Parsing it as billing would read
        # the 50 as a quantity and split the entry in half.
        if res.command == "add_item":
            mode = "admin"

        if not text:
            return res

        # Multi-word measures collapse to a single token before splitting, so "arai kilo"
        # survives as one concept rather than becoming 0.5 followed by kg.
        # Rewrite to the bare fraction, not to "arai": "one and half kilo" would otherwise
        # become "one arai kilo", and "arai kilo" is a measure alias meaning 0.5 kg — which
        # replaced the 1 instead of adding to it.
        for phrase in ("and a half", "and half", "மற்றும் அரை"):
            text = text.replace(f" {phrase} ", " 0.5 ")

        for phrase in sorted(self.lang.measures, key=len, reverse=True):
            spec = self.lang.measures[phrase]
            text = cut_phrase(text, phrase, f"§{spec['qty']}§{spec['unit']}")

        for chunk in self._split_items(text, mode):
            # "plantation AA 800 gram plus pee berry 200 gram" is one thing the customer is
            # buying — a blend — so it becomes a single line carrying both parts, not two.
            if self._has_join(chunk) or self._um_parts(chunk):
                combined = self._parse_combo(chunk, asr_confidence)
                if combined:
                    res.items.append(combined)
                    continue

            item, info = self._parse_one(chunk, asr_confidence)
            if item:
                res.items.append(item)
                res.match_score = max(res.match_score, item.match_score)
            elif info and "number" in info:
                res.number = info["number"]
            elif info:
                res.unmatched.append(info)
            elif chunk.strip():
                res.unparsed.append(chunk.strip())
        return res

    def _take_customer(self, text: str) -> tuple[str, str]:
        """Pull "phone number 98400 12345" out of the utterance, returning the rest.

        Requires the trigger *and* ten digits: "number" is also the unit alias for pieces,
        so a bare mention must never open a customer. Digits are concatenated because ASR
        groups them ("98400 12345") and, being spoken aloud, they can also arrive as
        separate words. The digits must be removed from the text — left in place, the
        grammar would happily bill ten kilos of something.
        """
        for phrase in self.lang.customer_trigger:
            if not find_phrase(text, phrase):
                continue
            before, after = f" {text} ".split(f" {phrase} ", 1)
            toks, digits, used = after.split(), "", 0
            for i, tok in enumerate(toks):
                if tok.isdigit():
                    digits += tok
                else:
                    val = self._value(tok)
                    if val is None or not float(val).is_integer() or not 0 <= val <= 9:
                        break
                    digits += str(int(val))
                used = i + 1
                if len(digits) >= 10:
                    break
            if len(digits) < 10:
                continue
            mobile = digits[2:12] if digits.startswith("91") and len(digits) >= 12 else digits[-10:]
            rest = (before + " " + " ".join(toks[used:])).strip()
            return rest, mobile
        return text, ""

    def _has_join(self, chunk: str) -> bool:
        return any(j and find_phrase(chunk.strip(), j) for j in self.lang.join)

    def _um_parts(self, chunk: str) -> list[str] | None:
        """Tamil conjoins nouns with a "-um" suffix on each: "sakkaraiyum vengayamum".
        There is no separate word to split on, so two or more item words carrying the
        suffix in one breath is itself the signal. Units and money are excluded — plenty
        of ordinary words end in um."""
        toks = chunk.split()
        marked = [i for i, tk in enumerate(toks)
                  if tk.endswith("ும்") and len(tk) > 3
                  and tk not in self.lang.units and tk not in self.lang.money
                  and tk not in self.lang.fillers]
        if len(marked) < 2:
            return None
        parts, start = [], 0
        for i in marked[:-1]:
            parts.append(" ".join(toks[start:i + 1]))
            start = i + 1
        parts.append(" ".join(toks[start:]))
        return [p for p in parts if p.strip()]

    def _parse_combo(self, chunk: str, asr_conf: float) -> LineItem | None:
        """Parse "A <qty> plus B <qty>" into one line.

        The customer buys a blend; the bill should say so. Amount is the sum of the parts,
        each priced at its own rate, so 800g of a Rs800/kg bean plus 200g of a Rs1200/kg
        one totals Rs880 — not an average, which would misprice every blend.
        """
        pattern = "|".join(re.escape(j) for j in sorted(self.lang.join, key=len, reverse=True) if j)
        parts = [p.strip() for p in re.split(rf"\s(?:{pattern})\s", f" {chunk} ") if p.strip()]
        if len(parts) < 2:
            parts = self._um_parts(chunk) or []
        if len(parts) < 2:
            return None

        items = []
        for part in parts:
            it, _ = self._parse_one(part, asr_conf)
            if it is None:
                return None                    # all-or-nothing: a half-parsed blend is worse
            items.append(it)

        total = round(sum(i.amount for i in items), 2)
        return LineItem(
            product_id=None,
            name=" + ".join(i.name for i in items),
            qty=round(sum(i.qty for i in items), 3),
            unit=items[0].unit,
            unit_price=0.0,
            amount=total,
            confidence=round(min(i.confidence for i in items), 3),
            verdict=("accept" if min(i.confidence for i in items) >= ACCEPT_THRESHOLD
                     else "confirm" if min(i.confidence for i in items) >= CONFIRM_THRESHOLD
                     else "reject"),
            match_score=min(i.match_score for i in items),
            needs_price=any(i.needs_price for i in items),
            combo=[{"product_id": i.product_id, "name": i.name, "qty": i.qty,
                    "unit": i.unit, "unit_price": i.unit_price, "amount": i.amount}
                   for i in items],
            raw=chunk.strip(),
        )

    def _split_items(self, text: str, mode: str = "billing") -> list[str]:
        """Split a multi-item utterance into one chunk per item.

        Three signals, in order of reliability:
          1. A sentence boundary from the ASR (SEP), inserted by norm().
          2. An explicit connector — "and", "மற்றும்", "apparam".
          3. A price. In dictation an item *ends* at its price ("potato 1 kilo 100 rupees
             sugar 1 kilo 45 rupees"), so a money word closes the chunk — but only once the
             chunk already holds an item word. Without that guard this would also split
             price-led billing, where the money comes first: "ten rupees | coriander".
          4. A fresh quantity after an item name — billing only. Billing has neither
             prices nor connectors ("two kilo sugar one kilo onion" is one breath with
             nothing to break on), and there the quantity leads each item. Admin puts the
             name first ("potato 1 kilo 100 rupees"), so the same rule would cut every
             entry in half. The two modes have opposite word order; the split has to know
             which one it is in.
        """
        # Commas already arrived here as SEP (norm() converts them), so there is no
        # comma left to split on.
        parts = [p.strip() for p in re.split(rf"{SEP}|\b(?:and|apparam|அப்புறம்)\b", text)
                 if p.strip()]

        out = []
        for part in parts:
            chunk, seen_item = [], False
            # A part holding a join keyword, or a Tamil-style "-um … -um" conjunction, is
            # one blend. Emit it whole: the quantity rule below would otherwise cut
            # "plantation 800 gram plus pee berry 200 gram" apart before _parse_combo ever
            # saw it, which is exactly why `plus` stopped working in billing.
            if self._has_join(part) or self._um_parts(part):
                out.append(part)
                continue

            toks = part.split()
            for pos, tok in enumerate(toks):
                is_qty = self._value(tok) is not None or tok.startswith("§")
                # A quantity that follows a named item opens the next one. Guarded by
                # seen_item so a leading "two kilo…" and a compound numeral ("irubathi
                # anju") never split themselves.
                if is_qty and seen_item and chunk and mode != "admin":
                    out.append(" ".join(chunk))
                    chunk, seen_item = [], False
                chunk.append(tok)
                if tok in self.lang.money and seen_item:
                    out.append(" ".join(chunk))
                    chunk, seen_item = [], False
                    continue
                if not (is_qty
                        or tok in self.lang.units
                        or tok in self.lang.money
                        or tok in self.lang.fillers):
                    seen_item = True
            if chunk:
                out.append(" ".join(chunk))
        return [c for c in out if c.strip()]

    def _parse_one(self, chunk: str, asr_conf: float) -> tuple[LineItem | None, dict | None]:
        """Returns (line item, info). When nothing matched the catalog, `info` carries what
        was heard — a bare number (an answer to "what price?") or a name plus a spoken price
        (a new SKU in admin mode). Discarding those was what made both flows impossible."""
        toks = [t for t in chunk.split() if t]
        qty: float | None = None
        unit: str | None = None
        money_amount: float | None = None
        rest: list[str] = []

        i = 0
        while i < len(toks):
            tok = toks[i]

            if tok.startswith("§"):                       # collapsed measure
                _, q, u = tok.split("§")
                qty, unit = float(q), u
                i += 1
                continue

            val, nxt = self._read_number(toks, i)
            if val is not None:
                # A number followed by a money word is price-led: "ten rupees of coriander".
                if nxt < len(toks) and toks[nxt] in self.lang.money:
                    money_amount = val
                    i = nxt + 1
                else:
                    qty = val
                    i = nxt
                continue

            if tok in self.lang.units and unit is None:
                unit = self.lang.units[tok]
                i += 1
                continue

            if tok in self.lang.fillers or tok in self.lang.money:
                i += 1
                continue

            # ASR ends sentences with a full stop, and norm() keeps "." so decimals
            # survive. A lone dot must not become part of the SKU name — that is how a
            # catalog ends up holding "பொட்டேட்டோ .".
            if any(c.isalnum() for c in tok):
                rest.append(tok.strip("."))
            i += 1

        if not rest:
            # No item words at all — just a number. That is how a shopkeeper answers
            # "what's the price?", so it has to survive rather than be dropped as noise.
            val = money_amount if money_amount is not None else qty
            return None, ({"number": val} if val is not None else None)

        name = " ".join(rest)
        product, score, matched = self.catalog.match(name, self.lang)
        if product is None:
            return None, {"name": name, "qty": qty, "unit": unit,
                          "money": money_amount, "raw": chunk.strip()}

        item = self._build(product, score, matched, qty, unit, money_amount, asr_conf, chunk)
        item.spoken_name = name
        return item, None

    def _build(self, product, score, matched, qty, unit, money, asr_conf, raw) -> LineItem:
        spoken_qty = qty
        product_unit = self.lang.canonical_unit(product["unit"]) or product["unit"]
        unit = unit or product_unit
        price = float(product["unit_price"])
        price_led = money is not None

        if price_led:
            amount = float(money)
            qty = round(amount / price, 3) if price > 0 else 0.0
        else:
            if qty is None:
                qty = 1.0
            qty = self._to_canonical(qty, unit, product_unit)
            unit = product_unit
            amount = round(qty * price, 2)

        # Confidence is the product of how well we heard it and how well it matched a real
        # product. Both have to hold — a crisp transcript of a word not in the catalog is
        # exactly as untrustworthy as a mumbled match.
        conf = round(asr_conf * score, 3)
        verdict = (
            "accept" if conf >= ACCEPT_THRESHOLD
            else "confirm" if conf >= CONFIRM_THRESHOLD
            else "reject"
        )
        return LineItem(
            spoken_qty=spoken_qty,
            product_id=product["id"], name=product["name"], qty=qty, unit=unit,
            unit_price=price, amount=round(amount, 2), confidence=conf, verdict=verdict,
            matched_on=matched, price_led=price_led, match_score=score,
            needs_price=price <= 0, raw=raw.strip(),
        )

    def _to_canonical(self, qty: float, spoken_unit: str, product_unit: str) -> float:
        """Convert a spoken unit to the product's pricing unit — '500 gram' priced per kg.

        Both sides go through the alias table first. Comparing the raw strings meant a
        catalog holding "Kg" never matched the spoken "kg", found no conversion, and
        returned the quantity untouched — 500 gram of a Rs50/kg item billed as Rs25,000.
        """
        spoken = self.lang.canonical_unit(spoken_unit)
        target_unit = self.lang.canonical_unit(product_unit)
        if spoken == target_unit:
            return qty
        spec = self.lang.unit_spec.get(spoken, {})
        target = self.lang.unit_spec.get(target_unit, {})
        if "multiplier" in spec:                                    # dozen -> pieces
            return qty * spec["multiplier"]
        if "base_grams" in spec and "base_grams" in target:
            return round(qty * spec["base_grams"] / target["base_grams"], 4)
        return qty


class Catalog:
    """In-memory catalog with fuzzy match. Shop catalogs are 100-400 SKUs, so an exhaustive
    scan is free and beats any index."""

    def __init__(self, products: list[dict]):
        self.products = products
        self._keys: dict[str, list[tuple[str, str]]] = {}

    def _forms(self, p: dict, lang: Lang) -> list[tuple[str, str]]:
        """Every string this product could be called, with its phonetic key."""
        if p["id"] not in self._keys:
            raw = [p["name"], p.get("name_ta") or "", p.get("short_desc") or ""]
            raw += p.get("aliases") or []
            self._keys[p["id"]] = [
                (norm(f), lang.phonetic(norm(f))) for f in raw if f and f.strip()
            ]
        return self._keys[p["id"]]

    def match(self, query: str, lang: Lang) -> tuple[dict | None, float, str]:
        q = norm(query)
        if not q:
            return None, 0.0, ""
        qp = lang.phonetic(q)
        best, best_score, best_form = None, 0.0, ""

        for p in self.products:
            for form, form_key in self._forms(p, lang):
                if form == q:
                    return p, 1.0, form
                score = SequenceMatcher(None, qp, form_key).ratio()
                # Containment is asymmetric, and treating it as symmetric was picking the
                # wrong SKU whenever one name contained another.
                #
                #   form in q — the shopkeeper said the catalog name plus extra words
                #               ("one packet sugar"). Strong: everything the catalog knows
                #               about was actually said.
                #   q in form — the catalog name has words the shopkeeper never said
                #               ("pea berry" inside "cherry pea berry"). Those extra words
                #               are evidence AGAINST, so the bonus is scaled by how much of
                #               the name was really spoken. A flat bonus here let
                #               "cherry pea berry" (0.88) beat the intended "pee berry"
                #               (0.80) every time.
                if form and form in q:
                    score = max(score, 0.92)
                elif q and q in form:
                    score = max(score, 0.92 * (len(q) / len(form)))
                if score > best_score:
                    best, best_score, best_form = p, score, form

        return (best, round(best_score, 3), best_form) if best_score >= 0.55 else (None, 0.0, "")
