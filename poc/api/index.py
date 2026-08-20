"""Vaakku PoC API.

One FastAPI app behind /api/*. Runs locally with uvicorn and on Vercel's Python runtime
unchanged. Everything here is disposable except the parser and the language pack.
"""

from __future__ import annotations

import base64
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from urllib.parse import parse_qs, urlencode

from fastapi import APIRouter, FastAPI, File, Form, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

sys.path.insert(0, str(Path(__file__).parent / "_lib"))

import httpx                                           # noqa: E402
import auth                                            # noqa: E402
import db                                              # noqa: E402
from parser import (ADMIN_SAME_ITEM_THRESHOLD, Catalog, Lang,  # noqa: E402
                    Parser, norm)
import gst                                             # noqa: E402
import receipt as receipts                            # noqa: E402
from sarvam import SarvamASR, get_asr                  # noqa: E402
from upi import build_uri, qr_data_uri                 # noqa: E402
import period                                          # noqa: E402
import reorder                                         # noqa: E402
import recipes                                         # noqa: E402
import vision                                          # noqa: E402

app = FastAPI(title="Vaakku PoC")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


@app.exception_handler(RequestValidationError)
async def readable_validation_error(request: Request, exc: RequestValidationError):
    """Say which field was wrong, in words.

    FastAPI's default is a `detail` array of loc/msg/type objects. That is fine when a
    developer is watching a browser console and useless when the caller is somebody else's
    agent in somebody else's codebase: the shapes do not match anything the rest of this
    API returns, and the one thing an integrator needs — which field, and what was expected
    — is buried three levels down.

    Note this fires BEFORE any handler runs, so a request with both a malformed body and a
    bad key gets 422 rather than 401. Worth knowing when a key looks like the problem.
    """
    problems = []
    for e in exc.errors():
        # loc is ('body', 'items', 0, 'qty') — the leading 'body' is noise to a caller who
        # already knows they sent a body.
        where = ".".join(str(x) for x in e.get("loc", ()) if x != "body") or "body"
        problems.append({"field": where, "problem": e.get("msg", ""),
                         "got": repr(e.get("input"))[:80]})
    return JSONResponse(
        {"ok": False, "error": "The request body is not the shape this endpoint expects",
         "problems": problems,
         "expected": {"items": [{"text": "800 gram X plus 200 gram Y"},
                                {"name": "Americano", "qty": 2}],
                      "customer_mobile": "9840099887"}},
        status_code=422)

# Routes are declared on a router and then mounted at several prefixes. Vercel's rewrite
# hands the function a path that is not reliably the one the browser asked for, and guessing
# wrong shows up as a FastAPI 404 that looks exactly like a broken deploy. Mounting at every
# plausible prefix costs nothing and removes the guess.
router = APIRouter()

# One Lang per pack, built once. Which pack a request uses follows the shop's chosen
# language, so a Kannada shop is parsed with Kannada numerals and units.
LANGS: dict[str, Lang] = {}
LANG_FOR = {"en": "ta-en", "ta": "ta-en", "hi": "hi-en",
            "ml": "ml-en", "te": "te-en", "kn": "kn-en"}
ASR_FOR = {"en": "en-IN", "ta": "ta-IN", "hi": "hi-IN",
           "ml": "ml-IN", "te": "te-IN", "kn": "kn-IN"}


# Shops created before the language picker kept the column default, which was the *pack*
# name "ta-en" rather than a language code. Every lookup keyed on codes missed it and fell
# through to Tamil, so an English shop's speech was transcribed as Tamil while the UI —
# whose own lookup also missed — silently rendered in English. Normalise once, here.
# "ta-en" lands on English, not Tamil. It was a column default, never a choice anyone
# made, and English is the safer place for an unchosen value: a wrong English transcript
# still lands in Latin script alongside a Latin catalog, whereas ta-IN turns English speech
# into Tamil script ("cappuccino plain" -> "கேப் எக்ஸினோ பிளேன்") which matches nothing.
# Shops that really are Tamil set it once in Settings and it sticks.
LEGACY_LANG = {"ta-en": "en", "ta_en": "en", "ta-in": "ta", "hi-en": "hi",
               "ml-en": "ml", "te-en": "te", "kn-en": "kn", "en-in": "en"}


def norm_lang(code: str) -> str:
    code = (code or "").strip().lower()
    if code in ASR_FOR:
        return code
    return LEGACY_LANG.get(code, "en")


def lang_for(code: str) -> Lang:
    pack = LANG_FOR.get(norm_lang(code), "ta-en")
    if pack not in LANGS:
        try:
            LANGS[pack] = Lang(pack)
        except Exception:                              # noqa: BLE001 — never break billing
            LANGS[pack] = LANGS.setdefault("ta-en", Lang("ta-en"))
    return LANGS[pack]


LANG = lang_for("ta")
DEFAULT_SHOP = os.environ.get("DEFAULT_SHOP_ID", "demo")


async def parser_for(shop_id: str, lang: str = "") -> Parser:
    if not lang:
        shop = await db.get_shop(shop_id) or {}
        lang = shop.get("lang") or "en"
    return Parser(lang_for(lang), Catalog(await db.get_products(shop_id)))


class ParseRequest(BaseModel):
    text: str
    shop_id: str = DEFAULT_SHOP
    asr_confidence: float = 1.0
    mode: str = "billing"
    lang: str = ""


class ShopRequest(BaseModel):
    mobile: str
    name: str = "Shop"
    vpa: str = ""


class CheckRequest(BaseModel):
    mobile: str


class SignupRequest(BaseModel):
    mobile: str
    passcode: str
    name: str = "Shop"
    vpa: str = ""
    lang: str = "ta"
    remember: bool = True


class LoginRequest(BaseModel):
    mobile: str
    passcode: str
    remember: bool = True


class SettingsRequest(BaseModel):
    name: str = ""
    lang: str = ""
    vpa: str = ""
    wa_number: str = ""
    gstin: str = ""


class DeleteRequest(BaseModel):
    id: str


class StaffRequest(BaseModel):
    mobile: str
    passcode: str
    name: str = ""


class FinalizeRequest(BaseModel):
    shop_id: str = DEFAULT_SHOP
    items: list[dict]
    vpa: str
    payee: str = "Shop"
    customer_mobile: str = ""


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
    category: str = "resale"
    recipe: list[dict] = []


def serialise(item) -> dict:
    return {
        "product_id": item.product_id, "name": item.name, "qty": item.qty,
        "unit": item.unit, "unit_price": item.unit_price, "amount": item.amount,
        "confidence": item.confidence, "verdict": item.verdict,
        "price_led": item.price_led, "raw": item.raw,
        "match_score": item.match_score, "spoken_name": item.spoken_name,
        "needs_price": item.needs_price, "combo": item.combo,
    }


def claims_of(request: Request) -> dict | None:
    """Read the bearer token. Endpoints that mutate a shop trust this, never a shop_id
    supplied in the body — otherwise the passcode would be decoration."""
    header = request.headers.get("authorization", "")
    token = header[7:] if header.lower().startswith("bearer ") else ""
    return auth.read_token(token)


def deny(msg: str, code: int = 401):
    return JSONResponse({"ok": False, "error": msg}, status_code=code)


def tidy_name(name: str) -> str:
    """Title-case a dictated name so the catalog reads like a catalog, not a transcript.
    Latin only — Indic scripts have no case, and .title() would corrupt combining marks."""
    if any(c > "\u0900" for c in name):
        return name.strip()
    return " ".join(w.capitalize() if w.islower() else w for w in name.strip().split())


def admin_proposals(res) -> list[dict]:
    """Turn an admin-mode utterance into a proposed catalog change, for the UI to confirm.

    The important judgement is existing-vs-new. In billing a fuzzy match is what you want;
    here it is destructive — "maida" scores 0.857 against Wheat Flour, and acting on that
    silently reprices a product the shopkeeper never mentioned. Anything below the strict
    threshold is treated as a new item named by what was actually said.
    """
    def rate(amount: float, qty: float | None) -> float:
        """Per-UOM rate. "potato 2 kilo is 100 rupees" sets 50/kg, not 100/kg — the
        shopkeeper is quoting a quantity, and storing the lump sum as the unit price
        would double every potato line thereafter."""
        return round(amount / qty, 2) if qty and qty > 0 else round(amount, 2)

    def certainty(action: str, score: float, qty: float | None) -> bool:
        """Safe to write without asking?

        Two things have to hold. The identity must be unambiguous — either nothing close
        exists (a genuine new item) or it is effectively an exact hit. And the rate must be
        unambiguous: "coffee 250 gram 150 rupees" could mean Rs0.60/g or a Rs150 pack, and
        guessing between those silently is how a catalog quietly goes wrong.
        """
        if qty not in (None, 1, 1.0):
            return False
        return score < 0.70 if action == "create" else score >= 0.98

    # One proposal per dictated item, not just the first — a shopkeeper setting up a
    # catalog says several in a row, and returning only the leading one silently dropped
    # the rest.
    out = []
    for it in res.items:
        if not it.price_led:
            continue
        q = it.spoken_qty
        if it.match_score >= ADMIN_SAME_ITEM_THRESHOLD:
            out.append({"action": "reprice", "id": it.product_id, "name": it.name,
                        "unit": it.unit, "price": rate(it.amount, q),
                        "was": it.unit_price, "qty": q,
                        "certain": certainty("reprice", it.match_score, q)})
        else:
            out.append({"action": "create", "id": "",
                        "name": tidy_name(it.spoken_name or it.name),
                        "unit": it.unit, "price": rate(it.amount, q),
                        "near": it.name, "near_score": it.match_score, "qty": q,
                        "certain": certainty("create", it.match_score, q)})
    for u in res.unmatched:
        if u.get("money"):
            out.append({"action": "create", "id": "", "name": tidy_name(u["name"]),
                        "unit": u.get("unit") or "piece", "near": "", "near_score": 0.0,
                        "qty": u.get("qty"), "price": rate(u["money"], u.get("qty")),
                        "certain": certainty("create", 0.0, u.get("qty"))})
    return out


