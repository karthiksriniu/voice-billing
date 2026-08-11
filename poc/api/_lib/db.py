"""Storage. Supabase via PostgREST over HTTP — not a Postgres driver.

Serverless functions and direct Postgres connections are a bad pair (connection exhaustion
on cold starts). PostgREST is stateless HTTP and sidesteps it entirely.

Falls back to the seed CSV in memory when Supabase isn't configured. That is not laziness:
a demo that cannot start without a database is a demo that dies in a shop with bad network,
and the whole point of this product is that it works when the network doesn't.
"""

from __future__ import annotations

import csv
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
SEED_CSV = Path(__file__).resolve().parent / "seed" / "catalog.csv"
DEMO_SHOP = "demo"

_memory: dict[str, list[dict]] = {}


def configured() -> bool:
    return bool(SUPABASE_URL and SUPABASE_KEY)


def shop_key(mobile: str) -> str:
    """Normalise a spoken or typed Indian mobile number to a stable shop id.

    Strips punctuation and a +91/0 prefix so the same shop reached by '+91 98400 12345',
    '09840012345' and '9840012345' is one shop, not three.
    """
    digits = re.sub(r"\D", "", mobile or "")
    if len(digits) > 10 and digits.startswith("91"):
        digits = digits[2:]
    return digits[-10:] if len(digits) >= 10 else (digits or "demo")


def product_key(shop_id: str, name: str) -> str:
    """Deterministic id so re-stating a price updates the row instead of inserting a
    duplicate. A null id was silently failing every write against Supabase."""
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return f"{shop_id}:{slug}" if slug else f"{shop_id}:{uuid.uuid4().hex[:8]}"


def _headers() -> dict:
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def _row(r: dict) -> dict:
    """Normalise a CSV or DB row into what the parser expects."""
    aliases = r.get("aliases") or []
    if isinstance(aliases, str):
        aliases = [a for a in aliases.split("|") if a]
    return {
        "id": r.get("id") or r.get("sku"),
        "sku": r.get("sku"),
        "name": r.get("name", ""),
        "name_ta": r.get("name_ta") or "",
        "short_desc": r.get("short_desc") or "",
        "long_desc": r.get("long_desc") or "",
        "description": r.get("description") or r.get("short_desc") or "",
        "unit": r.get("unit") or "piece",
        "unit_price": float(r.get("unit_price") or 0),
        "stock": float(r.get("stock") or 0),
        "aliases": aliases,
        # raw | consumable | menu | resale. The default is deliberately the inert one:
        # a shop that has never opened the categoriser keeps behaving exactly as before,
        # and no existing product silently acquires a recipe or stops decrementing.
        "category": (r.get("category") or "resale").strip() or "resale",
        "recipe": _recipe(r.get("recipe")),
    }


