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


class Lang:
    """A loaded language pack. Builds reverse lookup tables once."""

    def __init__(self, code: str = "ta-en"):
        self.data = json.loads((LANG_DIR / f"{code}.json").read_text(encoding="utf-8"))
        self.digits = self._reverse(self.data["digits"], float)
        self.fractions = self._reverse(self.data["fractions"], float)
        self.money = {norm(a) for a in self.data["money"]["aliases"]}
        self.fillers = {norm(f) for f in self.data["fillers"]}
        self.rules = self.data["phonetic_rules"]
        self.script = self.data["script"]

        self.units = {}
        for canon, spec in self.data["units"].items():
            for a in spec["aliases"]:
                self.units[norm(a)] = canon
        self.unit_spec = self.data["units"]

        # Multi-word measures are matched on the raw string before tokenising, since
        # "arai kilo" must not be read as the number 0.5 followed by the unit kg.
        self.measures = {
            norm(a): m for m in self.data["measures"].values() for a in m["aliases"]
        }
        self.modes = {
            norm(a): mode for mode, al in self.data["modes"].items() for a in al
        }
        self.commands = {
            norm(a): cmd for cmd, al in self.data["commands"].items() for a in al
        }

    @staticmethod
    def _reverse(table: dict, cast) -> dict:
        return {norm(a): cast(v) for v, aliases in table.items() for a in aliases}

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
        out = self.translit(s) if any("஀" <= c <= "௿" for c in s) else s
        for src, dst in self.rules:
            out = out.replace(src, dst)
        return re.sub(r"(.)\1+", r"\1", out)


# ASR returns amounts as symbols ("₹10", "Rs.10"), and the symbol is punctuation that the
# strip below would discard — taking the price-led signal with it and leaving a bare number
# the grammar reads as a quantity. Rewrite to the word form, in the order the grammar wants.
_CURRENCY_RE = re.compile(r"(?:₹|\brs\.?)\s*(\d+(?:\.\d+)?)")


def norm(s: str) -> str:
    """Lowercase, strip punctuation and accents, collapse whitespace."""
    s = unicodedata.normalize("NFKC", s).lower().strip()
    s = _CURRENCY_RE.sub(r"\1 rupees ", s)
    s = re.sub(r"[^\w\s஀-௿.]", " ", s)
    # Keep decimal points, drop every other dot. ASR ends sentences with one, and a token
    # of "ரூபாய்." matches no money word — it silently became part of the item name.
    s = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", s)
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


class Parser:
    def __init__(self, lang: Lang, catalog):
        self.lang = lang
        self.catalog = catalog

    # -- number handling ----------------------------------------------------

    def _value(self, tok: str) -> float | None:
        if re.fullmatch(r"\d+(\.\d+)?", tok):
            return float(tok)
        return self.lang.digits.get(tok, self.lang.fractions.get(tok))

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

    def parse(self, transcript: str, asr_confidence: float = 1.0) -> ParseResult:
        res = ParseResult(transcript=transcript)
        text = norm(transcript)
        if not text:
            return res

        # Longest phrase first, so "billing mode" is not shadowed by a shorter alias.
        for phrase in sorted(self.lang.modes, key=len, reverse=True):
            if find_phrase(text, phrase):
                res.mode_switch = self.lang.modes[phrase]
                text = cut_phrase(text, phrase)
                break

        for phrase in sorted(self.lang.commands, key=len, reverse=True):
            if find_phrase(text, phrase):
                res.command = self.lang.commands[phrase]
                text = cut_phrase(text, phrase)
                break

        if not text:
            return res

        # Multi-word measures collapse to a single token before splitting, so "arai kilo"
        # survives as one concept rather than becoming 0.5 followed by kg.
        for phrase in sorted(self.lang.measures, key=len, reverse=True):
            spec = self.lang.measures[phrase]
            text = cut_phrase(text, phrase, f"§{spec['qty']}§{spec['unit']}")

        for chunk in self._split_items(text):
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

    def _split_items(self, text: str) -> list[str]:
        """Split a multi-item utterance. A new item starts at a number or measure that
        follows a matched item, which is why splitting happens after normalisation."""
        parts = re.split(r"\b(?:and|மற்றும்|apparam|அப்புறம்)\b|,", text)
        return [p for p in parts if p.strip()]

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
        unit = unit or product["unit"]
        price = float(product["unit_price"])
        price_led = money is not None

        if price_led:
            amount = float(money)
            qty = round(amount / price, 3) if price > 0 else 0.0
        else:
            if qty is None:
                qty = 1.0
            qty = self._to_canonical(qty, unit, product["unit"])
            unit = product["unit"]
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
        """Convert a spoken unit to the product's pricing unit — '500 gram' priced per kg."""
        if spoken_unit == product_unit:
            return qty
        spec = self.lang.unit_spec.get(spoken_unit, {})
        target = self.lang.unit_spec.get(product_unit, {})
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
                # A spoken item often carries extra words ("sugar packet"); containment is
                # strong evidence the shopkeeper named this product.
                if form in q or q in form:
                    score = max(score, 0.88)
                if score > best_score:
                    best, best_score, best_form = p, score, form

        return (best, round(best_score, 3), best_form) if best_score >= 0.55 else (None, 0.0, "")