def result_payload(res, took_ms: int, mode: str = "billing", parser=None) -> dict:
    payload = {
        "transcript": res.transcript,
        "items": [serialise(i) for i in res.items],
        "command": res.command,
        "mode_switch": res.mode_switch,
        "unparsed": res.unparsed,
        # Names the grammar understood but the catalog has never heard of. With shops now
        # starting empty this is the common case, and dropping it server-side is what made
        # billing look deaf: perfect transcript, no line, no reason given.
        #
        # Each one carries the nearest things the shop really sells, so the answer to "we
        # don't stock that" can be "did you mean this?" — which teaches the catalog a
        # second name for one product instead of giving it a second product.
        "unmatched": [
            {**u, "candidates": (parser.catalog.candidates(u.get("name", ""), parser.lang)
                                 if parser else [])}
            for u in res.unmatched
        ],
        # Heard, and not addressed to us. Reported so the client can tell "the room was
        # talking" from "you said something and we failed you" — the second deserves a
        # message on screen, the first deserves silence.
        "noise": res.noise,
        "customer_mobile": res.customer_mobile,
        # Whether the phone was addressed by name. The client needs it to tell a hands-free
        # command from a button press that happened to contain the same words.
        "woke": res.woke,
        "number": res.number,
        "took_ms": took_ms,
    }
    if mode == "admin" or res.mode_switch == "admin":
        payload["admin"] = admin_proposals(res)
    return payload


@router.get("/health")
async def health():
    asr = get_asr()
    return {
        "ok": True,
        "asr_backend": asr.name,
        "asr_configured": SarvamASR().configured,
        # Whether a key is present, never any part of it. Import and recipe drafting both
        # fail with a message the shopkeeper cannot act on if this is missing, and there
        # was no way to tell that apart from a broken feature without checking here.
        "ai_configured": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "db": "supabase" if db.configured() else "seed-csv (in memory)",
        "lang": LANG.data["code"],
        "products": len(await db.get_products(DEFAULT_SHOP)),
    }


@router.get("/schema")
async def schema_probe():
    """Diagnostic: which tables and columns PostgREST can see right now."""
    return await db.probe()


@router.get("/catalog")
async def catalog(shop_id: str = DEFAULT_SHOP, fresh: int = 0):
    # The admin list must never show a stale catalog: the cache lives per serverless
    # instance, so a read can land somewhere that has not seen the write. Catalog reads are
    # rare (mode switch, after a save), so bypassing it costs nothing that matters.
    if fresh:
        db.invalidate(shop_id)
    return {"products": await db.get_products(shop_id)}


@router.post("/catalog")
async def add_product(req: ProductRequest, request: Request):
    c = claims_of(request)
    # A missing token used to fall through to the body's shop_id, which meant anyone could
    # write into any shop's catalog. The token is the only source of shop identity here.
    if not c:
        return deny("Sign in required")
    shop_id = c["shop"]
    # Staff bill; they do not reprice. Learning a price during billing is the one write a
    # worker can cause, and it only ever fills in a blank.
    if c["role"] != "owner" and req.unit_price and not req.id:
        return deny("Owner only", 403)
    # The UOM box is free text, so "Kg", "KILO" and "கிலோ" all arrive as themselves.
    # Canonicalise on the way in: a unit the conversion table cannot recognise silently
    # skips the conversion, and "500 gram" against a "Kg" product bills 500 kilos.
    # A write only replaces the fields it actually names. The catalog editor sends name,
    # unit and price and nothing else, and the upsert rewrites the whole row — so fixing a
    # typo in a product's name silently threw away every alias the shop had been taught
    # and its description with them. Anything the caller did not mention is carried over
    # from the stored row. A partial write must never be a destructive one.
    sent = req.model_fields_set
    payload = req.model_dump()
    if req.id:
        existing = next((p for p in await db.get_products(shop_id) if p["id"] == req.id), None)
        if existing:
            payload = {**existing, **{k: v for k, v in payload.items() if k in sent}}
            payload["id"] = req.id
    shop = await db.get_shop(shop_id) or {}
    payload["unit"] = lang_for(shop.get("lang") or "en").canonical_unit(payload.get("unit")) or "piece"
    product, error = await db.upsert_product(shop_id, payload)
    # The error is returned rather than swallowed. Previously a rejected write still came
    # back looking like a success, and the UI cheerfully announced a price change that had
    # not happened — the exact failure mode this product cannot afford.
    return JSONResponse({"product": product, "ok": not error, "error": error},
                        status_code=200 if not error else 502)


@router.post("/auth/check")
async def auth_check(req: CheckRequest):
    """Does this number already belong to a shop or a worker? Decides whether the landing
    page asks for a passcode (sign in) or the full signup form."""
    shop_id = db.shop_key(req.mobile)
    shop = await db.get_shop(shop_id)
    if shop and shop.get("passcode_hash"):
        return {"exists": True, "role": "owner", "shop_name": shop.get("name", "")}
    staff = await db.get_staff(db.shop_key(req.mobile))
    if staff:
        # Resolve the shop so a worker sees which shop they are signing in to, rather than
        # a bare passcode box.
        shop = await db.get_shop(staff["shop_id"]) or {}
        return {"exists": True, "role": staff.get("role", "user"),
                "shop_name": shop.get("name", "")}
    return {"exists": False, "role": None, "shop_name": ""}


@router.post("/auth/signup")
async def auth_signup(req: SignupRequest):
    code = auth.normalise_passcode(req.passcode)
    if len(code) != 6:
        return deny("Passcode must be 6 digits", 400)
    shop_id = db.shop_key(req.mobile)
    if len(shop_id) != 10:
        return deny("Enter a 10-digit mobile number", 400)
    existing = await db.get_shop(shop_id)
    if existing and existing.get("passcode_hash"):
        return deny("This number already has a shop — sign in instead", 409)
    error = await db.create_shop(shop_id, req.name, req.vpa,
                                 auth.hash_passcode(code), norm_lang(req.lang))
    if error:
        return deny(error, 502)
    await db.add_staff(shop_id, shop_id, auth.hash_passcode(code), "owner", req.name)
    return {"ok": True, "token": auth.issue_token(shop_id, shop_id, "owner", req.remember),
            "shop_id": shop_id, "role": "owner", "shop_name": req.name,
            "vpa": req.vpa, "lang": norm_lang(req.lang)}


@router.post("/auth/login")
async def auth_login(req: LoginRequest):
    code = auth.normalise_passcode(req.passcode)
    mobile = db.shop_key(req.mobile)
    shop = await db.get_shop(mobile)
    if shop and shop.get("passcode_hash") and auth.verify_passcode(code, shop["passcode_hash"]):
        return {"ok": True, "token": auth.issue_token(mobile, mobile, "owner", req.remember),
                "shop_id": mobile, "role": "owner", "shop_name": shop.get("name", ""),
                "vpa": shop.get("upi_vpa", ""), "lang": norm_lang(shop.get("lang"))}
    staff = await db.get_staff(mobile)
    if staff and auth.verify_passcode(code, staff.get("passcode_hash", "")):
        shop = await db.get_shop(staff["shop_id"]) or {}
        return {"ok": True,
                "token": auth.issue_token(staff["shop_id"], mobile, staff.get("role", "user"),
                                          req.remember),
                "shop_id": staff["shop_id"], "role": staff.get("role", "user"),
                "shop_name": shop.get("name", ""), "vpa": shop.get("upi_vpa", ""),
                "lang": shop.get("lang") or "ta"}
    # One message for both causes, so this can't be used to enumerate numbers.
    return deny("Wrong number or passcode", 401)


@router.get("/staff")
async def staff_list(request: Request):
    c = claims_of(request)
    if not c or c["role"] != "owner":
        return deny("Owner only")
    rows = await db.list_staff(c["shop"])
    return {"staff": [{"mobile": r["mobile"], "role": r.get("role", "user"),
                       "name": r.get("name", "")} for r in rows]}


@router.post("/staff")
async def staff_add(req: StaffRequest, request: Request):
    c = claims_of(request)
    if not c or c["role"] != "owner":
        return deny("Owner only")
    code = auth.normalise_passcode(req.passcode)
    mobile = db.shop_key(req.mobile)
    if len(mobile) != 10:
        return deny("Enter a 10-digit mobile number", 400)
    if len(code) != 6:
        return deny("Passcode must be 6 digits", 400)
    error = await db.add_staff(c["shop"], mobile, auth.hash_passcode(code), "user", req.name)
    return {"ok": not error, "error": error, "mobile": mobile}


