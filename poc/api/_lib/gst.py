"""GSTIN validation.

A GST number printed on every receipt is worth checking once, at the point it is typed.
The format is fixed and the last character is a checksum, so a transposed digit is
detectable — which is the difference between catching it here and discovering it on a
customer's bill months later.

  33 AAAAA0000A 1 Z 5
  |  |          | | |
  |  |          | | checksum
  |  |          | fixed 'Z' for ordinary registrations
  |  |          entity number for the same PAN in the state
  |  the holder's PAN
  state code (01-38, plus 97 for offshore, 99 for centre)
"""

from __future__ import annotations

import re

CHARSET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
SHAPE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z]$")
VALID_STATE = set(f"{n:02d}" for n in range(1, 39)) | {"97", "99"}


def checksum(first14: str) -> str:
    """The 15th character, by the mod-36 rule the GSTN publishes."""
    total = 0
    for i, ch in enumerate(first14):
        value = CHARSET.index(ch)
        product = value * (1 if i % 2 == 0 else 2)
        total += product // 36 + product % 36
    return CHARSET[(36 - total % 36) % 36]


def check(gstin: str) -> tuple[str, str]:
    """Returns (normalised, error). An empty string is valid — GST is optional here."""
    g = (gstin or "").strip().upper().replace(" ", "")
    if not g:
        return "", ""
    if len(g) != 15:
        return g, "A GST number is 15 characters"
    if not SHAPE.match(g):
        return g, "That is not the shape of a GST number"
    if g[:2] not in VALID_STATE:
        return g, f"{g[:2]} is not a state code"
    if g[14] != checksum(g[:14]):
        return g, "The check digit does not match — one character is wrong"
    return g, ""


STATE_NAMES = {
    "01": "Jammu & Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh",
    "05": "Uttarakhand", "06": "Haryana", "07": "Delhi", "08": "Rajasthan",
    "09": "Uttar Pradesh", "10": "Bihar", "11": "Sikkim", "12": "Arunachal Pradesh",
    "13": "Nagaland", "14": "Manipur", "15": "Mizoram", "16": "Tripura", "17": "Meghalaya",
    "18": "Assam", "19": "West Bengal", "20": "Jharkhand", "21": "Odisha",
    "22": "Chhattisgarh", "23": "Madhya Pradesh", "24": "Gujarat", "26": "Dadra & Nagar Haveli and Daman & Diu",
    "27": "Maharashtra", "29": "Karnataka", "30": "Goa", "31": "Lakshadweep",
    "32": "Kerala", "33": "Tamil Nadu", "34": "Puducherry", "35": "Andaman & Nicobar",
    "36": "Telangana", "37": "Andhra Pradesh", "38": "Ladakh", "97": "Other territory",
    "99": "Centre",
}


def state_of(gstin: str) -> str:
    return STATE_NAMES.get((gstin or "")[:2], "")