def _recipe(raw) -> list[dict]:
    """[{component_id, qty}] — anything that is not that shape is dropped rather than
    carried, because a malformed component would consume an unknown amount of an unknown
    thing on the next sale."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "[]")
        except ValueError:
            return []
    if not isinstance(raw, list):
        return []
    out = []
    for c in raw:
        if isinstance(c, dict) and c.get("component_id"):
            try:
                qty = float(c.get("qty") or 0)
            except (TypeError, ValueError):
                continue
            if qty > 0:
                out.append({"component_id": str(c["component_id"]), "qty": qty})
    return out


def _seed() -> list[dict]:
    if "products" not in _memory:
        with open(SEED_CSV, encoding="utf-8") as fh:
            _memory["products"] = [_row(r) for r in csv.DictReader(fh)]
    return _memory["products"]


# The catalog is read on every utterance. Fetching it over HTTP each time added ~700ms to
# a parse stage that otherwise runs in ~1ms — the single worst latency offender once
# Supabase was live. Cached per process (serverless reuses warm instances) and invalidated
# on write, so a spoken price change still takes effect immediately.
_CACHE_TTL_S = 60.0
_cache: dict[str, tuple[float, list[dict]]] = {}


def invalidate(shop_id: str) -> None:
    _cache.pop(shop_id, None)


async def get_products(shop_id: str) -> list[dict]:
    if not configured():
        return _seed()
    hit = _cache.get(shop_id)
    if hit and (time.monotonic() - hit[0]) < _CACHE_TTL_S:
        return hit[1]
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(
                f"{SUPABASE_URL}/rest/v1/products",
                headers=_headers(),
                params={"shop_id": f"eq.{shop_id}", "select": "*"},
            )
        owned = [_row(x) for x in r.json()] if r.status_code < 400 else []
    except Exception:                                  # noqa: BLE001
        return _seed()                                 # never let the counter stall
    # Only the sample shop is pre-stocked, so the deployed demo works on the first tap.
    # Every real shop starts empty and fills up from admin mode.
    products = _seed() if (not owned and shop_id == DEMO_SHOP) else owned
    _cache[shop_id] = (time.monotonic(), products)
    return products


async def upsert_product(shop_id: str, product: dict) -> tuple[dict, str]:
    """Returns (row, error). An empty error means it is genuinely stored."""
    row = _row(product)
    row["id"] = row.get("id") or product_key(shop_id, row["name"])
    if not configured():
        items = _seed()
        for i, p in enumerate(items):
            if p["id"] == row["id"] or p["name"].lower() == row["name"].lower():
                items[i] = {**p, **row}
                return items[i], ""
        items.append(row)
        return row, ""
    invalidate(shop_id)
    payload = {k: v for k, v in row.items()
               if k != "is_template" and k not in _absent}
    payload["shop_id"] = shop_id
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(
                f"{SUPABASE_URL}/rest/v1/products",
                headers={**_headers(),
                         "Prefer": "resolution=merge-duplicates,return=representation"},
                json=payload,
            )
            # The schema is applied by hand, so the code can be ahead of the database. A
            # column this deploy knows about and that database does not must not take
            # price editing down with it — drop the unknown field, remember it, and write
            # what the shop actually has. The feature behind it degrades; billing does not.
            if r.status_code == 400:
                missing = _unknown_columns(r.text, payload)
                if missing:
                    _absent.update(missing)
                    for k in missing:
                        payload.pop(k, None)
                    r = await c.post(
                        f"{SUPABASE_URL}/rest/v1/products",
                        headers={**_headers(),
                                 "Prefer": "resolution=merge-duplicates,return=representation"},
                        json=payload,
                    )
    except Exception as exc:                           # noqa: BLE001
        return row, f"{type(exc).__name__}: {exc}"
    if r.status_code >= 400:
        return row, f"supabase {r.status_code}: {r.text[:200]}"
    body = r.json()
    return ((body or [row])[0], "")


# Columns this deploy knows about that the live database does not. Learned from the first
# rejection rather than probed up front, so a shop with an up-to-date schema pays nothing.
_absent: set[str] = set()

# Only ever the columns added after the original schema. A typo in a core column has to
# stay a loud failure — quietly dropping `unit_price` would save a product with no price.
OPTIONAL_COLUMNS = ("category", "recipe", "description", "name_ta", "long_desc")


def _unknown_columns(body: str, payload: dict) -> set[str]:
    """PostgREST reports an unknown column as PGRST204 naming it. Trust the name, but only
    act on it for columns we are willing to lose."""
    text = (body or "").lower()
    if "pgrst204" not in text and "column" not in text:
        return set()
    return {k for k in OPTIONAL_COLUMNS if k in payload and f"'{k}'" in text}


async def upsert_shop(shop_id: str, name: str, vpa: str) -> str:
    """Register the shop. Not authentication — the mobile number is an identifier only,
    which is what keeps two shopkeepers demoing at once from sharing one catalog."""
    if not configured():
        return ""
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(
                f"{SUPABASE_URL}/rest/v1/shops",
                headers={**_headers(),
                         "Prefer": "resolution=merge-duplicates,return=representation"},
                json={"id": shop_id, "name": name, "upi_vpa": vpa},
            )
        return "" if r.status_code < 400 else f"supabase {r.status_code}: {r.text[:200]}"
    except Exception as exc:                           # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"


async def next_receipt_no(shop_id: str, fy: str) -> int:
    """Ask the database for the next number in this shop's series for this financial year.

    Deliberately not read-then-write from here: two finalises arriving together on two
    serverless instances would read the same value and both use it. The increment and the
    read are one statement inside Postgres, so the series cannot repeat or skip.
    Returns 0 when there is no database, which the caller reads as "unnumbered".
    """
    if not configured():
        key = f"{shop_id}:{fy}"
        _memory.setdefault("receipt_no", {})
        _memory["receipt_no"][key] = _memory["receipt_no"].get(key, 0) + 1
        return _memory["receipt_no"][key]
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(f"{SUPABASE_URL}/rest/v1/rpc/next_receipt_no",
                             headers=_headers(), json={"p_shop": shop_id, "p_fy": fy})
        if r.status_code < 400:
            return int(r.json())
    except Exception:                                  # noqa: BLE001
        pass
    return 0


EMPTY_REPORT = {"count": 0, "total": 0.0, "paid": 0.0, "cash": 0.0, "upi": 0.0, "days": []}


def _fold(rows: list[dict]) -> dict:
    """Sum bills the hard way, when Postgres cannot do it for us.

    Day bucketing here is IST-correct for the same reason it is in the SQL — see period.py.
    """
    from datetime import datetime, timedelta, timezone
    ist = timezone(timedelta(hours=5, minutes=30))
    out = {**EMPTY_REPORT, "days": []}
    per_day: dict[str, dict] = {}
    for r in rows:
        total = float(r.get("total") or 0)
        out["count"] += 1
        out["total"] += total
        if r.get("payment_state") == "confirmed":
            out["paid"] += total
        method = r.get("payment_method") or ""
        if method in ("cash", "upi"):
            out[method] += total
        try:
            when = datetime.fromisoformat((r.get("created_at") or "").replace("Z", "+00:00"))
            key = when.astimezone(ist).date().isoformat()
        except ValueError:
            continue
        d = per_day.setdefault(key, {"day": key, "total": 0.0, "count": 0,
                                     "paid": 0.0, "cash": 0.0, "upi": 0.0})
        d["total"] += total
        d["count"] += 1
        if r.get("payment_state") == "confirmed":
            d["paid"] += total
        if method in ("cash", "upi"):
            d[method] += total
    out["days"] = [{**per_day[k],
                    **{f: round(per_day[k][f], 2) for f in ("total", "paid", "cash", "upi")}}
                   for k in sorted(per_day)]
    for k in ("total", "paid", "cash", "upi"):
        out[k] = round(out[k], 2)
    return out


# The row-fetch fallback is bounded. A shop doing 200 bills a day fits a month inside this;
# one doing far more would silently under-report, which is worse than being slow — so the
# caller is told the figure is partial rather than being handed a confident wrong number.
FALLBACK_CAP = 4000


async def sales_report(shop_id: str, start_iso: str, end_iso: str,
                       mobile: str = "") -> dict:
    """Takings between two instants: the totals, and the daily series behind them.

    Aggregated inside Postgres where possible. The fallback exists because the schema is
    applied by hand and can lag a deploy — a missing function must degrade to a slower
    correct answer, not to an empty screen that looks like a shop with no sales.
    """
    if not configured():
        rows = [b for b in _memory.get("bills", []) if b.get("shop_id") == shop_id]
        return _fold(rows)
    params = {"p_shop": shop_id, "p_from": start_iso, "p_to": end_iso}
    if not mobile:
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                r = await c.post(f"{SUPABASE_URL}/rest/v1/rpc/sales_report",
                                 headers=_headers(), json=params)
            if r.status_code < 400:
                data = r.json() or {}
                return {**EMPTY_REPORT, **data,
                        **{k: float(data.get(k) or 0)
                           for k in ("total", "paid", "cash", "upi")}}
        except Exception:                              # noqa: BLE001
            pass
    # Both ends in one `and=(...)` group: PostgREST takes a column name once per query, so
    # a second `created_at` key would silently replace the first and widen the window.
    q = {"shop_id": f"eq.{shop_id}",
         "select": "total,payment_state,payment_method,created_at",
         "and": f"(created_at.gte.{start_iso},created_at.lt.{end_iso})",
         "order": "created_at.desc", "limit": str(FALLBACK_CAP)}
    if mobile:
        q["customer_mobile"] = f"eq.{mobile}"
    rows = await _get("bills", q)
    out = _fold(rows)
    out["partial"] = len(rows) >= FALLBACK_CAP
    return out


async def move_stock(shop_id: str, moves: list[dict], reason: str,
                     bill_id: str | None = None) -> str:
    """Write stock movements and apply them to the product rows.

    The ledger is the truth and the running figure on the product is a convenience — an
    append-only log can be recomputed after a bad write, whereas a bare number cannot be
    argued with. Shrinkage is the difference between what this ledger says should be there
    and what somebody counted, so it has to be complete rather than tidy.
    """
    if not moves:
        return ""
    # occurred_at is filled by the column default in Postgres, but nothing fills it in the
    # in-memory fallback — and the reorder forecast measures its observation window from the
    # oldest movement. Without a timestamp the window is zero days, demand is unknown, and
    # the offline demo silently shows no forecast at all.
    stamped = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    rows = [{"shop_id": shop_id, "product_id": m["product_id"],
             "delta": float(m["delta"]), "reason": reason, "bill_id": bill_id,
             "occurred_at": m.get("occurred_at") or stamped}
            for m in moves if m.get("product_id")]
    if not rows:
        return ""
    if not configured():
        # Applied to the products too, not merely logged. Without this the offline
        # fallback wrote a ledger nothing ever read: Stock & loss showed every shelf
        # frozen at its seed figure no matter how much was sold, which is exactly the
        # silent-wrong-number failure this screen exists to expose. The demo has to be
        # able to demonstrate the demo.
        _memory.setdefault("moves", []).extend(rows)
        by_id = {p["id"]: p for p in _seed()}
        for m in rows:
            p = by_id.get(m["product_id"])
            if p:
                p["stock"] = round(float(p.get("stock") or 0) + m["delta"], 3)
        return ""
    invalidate(shop_id)
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(f"{SUPABASE_URL}/rest/v1/stock_movements",
                             headers=_headers(), json=rows)
            if r.status_code >= 400:
                return f"supabase {r.status_code}: {r.text[:200]}"
            # Applied one product at a time on purpose: PostgREST cannot express
            # "stock = stock + delta" in a bulk patch, and a read-then-write of the whole
            # catalog would lose any sale that landed in between.
            for m in rows:
                await c.post(f"{SUPABASE_URL}/rest/v1/rpc/bump_stock", headers=_headers(),
                             json={"p_id": m["product_id"], "p_delta": m["delta"]})
    except Exception as exc:                           # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"
    return ""


async def stock_ledger(shop_id: str, since_days: int = 90) -> list[dict]:
    if not configured():
        return [m for m in _memory.get("moves", []) if m.get("shop_id") == shop_id]
    return await _get("stock_movements", {"shop_id": f"eq.{shop_id}", "select": "*",
                                          "order": "occurred_at.desc", "limit": "2000"})


async def recent_bills(shop_id: str, limit: int = 40) -> list[dict]:
    """Newest first, this shop only. The index on (shop_id, created_at desc) exists for
    exactly this query."""
    return await _get("bills", {"shop_id": f"eq.{shop_id}", "select": "*",
                                "order": "created_at.desc", "limit": str(limit)})


async def get_bill(shop_id: str, bill_id: str) -> dict | None:
    rows = await _get("bills", {"id": f"eq.{bill_id}", "shop_id": f"eq.{shop_id}",
                                "select": "*", "limit": "1"})
    return rows[0] if rows else None


async def save_bill(shop_id: str, bill: dict) -> str:
    bill_id = bill.get("id") or str(uuid.uuid4())
    if not configured():
        _memory.setdefault("bills", []).append({**bill, "id": bill_id})
        return bill_id
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            await c.post(
                f"{SUPABASE_URL}/rest/v1/bills",
                headers=_headers(),
                json={
                    "id": bill_id,
                    "shop_id": shop_id,
                    "total": bill["total"],
                    "items": bill["items"],
                    "payment_state": bill.get("payment_state", "pending"),
                    "upi_ref": bill.get("upi_ref", ""),
                    "customer_mobile": bill.get("customer_mobile", ""),
                    "receipt_status": bill.get("receipt_status", "none"),
                    "receipt_no": bill.get("receipt_no", ""),
                    "receipt": bill.get("receipt"),
                },
            )
    except Exception:                                  # noqa: BLE001
        pass
    return bill_id


async def log_utterance(shop_id: str, transcript: str, parsed: dict, corrected: bool = False):
    """Transcript only — never audio (DECISIONS.md D9). Corrected utterances are the
    highest-value Phase 1 corpus data, which is why the flag is recorded."""
    if not configured():
        _memory.setdefault("utterances", []).append(
            {"shop_id": shop_id, "transcript": transcript, "parsed": parsed}
        )
        return
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            await c.post(
                f"{SUPABASE_URL}/rest/v1/utterances",
                headers=_headers(),
                json={"shop_id": shop_id, "transcript": transcript,
                      "parsed": parsed, "was_corrected": corrected},
            )
    except Exception:                                  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Accounts. Passcodes arrive here already hashed (auth.hash_passcode) — nothing in this
# module ever sees or stores the digits.
# ---------------------------------------------------------------------------

_local_shops: dict[str, dict] = {}
_local_staff: dict[str, dict] = {}


async def _get(table: str, params: dict) -> list[dict]:
    if not configured():
        return []
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(f"{SUPABASE_URL}/rest/v1/{table}",
                            headers=_headers(), params={**params, "select": "*"})
        return r.json() if r.status_code < 400 else []
    except Exception:                                  # noqa: BLE001
        return []


async def _post(table: str, payload: dict, merge: bool = True) -> str:
    if not configured():
        return ""
    prefer = "return=representation"
    if merge:
        prefer = "resolution=merge-duplicates," + prefer
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(f"{SUPABASE_URL}/rest/v1/{table}",
                             headers={**_headers(), "Prefer": prefer}, json=payload)
        return "" if r.status_code < 400 else f"supabase {r.status_code}: {r.text[:200]}"
    except Exception as exc:                           # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"


async def get_shop(shop_id: str) -> dict | None:
    if not configured():
        return _local_shops.get(shop_id)
    rows = await _get("shops", {"id": f"eq.{shop_id}"})
    return rows[0] if rows else None


async def create_shop(shop_id: str, name: str, vpa: str, passcode_hash: str,
                      lang: str = "ta") -> str:
    row = {"id": shop_id, "name": name, "upi_vpa": vpa,
           "passcode_hash": passcode_hash, "lang": lang}
    if not configured():
        _local_shops[shop_id] = row
        return ""
    return await _post("shops", row)


async def delete_product(shop_id: str, product_id: str) -> tuple[bool, str]:
    invalidate(shop_id)
    if not configured():
        _memory["products"] = [p for p in _seed() if p["id"] != product_id]
        return True, ""
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.delete(f"{SUPABASE_URL}/rest/v1/products", headers=_headers(),
                               params={"shop_id": f"eq.{shop_id}", "id": f"eq.{product_id}"})
        return (r.status_code < 400, "" if r.status_code < 400
                else f"supabase {r.status_code}: {r.text[:200]}")
    except Exception as exc:                           # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


async def get_staff(mobile: str) -> dict | None:
    """Staff are looked up by mobile alone — a worker knows their number and passcode,
    not which shop id they belong to."""
    if not configured():
        return next((v for v in _local_staff.values() if v["mobile"] == mobile), None)
    rows = await _get("staff", {"mobile": f"eq.{mobile}"})
    return rows[0] if rows else None


async def list_staff(shop_id: str) -> list[dict]:
    if not configured():
        return [v for v in _local_staff.values() if v["shop_id"] == shop_id]
    return await _get("staff", {"shop_id": f"eq.{shop_id}"})


async def add_staff(shop_id: str, mobile: str, passcode_hash: str,
                    role: str = "user", name: str = "") -> str:
    row = {"id": f"{shop_id}:{mobile}", "shop_id": shop_id, "mobile": mobile,
           "passcode_hash": passcode_hash, "role": role, "name": name}
    if not configured():
        _local_staff[row["id"]] = row
        return ""
    return await _post("staff", row)


async def probe() -> dict:
    """Report what PostgREST can actually see. Supabase caches the schema, so a migration
    that ran fine can still 404 until the cache reloads — and the error is indistinguishable
    from never having run it. This tells the two apart."""
    if not configured():
        return {"configured": False}
    checks = {
        "shops": {"select": "id", "limit": "1"},
        "shops.passcode_hash": {"select": "passcode_hash", "limit": "1"},
        "staff": {"select": "id", "limit": "1"},
        "products": {"select": "id", "limit": "1"},
        "products.description": {"select": "description", "limit": "1"},
        "products.category": {"select": "category", "limit": "1"},
        "products.recipe": {"select": "recipe", "limit": "1"},
        "bills": {"select": "id", "limit": "1"},
        "stock_movements": {"select": "id", "limit": "1"},
    }
    # Functions are the half of a migration that fails silently. A missing column shows up
    # as a rejected write the shopkeeper sees; a missing function is caught, fallen back
    # from, and never mentioned — so it has to be asked about explicitly.
    rpcs = {
        "rpc.bump_stock": {"p_id": "__probe__", "p_delta": 0},
        "rpc.next_receipt_no": None,
        "rpc.sales_report": {"p_shop": "__probe__", "p_from": "2000-01-01T00:00:00Z",
                             "p_to": "2000-01-02T00:00:00Z"},
    }
    # The project ref is the SUPABASE_URL subdomain — not a secret (it appears in every
    # browser-side Supabase call) and the fastest way to confirm the SQL editor and this
    # deployment are pointed at the same database.
    host = SUPABASE_URL.split("//")[-1].split(".")[0]
    out = {"configured": True, "project_ref": host, "tables": []}
    try:
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.get(f"{SUPABASE_URL}/rest/v1/", headers=_headers())
        if r.status_code < 400:
            out["tables"] = sorted((r.json().get("definitions") or {}).keys())
    except Exception:                                  # noqa: BLE001
        pass
    async with httpx.AsyncClient(timeout=8.0) as c:
        for label, params in checks.items():
            table = label.split(".")[0]
            try:
                r = await c.get(f"{SUPABASE_URL}/rest/v1/{table}",
                                headers=_headers(), params=params)
                out[label] = "ok" if r.status_code < 400 else f"{r.status_code} {r.text[:90]}"
            except Exception as exc:                   # noqa: BLE001
                out[label] = f"{type(exc).__name__}"
        for label, body in rpcs.items():
            if body is None:
                continue                               # no side-effect-free way to call it
            name = label.split(".", 1)[1]
            try:
                r = await c.post(f"{SUPABASE_URL}/rest/v1/rpc/{name}",
                                 headers=_headers(), json=body)
                # 404 is the one that matters: the function is not there. Anything else
                # means it exists and merely disliked the probe's arguments.
                out[label] = ("missing" if r.status_code == 404
                              else "ok" if r.status_code < 400
                              else f"{r.status_code} {r.text[:90]}")
            except Exception as exc:                   # noqa: BLE001
                out[label] = f"{type(exc).__name__}"
    return out


async def clear_products(shop_id: str) -> tuple[int, str]:
    """Delete every product for one shop. Scoped to a single shop_id and reachable only
    with an owner token — there is no endpoint that can empty someone else's catalog."""
    invalidate(shop_id)
    if not configured():
        _memory["products"] = []
        return 0, ""
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            before = await c.get(f"{SUPABASE_URL}/rest/v1/products", headers=_headers(),
                                 params={"shop_id": f"eq.{shop_id}", "select": "id"})
            n = len(before.json()) if before.status_code < 400 else 0
            r = await c.delete(f"{SUPABASE_URL}/rest/v1/products", headers=_headers(),
                               params={"shop_id": f"eq.{shop_id}"})
        return (n, "") if r.status_code < 400 else (0, f"supabase {r.status_code}: {r.text[:200]}")
    except Exception as exc:                           # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"