@router.delete("/catalog")
async def clear_catalog(request: Request):
    """Empty this shop's catalog. Owner only, and scoped to the token's own shop — there is
    no way to reach another shop's products through this."""
    c = claims_of(request)
    if not c or c["role"] != "owner":
        return deny("Owner only")
    removed, error = await db.clear_products(c["shop"])
    return JSONResponse({"ok": not error, "removed": removed, "error": error},
                        status_code=200 if not error else 502)


@router.post("/catalog/delete")
async def delete_product(req: DeleteRequest, request: Request):
    """Remove one SKU. POST rather than DELETE-with-body, which proxies handle unevenly."""
    c = claims_of(request)
    if not c or c["role"] != "owner":
        return deny("Owner only")
    ok, error = await db.delete_product(c["shop"], req.id)
    return JSONResponse({"ok": ok and not error, "error": error},
                        status_code=200 if ok and not error else 502)


@router.get("/settings")
async def settings_get(request: Request):
    c = claims_of(request)
    if not c:
        return deny("Sign in required")
    if c["role"] != "owner":
        return deny("Owner only", 403)
    shop = await db.get_shop(c["shop"]) or {}
    gstin = shop.get("gstin", "")
    return {"ok": True, "mobile": c["shop"], "name": shop.get("name", ""),
            "lang": norm_lang(shop.get("lang")), "vpa": shop.get("upi_vpa", ""),
            "wa_number": shop.get("wa_number", ""), "gstin": gstin,
            "gst_state": gst.state_of(gstin),
            # Whether one exists, never any part of it — the digest is all that is stored,
            # and the button only needs to know whether it is generating or replacing.
            "has_order_key": bool(shop.get("order_key_hash")),
            "stored_lang": shop.get("lang", "")}


@router.post("/settings")
async def settings_set(req: SettingsRequest, request: Request):
    c = claims_of(request)
    if not c:
        return deny("Sign in required")
    if c["role"] != "owner":
        return deny("Owner only", 403)
    shop = await db.get_shop(c["shop"]) or {}
    name = req.name.strip() or shop.get("name", "")
    lang = norm_lang(req.lang or shop.get("lang"))
    vpa = req.vpa.strip() or shop.get("upi_vpa", "")

    # The WhatsApp line is the shop's, not the owner's sign-in number, so it is stored
    # separately and may be cleared. A blank is a deliberate answer here, not an omission.
    wa = db.shop_key(req.wa_number) if req.wa_number.strip() else ""
    if req.wa_number.strip() and len(wa) != 10:
        return deny("A WhatsApp number is 10 digits", 400)

    # Rejected loudly rather than stored hopefully. A wrong GST number on every receipt is
    # a compliance problem discovered by somebody else, months later.
    gstin, bad = gst.check(req.gstin)
    if bad:
        return deny(bad, 400)

    error = await db.update_shop(c["shop"], name, vpa, lang, wa_number=wa, gstin=gstin)
    return JSONResponse({"ok": not error, "error": error, "name": name, "lang": lang,
                         "vpa": vpa, "wa_number": wa, "gstin": gstin,
                         "gst_state": gst.state_of(gstin)},
                        status_code=200 if not error else 502)


@router.get("/history")
async def history(shop_id: str = DEFAULT_SHOP, mobile: str = "", limit: int = 5):
    """The last few bills for one customer at this shop, for the repeat-order chips."""
    mobile = db.shop_key(mobile) if mobile else ""
    if len(mobile) != 10:
        return {"ok": True, "bills": []}
    rows = await db.customer_bills(shop_id, mobile, max(1, min(limit, 10)))
    return {"ok": True, "mobile": mobile, "bills": [
        {"id": r.get("id"), "total": float(r.get("total") or 0),
         "created_at": r.get("created_at"),
         "items": [i for i in (r.get("items") or []) if i.get("name")]}
        for r in rows]}


@router.get("/shops")
async def shops_by_name(q: str = ""):
    """Find a shop id by name. Returns id and name only — no passcode material."""
    rows = await db.find_shops(q) if q else []
    # Language included so a misconfigured shop can be spotted without signing in as it.
    # Nothing secret here — id, name and language only, never passcode material.
    return {"shops": [{"id": r["id"], "name": r.get("name", ""),
                       "stored_lang": r.get("lang", ""),
                       "effective_lang": norm_lang(r.get("lang"))} for r in rows]}


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
    p = await parser_for(req.shop_id, req.lang)
    res = p.parse(req.text, asr_confidence=req.asr_confidence, mode=req.mode)
    payload = result_payload(res, int((time.perf_counter() - t0) * 1000), req.mode, p)
    await db.log_utterance(req.shop_id, req.text, payload)
    return payload


@router.post("/transcribe")
async def transcribe(audio: UploadFile = File(...), shop_id: str = Form(DEFAULT_SHOP),
                     mode: str = Form("billing"), lang: str = Form("")):
    """Audio in, line items out. Reports asr_ms separately from parse_ms because the
    latency budget in PLAN.md is about the parse stage, and the network hop here is an
    artefact of the PoC that the shipped product will not have."""
    t0 = time.perf_counter()
    raw = await audio.read()
    # The shop's own language is authoritative. Falling back to a client-supplied value
    # meant an omitted field silently became English, regardless of what the business had
    # chosen — the language belongs to the business, not to whatever the page happened
    # to send.
    if not lang:
        shop = await db.get_shop(shop_id) or {}
        lang = shop.get("lang") or ""
    asr = get_asr()
    tr = await asr.transcribe(raw, audio.filename or "clip.webm",
                              language=ASR_FOR[norm_lang(lang)])
    asr_ms = int((time.perf_counter() - t0) * 1000)

    if tr.error or not tr.text:
        return JSONResponse(
            {"transcript": "", "items": [], "command": None, "mode_switch": None,
             "unparsed": [], "error": tr.error or "nothing recognised",
             "asr_ms": asr_ms, "parse_ms": 0, "bytes": len(raw)},
            status_code=200,                           # a demo must degrade, not error
        )

    t1 = time.perf_counter()
    p = await parser_for(shop_id, lang)
    res = p.parse(tr.text, asr_confidence=tr.confidence, mode=mode)
    payload = result_payload(res, int((time.perf_counter() - t1) * 1000), mode, p)
    payload |= {"asr_ms": asr_ms, "parse_ms": payload["took_ms"], "bytes": len(raw),
                "lang": norm_lang(lang)}
    await db.log_utterance(shop_id, tr.text, payload)
    return payload


# A recipe may name something that itself has a recipe (a shop that sells "Iced Latte" out
# of a "Latte"). Depth is bounded because a recipe cycle entered by hand — A contains B
# contains A — would otherwise spin until the request times out, at the moment of sale.
RECIPE_DEPTH = 4


def explode(products: dict[str, dict], product_id: str, qty: float,
            depth: int = 0, seen: tuple = ()) -> list[dict]:
    """One sold line, as the movements it actually causes.

    A product with a recipe has no stock of its own — what leaves the shelf is its
    components, scaled by how many were sold. A product without one decrements itself,
    which is every shop that has never opened the recipe screen.
    """
    p = products.get(product_id)
    recipe = (p or {}).get("recipe") or []
    if not recipe or depth >= RECIPE_DEPTH or product_id in seen:
        return [{"product_id": product_id, "delta": -qty}]
    out: list[dict] = []
    for c in recipe:
        cid = c.get("component_id")
        if not cid or cid not in products:
            continue                     # a component that was deleted consumes nothing
        out += explode(products, cid, qty * float(c.get("qty") or 0),
                       depth + 1, seen + (product_id,))
    # A recipe whose every component has since been deleted must not make the sale
    # invisible. Falling back to the item itself keeps the ledger honest about the fact
    # that something was sold.
    return out or [{"product_id": product_id, "delta": -qty}]


async def make_bill(shop_id: str, items: list[dict], vpa: str, payee: str,
                    customer_mobile: str = "") -> dict:
    """Issue a bill: number it, store the document as issued, and take the goods off the
    shelf. Shared by the counter's Finalise and by accepting an order, because those are the
    same event reached two ways — and a second copy of this would be a second place for the
    receipt series to skip a number."""
    total = round(sum(float(i["amount"]) for i in items), 2)
    ref = f"VK{uuid.uuid4().hex[:8].upper()}"
    uri = build_uri(vpa, payee, total, ref=ref)

    shop = await db.get_shop(shop_id) or {}
    now = datetime.now(receipts.IST)
    fy = receipts.financial_year(now)
    n = await db.next_receipt_no(shop_id, fy)
    number = receipts.serial(fy, n) if n else ""
    bill = {"total": total, "items": items, "upi_ref": ref,
            "payment_state": "pending", "customer_mobile": customer_mobile}
    doc = receipts.build(shop, bill, number, now)
    bill_id = await db.save_bill(shop_id, {**bill, "receipt_no": number, "receipt": doc})

    products = {p["id"]: p for p in await db.get_products(shop_id)}
    moves: list[dict] = []
    for it in items:
        for part in (it.get("combo") or [it]):
            pid, qty = part.get("product_id"), part.get("qty")
            if not pid or not qty:
                continue
            moves += explode(products, pid, abs(float(qty)))
    await db.move_stock(shop_id, moves, "sale", bill_id)
    return {"bill_id": bill_id, "total": total, "ref": ref,
            "upi_uri": uri, "qr": qr_data_uri(uri),
            "receipt_no": number, "receipt": doc,
            "receipt_text": receipts.as_text(doc),
            "receipt_message": receipts.as_whatsapp(doc),
            "confirmation": "manual"}


