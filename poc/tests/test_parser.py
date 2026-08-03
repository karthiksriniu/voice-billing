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

    print("similar SKU names resolve to the one spoken")
    from parser import Catalog as _C3
    sim = _C3(SIMILAR_CATALOG)
    for spoken, want in SIMILAR:
        got, score, _ = sim.match(spoken, LANG)
        check(spoken, got and got["name"] == want,
              f"-> {got and got['name']!r} @ {score} (want {want!r})")

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
