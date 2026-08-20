"""Parser fixtures. This is the seed of the Phase 1 eval harness — the same shape the
200-utterance field corpus will use, so the corpus can drop straight in.

Cases are written the way a shopkeeper actually speaks, including the ones the app is
supposed to REJECT. A corpus without rejects can't evaluate the confidence gate at all.
"""

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api" / "_lib"))

from parser import Catalog, Lang, Parser  # noqa: E402


def load_catalog():
    rows = []
    with open(ROOT / "api" / "_lib" / "seed" / "catalog.csv", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            r["id"] = r["sku"]
            r["unit_price"] = float(r["unit_price"])
            r["aliases"] = [a for a in (r.get("aliases") or "").split("|") if a]
            rows.append(r)
    return Catalog(rows)


LANG = Lang("ta-en")
P = Parser(LANG, load_catalog())

# (utterance, expected sku, expected qty, expected unit, expected amount or None)
CASES = [
    # weight-led, whole units
    ("two kilo sugar",                 "SUG001", 2,    "kg", 90),
    ("ரெண்டு கிலோ சர்க்கரை",             "SUG001", 2,    "kg", 90),
    ("five kilo ponni rice",           "RIC001", 5,    "kg", 290),
    ("one kilo onion",                 "ONI001", 1,    "kg", 35),

    # colloquial fractions
    ("arai kilo thuvaram paruppu",     "TUR001", 0.5,  "kg", 70),
    ("kaal kilo tea",                  "TEA001", 0.25, "kg", 105),
    ("mukkaal kilo sugar",             "SUG001", 0.75, "kg", 33.75),
    ("pav sugar",                      "SUG001", 0.25, "kg", 11.25),

    # unit conversion
    ("500 gram chilli powder",         "CHI001", 0.5,  "kg", 160),

    # compound Tamil numerals
    ("irubathi anju egg",              "EGG001", 25,   "piece", 175),
    ("nooru pathu egg",                "EGG001", 110,  "piece", 770),
    ("irunooru aimbathu egg",          "EGG001", 250,  "piece", 1750),

    # count / packet led
    ("rendu packet biscuit",           "BIS001", 2,    "packet", 20),
    ("one dozen egg",                  "EGG001", 12,   "piece", 84),
    ("moonu paal packet",              "MIL001", 3,    "packet", 66),

    # bare item, implied quantity
    ("kothamalli",                     "COR001", 1,    "bundle", 10),

    # Code-mixed ASR output: Sarvam with language_code=ta-IN renders English words in
    # Tamil script, so "two kilo sugar" comes back as "2 கிலோ சுகர்". These are verbatim
    # transcripts from the deployed Sarvam endpoint, not invented.
    ("2 கிலோ சுகர்",                    "SUG001", 2,    "kg", 90),
    ("ஒரு கிலோ தக்காளி",                "TOM001", 1,    "kg", 30),
    ("ரெண்டு பிஸ்கட்",                   "BIS001", 2,    "packet", 20),
    ("அரை கிலோ ஆனியன்",                 "ONI001", 0.5,  "kg", 17.5),

    # transliteration variance the phonetic fold has to absorb
    ("two kilo chakkarai",             "SUG001", 2,    "kg", 90),
    ("arai kilo tuvaram paruppu",      "TUR001", 0.5,  "kg", 70),
    ("oru kilo thakkali",              "TOM001", 1,    "kg", 30),
]

# Price-led: the amount is spoken, quantity is derived from it.
PRICE_LED = [
    ("ten rupees coriander",           "COR001", 10),
    # Verbatim Sarvam output for "ten rupees coriander" spoken in Indian English.
    ("₹10 கோரியாண்டா",                  "COR001", 10),
    ("rs.20 kothamalli",               "COR001", 20),
    ("nooru pathu rubai tea",          "TEA001", 110),
    ("ஐந்து ரூபாய் biscuit",             "BIS001", 5),
    ("twenty rupees ku kothamalli",    "COR001", 20),
]

COMMANDS = [
    ("cancel last",       "cancel_last"),
    ("வேண்டாம்",           "cancel_last"),
    ("total podu",        "total"),
    ("மொத்தம்",            "total"),
]

MODES = [
    ("owner mode",        "admin"),
    ("விலை வாசி",          "admin"),
    ("vilai vaasi sugar nooru rubai", "admin"),
]

# Must NOT produce a confident line item. These are the cases that decide whether the
# confidence gate works, and they are the whole reason silent error rate is measurable.
REJECTS = [
    "enna sir eppadi irukeenga",     # talking to the customer, not the app
    "adhu venaam thirumba vaanga",
    "xyzzy plugh",
]


# Admin rate configuration: the spoken quantity must survive so a per-UOM rate can be
# worked out. A price-led line overwrites qty with amount/current_price, which once made
# "potato 2 kilo is 100 rupees" echo back the existing price instead of setting Rs50/kg.
RATES = [
    ("potato 2 kilo is 100 rupees",  "POT001", 2,    50.0),
    ("potato 1 kilo is 100 rupees",  "POT001", 1,    100.0),
    ("sugar 5 kilo 250 rupees",      "SUG001", 5,    50.0),
    ("coriander 40 rupees",          "COR001", None, 40.0),
]


# Verbatim Sarvam output for admin dictation, against an empty catalog. Both of these
# produced no proposal at all: the sentence-final full stop left "ரூபாய்." matching no
# money word (so it landed in the item name), and "ருபீஸ்" was not a known money word.
UNMATCHED = [
    ("தேயிலை ஒரு கிலோ ₹400 ரூபாய்.", "தேயிலை",        1.0,   "kg", 400.0),
    ("காபி 500 கிராம் 300 ருபீஸ்",   "காபி",          500.0, "g",  300.0),
    ("சுகர் ₹45.",                   "சுகர்",          None,  None, 45.0),
    ("ஃபில்டர் காஃபி ₹250.",          "ஃபில்டர் காஃபி", None,  None, 250.0),
]


# One clip, several dictated items. This collapsed into a single garbage SKU called
# "potato sugar kilo coffee" at Rs250/kg, because splitting only looked for "and" and
# commas — which is why admin dictation appeared to register nothing at all.
MULTI = [
    ("potato 1 kilo 100 rupees sugar 1 kilo 45 rupees coffee 250 rupees",
     [("potato", 100.0), ("sugar", 45.0), ("coffee", 250.0)]),
    ("பொட்டேட்டோ 1 கிலோ ₹100. சுகர் 1 கிலோ ₹45.",
     [("பொட்டேட்டோ", 100.0), ("சுகர்", 45.0)]),
    # Price-led billing must NOT split at the money word: there the price comes first.
    ("ten rupees coriander", [("coriander", 10.0)]),
]


# Billing says several items in one breath with no price and no connector between them.
BILL_MULTI = [
    ("two kilo sugar one kilo onion", [("SUG001", 2.0), ("ONI001", 1.0)]),
    ("two kilo sugar one kilo onion half kilo toor dal",
     [("SUG001", 2.0), ("ONI001", 1.0), ("TUR001", 0.5)]),
    ("rendu packet biscuit moonu paal packet", [("BIS001", 2.0), ("MIL001", 3.0)]),
    ("ரெண்டு கிலோ சர்க்கரை ஒரு கிலோ வெங்காயம்", [("SUG001", 2.0), ("ONI001", 1.0)]),
    # Must stay single: a compound numeral and a two-word item name.
    ("irubathi anju egg", [("EGG001", 25.0)]),
    ("arai kilo thuvaram paruppu", [("TUR001", 0.5)]),
    ("ten rupees coriander", [("COR001", 1.0)]),

    # Verbatim Sarvam output, punctuation and all. Every one of these billed as a SINGLE
    # line at the LAST quantity heard, because "+" is a join alias, norm() strips it to
    # the empty string, and an empty string in the join set matched the double space that
    # a trailing full stop leaves behind — so every utterance looked like a blend. The
    # fixtures above missed it for one reason only: none of them ended in a full stop.
    ("ரெண்டு கிலோ சர்க்கரை, ஒரு கிலோ வெங்காயம்.", [("SUG001", 2.0), ("ONI001", 1.0)]),
    ("two kilo sugar, one kilo onion.", [("SUG001", 2.0), ("ONI001", 1.0)]),
    ("two kilo sugar.", [("SUG001", 2.0)]),
    ("ஒரு கிலோ தக்காளி, அரை கிலோ வெங்காயம், ரெண்டு கிலோ சர்க்கரை.",
     [("TOM001", 1.0), ("ONI001", 0.5), ("SUG001", 2.0)]),
]


# Numerals, per language. The tables were Tamil with a few native words sprinkled in, so
# a Malayalam shopkeeper had no word for thirty, and Telugu "rendu vandalu" read as 2.
# The phonetic fallback covers the rest: an ASR spells a spoken number how it likes, and
# Sarvam writes 300 as "முன்னூறு" where the table said "முந்நூறு" — which made it not a
# number at all, so 300 g of onion billed as 7 paise.
NUMERALS = [
    ("ta-en", "முன்னூறு", 300), ("ta-en", "முந்நூறு", 300), ("ta-en", "ஏழுபது", 70),
    ("ml-en", "ഇരുനൂറ്", 200), ("ml-en", "മുപ്പത്", 30), ("ml-en", "എഴുപത്", 70),
    ("ml-en", "കാൽ", 0.25), ("ml-en", "മുക്കാൽ", 0.75), ("ml-en", "ഇരുനൂറു", 200),
    ("hi-en", "तीस", 30), ("hi-en", "साठ", 60), ("hi-en", "सात", 7),
    ("hi-en", "आठ", 8), ("hi-en", "आधा", 0.5), ("hi-en", "पाव", 0.25),
    ("te-en", "ముప్పై", 30), ("te-en", "రెండు వందలు", 200), ("te-en", "పావు", 0.25),
    ("kn-en", "ಇನ್ನೂರು", 200), ("kn-en", "ಮೂವತ್ತು", 30), ("kn-en", "ಮುಕ್ಕಾಲು", 0.75),
]


# Similar names must resolve to the SKU actually spoken. A flat containment bonus made
# "pea berry" match "Cherry Pea Berry" (0.88) over the intended "Pee Berry" (0.80),
# because the spoken words sat inside the longer name.
SIMILAR = [
    ("pea berry",        "Pee Berry"),
    ("pee berry",        "Pee Berry"),
    ("cherry pea berry", "Cherry Pea Berry"),
    ("sugar",            "Sugar"),
    ("brown sugar",      "Brown Sugar"),
    ("one packet sugar", "Sugar"),
]
SIMILAR_CATALOG = [
    {"id": "a", "name": "Cherry Pea Berry", "unit": "kg", "unit_price": 1500, "aliases": []},
    {"id": "b", "name": "Pee Berry", "unit": "kg", "unit_price": 1200, "aliases": []},
    {"id": "d", "name": "Sugar", "unit": "kg", "unit_price": 45, "aliases": []},
    {"id": "e", "name": "Brown Sugar", "unit": "kg", "unit_price": 90, "aliases": []},
]


# A blend is one line. The billing quantity-split runs first and used to tear these apart
# before _parse_combo saw them, which is why `plus` worked in admin but not in billing.
COMBO_CATALOG = [
    {"id": "p1", "name": "Plantation AA", "unit": "kg", "unit_price": 800,
     "aliases": ["plantation", "பிளான்டேஷன்"]},
    {"id": "p2", "name": "Pee Berry", "unit": "kg", "unit_price": 1200,
     "aliases": ["pea berry", "பீபெர்ரி"]},
    {"id": "s", "name": "Sugar", "unit": "kg", "unit_price": 45, "aliases": ["சர்க்கரை"]},
    {"id": "o", "name": "Onion", "unit": "kg", "unit_price": 35, "aliases": ["வெங்காயம்"]},
]
COMBOS = [
    # (utterance, part quantities, total) — None means "must NOT be a combo"
    ("plantation AA 800 gms plus pee berry 200 gms",     [0.8, 0.2], 880.0),
    ("plantation 800 gram mattrum pee berry 200 gram",   [0.8, 0.2], 880.0),
    ("plantation 800 gram மற்றும் pee berry 200 gram",   [0.8, 0.2], 880.0),
    # Tamil conjoins with a -um suffix on each noun; there is no separate word to split on.
    ("800 கிராம் பிளான்டேஷனும் 200 கிராம் பீபெர்ரியும்", [0.8, 0.2], 880.0),
    ("அரை கிலோ சர்க்கரையும் அரை கிலோ வெங்காயமும்",       [0.5, 0.5], 40.0),
]
NOT_COMBOS = [
    "two kilo sugar and one kilo onion",
    "two kilo sugar one kilo onion",
]


# "phone number 98400 12345" opens a bill against a customer. Needs the trigger AND ten
# digits, because "number" is also the unit alias for pieces — and the digits must be
# consumed, or the grammar bills ten kilos of whatever follows.
CUSTOMER = [
    ("phone number 9840012345",                    "9840012345", 0),
    ("phone number 98400 12345",                   "9840012345", 0),
    ("mobile number 9840012345 two kilo sugar",    "9840012345", 1),
    ("போன் நம்பர் 9840012345",                      "9840012345", 0),
    ("phone number 919840012345",                  "9840012345", 0),
    ("phone number 98400",                         "",           0),   # too few digits
    ("one number biscuit",                         "",           1),   # unit, not a customer
    ("two kilo sugar",                             "",           1),
]


# Word order. A shop that names the thing before weighing it says "plantation A 500 gram";
# a grocer reading a scale says "500 gram plantation A". Both are billing, and the split
# used to assume only the second existed — so the first lost its weight to a defaulted
# 1 kg, and in a two-item breath the weight drifted on to the NEXT item. Rs730 of coffee
# billed as Rs1600, silently, with both lines marked accept.
ORDER_CATALOG = [
    {"id": "p1", "name": "Plantation A", "unit": "kg", "unit_price": 900,
     "aliases": ["plantation"]},
    {"id": "p2", "name": "Cherry Peaberry", "unit": "kg", "unit_price": 1400,
     "aliases": ["peaberry"]},
    {"id": "am", "name": "Americano", "unit": "piece", "unit_price": 140, "aliases": []},
    {"id": "cup", "name": "Takeaway Cup", "unit": "piece", "unit_price": 4, "aliases": []},
]
# (utterance, [(sku, qty, amount), ...])
WORD_ORDER = [
    ("500 gram plantation A",             [("p1", 0.5, 450.0)]),
    ("plantation A 500 gram",             [("p1", 0.5, 450.0)]),
    ("plantation A half kg",              [("p1", 0.5, 450.0)]),
    ("plantation A 500 gram cherry peaberry 200 gram",
     [("p1", 0.5, 450.0), ("p2", 0.2, 280.0)]),
    ("500 gram plantation A 200 gram cherry peaberry",
     [("p1", 0.5, 450.0), ("p2", 0.2, 280.0)]),
    ("plantation A 500 gram and two americano",
     [("p1", 0.5, 450.0), ("am", 2.0, 280.0)]),
    # The orders that already worked, which the fix must not cost us.
    ("two kilo plantation A one kilo cherry peaberry",
     [("p1", 2.0, 1800.0), ("p2", 1.0, 1400.0)]),
    ("two americano",                     [("am", 2.0, 280.0)]),
]

# A quantity nobody spoke. On a counted unit one of them is what was meant; on a measured
# unit it is an invention with a price on it, and must never reach the bill silently.
ASSUMED_QTY = [
    ("plantation A",   "p1", 1.0, "confirm"),   # 1 kg = Rs900, nobody said "one"
    ("cherry peaberry", "p2", 1.0, "confirm"),
    ("americano",      "am", 1.0, "accept"),    # one americano is what that means
    ("two americano",  "am", 2.0, "accept"),
    ("one kilo plantation A", "p1", 1.0, "accept"),   # he DID say one
    ("plantation A 500 gram", "p1", 0.5, "accept"),
]

# Hands-free records the room, not just the shopkeeper. What comes back is mostly not
# addressed to us, and every unrecognised fragment used to raise a modal card offering to
# add it to the price list. A card that means nothing nine times in ten teaches the
# shopkeeper to dismiss cards unread — including the tenth, which is the one asking
# whether he really meant a kilo.
NOISE = [
    "thank you", "okay sir", "vanakkam", "uh okay so", "yes yes", "நன்றி",
    "hello hello", "sorry sir", "acha theek hai", "hmm", "just a minute",
]
# ...but these are somebody trying to buy something the shop has not listed yet, or a
# mishearing of something it has. Both still have to be asked about.
STILL_ASK = [
    "two kilo kismis",          # a real request, quantity and all
    "half kg badam",
    "ten rupees soap",
]


# A unit with no route to the one the shop prices in. "500 gram" of something sold by the
# piece bills five hundred of them, and the pass-through that does it has now caused three
# separate incidents (Rs25,000 of dal, 500 litres of milk, Rs21,000 of soap). The number is
# still the best available guess, but it no longer goes on a bill without being shown.
MISMATCHED_UNIT = [
    ("500 gram americano",     "am", "confirm"),   # grams of a thing sold by the piece
    ("two kilo americano",     "am", "confirm"),
    ("half litre americano",   "am", "confirm"),
    ("500 gram plantation A",  "p1", "accept"),    # converts cleanly, so it bills
    ("two americano",          "am", "accept"),
    ("two kilo plantation A",  "p1", "accept"),
]


def run():
    passed = failed = 0

    def check(label, cond, detail=""):
        nonlocal passed, failed
        if cond:
            passed += 1
        else:
            failed += 1
            print(f"  FAIL  {label}  {detail}")

    print("weight/count/measure cases")
    for text, sku, qty, unit, amount in CASES:
        r = P.parse(text, asr_confidence=1.0)
        if not r.items:
            check(text, False, "-> no item parsed")
            continue
        it = r.items[0]
        check(text, it.product_id == sku, f"-> sku {it.product_id} (want {sku})")
        check(text, abs(it.qty - qty) < 0.01, f"-> qty {it.qty} (want {qty})")
        check(text, it.unit == unit, f"-> unit {it.unit} (want {unit})")
        check(text, abs(it.amount - amount) < 0.01, f"-> amt {it.amount} (want {amount})")

    print("price-led cases")
    for text, sku, amount in PRICE_LED:
        r = P.parse(text, asr_confidence=1.0)
        if not r.items:
            check(text, False, "-> no item parsed")
            continue
        it = r.items[0]
        check(text, it.product_id == sku, f"-> sku {it.product_id} (want {sku})")
        check(text, abs(it.amount - amount) < 0.01, f"-> amt {it.amount} (want {amount})")
        check(text, it.price_led, "-> not flagged price_led")

    print("commands")
    for text, cmd in COMMANDS:
        check(text, P.parse(text).command == cmd, f"-> {P.parse(text).command} (want {cmd})")

    print("mode switches")
    for text, mode in MODES:
        check(text, P.parse(text).mode_switch == mode, f"-> {P.parse(text).mode_switch}")

    print("admin rate configuration")
    for text, sku, want_qty, want_rate in RATES:
        r = P.parse(text, mode="admin")
        if not r.items:
            check(text, False, "-> no item parsed")
            continue
        it = r.items[0]
        check(text, it.product_id == sku, f"-> sku {it.product_id} (want {sku})")
        check(text, it.spoken_qty == want_qty, f"-> spoken_qty {it.spoken_qty} (want {want_qty})")
        rate = round(it.amount / it.spoken_qty, 2) if it.spoken_qty else round(it.amount, 2)
        check(text, abs(rate - want_rate) < 0.01, f"-> rate {rate} (want {want_rate})")

    print("admin dictation into an empty catalog")
    from parser import Catalog as _C
    empty = Parser(LANG, _C([]))
    for text, name, qty, unit, money in UNMATCHED:
        r = empty.parse(text, mode="admin")
        u = r.unmatched[0] if r.unmatched else {}
        check(text, u.get("name") == name, f"-> name {u.get('name')!r} (want {name!r})")
        check(text, u.get("qty") == qty, f"-> qty {u.get('qty')} (want {qty})")
        check(text, u.get("unit") == unit, f"-> unit {u.get('unit')} (want {unit})")
        check(text, u.get("money") == money, f"-> money {u.get('money')} (want {money})")

    print("multi-item dictation in one clip")
    from parser import Catalog as _C2
    bare = Parser(LANG, _C2([]))
    for text, want in MULTI:
        mode = "billing" if "rupees coriander" in text else "admin"
        got = [(u["name"], u["money"]) for u in bare.parse(text, mode=mode).unmatched]
        check(text, got == want, f"-> {got} (want {want})")

    print("billing: several items in one breath")
    for text, want in BILL_MULTI:
        got = [(i.product_id, i.qty) for i in P.parse(text).items]
        check(text, got == want, f"-> {got} (want {want})")

    print("numerals in every language")
    from parser import Catalog as _C4
    for code, word, want in NUMERALS:
        pl = Parser(Lang(code), _C4([]))
        got, _ = pl._read_number(word.split(), 0)
        check(f"{code} {word}", got == want, f"-> {got} (want {want})")

    print("a unit spelt any way still converts")
    from parser import Catalog as _C5
    # A catalog holding "Kg" found no conversion for a spoken "gram", left the 500 alone
    # and billed 500 kilos: Rs25,000 for half a kilo of potato.
    for stored in ("Kg", "KILO", "kg", "கிலோ"):
        u = Parser(LANG, _C5([{"id": "P", "name": "potato", "unit": stored,
                               "unit_price": 50.0}]))
        it = u.parse("500 gram potato").items
        check(f"unit {stored!r}", it and it[0].qty == 0.5 and it[0].amount == 25.0,
              f"-> {[(i.qty, i.amount) for i in it]} (want 0.5 kg, Rs25)")

    print("a unit written rather than spoken")
    from parser import Catalog as _C5a
    # Nobody says "800g" out loud, so speech never produced it — but it is how a person
    # types and how a language model writes, and the order API feeds both into this parser.
    # Two separate faults met here: a canonical key that was in nobody's alias list, so "g"
    # was not a unit word at all, and a unit glued to its digits, so the number vanished.
    # Together "800 g" of a Rs900/kg coffee billed eight hundred KILOS — Rs720,000.
    for code in ("ta-en", "ml-en", "hi-en", "te-en", "kn-en"):
        pw = Parser(Lang(code), _C5a([
            {"id": "C", "name": "Coffee", "unit": "kg", "unit_price": 900.0},
            {"id": "M", "name": "Milk", "unit": "l", "unit_price": 60.0},
            # A product whose own name starts with digits: the glue-splitter must not
            # rename it while fixing a typing convention.
            {"id": "S", "name": "7Up", "unit": "piece", "unit_price": 40.0},
        ]))
        for said, want_qty, want_amt in (
                ("800 gram coffee", 0.8, 720.0),
                ("800 g coffee", 0.8, 720.0),
                ("800g coffee", 0.8, 720.0),
                ("1.5kg coffee", 1.5, 1350.0),
                ("500ml milk", 0.5, 30.0),
                ("2l milk", 2.0, 120.0),
                ("2 l milk", 2.0, 120.0),
                ("two 7Up", 2.0, 80.0)):
            it = pw.parse(said).items
            check(f"{code} {said!r}",
                  it and abs(it[0].qty - want_qty) < 1e-6 and it[0].amount == want_amt,
                  f"-> {[(i.name, i.qty, i.unit, i.amount) for i in it]} "
                  f"(want {want_qty}, Rs{want_amt})")

    print("volume converts, and never into mass")
    from parser import Catalog as _C5b
    # Litre and millilitre carried no conversion factor at all, so "500 ml" against a
    # per-litre product left the 500 alone and billed 500 litres — Rs30,000 for half a
    # litre of milk. Mass had been fixed; volume had not, and a coffee shop is mostly
    # volume. Every language pack, because the factors live in the pack.
    for code in ("ta-en", "ml-en", "hi-en", "te-en", "kn-en"):
        pv = Parser(Lang(code), _C5b([
            {"id": "M", "name": "Milk", "unit": "l", "unit_price": 60.0},
            {"id": "O", "name": "Oil", "unit": "ml", "unit_price": 0.5},
        ]))
        for said, want_qty, want_amt in (("500 ml milk", 0.5, 30.0),
                                         ("one litre milk", 1.0, 60.0),
                                         ("two litre oil", 2000.0, 1000.0)):
            it = pv.parse(said).items
            check(f"{code} {said!r}",
                  it and abs(it[0].qty - want_qty) < 1e-6 and it[0].amount == want_amt,
                  f"-> {[(i.qty, i.unit, i.amount) for i in it]} (want {want_qty}, Rs{want_amt})")

    # A gram is not a millilitre. Converting between them would be assuming water, which
    # is wrong for oil and honey — the quantity is left alone instead of being guessed at.
    pmix = Parser(LANG, _C5b([{"id": "H", "name": "Honey", "unit": "ml", "unit_price": 2.0}]))
    it = pmix.parse("500 gram honey").items
    check("mass is not silently converted to volume",
          it and it[0].qty == 500.0, f"-> {[(i.qty, i.unit) for i in it]}")

    print("wake word and command matrix, every language")
    from parser import Catalog as _C6
    import json as _json
    MATRIX_CAT = [
        {"id": "P1", "name": "Potato", "unit": "kg", "unit_price": 25},
        {"id": "P2", "name": "Tomato", "unit": "kg", "unit_price": 30},
        {"id": "P3", "name": "Filter Coffee", "unit": "piece", "unit_price": 20},
        {"id": "P4", "name": "Milk", "unit": "packet", "unit_price": 22},
        # Named to collide on purpose: a shop really can sell bill paper, cashew and
        # chutney, and none of them may fire a command.
        {"id": "P5", "name": "Bill Paper", "unit": "piece", "unit_price": 5},
        {"id": "P6", "name": "Cashew", "unit": "kg", "unit_price": 800},
        {"id": "P7", "name": "Chutney", "unit": "piece", "unit_price": 15},
    ]
    for code in ("ta-en", "ml-en", "hi-en", "te-en", "kn-en"):
        pack = _json.loads((ROOT / "api" / "_lib" / "lang" / f"{code}.json")
                           .read_text(encoding="utf-8"))
        pl = Parser(Lang(code), _C6(MATRIX_CAT))
        for cmd, aliases in sorted(pack["commands"].items()):
            for a in aliases:
                for prefix in ("", "chitti ", "சிட்டி "):
                    r = pl.parse(prefix + a)
                    check(f"{code} {prefix}{a}", r.command == cmd,
                          f"-> {r.command}/{r.mode_switch} (want {cmd})")
        for mode, aliases in sorted(pack["modes"].items()):
            for a in aliases:
                check(f"{code} mode {a}", pl.parse(a).mode_switch == mode,
                      f"-> {pl.parse(a).mode_switch} (want {mode})")
        for w in pack["wake"]:
            r = pl.parse(f"{w} two kilo potato")
            check(f"{code} wake {w}",
                  r.woke and r.items and r.items[0].product_id == "P1",
                  f"woke={r.woke} items={[i.name for i in r.items]}")
            hit, sc, _ = _C6(MATRIX_CAT).match(w, Lang(code))
            check(f"{code} wake {w} is not an item", hit is None, f"matched {hit}")
        for prod in MATRIX_CAT:
            for phrase in (f"two {prod['name']}", f"one kilo {prod['name']}"):
                r = pl.parse(phrase)
                check(f"{code} {phrase}", not r.command and not r.mode_switch,
                      f"-> {r.command}/{r.mode_switch}")

    print("similar SKU names resolve to the one spoken")
    from parser import Catalog as _C3
    sim = _C3(SIMILAR_CATALOG)
    for spoken, want in SIMILAR:
        got, score, _ = sim.match(spoken, LANG)
        check(spoken, got and got["name"] == want,
              f"-> {got and got['name']!r} @ {score} (want {want!r})")

    print("combined items stay one line")
    from parser import Catalog as _C4
    cp = Parser(LANG, _C4(COMBO_CATALOG))
    for text, qtys, total in COMBOS:
        items = cp.parse(text, mode="billing").items
        one = items[0] if len(items) == 1 else None
        check(text, one is not None and len(one.combo) == len(qtys),
              f"-> {len(items)} lines, combo={one and len(one.combo)}")
        if one and one.combo:
            check(text, [c["qty"] for c in one.combo] == qtys,
                  f"-> qtys {[c['qty'] for c in one.combo]} (want {qtys})")
            check(text, abs(one.amount - total) < 0.01, f"-> total {one.amount} (want {total})")
    for text in NOT_COMBOS:
        items = cp.parse(text, mode="billing").items
        check(text, len(items) == 2 and not any(i.combo for i in items),
              f"-> {len(items)} lines, combos={[bool(i.combo) for i in items]}")

    print("customer number opens a bill")
    for text, want_mobile, want_items in CUSTOMER:
        r = P.parse(text)
        check(text, r.customer_mobile == want_mobile,
              f"-> mobile {r.customer_mobile!r} (want {want_mobile!r})")
        check(text, len(r.items) == want_items,
              f"-> {len(r.items)} items (want {want_items})")

    print("word order: the quantity may come before or after the item")
    from parser import Catalog as _C5
    op = Parser(LANG, _C5(ORDER_CATALOG))
    for text, want in WORD_ORDER:
        r = op.parse(text, asr_confidence=1.0)
        got = [(i.product_id, i.qty, i.amount) for i in r.items]
        check(text, len(got) == len(want), f"-> {len(got)} lines (want {len(want)}): {got}")
        for (gs, gq, ga), (ws, wq, wa) in zip(got, want):
            check(text, gs == ws, f"-> sku {gs} (want {ws})")
            check(text, abs(gq - wq) < 0.001, f"-> qty {gq} (want {wq})")
            check(text, abs(ga - wa) < 0.01, f"-> amount {ga} (want {wa})")
        # Nothing may be left over: an orphaned quantity is what used to reattach itself
        # to the following item.
        check(text, r.number is None, f"-> orphaned number {r.number}")

    print("a quantity nobody spoke is never silently accepted")
    for text, sku, qty, verdict in ASSUMED_QTY:
        r = op.parse(text, asr_confidence=1.0)
        if not r.items:
            check(text, False, "-> no item parsed")
            continue
        it = r.items[0]
        check(text, it.product_id == sku, f"-> sku {it.product_id} (want {sku})")
        check(text, abs(it.qty - qty) < 0.001, f"-> qty {it.qty} (want {qty})")
        check(text, it.verdict == verdict,
              f"-> {it.verdict} @ {it.confidence} (want {verdict})")

    print("room noise is discarded, not turned into a question")
    # Run against the KIRANA catalog as well as the coffee one. Fuzzy matching finds
    # something for any string, and which something depends entirely on what the shop
    # stocks: on this catalog "thank you" reaches Tomato and "okay okay" Coffee Powder,
    # both well clear of the match bar — so they never appeared in the unmatched list
    # where the first version of this check was looking.
    for text in NOISE:
        r = P.parse(text, asr_confidence=1.0)
        check(f"kirana: {text}", not r.items and not r.unmatched,
              f"-> items={[i.name for i in r.items]} ask={[u['name'] for u in r.unmatched]}")
    for text in NOISE:
        r = op.parse(text, asr_confidence=1.0)
        check(text, not r.unmatched,
              f"-> raised a card for {[u['name'] for u in r.unmatched]}")
        check(text, not r.items, f"-> billed {[i.name for i in r.items]}")
    for text in STILL_ASK:
        r = op.parse(text, asr_confidence=1.0)
        check(text, bool(r.unmatched) or bool(r.items),
              f"-> dropped a real request as noise: {r.noise}")

    print("a unit that cannot be converted is shown, not billed")
    for text, sku, verdict in MISMATCHED_UNIT:
        r = op.parse(text, asr_confidence=1.0)
        if not r.items:
            check(text, False, "-> no item parsed")
            continue
        it = r.items[0]
        check(text, it.product_id == sku, f"-> sku {it.product_id} (want {sku})")
        check(text, it.verdict == verdict,
              f"-> {it.verdict} @ {it.confidence} qty {it.qty}{it.unit} (want {verdict})")

    print("rejects (must not yield an accepted line)")
    for text in REJECTS:
        r = P.parse(text, asr_confidence=1.0)
        accepted = [i for i in r.items if i.verdict == "accept"]
        check(text, not accepted,
              f"-> silently accepted {[(i.name, i.confidence) for i in accepted]}")

    print(f"\n{passed} passed, {failed} failed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
