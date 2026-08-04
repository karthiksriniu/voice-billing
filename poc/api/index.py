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

import auth                                            # noqa: E402
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


class LoginRequest(BaseModel):
    mobile: str
    passcode: str


class SettingsRequest(BaseModel):
    name: str = ""
    lang: str = ""
    vpa: str = ""


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


def result_payload(res, took_ms: int, mode: str = "billing") -> dict:
    payload = {
        "transcript": res.transcript,
        "items": [serialise(i) for i in res.items],
        "command": res.command,
        "mode_switch": res.mode_switch,
        "unparsed": res.unparsed,
        # Names the grammar understood but the catalog has never heard of. With shops now
        # starting empty this is the common case, and dropping it server-side is what made
        # billing look deaf: perfect transcript, no line, no reason given.
        "unmatched": res.unmatched,
        "customer_mobile": res.customer_mobile,
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
    product, error = await db.upsert_product(shop_id, req.model_dump())
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
    return {"ok": True, "token": auth.issue_token(shop_id, shop_id, "owner"),
            "shop_id": shop_id, "role": "owner", "shop_name": req.name,
            "vpa": req.vpa, "lang": norm_lang(req.lang)}


@router.post("/auth/login")
async def auth_login(req: LoginRequest):
    code = auth.normalise_passcode(req.passcode)
    mobile = db.shop_key(req.mobile)
    shop = await db.get_shop(mobile)
    if shop and shop.get("passcode_hash") and auth.verify_passcode(code, shop["passcode_hash"]):
        return {"ok": True, "token": auth.issue_token(mobile, mobile, "owner"),
                "shop_id": mobile, "role": "owner", "shop_name": shop.get("name", ""),
                "vpa": shop.get("upi_vpa", ""), "lang": norm_lang(shop.get("lang"))}
    staff = await db.get_staff(mobile)
    if staff and auth.verify_passcode(code, staff.get("passcode_hash", "")):
        shop = await db.get_shop(staff["shop_id"]) or {}
        return {"ok": True,
                "token": auth.issue_token(staff["shop_id"], mobile, staff.get("role", "user")),
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
    if not c or c["role"] != "owner":
        return deny("Owner only")
    shop = await db.get_shop(c["shop"]) or {}
    return {"ok": True, "mobile": c["shop"], "name": shop.get("name", ""),
            "lang": norm_lang(shop.get("lang")), "vpa": shop.get("upi_vpa", ""),
            "stored_lang": shop.get("lang", "")}


@router.post("/settings")
async def settings_set(req: SettingsRequest, request: Request):
    c = claims_of(request)
    if not c or c["role"] != "owner":
        return deny("Owner only")
    shop = await db.get_shop(c["shop"]) or {}
    name = req.name.strip() or shop.get("name", "")
    lang = norm_lang(req.lang or shop.get("lang"))
    vpa = req.vpa.strip() or shop.get("upi_vpa", "")
    error = await db.update_shop(c["shop"], name, vpa, lang)
    return JSONResponse({"ok": not error, "error": error,
                         "name": name, "lang": lang, "vpa": vpa},
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
    payload = result_payload(res, int((time.perf_counter() - t0) * 1000), req.mode)
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
    payload = result_payload(res, int((time.perf_counter() - t1) * 1000), mode)
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
    await db.save_bill(req.shop_id, {
        "id": req.bill_id, "total": 0, "items": [], "payment_state": "confirmed",
        "customer_mobile": mobile if wants else "", "receipt_status": status,
    })
    return {"ok": True, "bill_id": req.bill_id, "receipt_status": status,
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