async def find_shops(name_like: str) -> list[dict]:
    """Look a shop up by name — used to point the owner at the right shop_id."""
    if not configured():
        return []
    return await _get("shops", {"name": f"ilike.*{name_like}*"})


async def update_shop(shop_id: str, name: str, vpa: str, lang: str,
                      wa_number: str | None = None, gstin: str | None = None) -> str:
    """Change the business name, UPI ID, language, WhatsApp line or GST number. Passcode is
    deliberately not touched here — changing it needs the old one, which is a separate flow.

    Fields left as None are not written, so a caller that knows nothing about GST cannot
    blank it by omission."""
    fields = {"name": name, "upi_vpa": vpa, "lang": lang}
    if wa_number is not None:
        fields["wa_number"] = wa_number
    if gstin is not None:
        fields["gstin"] = gstin
    if not configured():
        row = _local_shops.setdefault(shop_id, {"id": shop_id})
        row.update(fields)
        return ""
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.patch(f"{SUPABASE_URL}/rest/v1/shops",
                              headers=_headers(), params={"id": f"eq.{shop_id}"},
                              json=fields)
        return "" if r.status_code < 400 else f"supabase {r.status_code}: {r.text[:200]}"
    except Exception as exc:                           # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"


async def update_bill(shop_id: str, bill_id: str, fields: dict) -> str:
    """Patch an existing bill. Deliberately not save_bill: that inserts, so a second write
    for the same id conflicts and is thrown away — which is why a captured customer number
    never reached the row. It also carries a whole bill body, so merging it would have
    overwritten the real total and items with the placeholders the caller sent."""
    if not configured():
        for b in _memory.get("bills", []):
            if b.get("id") == bill_id:
                b.update(fields)
        return ""
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.patch(f"{SUPABASE_URL}/rest/v1/bills", headers=_headers(),
                              params={"id": f"eq.{bill_id}", "shop_id": f"eq.{shop_id}"},
                              json=fields)
        return "" if r.status_code < 400 else f"supabase {r.status_code}: {r.text[:200]}"
    except Exception as exc:                           # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"


async def customer_bills(shop_id: str, mobile: str, limit: int = 5,
                         select: str = "id,total,items,created_at") -> list[dict]:
    """This customer's most recent bills, newest first. Scoped to the shop: one customer's
    history at one shop is that shop's own record, and must not leak across businesses.

    Matched on the last ten digits rather than exactly. Numbers reach the bill from two
    places — spoken at the start of a sale, and typed by the customer on the pay screen —
    and one of them may carry a 91. An exact match would tell a shopkeeper that a regular
    has never been in, which is worse than no search at all.
    """
    if not configured() or not mobile:
        return []
    key = shop_key(mobile)
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(
                f"{SUPABASE_URL}/rest/v1/bills",
                headers=_headers(),
                params={"shop_id": f"eq.{shop_id}",
                        "customer_mobile": f"like.*{key}",
                        "select": select,
                        "order": "created_at.desc", "limit": str(limit)},
            )
        return r.json() if r.status_code < 400 else []
    except Exception:                                  # noqa: BLE001
        return []
