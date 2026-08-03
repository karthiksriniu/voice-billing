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

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent / "_lib"))

import db                                              # noqa: E402
from parser import Catalog, Lang, Parser               # noqa: E402
from sarvam import SarvamASR, get_asr                  # noqa: E402
from upi import build_uri, qr_data_uri                 # noqa: E402

app = FastAPI(title="Vaakku PoC")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

LANG = Lang(os.environ.get("LANG_PACK", "ta-en"))
DEFAULT_SHOP = os.environ.get("DEFAULT_SHOP_ID", "demo")


async def parser_for(shop_id: str) -> Parser:
    return Parser(LANG, Catalog(await db.get_products(shop_id)))


class ParseRequest(BaseModel):
    text: str
    shop_id: str = DEFAULT_SHOP
    asr_confidence: float = 1.0


class FinalizeRequest(BaseModel):
    shop_id: str = DEFAULT_SHOP
    items: list[dict]
    vpa: str
    payee: str = "Shop"


class ProductRequest(BaseModel):
    shop_id: str = DEFAULT_SHOP
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
    }


def result_payload(res, took_ms: int) -> dict:
    return {
        "transcript": res.transcript,
        "items": [serialise(i) for i in res.items],
        "command": res.command,
        "mode_switch": res.mode_switch,
        "unparsed": res.unparsed,
        "took_ms": took_ms,
    }


@app.get("/api/health")
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


@app.get("/api/catalog")
async def catalog(shop_id: str = DEFAULT_SHOP):
    return {"products": await db.get_products(shop_id)}


@app.post("/api/catalog")
async def add_product(req: ProductRequest):
    return {"product": await db.upsert_product(req.shop_id, req.model_dump())}


@app.post("/api/parse")
async def parse_text(req: ParseRequest):
    """Text in, line items out. The demo's offline path, and the endpoint the Phase 1
    eval harness will drive."""
    t0 = time.perf_counter()
    p = await parser_for(req.shop_id)
    res = p.parse(req.text, asr_confidence=req.asr_confidence)
    payload = result_payload(res, int((time.perf_counter() - t0) * 1000))
    await db.log_utterance(req.shop_id, req.text, payload)
    return payload


@app.post("/api/transcribe")
async def transcribe(audio: UploadFile = File(...), shop_id: str = Form(DEFAULT_SHOP)):
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
    payload = result_payload(res, int((time.perf_counter() - t1) * 1000))
    payload |= {"asr_ms": asr_ms, "parse_ms": payload["took_ms"], "bytes": len(raw)}
    await db.log_utterance(shop_id, tr.text, payload)
    return payload


@app.post("/api/finalize")
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


@app.post("/api/confirm")
async def confirm(bill_id: str = Form(...), shop_id: str = Form(DEFAULT_SHOP)):
    await db.save_bill(shop_id, {"id": bill_id, "total": 0, "items": [],
                                 "payment_state": "confirmed"})
    return {"ok": True, "bill_id": bill_id}


# Local dev only. On Vercel, public/ is served by the platform.
_public = Path(__file__).resolve().parents[1] / "public"
if _public.exists() and not os.environ.get("VERCEL"):
    app.mount("/", StaticFiles(directory=str(_public), html=True), name="static")
