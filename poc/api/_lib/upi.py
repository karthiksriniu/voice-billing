"""Dynamic UPI QR. Same URI construction as experiments/upi-qr/generate.py — that
experiment is the thing that validates this code, so keep the two in step.

Note the deliberate omissions: no `mc` (merchant category on a personal VPA is untested,
see the QR experiment) and a short ASCII `tn`. Both keep the symbol at a low version, which
keeps modules large and scannable off a cheap screen in bad light.
"""

from __future__ import annotations

import base64
import io
from urllib.parse import quote

import segno

VPA_SAFE = "@.-_"          # real-world UPI QRs leave these unencoded in `pa`


def build_uri(vpa: str, payee: str, amount: float, ref: str = "", note: str = "") -> str:
    params = [
        ("pa", quote(vpa, safe=VPA_SAFE)),
        ("pn", quote(payee, safe="")),
        ("am", f"{amount:.2f}"),
        ("cu", "INR"),
    ]
    if note:
        params.append(("tn", quote(note[:24], safe="")))
    if ref:
        params.append(("tr", quote(ref[:24], safe="")))
    return "upi://pay?" + "&".join(f"{k}={v}" for k, v in params)


def qr_data_uri(uri: str, scale: int = 8) -> str:
    """PNG as a data: URI so the QR renders with no extra request — the customer is
    standing at the counter waiting, and a second round trip is a second of silence."""
    buf = io.BytesIO()
    segno.make(uri, error="m").save(buf, kind="png", scale=scale, border=3)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