@router.post("/finalize")
async def finalize(req: FinalizeRequest):
    """The counter's Finalise. Everything it does is in make_bill, which accepting an order
    also calls — the two are the same event reached from different screens."""
    return await make_bill(req.shop_id, req.items, req.vpa, req.payee, req.customer_mobile)


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------
# An order is not a bill. It is what somebody asked for — over the phone, through an agent,
# or at the counter for collection later — and it can be refused, can sit for an hour, and
# may name something the shop has run out of. Keeping the two apart means a refusal never
# has to be explained as a cancelled bill, and an order nobody has accepted cannot take a
# receipt number out of the shop's series.
#
# The text arrives in the same shape a shopkeeper would say it, and goes through the same
# parser: "800 gram plantation double A plus 200 gram cherry peaberry" resolves to one
# blended line over two products, priced from the shop's own catalog. There is no second
# grammar for machines to get wrong.


class OrderLine(BaseModel):
    text: str = ""                  # "800 gram plantation double A plus 200 gram cherry peaberry"
    name: str = ""                  # or a plain catalog name
    qty: float = 1


# A template variable that was never substituted. It arrives as a literal "{{...}}" and
# would otherwise be parsed as a product name, matched against nothing, and reported as an
# item the shop does not sell — sending an integrator to fix their catalog when the fault
# is in their prompt template.
UNRENDERED = re.compile(r"\{\{[^}]*\}\}|\{%[^%]*%\}|\$\{[^}]*\}")


class OrderRequest(BaseModel):
    items: list[OrderLine] = []
    customer_mobile: str = ""
    customer_name: str = ""
    note: str = ""
    shop_id: str = ""               # only honoured for a counter session, never for a key
    # Parse and price it, then throw it away. An integration is wired up by trial and
    # error, and every trial without this lands in a real shopkeeper's queue as an order
    # they have to refuse — which is how a shop learns to stop trusting the queue.
    dry_run: bool = False

    @field_validator("items", mode="before")
    @classmethod
    def accept_plain_text(cls, v):
        """Take the order however the caller has it.

        A language model writes an order as a sentence. Demanding a JSON array of objects
        makes that a second grammar for a machine to get wrong — which is exactly what this
        endpoint set out to avoid, and exactly what it was doing. So all three shapes are
        accepted and all three end up in the same parser:

            "800 gram X plus 200 gram Y, 2 americano"     one string, whole order
            ["800 gram X plus 200 gram Y", "2 americano"] a line each
            [{"text": "..."}, {"name": "Y", "qty": 2}]    structured

        Commas and full stops already separate items for a shopkeeper dictating a bill, so
        a whole order in one string splits the same way a spoken one does.
        """
        if isinstance(v, str):
            return [{"text": v}]
        if isinstance(v, list):
            return [{"text": x} if isinstance(x, str) else x for x in v]
        return v


class OrderActionRequest(BaseModel):
    order_id: str
    reason: str = ""


def order_key_from(request: Request) -> str:
    """Find the order key wherever the caller's platform decided to put it.

    Agent and automation platforms each expose one auth widget and not the others: some
    offer only Bearer, some only Basic, some only a named header. Which of those a shop's
    integration can use is not something the shop chooses, so all of them are accepted.
    The key's own prefix is what identifies it, which is why the scheme does not have to.

    Not accepted from the query string, deliberately. A URL is written to access logs, proxy
    logs and analytics by default, and a credential in one is a credential that leaks
    somewhere nobody thought to look. Every scheme below travels in a header instead.
    """
    named = request.headers.get("x-order-key", "").strip()
    if named:
        return named

    header = request.headers.get("authorization", "").strip()
    if not header:
        return ""
    scheme, _, rest = header.partition(" ")
    rest = rest.strip()

    if scheme.lower() == "bearer":
        return rest
    if scheme.lower() == "basic":
        # The key may be the username or the password depending on the platform, and some
        # send a placeholder in the other field. Both halves are offered to the lookup and
        # the prefix decides — guessing a convention would fail for half of them.
        try:
            decoded = base64.b64decode(rest + "=" * (-len(rest) % 4)).decode("utf-8", "replace")
        except Exception:                              # noqa: BLE001
            return ""
        user, _, password = decoded.partition(":")
        for candidate in (user.strip(), password.strip()):
            if auth.looks_like_order_key(candidate):
                return candidate
        return ""
    # No scheme at all — "Authorization: bolo_ord_...". Wrong per the RFC and common enough
    # in hand-configured integrations; the prefix makes it unambiguous.
    return header if auth.looks_like_order_key(header) else ""


async def order_caller(request: Request) -> tuple[str, str, dict | None]:
    """Who is placing this order: an automated caller with a key, or the counter.

    Returns (shop_id, source, error). The key is looked up by digest — the raw value exists
    only in the caller's configuration, and a database dump does not confer the ability to
    place orders.
    """
    key = order_key_from(request)
    # A session token also arrives as a Bearer, so the prefix is what tells them apart. Only
    # something shaped like an order key is looked up as one; anything else falls through to
    # the counter's own session.
    if key and auth.looks_like_order_key(key):
        shop = await db.shop_by_order_key(auth.order_key_hash(key))
        if not shop:
            return "", "", {"error": "Unknown or revoked order key", "code": 401}
        return shop["id"], "api", None
    c = claims_of(request)
    if c:
        return c["shop"], "counter", None
    # Said in full, because a caller seeing this has no way to know which of these their
    # platform is capable of sending.
    return "", "", {
        "error": "No credential recognised",
        "accepted": ["Authorization: Bearer <key>",
                     "Authorization: Basic <base64 of key: or :key>",
                     "X-Order-Key: <key>"],
        "hint": "The key starts with bolo_ord_ and is generated in Settings.",
        "code": 401}


async def resolve_lines(shop_id: str, lines: list[OrderLine]) -> tuple[list[dict], list[str]]:
    """Turn what was asked for into priced catalog lines, and say what could not be turned.

    Unreadable lines are returned rather than dropped. An order that quietly loses an item
    is worse than one that arrives flagged: the shopkeeper can fix a flagged line, and
    cannot fix one they never saw.
    """
    p = await parser_for(shop_id)
    items, unmatched = [], []
    for line in lines:
        text = (line.text or line.name or "").strip()
        if not text:
            continue
        qty = float(line.qty or 1)
        # A bare catalog name carries its quantity separately, so it is spoken back to the
        # parser the way a shopkeeper would say it — "two americano", not "americano".
        spoken = text if line.text else f"{qty:g} {text}"
        res = p.parse(spoken, mode="billing")
        good = [serialise(i) for i in res.items if i.verdict != "reject"]
        if not good:
            unmatched.append(text)
            continue
        # An explicit qty multiplies a parsed line only when the text did not carry one
        # itself, so "two americano" with qty 3 is not silently six coffees.
        if line.text and qty != 1:
            for it in good:
                it["qty"] = round(it["qty"] * qty, 3)
                it["amount"] = round(it["amount"] * qty, 2)
                for part in (it.get("combo") or []):
                    part["qty"] = round(float(part["qty"]) * qty, 3)
        items += good
        unmatched += [u.get("name", "") if isinstance(u, dict) else str(u)
                      for u in (res.unmatched or [])]
    return items, [u for u in unmatched if u]


@router.post("/orders")
async def order_create(req: OrderRequest, request: Request):
    """Take an order. Reachable by an automated caller holding an order key, or from the
    counter with a signed-in session — the same queue either way."""
    shop_id, source, err = await order_caller(request)
    if err:
        return JSONResponse({"ok": False, **{k: v for k, v in err.items() if k != "code"}},
                            status_code=err["code"])
    if not req.items:
        return deny("No items", 400)

    catalog = await db.get_products(shop_id)
    # An unrendered template reaches here as a literal "{{order_items}}" and would be
    # reported as a product the shop does not stock — sending the integrator to fix a
    # catalog when the fault is three layers up in their prompt.
    raw = " ".join((l.text or l.name or "") for l in req.items)
    leftover = UNRENDERED.findall(raw)
    if leftover:
        return JSONResponse(
            {"ok": False, "error": "A template placeholder was not filled in",
             "reason": "unrendered_template", "found": leftover[:5],
             "hint": "The agent sent the variable's name rather than its value. "
                     "Check the variable exists and is in scope where the body is built."},
            status_code=422)

    items, unmatched = await resolve_lines(shop_id, req.items)
    if not items and unmatched:
        # Nothing understood at all. Refused rather than queued, so an agent gets a clear
        # failure it can read back to the customer instead of the shop receiving a blank
        # order it cannot serve.
        #
        # An empty catalog and a wrong product name produce the same silence here and need
        # completely different fixes — one is "the shop has not set up its prices", the
        # other is "you asked for something they do not sell". Told apart, with a sample of
        # what this shop does sell, because an integrator cannot guess the names.
        priced = [p for p in catalog if float(p.get("unit_price") or 0) > 0]
        if not priced:
            return JSONResponse(
                {"ok": False, "error": "This shop has no priced items yet",
                 "reason": "empty_catalog", "shop_id": shop_id,
                 "hint": "Add items with prices in the app before sending orders."},
                status_code=422)
        return JSONResponse(
            {"ok": False, "error": "Could not match any item",
             "reason": "no_match", "unmatched": unmatched,
             "hint": "Use a name or alias this shop actually sells.",
             "catalog_size": len(priced),
             "sample": sorted(p["name"] for p in priced)[:15]},
            status_code=422)

    total = round(sum(float(i["amount"]) for i in items), 2)
    echo = [{"name": i["name"], "qty": i["qty"], "unit": i["unit"], "amount": i["amount"],
             "parts": [{"name": c["name"], "qty": c["qty"]} for c in (i.get("combo") or [])]}
            for i in items]
    if req.dry_run:
        return JSONResponse({"ok": True, "dry_run": True, "status": "not_saved",
                             "total": total, "items": echo, "unmatched": unmatched},
                            status_code=200)
    order_id, error = await db.save_order(shop_id, {
        "source": source, "items": items, "total": total,
        "customer_mobile": db.shop_key(req.customer_mobile) if req.customer_mobile else "",
        "customer_name": tidy_name(req.customer_name), "note": req.note[:300],
        "status": "pending"})
    if error:
        return JSONResponse({"ok": False, "error": error}, status_code=502)
    return JSONResponse({
        "ok": True, "order_id": order_id, "status": "pending", "total": total,
        # Echoed back so the caller can read the order to the customer before hanging up.
        # An agent that cannot confirm what was understood will confirm what it assumed.
        "items": echo, "unmatched": unmatched}, status_code=201)


