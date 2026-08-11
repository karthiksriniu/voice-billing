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
# A trading day was too short for the device it runs on. A shop opens before six and
# closes after ten, so a session started before opening expired mid-evening — in the
# middle of billing, which is the worst possible moment to ask for a passcode.
TOKEN_TTL_S = 60 * 60 * 12                  # one day, when not remembered
TOKEN_TTL_REMEMBERED_S = 60 * 60 * 24 * 7   # a week, on the shop's own counter phone


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


def issue_token(shop_id: str, mobile: str, role: str, remember: bool = False) -> str:
    """`remember` is the shopkeeper saying this phone is the shop's, not a borrowed one.
    It buys a week instead of a day. Signing out still ends it immediately, which is what
    makes the longer life acceptable on a device that sits on a counter."""
    ttl = TOKEN_TTL_REMEMBERED_S if remember else TOKEN_TTL_S
    body = {"shop": shop_id, "mobile": mobile, "role": role,
            "exp": int(time.time()) + ttl}
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


# --- Order keys ------------------------------------------------------------
# The credential an automated caller — a phone agent, an ordering bot — uses to place
# orders on a shop's behalf. Deliberately not a session token: it belongs to a machine, it
# does not expire on a schedule a machine can notice, and it must be revocable on its own
# without signing the shopkeeper out of their counter.
#
# It authorises exactly one thing: creating a pending order. It cannot read the catalog,
# the bills or the settings, and it cannot accept an order — only the shopkeeper standing
# in the shop can turn one into a sale.

ORDER_KEY_PREFIX = "bolo_ord_"


def new_order_key() -> str:
    """32 bytes of entropy. Prefixed so a leaked one is recognisable in a log or a repo,
    which is what makes automated secret scanning able to catch it."""
    return ORDER_KEY_PREFIX + secrets.token_urlsafe(32)


def order_key_hash(key: str) -> str:
    """SHA-256, not PBKDF2.

    The slow hash exists to make a six-digit passcode survive a brute force. This secret is
    32 random bytes, where an attacker gains nothing from speed, and the endpoint has to
    look it up on every call — so the fast digest is both sufficient and correct here.
    """
    key = (key or "").strip()
    if not key:
        return ""
    return hashlib.sha256(key.encode()).hexdigest()


def looks_like_order_key(key: str) -> bool:
    return (key or "").strip().startswith(ORDER_KEY_PREFIX)
