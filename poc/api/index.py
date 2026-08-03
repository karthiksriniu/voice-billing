"""Vaakku PoC API.

One FastAPI app behind /api/*. Runs locally with uvicorn and on Vercel's Python runtime
unchanged. Everything here is disposable except the parser and the language pack.
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path

from urllib.parse import parse_qs, urlencode

from fastapi import APIRouter, FastAPI, File, Form, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent / "_lib"))

import db                                              # noqa: E402
from parser import (ADMIN_SAME_ITEM_THRESHOLD, Catalog, Lang,  # noqa: E402
                    Parser)
from sarvam import SarvamASR, get_asr                  # noqa: E402
from upi import build_uri, qr_data_uri                 # noqa: E402

app = FastAPI(title="Vaakku PoC")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

# Routes are declared on a router and then mounted at several prefixes. Vercel's rewrite
# hands the function a path that is not reliably the one the browser asked for, and guessing
# wrong shows up as a FastAPI 404 that looks exactly like a broken deploy. Mounting at every
# plausible prefix costs nothing and removes the guess.
router = APIRouter()

LANG = Lang(os.environ.get("LANG_PACK", "ta-en"))
DEFAULT_SHOP = os.environ.get("DEFAULT_SHOP_ID", "demo")


async def parser_for(shop_id: str) -> Parser:
    return Parser(LANG, Catalog(await db.get_products(shop_id)))


class ParseRequest(BaseModel):
    text: str
    shop_id: str = DEFAULT_SHOP
    asr_confidence: float = 1.0
    mode: str = "billing"


class ShopRequest(BaseModel):
    mobile: str
    name: str = "Shop"
    vpa: str = ""


class FinalizeRequest(BaseModel):
    shop_id: str = DEFAULT_SHOP
    items: list[dict]
    vpa: str
    payee: str = "Shop"


class ProductRequest(BaseModel):
    shop_id: str = DEFAULT_SHOP
    id: str = ""
    sku: str = ""
    name: str
    name_ta: str = ""
    short_desc: str = ""
    unit: str = "piece"
    unit_price: float = 0
    stock: float = 0
    aliases: list[str] = []


def serialise(item) -> dict:
    return {
        "product_id": item.product_id, "name": item.name, "qty": item.qty,
        "unit": item.unit, "unit_price": item.unit_price, "amount": item.amount,
        "confidence": item.confidence, "verdict": item.verdict,
        "price_led": item.price_led, "raw": item.raw,
        "match_score": item.match_score, "spoken_name": item.spoken_name,
        "needs_price": item.needs_price,
    }


def admin_proposal(res) -> dict | None:
    """Turn an admin-mode utterance into a proposed catalog change, for the UI to confirm.

    The important judgement is existing-vs-new. In billing a fuzzy match is what you want;
    here it is destructive — "maida" scores 0.857 against Wheat Flour, and acting on that
    silently reprices a product the shopkeeper never mentioned. Anything below the strict
    threshold is treated as a new item named by what was actually said.
    """
    for it in res.items:
        if not it.price_led:
            continue
        if it.match_score >= ADMIN_SAME_ITEM_THRESHOLD:
            return {"action": "reprice", "id": it.product_id, "name": it.name,
                    "unit": it.unit, "price": it.amount, "was": it.unit_price}
        return {"action": "create", "id": "", "name": it.spoken_name or it.name,
                "unit": it.unit, "price": it.amount, "near": it.name,
                "near_score": it.match_score}
    for u in res.unmatched:
        if u.get("money"):
            return {"action": "create", "id": "", "name": u["name"],
                    "unit": u.get("unit") or "piece", "price": u["money"]}
    return None


def result_payload(res, took_ms: int, mode: str = "billing") -> dict:
    payload = {
        "transcript": res.transcript,
        "items": [serialise(i) for i in res.items],
        "command": res.command,
        "mode_switch": res.mode_switch,
        "unparsed": res.unparsed,
        "number": res.number,
        "took_ms": took_ms,
    }
    if mode == "admin" or res.mode_switch == "admin":
        payload["admin"] = admin_proposal(res)
    return payload


@router.get("/health")
async def health():
    asr = get_asr()
    return {
        "ok": True,
        "asr_backend": asr.name,
        "asr_configured": SarvamASR().configured,
        "db": "supabase" if db.configured() else "seed-csv (in memory)",
        "lang": LANG.data["code"],
        "products": len(await db.get_products(DEFAULT_SHOP)),
    }


@router.get("/catalog")
async def catalog(shop_id: str = DEFAULT_SHOP):
    return {"products": await db.get_products(shop_id)}


@router.post("/catalog")
async def add_product(req: ProductRequest):
    product, error = await db.upsert_product(req.shop_id, req.model_dump())
    # The error is returned rather than swallowed. Previously a rejected write still came
    # back looking like a success, and the UI cheerfully announced a price change that had
    # not happened — the exact failure mode this product cannot afford.
    return JSONResponse({"product": product, "ok": not error, "error": error},
                        status_code=200 if not error else 502)


@router.post("/shop")
async def register_shop(req: ShopRequest):
    """Mobile number as identifier, not authentication (no OTP — see DECISIONS.md D5 notes
    on TRAI DLT). It exists so two shops don't share one catalog."""
    shop_id = db.shop_key(req.mobile)
    error = await db.upsert_shop(shop_id, req.name, req.vpa)
    products = await db.get_products(shop_id)
    return {"shop_id": shop_id, "ok": not error, "error": error,
            "priced": sum(1 for p in products if p["unit_price"] > 0),
            "known_words": len(products)}