@router.get("/orders")
async def orders_list(request: Request, status: str = "pending"):
    """The queue. Oldest first — the customer who has waited longest is at the top."""
    c = claims_of(request)
    if not c:
        return deny("Sign in required")
    rows = await db.list_orders(c["shop"], status if status != "all" else "")
    return {"ok": True, "orders": [{
        "id": r.get("id"), "source": r.get("source", "api"),
        "status": r.get("status", "pending"),
        "customer_mobile": r.get("customer_mobile", ""),
        "customer_name": r.get("customer_name", ""),
        "note": r.get("note", ""), "total": float(r.get("total") or 0),
        "items": r.get("items") or [], "created_at": r.get("created_at"),
        "bill_id": r.get("bill_id"), "reject_reason": r.get("reject_reason", ""),
    } for r in rows]}


@router.post("/orders/accept")
async def order_accept(req: OrderActionRequest, request: Request):
    """Turn an order into a bill. Only from the counter, and only by someone signed in:
    accepting commits the shop to making it, and takes the goods off the shelf."""
    c = claims_of(request)
    if not c:
        return deny("Sign in required")
    order = await db.get_order(c["shop"], req.order_id)
    if not order:
        return deny("No such order", 404)
    if order.get("status") != "pending":
        # Two taps on a slow connection must not bill a customer twice.
        return JSONResponse({"ok": False, "error": f"Already {order.get('status')}",
                             "status": order.get("status")}, status_code=409)

    shop = await db.get_shop(c["shop"]) or {}
    bill = await make_bill(c["shop"], order.get("items") or [],
                           shop.get("upi_vpa", ""), shop.get("name", "Shop"),
                           order.get("customer_mobile", ""))
    error = await db.settle_order(c["shop"], req.order_id,
                                  {"status": "accepted", "bill_id": bill["bill_id"]})
    return JSONResponse({"ok": not error, "error": error, "order_id": req.order_id,
                         **bill,
                         "message": order_message(shop, order, bill),
                         "mobile": order.get("customer_mobile", "")},
                        status_code=200 if not error else 502)


@router.post("/orders/reject")
async def order_reject(req: OrderActionRequest, request: Request):
    """Refuse an order — usually because the shelf cannot cover it. No bill, no receipt
    number, no stock movement: nothing happened except that the customer must be told."""
    c = claims_of(request)
    if not c:
        return deny("Sign in required")
    order = await db.get_order(c["shop"], req.order_id)
    if not order:
        return deny("No such order", 404)
    if order.get("status") != "pending":
        return JSONResponse({"ok": False, "error": f"Already {order.get('status')}",
                             "status": order.get("status")}, status_code=409)
    error = await db.settle_order(c["shop"], req.order_id,
                                  {"status": "rejected", "reject_reason": req.reason[:200]})
    shop = await db.get_shop(c["shop"]) or {}
    return JSONResponse({"ok": not error, "error": error, "order_id": req.order_id,
                         "message": order_message(shop, order, None, req.reason),
                         "mobile": order.get("customer_mobile", "")},
                        status_code=200 if not error else 502)


def order_message(shop: dict, order: dict, bill: dict | None, reason: str = "") -> str:
    """What the customer is told. Built server-side so the wording is the same whether it
    goes out through a provider or through a tap on wa.me."""
    name = shop.get("name", "")
    lines = [f"*{name}*", ""]
    if bill:
        lines.append("Your order is ready.")
        lines.append("")
        for i in (order.get("items") or []):
            qty = f"{float(i.get('qty') or 0):g} {i.get('unit', '')}".strip()
            lines.append(f"{i.get('name', '')} — {qty} — ₹{receipts.rupees(float(i.get('amount') or 0))}")
        lines += ["", f"*Total ₹{receipts.rupees(bill['total'])}*",
                  "", "Scan the QR at the counter, or pay to:",
                  shop.get("upi_vpa", "")]
    else:
        lines.append("Sorry — we cannot fulfil your order.")
        if reason:
            lines.append(reason)
        lines.append("Please call the shop if you would like to change it.")
    return "\n".join([x for x in lines if x is not None])


class OrderKeyRequest(BaseModel):
    confirm: bool = False


@router.post("/order-key")
async def order_key_make(req: OrderKeyRequest, request: Request):
    """Generate (or replace) this shop's order key. Owner only.

    Returned exactly once, in this response, and never again — only its digest is stored.
    Generating a new one immediately invalidates the old, which is what makes it possible
    to cut off an integration that has gone wrong without touching anything else.
    """
    c = claims_of(request)
    if not c:
        return deny("Sign in required")
    if c["role"] != "owner":
        return deny("Owner only", 403)
    key = auth.new_order_key()
    error = await db.set_order_key(c["shop"], auth.order_key_hash(key))
    return JSONResponse({"ok": not error, "error": error, "key": "" if error else key,
                         "shop_id": c["shop"]},
                        status_code=200 if not error else 502)


@router.get("/lang")
async def lang_words(code: str = "en"):
    """The wake words for a language. Public on purpose — there is nothing secret in a
    word the shopkeeper says out loud in a shop, and the listener needs them before any
    session exists. Served from the pack so a new language stays data, not code."""
    return {"code": norm_lang(code), "wake": lang_for(code).wake}


class DiagRequest(BaseModel):
    report: str = ""


@router.post("/diag")
async def diag(req: DiagRequest):
    """Take the hands-free check's report and keep it under a short code.

    Printing it to the runtime log was not enough: those logs are a live tail, so a report
    is only visible to somebody already watching at that second, which is nobody. It is
    stored instead and handed back a six-character code. Reading it needs that code, so
    nothing is browsable by anyone who happens to guess the URL — and there is nothing
    identifying in it either: device, permissions, audio levels, and what the speech
    recogniser thought it heard while somebody said a wake word into their own phone.
    """
    code = uuid.uuid4().hex[:6].upper()
    print(f"=== HANDS-FREE CHECK {code} ===\n" + (req.report or "")[:6000] + "\n=== END ===")
    await db.log_utterance(f"diag:{code}", (req.report or "")[:8000], {"kind": "hands-free"})
    return {"ok": True, "code": code}


@router.get("/diag")
async def read_diag(code: str = ""):
    """Fetch a report by its code. Without one there is nothing to see."""
    code = (code or "").strip().upper()
    if len(code) != 6:
        return deny("A six-character code is needed", 400)
    rows = await db._get("utterances", {
        "shop_id": f"eq.diag:{code}", "select": "transcript,created_at",
        "order": "created_at.desc", "limit": "1"})
    if not rows:
        return JSONResponse({"ok": False, "error": "No report with that code"},
                            status_code=404)
    return {"ok": True, "code": code, "at": rows[0]["created_at"],
            "report": rows[0]["transcript"]}


class AliasRequest(BaseModel):
    product_id: str
    alias: str


@router.post("/alias")
async def add_alias(req: AliasRequest, request: Request):
    """Teach the catalog another name for something it already sells.

    This is the cure for the duplicate SKU. Without it, a shopkeeper who says
    "பொட்டேட்டோ" for a product stored as "உருளைக்கிழங்கு" is offered a new item, says
    yes because they are mid-sale, and the shop ends up with two potatoes at two prices.
    No string metric can bridge a synonym; only being told can.

    A worker may add one. They meet the unknown words all day, and refusing them leaves
    the shop making duplicates instead. It changes no price, and the owner can see and
    remove every alias in the price list — reversible, and visible, is what makes it safe.
    """
    c = claims_of(request)
    if not c:
        return deny("Sign in required")
    alias = (req.alias or "").strip()
    if not alias:
        return deny("Nothing to add", 400)
    products = await db.get_products(c["shop"])
    target = next((p for p in products if p["id"] == req.product_id), None)
    if not target:
        return deny("No such item", 404)
    existing = [a for a in (target.get("aliases") or []) if a and a.strip()]
    if any(norm(a) == norm(alias) for a in existing + [target["name"]]):
        return {"ok": True, "product": target, "already": True}
    payload = dict(target)
    payload["aliases"] = existing + [alias]
    product, error = await db.upsert_product(c["shop"], payload)
    return JSONResponse({"ok": not error, "error": error, "product": product},
                        status_code=200 if not error else 502)


