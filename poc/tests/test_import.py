"""Document import, with the model stubbed out.

What is worth testing here is not whether Claude can read a photograph — that is checked
against real paper — but everything around it: that a row which matches an existing SKU
carries its id so the import updates rather than duplicates, that a row which does not is
offered as new, that an invoice line with nothing behind it cannot silently move stock,
and that nothing at all is written by the read endpoint.

The last one is the point. Import is a review screen; a regression that made it an upload
button would reprice a shop from a photograph.
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
import vision                                      # noqa: E402

client = TestClient(api_index.fastapi_app)

SHOP = "t_import"
PRODUCTS = [
    {"id": "p1", "name": "Sugar", "unit": "kg", "unit_price": 44.0, "aliases": ["cheeni"],
     "stock": 3.0, "description": ""},
    {"id": "p2", "name": "Toor Dal", "unit": "kg", "unit_price": 152.0, "aliases": [],
     "stock": 0.0, "description": ""},
]

WRITES: list[tuple] = []


def owner_token() -> str:
    return auth.issue_token(SHOP, "9000000000", "owner")


def stub(model_rows: dict):
    """Everything below the endpoint replaced: the model, the catalog, and both writers.
    A write that reaches WRITES is a write the endpoint made."""
    WRITES.clear()

    async def fake_read(files, kind):
        return {"ok": True, "kind": kind, "data": model_rows, "cost_paise": 7,
                "truncated": False}

    async def fake_products(shop_id):
        return [dict(p) for p in PRODUCTS]

    async def fake_shop(shop_id):
        return {"id": SHOP, "name": "Test", "lang": "en"}

    async def fake_upsert(shop_id, product):
        WRITES.append(("product", product))
        return {**product, "id": product.get("id") or "new"}, ""

    async def fake_move(shop_id, moves, reason, bill_id=None):
        WRITES.append(("stock", reason, moves))
        return ""

    vision.read = fake_read
    db.get_products = fake_products
    db.get_shop = fake_shop
    db.upsert_product = fake_upsert
    db.move_stock = fake_move
    db.invalidate = lambda shop_id: None


def post_read(kind: str, token: str | None = None):
    return client.post(
        "/api/import",
        headers={"Authorization": f"Bearer {token or owner_token()}"},
        files=[("files", ("page.png", b"\x89PNG\r\n\x1a\n", "image/png"))],
        data={"kind": kind},
    )


def check(label: str, got, want):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {label}" + ("" if ok else f"\n     got {got!r}\n     want {want!r}"))
    return ok


def main() -> int:
    fails = 0

    # --- catalog: an exact-name row must attach to the product it already is ----------
    stub({"items": [
        {"name": "Sugar", "unit": "kg", "price": 46.0, "verbatim": "Sugar 1 kg 46"},
        {"name": "Rice Ponni", "unit": "kg", "price": 58.0, "verbatim": "Rice Ponni 1 kg 58"},
    ], "skipped": 1})
    r = post_read("catalog").json()
    fails += not check("read succeeds", r.get("ok"), True)
    fails += not check("skipped reported", r.get("skipped"), 1)
    rows = r["items"]
    fails += not check("Sugar matched to p1", rows[0]["match"]["id"], "p1")
    fails += not check("old price shown", rows[0]["was"], 44.0)
    fails += not check("Rice is new", rows[1]["match"], None)
    # The read endpoint proposes. Anything else is the bug this test exists for.
    fails += not check("read wrote nothing", WRITES, [])

    # --- unit canonicalised on the way in ---------------------------------------------
    stub({"items": [{"name": "Ghee", "unit": "ml", "price": 90.0, "verbatim": "Ghee 90"}],
          "skipped": 0})
    r = post_read("catalog").json()
    fails += not check("unit kept", r["items"][0]["unit"], "ml")

    # --- apply: a matched row updates in place and keeps what it was not told ----------
    stub({"items": [], "skipped": 0})
    client.post("/api/import/apply",
                headers={"Authorization": f"Bearer {owner_token()}"},
                json={"kind": "catalog", "items": [
                    {"id": "p1", "name": "Sugar", "unit": "kg", "price": 46.0}]})
    written = [w for w in WRITES if w[0] == "product"]
    fails += not check("one product written", len(written), 1)
    fails += not check("price applied", written[0][1]["unit_price"], 46.0)
    # A rate card names a price and nothing else. An import that dropped a product's
    # aliases would break every spoken name the shop had taught it.
    fails += not check("aliases survive", written[0][1].get("aliases"), ["cheeni"])
    fails += not check("stock survives", written[0][1].get("stock"), 3.0)

    # --- inward: a line with no SKU behind it cannot move stock -----------------------
    stub({"supplier": "ABC Traders", "invoice_no": "112", "invoice_date": "2026-08-01",
          "items": [
              {"name": "Sugar", "qty": 25.0, "unit": "kg", "rate": 40.0, "verbatim": "Sugar 25kg"},
              {"name": "Cardamom", "qty": 1.0, "unit": "kg", "rate": 2200.0, "verbatim": "Elaichi 1kg"},
          ], "skipped": 0})
    r = post_read("inward").json()
    fails += not check("supplier carried", r.get("supplier"), "ABC Traders")
    fails += not check("Sugar matched", r["items"][0]["match"]["id"], "p1")
    fails += not check("Cardamom unmatched", r["items"][1]["match"], None)

    stub({"items": [], "skipped": 0})
    j = client.post("/api/import/apply",
                    headers={"Authorization": f"Bearer {owner_token()}"},
                    json={"kind": "inward", "items": [
                        {"product_id": "p1", "qty": 25},
                        {"product_id": "ghost", "qty": 9}]}).json()
    moves = [w for w in WRITES if w[0] == "stock"]
    fails += not check("one stock write", len(moves), 1)
    fails += not check("only the known product moved", moves[0][2],
                       [{"product_id": "p1", "delta": 25.0}])
    fails += not check("applied count is honest", j.get("applied"), 1)

    # --- who may import ---------------------------------------------------------------
    stub({"items": [], "skipped": 0})
    staff = auth.issue_token(SHOP, "9111111111", "staff")
    fails += not check("staff refused", post_read("catalog", staff).status_code, 403)
    fails += not check("signed out refused",
                       client.post("/api/import", files=[
                           ("files", ("p.png", b"x", "image/png"))],
                           data={"kind": "catalog"}).status_code, 401)
    fails += not check("no write survived the refusals", WRITES, [])

    # --- a file we cannot read is rejected before it costs anything --------------------
    bad = client.post("/api/import",
                      headers={"Authorization": f"Bearer {owner_token()}"},
                      files=[("files", ("notes.txt", b"hello", "text/plain"))],
                      data={"kind": "catalog"})
    fails += not check("text file refused", bad.status_code, 400)

    print("\nall import checks passed" if not fails else f"\n{fails} FAILED")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
