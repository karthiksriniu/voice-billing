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

    # count / packet led
    ("rendu packet biscuit",           "BIS001", 2,    "packet", 20),
    ("one dozen egg",                  "EGG001", 12,   "piece", 84),
    ("moonu paal packet",              "MIL001", 3,    "packet", 66),

    # bare item, implied quantity
    ("kothamalli",                     "COR001", 1,    "bundle", 10),

    # transliteration variance the phonetic fold has to absorb
    ("two kilo chakkarai",             "SUG001", 2,    "kg", 90),
    ("arai kilo tuvaram paruppu",      "TUR001", 0.5,  "kg", 70),
    ("oru kilo thakkali",              "TOM001", 1,    "kg", 30),
]

# Price-led: the amount is spoken, quantity is derived from it.
PRICE_LED = [
    ("ten rupees coriander",           "COR001", 10),
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
