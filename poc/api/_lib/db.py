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
import time
import uuid
from pathlib import Path

import httpx

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
SEED_CSV = Path(__file__).resolve().parent / "seed" / "catalog.csv"

_memory: dict[str, list[dict]] = {}


def configured() -> bool:
    return bool(SUPABASE_URL and SUPABASE_KEY)


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
        rows = r.json() if r.status_code < 400 else []
        products = [_row(x) for x in rows] if rows else _seed()
    except Exception:                                  # noqa: BLE001
        return _seed()                                 # never let the counter stall
    _cache[shop_id] = (time.monotonic(), products)
    return products


async def upsert_product(shop_id: str, product: dict) -> dict:
    row = _row(product)
    if not configured():
        items = _seed()
        for i, p in enumerate(items):
            if p["id"] == row["id"] or p["name"].lower() == row["name"].lower():
                items[i] = {**p, **row}
                return items[i]
        items.append(row)
        return row
    invalidate(shop_id)
    payload = {**row, "shop_id": shop_id, "aliases": row["aliases"]}
    async with httpx.AsyncClient(timeout=10.0) as c:
        r = await c.post(
            f"{SUPABASE_URL}/rest/v1/products",
            headers={**_headers(), "Prefer": "resolution=merge-duplicates,return=representation"},
            json=payload,
        )
    return (r.json() or [row])[0] if r.status_code < 400 else row


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