@router.post("/parse")
async def parse_text(req: ParseRequest):
    """Text in, line items out. The demo's offline path, and the endpoint the Phase 1
    eval harness will drive."""
    t0 = time.perf_counter()
    p = await parser_for(req.shop_id)
    res = p.parse(req.text, asr_confidence=req.asr_confidence)
    payload = result_payload(res, int((time.perf_counter() - t0) * 1000), req.mode)
    await db.log_utterance(req.shop_id, req.text, payload)
    return payload


@router.post("/transcribe")
async def transcribe(audio: UploadFile = File(...), shop_id: str = Form(DEFAULT_SHOP),
                     mode: str = Form("billing")):
    """Audio in, line items out. Reports asr_ms separately from parse_ms because the
    latency budget in PLAN.md is about the parse stage, and the network hop here is an
    artefact of the PoC that the shipped product will not have."""
    t0 = time.perf_counter()
    raw = await audio.read()
    asr = get_asr()
    tr = await asr.transcribe(raw, audio.filename or "clip.webm")
    asr_ms = int((time.perf_counter() - t0) * 1000)

    if tr.error or not tr.text:
        return JSONResponse(
            {"transcript": "", "items": [], "command": None, "mode_switch": None,
             "unparsed": [], "error": tr.error or "nothing recognised",
             "asr_ms": asr_ms, "parse_ms": 0, "bytes": len(raw)},
            status_code=200,                           # a demo must degrade, not error
        )

    t1 = time.perf_counter()
    p = await parser_for(shop_id)
    res = p.parse(tr.text, asr_confidence=tr.confidence)
    payload = result_payload(res, int((time.perf_counter() - t1) * 1000), mode)
    payload |= {"asr_ms": asr_ms, "parse_ms": payload["took_ms"], "bytes": len(raw)}
    await db.log_utterance(shop_id, tr.text, payload)
    return payload


@router.post("/finalize")
async def finalize(req: FinalizeRequest):
    total = round(sum(float(i["amount"]) for i in req.items), 2)
    # Short ref and no note, on purpose: every character grows the QR symbol version, and a
    # denser symbol is measurably harder for a cheap camera to read in bad light. A `tn` of
    # "Bill" tells the customer nothing and costs ~10 characters.
    ref = f"VK{uuid.uuid4().hex[:8].upper()}"
    uri = build_uri(req.vpa, req.payee, total, ref=ref)
    bill_id = await db.save_bill(
        req.shop_id,
        {"total": total, "items": req.items, "upi_ref": ref, "payment_state": "pending"},
    )
    return {
        "bill_id": bill_id, "total": total, "ref": ref,
        "upi_uri": uri, "qr": qr_data_uri(uri),
        # Stated plainly because the demo must not imply we detect payment (D5).
        "confirmation": "manual",
    }


@router.post("/confirm")
async def confirm(bill_id: str = Form(...), shop_id: str = Form(DEFAULT_SHOP)):
    await db.save_bill(shop_id, {"id": bill_id, "total": 0, "items": [],
                                 "payment_state": "confirmed"})
    return {"ok": True, "bill_id": bill_id}


for _prefix in ("/api", "/api/index", ""):
    app.include_router(router, prefix=_prefix)


# Local dev only. On Vercel, public/ is served by the platform.
_public = Path(__file__).resolve().parents[1] / "public"
if not os.environ.get("VERCEL"):
    if _public.exists():
        app.mount("/", StaticFiles(directory=str(_public), html=True), name="static")
else:
    @app.api_route("/{rest:path}", methods=["GET", "POST"])
    async def _unrouted(rest: str, request: Request):
        """Diagnostic of last resort: report the path actually received rather than a bare
        404, so a routing mismatch is one curl away from being understood."""
        return JSONResponse(
            {"error": "no matching route", "seen_path": request.url.path,
             "routes": sorted({r.path for r in router.routes})},
            status_code=404,
        )


class VercelPathMiddleware:
    """Restore the request path that Vercel's rewrite throws away.

    A rewrite to /api/index replaces the path outright, so /api/health and /api/parse both
    arrive as /api/index and every route 404s. vercel.json carries the real path through in
    __vpath; this puts it back into the ASGI scope before routing sees it. Pure ASGI rather
    than a FastAPI middleware because the path has to be fixed before the router runs.
    """

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            params = parse_qs(scope.get("query_string", b"").decode(), keep_blank_values=True)
            vpath = (params.pop("__vpath", [""]) or [""])[0]
            if vpath:
                scope = dict(scope)
                scope["path"] = "/api/" + vpath.lstrip("/")
                scope["raw_path"] = scope["path"].encode()
                scope["query_string"] = urlencode(params, doseq=True).encode()
        await self.inner(scope, receive, send)


fastapi_app = app
app = VercelPathMiddleware(fastapi_app)