@router.post("/alias/remove")
async def remove_alias(req: AliasRequest, request: Request):
    """Owners only: an alias that bills the wrong item is exactly what has to be undoable."""
    c = claims_of(request)
    if not c:
        return deny("Sign in required")
    if c["role"] != "owner":
        return deny("Owner only", 403)
    products = await db.get_products(c["shop"])
    target = next((p for p in products if p["id"] == req.product_id), None)
    if not target:
        return deny("No such item", 404)
    payload = dict(target)
    payload["aliases"] = [a for a in (target.get("aliases") or [])
                          if a and norm(a) != norm(req.alias)]
    product, error = await db.upsert_product(c["shop"], payload)
    return JSONResponse({"ok": not error, "error": error, "product": product},
                        status_code=200 if not error else 502)


@router.get("/bills")
async def bills_list(request: Request, limit: int = 40, mobile: str = ""):
    """The shop's own recent bills. Exists to answer two questions at a counter: did that
    one get paid, and can you send me that receipt again.

    With `mobile`, the same list narrowed to one customer — "what did she buy last time",
    which is the question a regular's arrival actually raises.
    """
    c = claims_of(request)
    if not c:
        return deny("Sign in required")
    n = max(1, min(limit, 100))
    # Normalised through the same function that makes a shop id, so a number searched as
    # "+91 98400 12345" finds bills stored as "9840012345". A search that silently returns
    # nothing because of a space reads as "this customer has never been here".
    if mobile:
        rows = await db.customer_bills(c["shop"], mobile, n, select="*")
    else:
        rows = await db.recent_bills(c["shop"], n)
    out = []
    for r in rows:
        doc = r.get("receipt") or {}
        out.append({
            "id": r.get("id"),
            "receipt_no": r.get("receipt_no", ""),
            "created_at": r.get("created_at"),
            "total": float(r.get("total") or 0),
            "items": len([x for x in (r.get("items") or []) if x.get("name")]),
            "customer_mobile": r.get("customer_mobile", ""),
            # 'confirmed' only ever means somebody said so — cash from the shopkeeper, or
            # a provider confirmation. It is never inferred from the handset.
            "paid": r.get("payment_state") == "confirmed",
            "method": r.get("payment_method", ""),
            "receipt_status": r.get("receipt_status", "none"),
            "title": doc.get("title", ""),
        })
    # For a customer search the sum of what is on screen is the answer to "how much has
    # she spent here", so it is computed from these rows rather than fetched again.
    summary = None
    if mobile and out:
        summary = {"mobile": out[0]["customer_mobile"] or db.shop_key(mobile),
                   "count": len(out),
                   "total": round(sum(b["total"] for b in out), 2),
                   "unpaid": round(sum(b["total"] for b in out if not b["paid"]), 2),
                   "last": out[0]["created_at"], "first": out[-1]["created_at"],
                   # Truthful about its own limits: at the cap this is the last N bills,
                   # not the customer's lifetime, and saying so beats a confident number
                   # that quietly stops growing.
                   "capped": len(out) >= n}
    return {"ok": True, "bills": out, "customer": summary}


@router.get("/sales")
async def sales(request: Request, frm: str = "", to: str = "", mobile: str = ""):
    """What the shop took, over the periods a shopkeeper actually thinks in.

    Today, this week and this month come back together from a single query, because they
    overlap and the shopkeeper reads them as one row. A custom range replaces all three:
    having asked a specific question, the answer should not be buried among three others.
    """
    c = claims_of(request)
    if not c:
        return deny("Sign in required")

    custom = period.range_bounds(frm, to) if (frm or to) else None
    if frm or to:
        if not custom:
            return deny("Dates must be YYYY-MM-DD", 400)
        rep = await db.sales_report(c["shop"], custom[0], custom[1], mobile)
        a, b = period.span_ist(custom[0], custom[1])
        rep["unpaid"] = round(float(rep.get("total") or 0) - float(rep.get("paid") or 0), 2)
        return {"ok": True, "range": {**rep, "from": a, "to": b}}

    now = period.now_ist()
    day, week, month = (period.day_bounds(now), period.week_bounds(now),
                        period.month_bounds(now))
    # One fetch covering all three. On the 1st of a month that falls mid-week, the week
    # reaches further back than the month does, so the start is whichever is earliest —
    # not the month's, which would truncate the week and understate it.
    start = period.earliest(day, week, month)
    end = max(day[1], week[1], month[1])
    rep = await db.sales_report(c["shop"], start, end, mobile)

    def slice_of(window) -> dict:
        a, b = period.span_ist(*window)
        days = [d for d in rep.get("days", []) if a <= (d.get("day") or "") <= b]
        out = {"count": sum(int(d.get("count") or 0) for d in days), "from": a, "to": b}
        for f in ("total", "paid", "cash", "upi"):
            out[f] = round(sum(float(d.get(f) or 0) for d in days), 2)
        # Billed and collected are different numbers, and the gap is the one a shopkeeper
        # wants at a glance. Showing only "total sales" would quietly count money that has
        # not arrived.
        out["unpaid"] = round(out["total"] - out["paid"], 2)
        return out

    return {"ok": True, "today": slice_of(day), "week": slice_of(week),
            "month": slice_of(month), "days": rep.get("days", []),
            "partial": rep.get("partial", False)}


@router.get("/receipt/{bill_id}")
async def receipt_get(bill_id: str, request: Request):
    """The stored document, in the three shapes it is needed in: structured for the
    screen, 32-column text for a 58mm roll, and prose for a message."""
    c = claims_of(request)
    if not c:
        return deny("Sign in required")
    row = await db.get_bill(c["shop"], bill_id)
    if not row:
        return JSONResponse({"ok": False, "error": "No such bill"}, status_code=404)
    doc = row.get("receipt")
    if not doc:
        # Bills issued before receipts existed. Rebuilt from what was kept, and honestly
        # unnumbered — inventing a serial after the fact would corrupt the series.
        shop = await db.get_shop(c["shop"]) or {}
        doc = receipts.build(shop, row, row.get("receipt_no", ""),
                             receipts.issued_when(row))
    return {"ok": True, "bill_id": bill_id, "receipt": doc,
            "text": receipts.as_text(doc), "message": receipts.as_whatsapp(doc)}


class SendRequest(BaseModel):
    bill_id: str
    mobile: str


@router.post("/receipt/send")
async def receipt_send(req: SendRequest, request: Request):
    """Send a stored receipt over the WhatsApp Business Cloud API.

    Unconfigured by default, and it says so rather than pretending. Sending needs four
    things that belong to the shop and cannot be invented here: a Meta app with a WhatsApp
    Business Account, the phone number id of the verified sender, a permanent access
    token, and an approved utility template — Meta does not allow arbitrary text to a
    customer who has not messaged first.

    It also costs money. Utility conversations in India bill per conversation, so a shop
    sending a few hundred receipts a month is well past the 10-rupee marginal ceiling in
    CLAUDE.md. This is a premium-tier path (DECISIONS.md), and the free one — a wa.me link
    the shopkeeper taps — is what the app uses by default.
    """
    c = claims_of(request)
    if not c:
        return deny("Sign in required")
    token = os.environ.get("WHATSAPP_TOKEN", "")
    phone_id = os.environ.get("WHATSAPP_PHONE_ID", "")
    template = os.environ.get("WHATSAPP_TEMPLATE", "")
    if not (token and phone_id and template):
        return JSONResponse(
            {"ok": False, "configured": False,
             "error": "WhatsApp sending is not set up for this deployment"},
            status_code=501)

    mobile = db.shop_key(req.mobile)
    if len(mobile) != 10:
        return deny("A 10-digit number is needed", 400)
    row = await db.get_bill(c["shop"], req.bill_id)
    if not row:
        return JSONResponse({"ok": False, "error": "No such bill"}, status_code=404)
    shop = await db.get_shop(c["shop"]) or {}
    doc = row.get("receipt") or receipts.build(shop, row, row.get("receipt_no", ""),
                                               receipts.issued_when(row))

    payload = {
        "messaging_product": "whatsapp",
        "to": f"91{mobile}",
        "type": "template",
        "template": {
            "name": template,
            "language": {"code": "en"},
            # Positional variables, in the order the approved template declares them.
            "components": [{"type": "body", "parameters": [
                {"type": "text", "text": shop.get("name", "")},
                {"type": "text", "text": doc.get("number", "")},
                {"type": "text", "text": f"{doc.get('total', 0):.2f}"},
            ]}],
        },
    }
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            r = await client.post(
                f"https://graph.facebook.com/v21.0/{phone_id}/messages",
                headers={"Authorization": f"Bearer {token}"}, json=payload)
    except Exception as exc:                           # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                            status_code=502)
    sent = r.status_code < 400
    # Recorded either way. "requested" already means captured-but-not-delivered, and a
    # receipt that failed to send must not read as one that arrived.
    await db.update_bill(c["shop"], req.bill_id,
                         {"receipt_status": "sent" if sent else "requested",
                          "customer_mobile": mobile})
    return JSONResponse({"ok": sent, "configured": True,
                         "error": "" if sent else r.text[:300]},
                        status_code=200 if sent else 502)


class StockRequest(BaseModel):
    moves: list[dict]           # [{product_id, qty}]
    reason: str = "inward"      # inward | count | wastage


