"""Orders taken before the customer is at the counter.

This is the first endpoint in the product that an unattended machine calls. Everything a
person would notice — a wrong name, a missing item, a double charge — has to be caught here
instead, because on the other end is an agent reading a total down a phone line and a
shopkeeper who will make whatever this says.

Three things it must never do: bill twice for one order, let one shop's key reach another
shop's queue, or silently drop an item it could not understand.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "api" / "_lib"))

from fastapi.testclient import TestClient          # noqa: E402

import api.index as api_index                      # noqa: E402
import auth                                        # noqa: E402
import db                                          # noqa: E402

client = TestClient(api_index.fastapi_app)
FAILS = 0

SHOP, OTHER = "t_orders", "t_other"
CATALOG = [
    {"id": "pda", "name": "Plantation Double A", "unit": "kg", "unit_price": 900.0,
     "stock": 20.0, "category": "resale", "recipe": [], "aliases": []},
    {"id": "cpb", "name": "Cherry Peaberry", "unit": "kg", "unit_price": 1400.0,
     "stock": 10.0, "category": "resale", "recipe": [], "aliases": []},
    {"id": "beans", "name": "Coffee Beans", "unit": "kg", "unit_price": 900.0,
     "stock": 5.0, "category": "raw", "recipe": [], "aliases": []},
    {"id": "cup", "name": "Takeaway Cup", "unit": "piece", "unit_price": 4.0,
     "stock": 400.0, "category": "consumable", "recipe": [], "aliases": []},
    {"id": "americano", "name": "Americano", "unit": "piece", "unit_price": 140.0,
     "stock": 0.0, "category": "menu", "aliases": [],
     "recipe": [{"component_id": "beans", "qty": 0.018},
                {"component_id": "cup", "qty": 1}]},
]

STORE: dict = {}


def check(label, got, want):
    global FAILS
    ok = got == want
    if not ok:
        FAILS += 1
    print(f"{'ok  ' if ok else 'FAIL'} {label}"
          + ("" if ok else f"\n     got  {got!r}\n     want {want!r}"))


def near(label, got, want, tol=0.01):
    global FAILS
    ok = got is not None and abs(got - want) <= tol
    if not ok:
        FAILS += 1
    print(f"{'ok  ' if ok else 'FAIL'} {label}" + ("" if ok else f"\n     got {got!r} want ~{want}"))


def reset():
    STORE.clear()
    STORE.update({"orders": [], "bills": [], "moves": [], "keys": {}})

    async def fake_products(shop_id):
        return [dict(p) for p in CATALOG]

    async def fake_shop(shop_id):
        return {"id": shop_id, "name": "Ragas Coffee", "lang": "en",
                "upi_vpa": "ragas@upi"}

    async def fake_save_order(shop_id, order):
        oid = f"o{len(STORE['orders']) + 1}"
        STORE["orders"].append({**order, "id": oid, "shop_id": shop_id,
                                "created_at": f"2026-08-08T0{len(STORE['orders'])}:00:00Z"})
        return oid, ""

    async def fake_list_orders(shop_id, status="pending", limit=50):
        return [o for o in STORE["orders"] if o["shop_id"] == shop_id
                and (not status or o.get("status") == status)]

    async def fake_get_order(shop_id, order_id):
        return next((o for o in STORE["orders"]
                     if o["id"] == order_id and o["shop_id"] == shop_id), None)

    async def fake_settle(shop_id, order_id, fields):
        for o in STORE["orders"]:
            if o["id"] == order_id and o["shop_id"] == shop_id:
                o.update(fields)
                return ""
        return "no such order"

    async def fake_by_key(key_hash):
        sid = STORE["keys"].get(key_hash)
        return {"id": sid, "name": "Ragas Coffee", "upi_vpa": "ragas@upi"} if sid else None

    async def fake_set_key(shop_id, key_hash):
        STORE["keys"] = {h: s for h, s in STORE["keys"].items() if s != shop_id}
        STORE["keys"][key_hash] = shop_id
        return ""

    async def fake_save_bill(shop_id, bill):
        bid = f"b{len(STORE['bills']) + 1}"
        STORE["bills"].append({**bill, "id": bid})
        return bid

    async def fake_receipt_no(shop_id, fy):
        return len(STORE["bills"]) + 1

    async def fake_move(shop_id, moves, reason, bill_id=None):
        STORE["moves"].append((reason, moves))
        return ""

    db.get_products = fake_products
    db.get_shop = fake_shop
    db.save_order = fake_save_order
    db.list_orders = fake_list_orders
    db.get_order = fake_get_order
    db.settle_order = fake_settle
    db.shop_by_order_key = fake_by_key
    db.set_order_key = fake_set_key
    db.save_bill = fake_save_bill
    db.next_receipt_no = fake_receipt_no
    db.move_stock = fake_move
    db.invalidate = lambda s: None


def owner(shop=SHOP):
    return {"Authorization": f"Bearer {auth.issue_token(shop, '9000000000', 'owner')}"}


def make_key(shop=SHOP):
    return client.post("/api/order-key", headers=owner(shop), json={}).json()["key"]


# ---------------------------------------------------------------------------

def test_intake():
    reset()
    key = make_key()
    check("the key is returned once, and looks like one",
          key.startswith(auth.ORDER_KEY_PREFIX), True)

    # The shape the brief asked for: a blend named the way a person says it.
    r = client.post("/api/orders", headers={"X-Order-Key": key}, json={
        "customer_mobile": "+91 98400 12345",
        "items": [{"text": "800 gram plantation double A plus 200 gram cherry peaberry"},
                  {"name": "Americano", "qty": 2}]})
    check("an agent's order is accepted", r.status_code, 201)
    j = r.json()
    check("two lines came back", len(j["items"]), 2)
    # 0.8 x 900 + 0.2 x 1400 = 720 + 280
    near("the blend is priced from its parts", j["items"][0]["amount"], 1000.0)
    check("...and its parts are named back to the caller",
          [(p["name"], p["qty"]) for p in j["items"][0]["parts"]],
          [("Plantation Double A", 0.8), ("Cherry Peaberry", 0.2)])
    near("a plain menu line is priced too", j["items"][1]["amount"], 280.0)
    near("the total is the sum", j["total"], 1280.0)
    check("the number is normalised for later lookup",
          STORE["orders"][0]["customer_mobile"], "9840012345")
    # Taking an order commits nothing: no bill, no stock movement, no receipt number.
    check("nothing was billed", STORE["bills"], [])
    check("nothing left the shelf", STORE["moves"], [])


def test_unmatched():
    reset()
    key = make_key()
    # One good line, one the shop does not sell. The good one must survive and the bad one
    # must be reported — an order that quietly loses an item is worse than a flagged one.
    j = client.post("/api/orders", headers={"X-Order-Key": key}, json={
        "items": [{"name": "Americano", "qty": 1},
                  {"text": "two kilo unobtainium"}]}).json()
    check("the line it knew is kept", len(j["items"]), 1)
    check("the line it did not is named", any("unobtainium" in u.lower()
                                              for u in j["unmatched"]), True)

    # Nothing understood at all is a failure, not an empty order for the shop to puzzle over.
    r = client.post("/api/orders", headers={"X-Order-Key": key},
                    json={"items": [{"text": "two kilo unobtainium"}]})
    check("an order of pure nonsense is refused", r.status_code, 422)
    check("...and nothing is queued", len(STORE["orders"]), 1)


def test_422_is_explained():
    """Two different faults share the 422 number and need opposite fixes. An integrator
    reading only the status code cannot tell a malformed body from a name the shop does not
    stock, so the body has to say which."""
    reset()
    key = make_key()
    head = {"X-Order-Key": key}

    # 1. A body of the wrong shape. FastAPI's own validation, before any handler runs.
    #    Note a list of plain strings is NOT malformed any more — see test_shapes — so this
    #    has to be something no coercion can rescue.
    r = client.post("/api/orders", headers=head, json={"items": 42})
    j = r.json()
    check("a malformed body is 422", r.status_code, 422)
    check("...and says so in words", "shape" in j.get("error", "").lower(), True)
    check("...naming the field", j["problems"][0]["field"], "items")
    check("...and shows what the body should look like", "items" in j.get("expected", {}), True)
    check("...without FastAPI's raw detail array", "detail" in j, False)

    r = client.post("/api/orders", headers=head,
                    json={"items": [{"name": "Americano", "qty": "two"}]})
    check("an unparseable quantity is caught the same way", r.status_code, 422)
    check("...pointing at the quantity",
          "qty" in r.json()["problems"][0]["field"], True)

    # 2. A well-formed body naming something the shop does not sell.
    j = client.post("/api/orders", headers=head,
                    json={"items": [{"text": "two kilo unobtainium"}]}).json()
    check("an unknown product is a different reason", j["reason"], "no_match")
    check("...and the caller is shown what the shop does sell",
          "Americano" in j["sample"], True)
    check("...with the catalog's size", j["catalog_size"] > 0, True)

    # 3. The same silence, but because the shop has no prices at all — the opposite fix.
    async def empty(shop_id):
        return []
    real, db.get_products = db.get_products, empty
    j = client.post("/api/orders", headers=head,
                    json={"items": [{"name": "Americano"}]}).json()
    check("an empty catalog is named as such", j["reason"], "empty_catalog")
    check("...and does not pretend the name was wrong", "unmatched" in j, False)
    db.get_products = real

    # A body with no items at all is a 400, not a 422 — nothing was malformed, there was
    # simply nothing to order.
    check("an empty order is 400, not 422",
          client.post("/api/orders", headers=head, json={"items": []}).status_code, 400)


def test_dry_run():
    """Wiring up an integration means getting it wrong several times. Without this, every
    attempt lands in a real queue as an order somebody has to refuse."""
    reset()
    key = make_key()
    j = client.post("/api/orders", headers={"X-Order-Key": key}, json={
        "dry_run": True,
        "items": [{"text": "800 gram plantation double A plus 200 gram cherry peaberry"}]}).json()
    check("a dry run succeeds", j["ok"], True)
    check("...and says it saved nothing", j["status"], "not_saved")
    near("...while pricing it for real", j["total"], 1000.0)
    check("...and nothing reached the queue", STORE["orders"], [])


def test_shapes():
    """Take the order however the caller has it.

    A language model writes an order as a sentence. Insisting on a JSON array of objects
    made that a second grammar for a machine to get wrong — the exact thing this endpoint
    exists to avoid. All three shapes must land on the same parser and price identically.
    """
    reset()
    key = make_key()
    head = {"X-Order-Key": key}
    want = 1280.0

    # 1. The whole order as one string, the way an agent hands over a transcript.
    j = client.post("/api/orders", headers=head, json={"dry_run": True,
        "items": "800 gram plantation double A plus 200 gram cherry peaberry, 2 americano"
    }).json()
    check("a plain string is accepted", j["ok"], True)
    check("...and splits into its lines", len(j["items"]), 2)
    near("...priced the same as the structured form", j["total"], want)

    # 2. A line each.
    j = client.post("/api/orders", headers=head, json={"dry_run": True, "items": [
        "800 gram plantation double A plus 200 gram cherry peaberry", "2 americano"]}).json()
    near("a list of strings prices the same", j["total"], want)

    # 3. Structured, as before.
    j = client.post("/api/orders", headers=head, json={"dry_run": True, "items": [
        {"text": "800 gram plantation double A plus 200 gram cherry peaberry"},
        {"name": "Americano", "qty": 2}]}).json()
    near("the structured form is unchanged", j["total"], want)

    # A unit as a machine writes it, not as anyone says it. "800 g" of a Rs900/kg coffee
    # billed eight hundred KILOS before the parser learned that "g" is a unit.
    j = client.post("/api/orders", headers=head,
                    json={"dry_run": True, "items": "800g plantation double A"}).json()
    near("a glued unit is 0.8 kg, not 800", j["items"][0]["qty"], 0.8)
    near("...and priced accordingly", j["total"], 720.0)


def test_unrendered_template():
    """A placeholder the agent never filled in. Reported as what it is — otherwise it looks
    like a product the shop does not stock, and the integrator goes and edits the catalog."""
    reset()
    key = make_key()
    for placeholder in ("{{order_items_text}}", "${items}", "{% items %}"):
        r = client.post("/api/orders", headers={"X-Order-Key": key},
                        json={"items": placeholder})
        j = r.json()
        check(f"{placeholder} is caught", r.status_code, 422)
        check("...as a template fault, not a missing product", j["reason"],
              "unrendered_template")
        check("...naming what was left unfilled", placeholder in j["found"], True)
    check("nothing was queued", STORE["orders"], [])


def test_auth_schemes():
    """The key travels however the caller's platform can send it.

    Agent platforms each expose one auth widget and not the others. Which one a shop's
    integration is capable of using is not the shop's choice, so every common shape has to
    reach the same lookup — and a session token, which arrives as a Bearer too, must not be
    mistaken for one.
    """
    reset()
    key = make_key()
    body = {"items": "2 americano", "dry_run": True}
    import base64 as b64

    def send(headers):
        return client.post("/api/orders", headers=headers, json=body)

    check("named header", send({"X-Order-Key": key}).status_code, 200)
    check("Authorization: Bearer", send({"Authorization": f"Bearer {key}"}).status_code, 200)
    check("lower-case scheme", send({"Authorization": f"bearer {key}"}).status_code, 200)

    # Basic, with the key as the username — the shape a curl -u "key:" produces.
    as_user = b64.b64encode(f"{key}:".encode()).decode()
    check("Basic, key as username", send({"Authorization": f"Basic {as_user}"}).status_code, 200)
    # ...and as the password, which is what other platforms send.
    as_pass = b64.b64encode(f"api:{key}".encode()).decode()
    check("Basic, key as password", send({"Authorization": f"Basic {as_pass}"}).status_code, 200)

    # No scheme at all. Wrong per the RFC, common in hand-configured integrations, and
    # unambiguous because of the prefix.
    check("bare key in Authorization", send({"Authorization": key}).status_code, 200)

    # Things that must NOT authenticate.
    check("a wrong key is refused", send({"Authorization": "Bearer bolo_ord_nope"}).status_code, 401)
    check("garbled Basic is refused",
          send({"Authorization": "Basic !!!not-base64!!!"}).status_code, 401)
    check("no credential is refused", send({}).status_code, 401)

    # The refusal has to say what this endpoint will take — the caller cannot see the code.
    j = send({}).json()
    check("...and lists the schemes it accepts", len(j["accepted"]), 3)
    check("...and where the key comes from", "Settings" in j["hint"], True)

    # A counter session is also a Bearer. It must be read as a session, not looked up as an
    # order key and rejected.
    r = send(owner())
    check("a session token still works as a session", r.status_code, 200)


def test_auth():
    reset()
    key = make_key(SHOP)
    body = {"items": [{"name": "Americano", "qty": 1}]}

    check("no credential at all is refused",
          client.post("/api/orders", json=body).status_code, 401)
    check("a made-up key is refused",
          client.post("/api/orders", headers={"X-Order-Key": "bolo_ord_nope"},
                      json=body).status_code, 401)

    # A key names its own shop. A body cannot talk it into another one.
    client.post("/api/orders", headers={"X-Order-Key": key},
                json={**body, "shop_id": OTHER})
    check("the key's shop wins over the body's", STORE["orders"][0]["shop_id"], SHOP)

    # Rotating replaces: the old key must stop working the moment a new one exists.
    old = key
    new = make_key(SHOP)
    check("a new key is different", new != old, True)
    check("the old key is dead",
          client.post("/api/orders", headers={"X-Order-Key": old},
                      json=body).status_code, 401)
    check("the new key works",
          client.post("/api/orders", headers={"X-Order-Key": new},
                      json=body).status_code, 201)

    # An order key places orders. It does not get to accept them — that commits the shop.
    check("an order key cannot accept an order",
          client.post("/api/orders/accept", headers={"X-Order-Key": new},
                      json={"order_id": "o1"}).status_code, 401)
    check("an order key cannot read the queue",
          client.get("/api/orders", headers={"X-Order-Key": new}).status_code, 401)
    check("an order key cannot mint another key",
          client.post("/api/order-key", headers={"X-Order-Key": new},
                      json={}).status_code, 401)
    check("staff cannot mint one either",
          client.post("/api/order-key",
                      headers={"Authorization":
                               f"Bearer {auth.issue_token(SHOP, '9111111111', 'staff')}"},
                      json={}).status_code, 403)


def test_accept():
    reset()
    key = make_key()
    client.post("/api/orders", headers={"X-Order-Key": key}, json={
        "customer_mobile": "9840012345",
        "items": [{"name": "Americano", "qty": 10}]})

    q = client.get("/api/orders", headers=owner()).json()
    check("the shopkeeper sees it waiting", q["orders"][0]["status"], "pending")
    check("...with the customer's number", q["orders"][0]["customer_mobile"], "9840012345")

    j = client.post("/api/orders/accept", headers=owner(),
                    json={"order_id": "o1"}).json()
    check("accepting issues a bill", bool(j.get("bill_id")), True)
    check("...with a receipt number", bool(j.get("receipt_no")), True)
    check("...and a QR carrying the amount", j["qr"].startswith("data:image"), True)
    near("the amount is the order's", j["total"], 1400.0)

    # Ten Americanos: the recipe is what leaves the shelf, not ten of a thing never stocked.
    reason, moves = STORE["moves"][0]
    check("a sale is recorded", reason, "sale")
    took = {m["product_id"]: round(m["delta"], 4) for m in moves}
    check("the recipe was exploded", took, {"beans": -0.18, "cup": -10.0})

    check("the order is closed out", STORE["orders"][0]["status"], "accepted")
    check("...and linked to its bill", STORE["orders"][0]["bill_id"], j["bill_id"])
    check("the customer is told it is ready", "ready" in j["message"].lower(), True)
    check("...and how to pay", "ragas@upi" in j["message"], True)

    # The one that would cost a customer real money: a second tap on a slow connection.
    r2 = client.post("/api/orders/accept", headers=owner(), json={"order_id": "o1"})
    check("accepting twice is refused", r2.status_code, 409)
    check("...and bills once", len(STORE["bills"]), 1)


def test_reject():
    reset()
    key = make_key()
    client.post("/api/orders", headers={"X-Order-Key": key},
                json={"customer_mobile": "9840012345",
                      "items": [{"name": "Americano", "qty": 1}]})
    j = client.post("/api/orders/reject", headers=owner(),
                    json={"order_id": "o1", "reason": "Out of milk today"}).json()
    check("rejecting works", j["ok"], True)
    check("the reason reaches the customer", "Out of milk today" in j["message"], True)
    check("the order is marked refused", STORE["orders"][0]["status"], "rejected")
    # A refusal is not a sale: no bill, no receipt number, no stock movement.
    check("nothing was billed", STORE["bills"], [])
    check("nothing left the shelf", STORE["moves"], [])
    # A reason the shopkeeper picked in one tap has to reach the customer verbatim. A
    # refusal with no reason still goes out — some refusals are nobody's business but the
    # shop's — and must not leave a dangling blank line where the reason would be.
    STORE["orders"][0]["status"] = "pending"
    j2 = client.post("/api/orders/reject", headers=owner(),
                     json={"order_id": "o1", "reason": ""}).json()
    check("a refusal with no reason still tells the customer",
          "cannot fulfil" in j2["message"].lower(), True)
    check("...without an empty line where the reason would be",
          "\n\n\n" in j2["message"], False)

    check("rejecting twice is refused",
          client.post("/api/orders/reject", headers=owner(),
                      json={"order_id": "o1"}).status_code, 409)


def test_counter_order():
    reset()
    # The counter's own "place order": a session, not a key, and marked as such so the
    # shopkeeper can tell a phone order from one taken at the till.
    j = client.post("/api/orders", headers=owner(),
                    json={"items": [{"name": "Americano", "qty": 1}],
                          "customer_mobile": "9840099887"}).json()
    check("the counter can queue an order", j["ok"], True)
    check("...and it is marked as taken there", STORE["orders"][0]["source"], "counter")

    # One shop's queue is not another's.
    check("another shop sees nothing",
          client.get("/api/orders", headers=owner(OTHER)).json()["orders"], [])
    check("another shop cannot accept it",
          client.post("/api/orders/accept", headers=owner(OTHER),
                      json={"order_id": "o1"}).status_code, 404)


if __name__ == "__main__":
    test_intake()
    test_unmatched()
    test_422_is_explained()
    test_dry_run()
    test_shapes()
    test_unrendered_template()
    test_auth_schemes()
    test_auth()
    test_accept()
    test_reject()
    test_counter_order()
    print("\nall order checks passed" if not FAILS else f"\n{FAILS} FAILED")
    raise SystemExit(1 if FAILS else 0)
