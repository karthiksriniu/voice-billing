"""Days of cover, and the point at which a shop has to buy more.

The dangerous direction here is one-sided. A forecast that overstates cover says "you have
plenty" and the shop runs out mid-service; one that understates it says "order now" and the
shop buys early. Only the first is a real loss, so every test below that could go either way
is written to catch the optimistic error.

The specific way it would happen: dividing sales by the window they were *fetched* over
rather than the window they *cover*. A shop three days in, fetched over ninety, would see
its demand divided by thirty and be told its beans last a year.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "api" / "_lib"))

import reorder                                   # noqa: E402

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
FAILS = 0


def check(label, got, want):
    global FAILS
    ok = got == want
    if not ok:
        FAILS += 1
    print(f"{'ok  ' if ok else 'FAIL'} {label}"
          + ("" if ok else f"\n     got  {got!r}\n     want {want!r}"))


def near(label, got, want, tol=0.05):
    global FAILS
    ok = got is not None and abs(got - want) <= tol
    if not ok:
        FAILS += 1
    print(f"{'ok  ' if ok else 'FAIL'} {label}" + ("" if ok else f"\n     got {got!r} want ~{want}"))


def moves(*days_ago):
    return [{"occurred_at": (NOW - timedelta(days=d)).isoformat().replace("+00:00", "Z")}
            for d in days_ago]


def row(pid, name, cat, stock, sold, price, unit="kg"):
    return {"id": pid, "name": name, "category": cat, "stock": stock, "sold": sold,
            "unit_price": price, "unit": unit}


# ---------------------------------------------------------------------------
# How long the ledger has really been running
# ---------------------------------------------------------------------------

def test_window():
    near("the span is measured from the oldest movement",
         reorder.observed_days(moves(14, 3, 0.5), NOW), 14.0)
    check("an empty ledger has no span", reorder.observed_days([], NOW), 0.0)
    near("a movement with no timestamp is skipped, not counted as today",
         reorder.observed_days([{"occurred_at": ""}] + moves(9), NOW), 9.0)
    near("a naive timestamp is read as UTC rather than crashing",
         reorder.observed_days([{"occurred_at": "2026-08-01T12:00:00"}], NOW), 7.0)

    # The whole point. Ninety days of fetch window over seven days of trade.
    seven = reorder.observed_days(moves(7), NOW)
    rows = [row("beans", "Coffee Beans", "raw", 5.0, 1.26, 900)]
    reorder.annotate(rows, seven)
    near("demand is per real day, not per fetched day", rows[0]["daily_demand"], 0.18)
    near("...so cover is ~28 days, not ~350", rows[0]["days_cover"], 27.8, tol=0.5)


# ---------------------------------------------------------------------------
# Cover, and when to order
# ---------------------------------------------------------------------------

def test_cover():
    rows = [
        row("beans", "Coffee Beans", "raw", 5.0, 3.6, 900),        # 0.18/day over 20 days
        row("cups", "Takeaway Cup", "consumable", 60.0, 320.0, 4, "piece"),
        row("ice", "Ice", "raw", 0.0, 20.0, 20),                   # out
        row("pot", "French Press Pot", "resale", 3.0, 2.0, 1850, "piece"),
        row("americano", "Americano", "menu", 0.0, 0.0, 140, "piece"),
    ]
    reorder.annotate(rows, 20.0)
    by = {r["id"]: r for r in rows}

    near("beans: 5 kg at 0.18/day", by["beans"]["days_cover"], 27.8, tol=0.3)
    check("beans are comfortable", by["beans"]["state"], "ok")

    near("cups: 60 left at 16/day", by["cups"]["days_cover"], 3.8, tol=0.2)
    check("cups need ordering", by["cups"]["state"], "low")

    check("an empty shelf is out, not merely low", by["ice"]["state"], "out")
    check("...and has no cover to report", by["ice"]["days_cover"], 0.0)

    # A menu item is assembled at the till and never sat on a shelf; a bar for it would be
    # a bar for something that does not exist.
    check("a menu item is not forecast", by["americano"]["state"], "made_to_order")
    check("...and carries no cover", by["americano"]["days_cover"], None)

    # Packaging waits on a weekly distributor cycle; ingredients come faster.
    check("packaging assumes the slower channel", by["cups"]["lead_days"], 7)
    check("ingredients assume the faster one", by["beans"]["lead_days"], 3)

    # A menu item carries none of it — nothing to order, nothing to draw.
    for f in ("reorder_days", "reorder_level", "horizon_days"):
        check(f"a menu item has no {f}", by["americano"][f], None)

    # The flag sits in the same place on every bar, so the eye reads one question.
    for r in rows:
        if r["horizon_days"]:
            near(f"{r['id']}: the reorder flag sits at 40% of the bar",
                 r["reorder_days"] / r["horizon_days"], 0.4, tol=0.01)


def test_thin_history():
    rows = [row("beans", "Coffee Beans", "raw", 5.0, 2.0, 900)]
    reorder.annotate(rows, 1.0)
    # Two days of trade cannot say how long anything lasts. Saying so beats a confident bar.
    check("one day of history forecasts nothing", rows[0]["state"], "unknown")
    check("...and shows no cover", rows[0]["days_cover"], None)

    rows = [row("pot", "Pot", "resale", 4.0, 0.0, 1850, "piece")]
    reorder.annotate(rows, 60.0)
    check("something that has never sold is not 'about to run out'", rows[0]["state"], "unknown")

    rows = [row("x", "X", "raw", 1.0, 1.0, 10)]
    reorder.annotate(rows, 0.0)
    check("a ledger with no span does not divide by zero", rows[0]["state"], "unknown")


# ---------------------------------------------------------------------------
# ABC: where the money actually is
# ---------------------------------------------------------------------------

def test_abc():
    # A cheap thing that moves constantly outranks an expensive thing that barely moves.
    # Straws at Rs1 x 100/day = Rs36,500/yr; a Rs1,850 pot at 0.03/day = Rs20,000/yr.
    rows = [
        row("straw", "Straw", "consumable", 500.0, 1000.0, 1, "piece"),
        row("pot", "French Press Pot", "resale", 3.0, 0.3, 1850, "piece"),
        row("napkin", "Napkin", "consumable", 400.0, 20.0, 0.5, "piece"),
    ]
    reorder.annotate(rows, 10.0)
    by = {r["id"]: r for r in rows}
    check("a fast cheap line is an A item", by["straw"]["abc"], "A")
    check("a slow dear one is not", by["pot"]["abc"] in ("B", "C"), True)

    # A items are held tight — the shop's money is in them. C items carry a fatter buffer,
    # being cheap to over-hold and painful to run out of.
    check("an A item reorders closer to the wire",
          by["straw"]["reorder_days"] < by["napkin"]["reorder_days"]
          or by["straw"]["lead_days"] != by["napkin"]["lead_days"], True)

    check("a shop with no sales at all classifies without crashing",
          set(reorder.classify([row("a", "A", "raw", 1, 0, 0)]).values()), {"C"})


def test_suggestion():
    rows = [
        row("cups", "Takeaway Cup", "consumable", 60.0, 320.0, 4, "piece"),
        row("ice", "Ice", "raw", 0.0, 20.0, 20),
        row("beans", "Coffee Beans", "raw", 5.0, 3.6, 900),
    ]
    reorder.annotate(rows, 20.0)
    s = reorder.summarise(rows)
    check("only what needs buying is listed", s["count"], 2)
    check("an empty shelf comes first", s["items"][0]["id"], "ice")
    check("what is comfortable is left out",
          "beans" in [i["id"] for i in s["items"]], False)
    check("the suggested quantity is worth buying",
          all(i["suggest"] > 0 for i in s["items"]), True)


def test_endpoint():
    """The wiring. `annotate` being right is worth nothing if /api/stock never calls it,
    or calls it with the fetch window instead of the ledger's own span."""
    from fastapi.testclient import TestClient
    import api.index as api_index
    import auth
    import db

    client = TestClient(api_index.fastapi_app)
    SHOP = "t_reorder"
    head = {"Authorization": f"Bearer {auth.issue_token(SHOP, '9000000000', 'owner')}"}
    now = datetime.now(timezone.utc)

    products = [
        {"id": "beans", "name": "Coffee Beans", "unit": "kg", "unit_price": 900.0,
         "stock": 5.0, "category": "raw", "recipe": [], "aliases": []},
        {"id": "cups", "name": "Takeaway Cup", "unit": "piece", "unit_price": 4.0,
         "stock": 60.0, "category": "consumable", "recipe": [], "aliases": []},
        {"id": "americano", "name": "Americano", "unit": "piece", "unit_price": 140.0,
         "stock": 0.0, "category": "menu", "recipe": [], "aliases": []},
    ]
    # Twenty days of trade, fetched over the ledger's usual ninety-day window.
    ledger = []
    for d in range(20):
        when = (now - timedelta(days=19 - d)).isoformat().replace("+00:00", "Z")
        ledger.append({"product_id": "beans", "delta": -0.18, "reason": "sale",
                       "occurred_at": when})
        ledger.append({"product_id": "cups", "delta": -16.0, "reason": "sale",
                       "occurred_at": when})

    async def fake_products(shop_id):
        return [dict(p) for p in products]

    async def fake_ledger(shop_id, since_days=90):
        return ledger

    db.get_products = fake_products
    db.stock_ledger = fake_ledger

    j = client.get("/api/stock", headers=head).json()
    by = {r["name"]: r for r in j["items"]}

    # 3.6 kg over the 19 days the ledger actually spans = 0.19/day; 5 kg lasts ~26 days.
    # Over the 90-day fetch window it would read as 125 days, which is the failure mode.
    near("the window is the ledger's, not the fetch's", by["Coffee Beans"]["days_cover"],
         26.4, tol=1.0)
    check("cups are flagged low", by["Takeaway Cup"]["state"], "low")
    check("beans are not", by["Coffee Beans"]["state"], "ok")
    check("a menu item is excluded from the forecast",
          by["Americano"]["state"], "made_to_order")
    check("the shop is told what to buy", j["reorder"]["count"], 1)
    check("...and which", j["reorder"]["items"][0]["name"], "Takeaway Cup")
    near("the basis is reported so the screen can qualify it",
         j["basis"]["days_observed"], 19.0, tol=1.5)
    check("lead times are stated, not hidden", j["basis"]["lead_days"]["raw"], 3)


if __name__ == "__main__":
    test_window()
    test_cover()
    test_thin_history()
    test_abc()
    test_suggestion()
    test_endpoint()
    print("\nall reorder checks passed" if not FAILS else f"\n{FAILS} FAILED")
    raise SystemExit(1 if FAILS else 0)