@router.post("/stock")
async def stock_write(req: StockRequest, request: Request):
    """Goods in, a physical count, or wastage. Owner only — a stock figure decides whether
    the shop believes it is being robbed, so it is not a worker's to move."""
    c = claims_of(request)
    if not c:
        return deny("Sign in required")
    if c["role"] != "owner":
        return deny("Owner only", 403)
    if req.reason not in ("inward", "count", "wastage"):
        return deny("Unknown reason", 400)

    products = {p["id"]: p for p in await db.get_products(c["shop"])}
    moves = []
    for m in req.moves:
        p = products.get(m.get("product_id"))
        if not p:
            continue
        qty = float(m.get("qty") or 0)
        if req.reason == "count":
            # A count is not a delta — it is an assertion about what is on the shelf, so
            # the movement is whatever makes the ledger agree with the shopkeeper's eyes.
            moves.append({"product_id": p["id"], "delta": qty - float(p.get("stock") or 0)})
        else:
            moves.append({"product_id": p["id"],
                          "delta": abs(qty) if req.reason == "inward" else -abs(qty)})
    error = await db.move_stock(c["shop"], moves, req.reason)
    return JSONResponse({"ok": not error, "error": error, "applied": len(moves)},
                        status_code=200 if not error else 502)


# ---------------------------------------------------------------------------
# Categories and recipes
# ---------------------------------------------------------------------------
# A café's shelf holds four different kinds of thing, and treating them as one list is why
# its stock screen never made sense. Only raw and consumable can meaningfully shrink; a
# menu item never sat on a shelf as itself, which is precisely why its sales have to be
# exploded into components before the ledger sees them.
CATEGORIES = ("raw", "consumable", "menu", "resale")

# What a recipe may draw on. A prepared item made of other prepared items is a real thing,
# but letting the AI reach for one turns a flat ingredient list into a tree the shopkeeper
# has to hold in their head — so the draft is offered only over things that are bought.
COMPONENT_CATEGORIES = ("raw", "consumable")


class RecipeRequest(BaseModel):
    product_id: str
    components: list[dict] = []      # [{component_id, qty}]


class RecipeDraftRequest(BaseModel):
    product_id: str
    hint: str = ""


def clean_recipe(components: list[dict], known: dict[str, dict],
                 product_id: str) -> list[dict]:
    """Only real components, only positive quantities, and never the item itself.

    A recipe that names its own product would loop on the next sale; one naming a deleted
    product would consume something that no longer exists. Both are dropped here rather
    than defended against at the counter.
    """
    out, seen = [], set()
    for c in components:
        cid = str(c.get("component_id") or "")
        try:
            qty = float(c.get("qty") or 0)
        except (TypeError, ValueError):
            continue
        if not cid or cid == product_id or cid in seen or cid not in known or qty <= 0:
            continue
        seen.add(cid)
        out.append({"component_id": cid, "qty": round(qty, 6)})
    return out


@router.post("/recipe")
async def recipe_save(req: RecipeRequest, request: Request):
    """Store what a menu item is made of. Owner only — this decides what every future sale
    takes off the shelf."""
    c = claims_of(request)
    if not c:
        return deny("Sign in required")
    if c["role"] != "owner":
        return deny("Owner only", 403)
    products = {p["id"]: p for p in await db.get_products(c["shop"])}
    target = products.get(req.product_id)
    if not target:
        return deny("No such item", 404)
    recipe = clean_recipe(req.components, products, req.product_id)
    # A saved recipe makes this a menu item by definition: it is assembled, not stocked.
    # Clearing the recipe hands it back to whatever it was, defaulting to resale.
    payload = {**target, "recipe": recipe,
               "category": "menu" if recipe else (
                   target.get("category") if target.get("category") != "menu" else "resale")}
    row, error = await db.upsert_product(c["shop"], payload)
    # The database is migrated by hand, so it can be behind this deploy. If the write went
    # through with the recipe column quietly stripped, saying "saved" would be a lie the
    # shopkeeper only discovers when their stock never moves.
    if not error and "recipe" in db._absent:
        error = "Recipes need a database update — run the latest schema.sql"
    return JSONResponse({"ok": not error, "error": error, "product": row,
                         "components": len(recipe)},
                        status_code=200 if not error else 502)


@router.post("/recipe/draft")
async def recipe_draft(req: RecipeDraftRequest, request: Request):
    """Ask for a first draft. Returns it for editing; writes nothing.

    Entering twenty recipes by hand on a phone is the kind of task that never gets done,
    and an empty recipe means the café's whole input side stays invisible. A draft the
    shopkeeper corrects in two taps is a far better trade than a screen they abandon.
    """
    c = claims_of(request)
    if not c:
        return deny("Sign in required")
    if c["role"] != "owner":
        return deny("Owner only", 403)
    products = await db.get_products(c["shop"])
    target = next((p for p in products if p["id"] == req.product_id), None)
    if not target:
        return deny("No such item", 404)
    components = [{"id": p["id"], "name": p["name"], "unit": p["unit"],
                   "category": p.get("category", "")}
                  for p in products
                  if p.get("category") in COMPONENT_CATEGORIES and p["id"] != req.product_id]
    out = await recipes.propose(target["name"], components, req.hint)
    if not out.get("ok"):
        return JSONResponse(out, status_code=502)
    known = {p["id"]: p for p in products}
    drafted = []
    for comp in out["components"]:
        p = known.get(comp["component_id"])
        if p:
            drafted.append({"component_id": p["id"], "name": p["name"], "unit": p["unit"],
                            "qty": comp["qty"], "why": comp.get("why", "")})
    return {"ok": True, "product_id": target["id"], "name": target["name"],
            "components": drafted, "note": out.get("note", ""),
            "cost_paise": out.get("cost_paise", 0)}


@router.post("/ai/check")
async def ai_check(request: Request):
    """One real call, to tell a key that is present from a key that works.

    Owner only and behind a tap: it costs a fraction of a paisa, but an open endpoint that
    spends somebody else's credits is an open endpoint that will be found.
    """
    c = claims_of(request)
    if not c:
        return deny("Sign in required")
    if c["role"] != "owner":
        return deny("Owner only", 403)
    return await recipes.check()


@router.get("/recipes")
async def recipes_list(request: Request):
    """Everything sellable, with what it is made of — the recipe screen's whole payload.

    Menu items first and unrecipe'd ones before the rest, because the screen exists to
    close gaps and the gaps should not need looking for.
    """
    c = claims_of(request)
    if not c:
        return deny("Sign in required")
    products = await db.get_products(c["shop"])
    by_id = {p["id"]: p for p in products}
    sellable, components = [], []
    for p in products:
        cat = p.get("category") or "resale"
        if cat in COMPONENT_CATEGORIES:
            components.append({"id": p["id"], "name": p["name"], "unit": p["unit"],
                               "category": cat, "stock": float(p.get("stock") or 0)})
            continue
        parts = []
        for comp in (p.get("recipe") or []):
            src = by_id.get(comp["component_id"])
            if src:
                parts.append({"component_id": src["id"], "name": src["name"],
                              "unit": src["unit"], "qty": comp["qty"],
                              # What one serving costs the shop in materials. The first
                              # honest answer this app can give to "what is my margin".
                              "cost": round(comp["qty"] * float(src["unit_price"] or 0), 4)})
        sellable.append({"id": p["id"], "name": p["name"], "unit": p["unit"],
                         "unit_price": float(p["unit_price"] or 0), "category": cat,
                         "components": parts,
                         "cost": round(sum(x["cost"] for x in parts), 2)})
    sellable.sort(key=lambda s: (bool(s["components"]), s["name"].lower()))
    return {"ok": True, "items": sellable, "components": components,
            "categories": list(CATEGORIES)}


@router.get("/stock")
async def stock_report(request: Request):
    """What should be on the shelf, against what was counted.

    The number the shopkeeper actually cares about is the gap. Loss is felt more sharply
    than foregone gain, which is why this is the screen worth building before any margin
    dashboard.
    """
    c = claims_of(request)
    if not c:
        return deny("Sign in required")
    products = await db.get_products(c["shop"])
    ledger = await db.stock_ledger(c["shop"])
    agg: dict[str, dict] = {}
    for m in ledger:
        a = agg.setdefault(m["product_id"], {"sold": 0.0, "inward": 0.0,
                                             "counted": 0.0, "wastage": 0.0})
        delta = float(m.get("delta") or 0)
        if m["reason"] == "sale":
            a["sold"] += -delta
        elif m["reason"] == "inward":
            a["inward"] += delta
        elif m["reason"] == "wastage":
            a["wastage"] += -delta
        else:
            a["counted"] += delta          # the correction a count applied
    rows = []
    for p in products:
        a = agg.get(p["id"], {"sold": 0.0, "inward": 0.0, "counted": 0.0, "wastage": 0.0})
        cat = p.get("category") or "resale"
        # A count correction IS the shrinkage: it is the amount the shelf disagreed with
        # the ledger by. Negative means goods left without being billed.
        rows.append({"id": p["id"], "name": p["name"], "unit": p["unit"],
                     "category": cat,
                     "unit_price": float(p["unit_price"] or 0),
                     "stock": float(p.get("stock") or 0),
                     "sold": round(a["sold"], 3), "inward": round(a["inward"], 3),
                     "wastage": round(a["wastage"], 3),
                     "unaccounted": round(a["counted"], 3),
                     "value_lost": round(-a["counted"] * float(p["unit_price"] or 0), 2)})
    # How long each shelf lasts at the rate this shop actually sells, and the point at
    # which ordering has to start. Demand is measured from the ledger's own span — not the
    # 90-day fetch window, which would divide a new shop's week of trade by ninety and tell
    # it the beans will last a year.
    days_seen = reorder.observed_days(ledger)
    reorder.annotate(rows, days_seen)
    rows.sort(key=lambda r: r["value_lost"], reverse=True)

    # Grouped because the four kinds of thing on a café's shelf answer different questions.
    # Beans running short is a supply problem; cups running short is a purchasing one; a
    # menu item has no shelf at all and only appears here to be told so.
    groups = []
    for cat in CATEGORIES:
        members = [r for r in rows if r["category"] == cat]
        if not members:
            continue
        groups.append({
            "category": cat, "items": members, "count": len(members),
            "value_lost": round(sum(r["value_lost"] for r in members
                                    if r["value_lost"] > 0), 2),
            # What the shelf is worth at selling price. Not a loss figure — it is the
            # answer to "how much of my money is sitting in the back room", which is the
            # other thing a shopkeeper has never been able to see.
            "on_hand": round(sum(r["stock"] * r["unit_price"] for r in members), 2),
        })
    return {"ok": True, "items": rows, "groups": groups,
            "total_lost": round(sum(r["value_lost"] for r in rows if r["value_lost"] > 0), 2),
            "reorder": reorder.summarise(rows),
            # Stated so the screen can say which numbers were measured and which assumed.
            # A forecast presented without its basis gets trusted further than it has earned.
            "basis": {"days_observed": round(days_seen, 1),
                      "min_days": reorder.MIN_DAYS_OBSERVED,
                      "lead_days": reorder.LEAD_DAYS}}


