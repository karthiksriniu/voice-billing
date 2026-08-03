"""Storage. Supabase via PostgREST over HTTP — not a Postgres driver.

Serverless functions and direct Postgres connections are a bad pair (connection exhaustion
on cold starts). PostgREST is stateless HTTP and sidesteps it entirely.

Falls back to the seed CSV in memory when Supabase isn't configured. That is not laziness:
a demo that cannot start without a database is a demo that dies in a shop with bad network,
and the whole point of this product is that it works when the network doesn't.
"""

from __future__ import annotations

import csv
import os
import re
import time
import uuid
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
    }


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


def _templates(shop_id: str, owned: list[dict]) -> list[dict]:
    """Seed entries the shop has not priced yet, returned at price 0.

    DECISIONS.md D4: ship names and aliases, never prices. A seeded name that is wrong
    costs nothing — the shopkeeper says something else. A seeded price that is wrong is a
    wrong bill. Price 0 marks "known word, unknown price", which is what triggers the app
    to ask once during billing and remember the answer.
    """
    have = {(p.get("name") or "").lower() for p in owned}
    out = []
    for t in _seed():
        if (t["name"] or "").lower() in have:
            continue
        out.append({**t, "id": product_key(shop_id, t["name"]),
                    "unit_price": 0.0, "is_template": True})
    return out


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
    if not owned and shop_id == DEMO_SHOP:
        # The sample shop keeps its seeded prices so the deployed demo is usable on the
        # first tap. A real shop starts unpriced on purpose (D4) — see _templates.
        products = _seed()
    else:
        products = owned + _templates(shop_id, owned)
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
    payload = {k: v for k, v in row.items() if k != "is_template"}
    payload["shop_id"] = shop_id
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
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


async def create_shop(shop_id: str, name: str, vpa: str, passcode_hash: str) -> str:
    if not configured():
        _local_shops[shop_id] = {"id": shop_id, "name": name, "upi_vpa": vpa,
                                 "passcode_hash": passcode_hash}
        return ""
    return await _post("shops", {"id": shop_id, "name": name, "upi_vpa": vpa,
                                 "passcode_hash": passcode_hash})


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
        "bills": {"select": "id", "limit": "1"},
    }
    out = {"configured": True}
    async with httpx.AsyncClient(timeout=8.0) as c:
        for label, params in checks.items():
            table = label.split(".")[0]
            try:
                r = await c.get(f"{SUPABASE_URL}/rest/v1/{table}",
                                headers=_headers(), params=params)
                out[label] = "ok" if r.status_code < 400 else f"{r.status_code} {r.text[:90]}"
            except Exception as exc:                   # noqa: BLE001
                out[label] = f"{type(exc).__name__}"
    return out
