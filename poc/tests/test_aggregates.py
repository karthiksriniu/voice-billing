"""Periods, recipes, and what a sale actually takes off the shelf.

Two things here can be wrong without anyone noticing for months:

  A day boundary computed in UTC files a shop's late evening under yesterday. Every
  single-day figure is then quietly wrong, and it is wrong in the direction that makes the
  app disagree with the shopkeeper's own drawer — which is how an owner learns to stop
  believing the screen.

  A recipe explosion that misses is worse. It does not show up as an error; it shows up as
  beans that never seem to run out, and then as a shrinkage figure that says the staff are
  stealing. Getting this wrong makes the app accuse someone.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "api" / "_lib"))

import period                                    # noqa: E402
from api.index import explode, clean_recipe      # noqa: E402
import db                                        # noqa: E402

IST = period.IST
FAILS = 0


def check(label: str, got, want):
    global FAILS
    ok = got == want
    if not ok:
        FAILS += 1
    print(f"{'ok  ' if ok else 'FAIL'} {label}"
          + ("" if ok else f"\n     got  {got!r}\n     want {want!r}"))


# ---------------------------------------------------------------------------
# Day boundaries, in the timezone the shop actually reads
# ---------------------------------------------------------------------------

def test_periods():
    # 00:30 IST on the 9th is 19:00 UTC on the 8th. A UTC-based day would file this under
    # the 8th and understate a night the shop was open for.
    late = datetime(2026, 8, 9, 0, 30, tzinfo=IST)
    a, b = period.day_bounds(late)
    check("late-night day starts at IST midnight", a, "2026-08-08T18:30:00Z")
    check("...and ends at the next one", b, "2026-08-09T18:30:00Z")

    utc_instant = late.astimezone(timezone.utc)
    check("the bill's own UTC instant falls inside its IST day",
          a <= utc_instant.isoformat().replace("+00:00", "Z") < b, True)

    # 2026-08-09 is a Sunday; the trading week it belongs to opened on Monday the 3rd.
    wa, wb = period.week_bounds(late)
    check("week opens on the Monday", wa, "2026-08-02T18:30:00Z")
    check("week is exactly seven days",
          (datetime.fromisoformat(wb.replace("Z", "+00:00"))
           - datetime.fromisoformat(wa.replace("Z", "+00:00"))), timedelta(days=7))

    ma, mb = period.month_bounds(late)
    check("month opens on the 1st", ma, "2026-07-31T18:30:00Z")
    check("month closes on the 1st of the next", mb, "2026-08-31T18:30:00Z")

    # December has to roll the year, which a naive month+1 does not.
    dec = datetime(2026, 12, 20, 12, 0, tzinfo=IST)
    check("December rolls into January", period.month_bounds(dec)[1], "2026-12-31T18:30:00Z")

    # The 1st of a month landing mid-week: the week reaches back further than the month.
    first = datetime(2026, 4, 1, 10, 0, tzinfo=IST)          # a Wednesday
    d, w, m = (period.day_bounds(first), period.week_bounds(first),
               period.month_bounds(first))
    check("a mid-week 1st needs the week's start, not the month's",
          period.earliest(d, w, m), w[0])

    # A picked range is inclusive of both ends: 1st to 31st means the whole month, and a
    # half-open reading would drop the last day's takings.
    r = period.range_bounds("2026-08-01", "2026-08-31")
    check("range covers the last day too", r[1], "2026-08-31T18:30:00Z")
    check("range spans 31 days",
          (datetime.fromisoformat(r[1].replace("Z", "+00:00"))
           - datetime.fromisoformat(r[0].replace("Z", "+00:00"))), timedelta(days=31))
    check("a backwards range is straightened out",
          period.range_bounds("2026-08-31", "2026-08-01"), r)
    check("nonsense dates are refused", period.range_bounds("yesterday", "today"), None)
    check("span comes back as the dates that were asked for",
          period.span_ist(*r), ("2026-08-01", "2026-08-31"))


# ---------------------------------------------------------------------------
# What one sold line takes off the shelf
# ---------------------------------------------------------------------------

def shop() -> dict[str, dict]:
    def p(pid, name, cat, unit="kg", price=0.0, recipe=None):
        return {"id": pid, "name": name, "category": cat, "unit": unit,
                "unit_price": price, "recipe": recipe or []}
    return {x["id"]: x for x in [
        p("beans", "Coffee Beans", "raw", "kg", 900),
        p("ice", "Ice", "raw", "kg", 20),
        p("milk", "Milk", "raw", "litre", 60),
        p("cup", "Takeaway Cup", "consumable", "piece", 4),
        p("straw", "Straw", "consumable", "piece", 1),
        p("press", "French Press", "resale", "piece", 1200),
        p("americano", "Americano", "menu", "piece", 140, [
            {"component_id": "beans", "qty": 0.02},
            {"component_id": "ice", "qty": 0.1},
            {"component_id": "cup", "qty": 1},
            {"component_id": "straw", "qty": 1},
        ]),
        p("latte", "Iced Latte", "menu", "piece", 180, [
            {"component_id": "americano", "qty": 1},
            {"component_id": "milk", "qty": 0.15},
        ]),
        p("orphan", "Ghost Special", "menu", "piece", 99,
          [{"component_id": "deleted", "qty": 1}]),
    ]}


def as_map(moves: list[dict]) -> dict:
    out: dict[str, float] = {}
    for m in moves:
        out[m["product_id"]] = round(out.get(m["product_id"], 0) + m["delta"], 6)
    return out


def test_explode():
    s = shop()

    # A shop that has never opened the recipe screen must behave exactly as before.
    check("no recipe: the item itself leaves the shelf",
          as_map(explode(s, "press", 2)), {"press": -2})

    # The whole point: nobody buys an Americano off a shelf.
    check("one Americano consumes its parts, not itself",
          as_map(explode(s, "americano", 1)),
          {"beans": -0.02, "ice": -0.1, "cup": -1, "straw": -1})
    check("the menu item itself never moves",
          "americano" in as_map(explode(s, "americano", 1)), False)

    check("three Americanos scale linearly",
          as_map(explode(s, "americano", 3)),
          {"beans": -0.06, "ice": -0.3, "cup": -3, "straw": -3})

    # A recipe naming another prepared item resolves all the way down to real goods.
    check("a latte resolves through the americano to raw materials",
          as_map(explode(s, "latte", 1)),
          {"beans": -0.02, "ice": -0.1, "cup": -1, "straw": -1, "milk": -0.15})

    # Half a kilo of a weighed menu item, if a shop ever prices one that way.
    check("fractional quantities carry through",
          as_map(explode(s, "americano", 0.5)),
          {"beans": -0.01, "ice": -0.05, "cup": -0.5, "straw": -0.5})

    # A component deleted from the catalog must not make the sale invisible.
    check("a recipe of deleted parts falls back to the item",
          as_map(explode(s, "orphan", 1)), {"orphan": -1})

    # A cycle entered by hand would otherwise spin until the request times out, at the
    # exact moment a customer is standing there.
    cyc = shop()
    cyc["a"] = {"id": "a", "name": "A", "unit": "piece", "unit_price": 0, "category": "menu",
                "recipe": [{"component_id": "b", "qty": 1}]}
    cyc["b"] = {"id": "b", "name": "B", "unit": "piece", "unit_price": 0, "category": "menu",
                "recipe": [{"component_id": "a", "qty": 1}]}
    moves = explode(cyc, "a", 1)
    check("a recipe cycle terminates", len(moves) >= 1, True)
    check("...and is still a real movement", all(m["delta"] < 0 for m in moves), True)

    # An unknown product id (a deleted SKU still on an open bill) still records the sale.
    check("an unknown product still moves", as_map(explode(s, "gone", 1)), {"gone": -1})


def test_clean_recipe():
    s = shop()
    check("a component that is not stocked is dropped",
          clean_recipe([{"component_id": "nope", "qty": 1}], s, "americano"), [])
    check("an item cannot contain itself",
          clean_recipe([{"component_id": "americano", "qty": 1}], s, "americano"), [])
    check("zero and negative quantities are not components",
          clean_recipe([{"component_id": "beans", "qty": 0},
                        {"component_id": "ice", "qty": -3}], s, "americano"), [])
    check("a duplicate component is kept once",
          clean_recipe([{"component_id": "beans", "qty": 0.02},
                        {"component_id": "beans", "qty": 0.05}], s, "americano"),
          [{"component_id": "beans", "qty": 0.02}])
    check("a non-numeric quantity is dropped, not coerced",
          clean_recipe([{"component_id": "beans", "qty": "lots"}], s, "americano"), [])


def test_row_defaults():
    # Every product that existed before categories keeps behaving as it did: resale, no
    # recipe, decrements itself.
    r = db._row({"id": "x", "name": "Old Product", "unit": "kg"})
    check("an uncategorised product is inert", (r["category"], r["recipe"]), ("resale", []))


# ---------------------------------------------------------------------------
# Folding bills into periods
# ---------------------------------------------------------------------------

def test_fold():
    rows = [
        # 00:30 IST on the 9th — belongs to the 9th, not the 8th.
        {"total": 100, "payment_state": "confirmed", "payment_method": "cash",
         "created_at": "2026-08-08T19:00:00+00:00"},
        {"total": 250, "payment_state": "pending", "payment_method": "",
         "created_at": "2026-08-09T06:00:00+00:00"},
        {"total": 60, "payment_state": "confirmed", "payment_method": "upi",
         "created_at": "2026-08-10T04:00:00+00:00"},
    ]
    out = db._fold(rows)
    check("everything is counted", (out["count"], out["total"]), (3, 410.0))
    check("only settled money is 'paid'", out["paid"], 160.0)
    check("cash and UPI are told apart", (out["cash"], out["upi"]), (100.0, 60.0))
    days = {d["day"]: d for d in out["days"]}
    check("a 19:00 UTC bill lands on the next IST day", sorted(days), ["2026-08-09", "2026-08-10"])
    check("both of that day's bills are on it", days["2026-08-09"]["total"], 350.0)
    check("the split is carried per day so a week can be sliced out",
          (days["2026-08-09"]["cash"], days["2026-08-09"]["paid"]), (100.0, 100.0))
    check("daily totals sum to the overall total",
          round(sum(d["total"] for d in out["days"]), 2), out["total"])
    check("a bill with a broken timestamp is still counted in the total",
          db._fold([{"total": 5, "created_at": "not a date"}])["total"], 5.0)


# ---------------------------------------------------------------------------
# The endpoints, with storage stubbed
# ---------------------------------------------------------------------------

def test_endpoints():
    from fastapi.testclient import TestClient
    import api.index as api_index
    import auth
    import recipes as recipe_ai

    client = TestClient(api_index.fastapi_app)
    SHOP = "t_agg"
    token = auth.issue_token(SHOP, "9000000000", "owner")
    head = {"Authorization": f"Bearer {token}"}
    products = list(shop().values())
    saved: list[dict] = []

    async def fake_products(shop_id):
        return [dict(p) for p in products]

    async def fake_shop(shop_id):
        return {"id": SHOP, "name": "Cafe", "lang": "en"}

    async def fake_upsert(shop_id, product):
        saved.append(product)
        return product, ""

    now = period.now_ist()
    today_ist = now.date().isoformat()

    async def fake_report(shop_id, start, end, mobile=""):
        # One bill today; nothing else. Enough to prove the slicing picks the right window.
        return {"count": 1, "total": 140.0, "paid": 140.0, "cash": 140.0, "upi": 0.0,
                "days": [{"day": today_ist, "total": 140.0, "count": 1,
                          "paid": 140.0, "cash": 140.0, "upi": 0.0}]}

    async def fake_customer(shop_id, mobile, limit=5, select="*"):
        return [{"id": "b1", "total": 200, "payment_state": "confirmed",
                 "payment_method": "cash", "customer_mobile": "9840012345",
                 "created_at": "2026-08-08T06:00:00+00:00", "items": [{"name": "x"}]},
                {"id": "b2", "total": 90, "payment_state": "pending",
                 "customer_mobile": "9840012345",
                 "created_at": "2026-08-01T06:00:00+00:00", "items": []}]

    db.get_products = fake_products
    db.get_shop = fake_shop
    db.upsert_product = fake_upsert
    db.sales_report = fake_report
    db.customer_bills = fake_customer
    db.invalidate = lambda shop_id: None

    j = client.get("/api/sales", headers=head).json()
    check("sales returns all three periods", sorted(k for k in ("today", "week", "month")
                                                   if k in j), ["month", "today", "week"])
    check("today's takings", j["today"]["total"], 140.0)
    check("billed and collected are reported apart", j["today"]["unpaid"], 0.0)
    check("the month contains today", j["month"]["total"], 140.0)

    j = client.get("/api/sales?frm=2026-08-01&to=2026-08-31", headers=head).json()
    check("a range answers on its own", "range" in j and "today" not in j, True)
    check("the range is labelled with the dates asked for",
          (j["range"]["from"], j["range"]["to"]), ("2026-08-01", "2026-08-31"))
    check("a malformed range is refused",
          client.get("/api/sales?frm=nope&to=2026-08-31", headers=head).status_code, 400)
    check("sales needs a session", client.get("/api/sales").status_code, 401)

    j = client.get("/api/bills?mobile=+91 98400 12345", headers=head).json()
    check("a customer search summarises what they have spent", j["customer"]["total"], 290.0)
    check("...and what is still owed", j["customer"]["unpaid"], 90.0)
    check("the plain list carries no customer block",
          client.get("/api/bills", headers=head).json().get("customer"), None)

    j = client.get("/api/recipes", headers=head).json()
    names = [s["name"] for s in j["items"]]
    check("raw and consumable are offered as components, not as sellables",
          "Coffee Beans" in names, False)
    check("...and do appear in the component list",
          "Coffee Beans" in [c["name"] for c in j["components"]], True)
    check("items without a recipe are listed first", names[0], "French Press")
    americano = next(s for s in j["items"] if s["name"] == "Americano")
    # 0.02 kg beans at 900 + 0.1 kg ice at 20 + a 4 cup + a 1 straw.
    check("materials cost is computed from the recipe", americano["cost"], 25.0)

    saved.clear()
    r = client.post("/api/recipe", headers=head, json={
        "product_id": "press",
        "components": [{"component_id": "beans", "qty": 0.25},
                       {"component_id": "press", "qty": 1},        # itself
                       {"component_id": "ghost", "qty": 1}]}).json()
    check("only the real component survives the save", r["components"], 1)
    check("saving a recipe makes it a menu item", saved[0]["category"], "menu")
    check("...and stores it in the component's own unit",
          saved[0]["recipe"], [{"component_id": "beans", "qty": 0.25}])

    saved.clear()
    r = client.post("/api/recipe", headers=head,
                    json={"product_id": "americano", "components": []}).json()
    check("clearing a recipe hands the item back", saved[0]["category"], "resale")

    staff = auth.issue_token(SHOP, "9111111111", "staff")
    check("staff cannot rewrite a recipe",
          client.post("/api/recipe", headers={"Authorization": f"Bearer {staff}"},
                      json={"product_id": "press", "components": []}).status_code, 403)

    async def fake_propose(item, components, hint=""):
        check("the draft is only offered the shop's own raw and consumable items",
              sorted(c["name"] for c in components),
              ["Coffee Beans", "Ice", "Milk", "Straw", "Takeaway Cup"])
        return {"ok": True, "cost_paise": 9, "note": "Assumed a double shot.",
                "components": [{"component_id": "beans", "qty": 0.018, "why": "double shot"},
                               {"component_id": "nowhere", "qty": 1, "why": "invented"}]}

    # The wiring, not the arithmetic: a real finalise has to reach the explosion. Tested
    # separately because `explode` being correct is worth nothing if /finalize never
    # consults the catalog — which is how it behaved before recipes existed.
    ledger: list = []

    async def fake_move(shop_id, moves, reason, bill_id=None):
        ledger.append((reason, moves))
        return ""

    async def fake_save_bill(shop_id, bill):
        return "bill-1"

    async def fake_receipt_no(shop_id, fy):
        return 1

    db.move_stock = fake_move
    db.save_bill = fake_save_bill
    db.next_receipt_no = fake_receipt_no
    client.post("/api/finalize", json={
        "shop_id": SHOP, "vpa": "shop@upi", "payee": "Cafe",
        "items": [{"product_id": "americano", "name": "Americano", "qty": 2, "amount": 280},
                  {"product_id": "press", "name": "French Press", "qty": 1, "amount": 1200}]})
    reason, moves = ledger[0]
    check("a sale is recorded as a sale", reason, "sale")
    check("the prepared item consumed its parts and the plain one consumed itself",
          as_map(moves),
          {"beans": -0.04, "ice": -0.2, "cup": -2, "straw": -2, "press": -1})

    recipe_ai.propose = fake_propose
    saved.clear()
    j = client.post("/api/recipe/draft", headers=head,
                    json={"product_id": "americano"}).json()
    check("a component the shop does not stock is dropped from the draft",
          [c["component_id"] for c in j["components"]], ["beans"])
    check("the draft is named for the shopkeeper", j["components"][0]["name"], "Coffee Beans")
    # The whole discipline of this feature in one assertion.
    check("drafting writes nothing", saved, [])


if __name__ == "__main__":
    test_periods()
    test_explode()
    test_clean_recipe()
    test_row_defaults()
    test_fold()
    test_endpoints()
    print("\nall aggregate checks passed" if not FAILS else f"\n{FAILS} FAILED")
    raise SystemExit(1 if FAILS else 0)
