"""Vaakku PoC API.

One FastAPI app behind /api/*. Runs locally with uvicorn and on Vercel's Python runtime
unchanged. Everything here is disposable except the parser and the language pack.
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from urllib.parse import parse_qs, urlencode

from fastapi import APIRouter, FastAPI, File, Form, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

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

app = FastAPI(title="Vaakku PoC")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

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


@router.post("/finalize")
async def finalize(req: FinalizeRequest):
    total = round(sum(float(i["amount"]) for i in req.items), 2)
    # Short ref and no note, on purpose: every character grows the QR symbol version, and a
    # denser symbol is measurably harder for a cheap camera to read in bad light. A `tn` of
    # "Bill" tells the customer nothing and costs ~10 characters.
    ref = f"VK{uuid.uuid4().hex[:8].upper()}"
    uri = build_uri(req.vpa, req.payee, total, ref=ref)

    # The number is allotted here, at the moment the bill is issued, and the document is
    # stored as issued rather than rebuilt on demand — prices move and shops get renamed,
    # and a reprint next year has to say what it said on the day.
    shop = await db.get_shop(req.shop_id) or {}
    now = datetime.now(receipts.IST)
    fy = receipts.financial_year(now)
    n = await db.next_receipt_no(req.shop_id, fy)
    number = receipts.serial(fy, n) if n else ""
    bill = {"total": total, "items": req.items, "upi_ref": ref,
            "payment_state": "pending", "customer_mobile": req.customer_mobile}
    doc = receipts.build(shop, bill, number, now)
    bill_id = await db.save_bill(req.shop_id, {**bill, "receipt_no": number,
                                               "receipt": doc})
    return {
        "bill_id": bill_id, "total": total, "ref": ref,
        "upi_uri": uri, "qr": qr_data_uri(uri),
        "receipt_no": number, "receipt": doc,
        "receipt_text": receipts.as_text(doc),
        "receipt_message": receipts.as_whatsapp(doc),
        # Stated plainly because the demo must not imply we detect payment (D5).
        "confirmation": "manual",
    }


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
async def bills_list(request: Request, limit: int = 40):
    """The shop's own recent bills. Exists to answer two questions at a counter: did that
    one get paid, and can you send me that receipt again."""
    c = claims_of(request)
    if not c:
        return deny("Sign in required")
    rows = await db.recent_bills(c["shop"], max(1, min(limit, 100)))
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
    return {"ok": True, "bills": out}


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