# ---------------------------------------------------------------------------
# Document import
# ---------------------------------------------------------------------------
# A shop bills on day one with an empty catalog (D4), and it stays half-empty for weeks
# because typing forty products on a phone is nobody's evening. But almost every shop
# already owns the list on paper — a rate card, a menu board, the supplier's invoice. This
# reads it.
#
# The model transcribes; this code decides. Matching an extracted line to a SKU the shop
# already sells uses the same phonetic matcher as the voice path, so an import and a spoken
# correction agree with each other, and the model is never in a position to overwrite a
# price by concluding two names are the same thing.

IMPORT_MIMES = {"image/jpeg": "image/jpeg", "image/jpg": "image/jpeg",
                "image/png": "image/png", "image/webp": "image/webp",
                "image/heic": "image/jpeg", "image/heif": "image/jpeg",
                "application/pdf": "application/pdf"}

# Above this, the extracted name and a catalog name are the same product. Deliberately
# higher than the billing gate: billing has the shopkeeper's ear a second later, an import
# proposes a silent price change to a row they may skim past.
IMPORT_MATCH = 0.90


@router.post("/import")
async def import_document(request: Request,
                          files: list[UploadFile] = File(...),
                          kind: str = Form("catalog")):
    """Photographs or a PDF in, proposed rows out. Writes nothing."""
    c = claims_of(request)
    if not c:
        return deny("Sign in required")
    # Prices and stock both belong to the owner. A worker importing a rate card would be
    # repricing the shop from a photograph.
    if c["role"] != "owner":
        return deny("Owner only", 403)
    if kind not in ("catalog", "inward"):
        return deny("Unknown kind", 400)

    blobs: list[tuple[bytes, str]] = []
    for f in files:
        mime = IMPORT_MIMES.get((f.content_type or "").lower())
        if not mime:
            return JSONResponse({"ok": False,
                                 "error": f"{f.filename or 'file'}: photos or PDF only"},
                                status_code=400)
        raw = await f.read()
        if len(raw) > vision.MAX_BYTES:
            return JSONResponse({"ok": False,
                                 "error": f"{f.filename or 'file'} is too large"},
                                status_code=400)
        blobs.append((raw, mime))

    t0 = time.perf_counter()
    out = await vision.read(blobs, kind)
    if not out.get("ok"):
        return JSONResponse(out, status_code=502)

    shop = await db.get_shop(c["shop"]) or {}
    lang = lang_for(shop.get("lang") or "en")
    products = await db.get_products(c["shop"])
    cat = Catalog(products)

    rows = []
    for raw in out["data"].get("items", []):
        name = tidy_name(raw.get("name") or "")
        if not name:
            continue
        near = cat.candidates(name, lang, n=3)
        top = near[0] if near else None
        matched = top if top and top["score"] >= IMPORT_MATCH else None
        row = {"name": name, "verbatim": raw.get("verbatim", ""),
               "unit": lang.canonical_unit(raw.get("unit") or "") or "piece",
               "match": matched, "candidates": near}
        if kind == "catalog":
            row["price"] = round(float(raw.get("price") or 0), 2)
            # Shown so a review screen can lead with what actually changes. A rate card
            # is mostly prices the shop already has; the two that moved are the point.
            row["was"] = float(matched["unit_price"] or 0) if matched else None
        else:
            row["qty"] = round(float(raw.get("qty") or 0), 3)
            row["rate"] = round(float(raw.get("rate") or 0), 2)
        rows.append(row)

    return {"ok": True, "kind": kind, "items": rows,
            "skipped": int(out["data"].get("skipped") or 0),
            "supplier": out["data"].get("supplier", ""),
            "invoice_no": out["data"].get("invoice_no", ""),
            "invoice_date": out["data"].get("invoice_date", ""),
            "truncated": out.get("truncated", False),
            "cost_paise": out.get("cost_paise", 0),
            "took_ms": int((time.perf_counter() - t0) * 1000)}


class ApplyRequest(BaseModel):
    kind: str = "catalog"
    items: list[dict] = []


@router.post("/import/apply")
async def import_apply(req: ApplyRequest, request: Request):
    """Write the rows the shopkeeper kept. Only rows they sent arrive here — the review
    screen is where an unwanted row is dropped, and there is no 'apply all' shortcut that
    skips it."""
    c = claims_of(request)
    if not c:
        return deny("Sign in required")
    if c["role"] != "owner":
        return deny("Owner only", 403)

    shop = await db.get_shop(c["shop"]) or {}
    lang = lang_for(shop.get("lang") or "en")

    if req.kind == "inward":
        known = {p["id"] for p in await db.get_products(c["shop"])}
        moves = [{"product_id": i["product_id"], "delta": abs(float(i.get("qty") or 0))}
                 for i in req.items if i.get("product_id") in known]
        error = await db.move_stock(c["shop"], moves, "inward")
        return JSONResponse({"ok": not error, "error": error, "applied": len(moves)},
                            status_code=200 if not error else 502)

    written, failed = 0, []
    for i in req.items:
        payload = {"name": tidy_name(i.get("name") or ""),
                   "unit": lang.canonical_unit(i.get("unit") or "") or "piece",
                   "unit_price": round(float(i.get("price") or 0), 2)}
        if not payload["name"]:
            continue
        # An id means "this is the product already on the shelf" — the row keeps its
        # aliases, its stock and its description, and only the fields sent here move.
        if i.get("id"):
            existing = next((p for p in await db.get_products(c["shop"])
                             if p["id"] == i["id"]), None)
            if existing:
                payload = {**existing, **payload, "id": i["id"]}
        _, error = await db.upsert_product(c["shop"], payload)
        if error:
            failed.append({"name": payload["name"], "error": error})
        else:
            written += 1
    db.invalidate(c["shop"])
    return {"ok": not failed, "written": written, "failed": failed}


class SettleRequest(BaseModel):
    bill_id: str
    shop_id: str = DEFAULT_SHOP
    method: str = "cash"


@router.post("/settle")
async def settle(req: SettleRequest, request: Request):
    """Close a bill as paid.

    Only 'cash' can be claimed from the counter, and only by someone signed in to the
    shop: it is the shopkeeper stating a fact they witnessed. A UPI settlement has to
    come from the payment provider server-side — we never infer it from the handset, so
    the endpoint refuses to record one on the client's say-so.
    """
    c = claims_of(request)
    if not c:
        return deny("Sign in required")
    if req.method != "cash":
        return deny("Only a cash payment can be closed here", 400)
    error = await db.update_bill(c["shop"], req.bill_id, {
        "payment_state": "confirmed",
        "payment_method": "cash",
        "paid_at": datetime.now(timezone.utc).isoformat(),
    })
    return JSONResponse({"ok": not error, "error": error},
                        status_code=200 if not error else 502)


class ReceiptRequest(BaseModel):
    bill_id: str
    shop_id: str = DEFAULT_SHOP
    mobile: str = ""


@router.post("/receipt")
async def receipt(req: ReceiptRequest):
    """Close the sale, optionally capturing the customer's number for a receipt.

    No messaging provider is configured, so a captured number is stored and reported as
    `requested`, never `sent`. Saying "sent" when nothing left the building is the same
    class of lie as a catalog write that reports success on a rejected row — and this one
    would be told to a customer standing at the counter.
    """
    mobile = db.shop_key(req.mobile) if req.mobile else ""
    wants = bool(mobile) and len(mobile) == 10
    status = "requested" if wants else "none"
    error = await db.update_bill(req.shop_id, req.bill_id, {
        "payment_state": "confirmed",
        "customer_mobile": mobile if wants else "",
        "receipt_status": status,
    })
    return {"ok": not error, "error": error, "bill_id": req.bill_id,
            "receipt_status": status if not error else "none",
            "delivered": False, "mobile": mobile if wants else ""}


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
