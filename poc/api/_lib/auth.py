"""Passcode hashing and session tokens.

A 6-digit passcode is a convenience gate, not security: a million combinations falls to a
script in minutes. It is the right trade for a shop counter — a shopkeeper will not type a
passphrase between customers — but it means the storage has to be sound even though the
secret is weak. So: PBKDF2 per-user salted hashes, never the digits themselves, and signed
tokens rather than trusting a client-supplied shop_id.

What this deliberately does NOT provide, and should be added before real shops rely on it:
rate limiting / lockout after repeated failures, and any passcode reset path.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

ITERATIONS = 120_000
TOKEN_TTL_S = 60 * 60 * 12          # a trading day


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def hash_passcode(passcode: str, salt: str = "") -> str:
    """Returns 'pbkdf2$<iterations>$<salt>$<hash>'."""
    salt = salt or _b64e(secrets.token_bytes(16))
    dk = hashlib.pbkdf2_hmac("sha256", passcode.encode(), salt.encode(), ITERATIONS)
    return f"pbkdf2${ITERATIONS}${salt}${_b64e(dk)}"


def verify_passcode(passcode: str, stored: str) -> bool:
    try:
        scheme, iters, salt, expected = (stored or "").split("$")
        if scheme != "pbkdf2":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", passcode.encode(), salt.encode(), int(iters))
        return hmac.compare_digest(_b64e(dk), expected)
    except (ValueError, AttributeError):
        return False


def _secret() -> bytes:
    """Must be stable across serverless instances, or tokens issued by one break on the
    next. Derived from an existing server-side secret when none is set explicitly."""
    raw = (os.environ.get("SESSION_SECRET")
           or os.environ.get("SUPABASE_SERVICE_KEY")
           or "bolo-bill-insecure-fallback")
    return hashlib.sha256(raw.encode()).digest()


def issue_token(shop_id: str, mobile: str, role: str) -> str:
    body = {"shop": shop_id, "mobile": mobile, "role": role,
            "exp": int(time.time()) + TOKEN_TTL_S}
    payload = _b64e(json.dumps(body, separators=(",", ":")).encode())
    sig = _b64e(hmac.new(_secret(), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{sig}"


def read_token(token: str) -> dict | None:
    """Returns the claims, or None if the token is forged, malformed or expired."""
    try:
        payload, sig = (token or "").split(".")
    except ValueError:
        return None
    expected = _b64e(hmac.new(_secret(), payload.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        claims = json.loads(_b64d(payload))
    except (ValueError, json.JSONDecodeError):
        return None
    return None if claims.get("exp", 0) < time.time() else claims


def normalise_passcode(p: str) -> str:
    return "".join(ch for ch in (p or "") if ch.isdigit())
