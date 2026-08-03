#!/usr/bin/env python3
"""Generate the UPI dynamic-QR test matrix (Phase 1, Risk 1).

Question under test: does a `upi://pay` deep link with a pre-filled amount work
consistently across GPay, PhonePe, Paytm and BHIM when the payee is a *personal*
(P2P) VPA rather than an onboarded merchant?

Writes one PNG per variant plus a single HTML scan sheet to ./out/.

    python generate.py --vpa you@okhdfcbank --name "Test Shop"
"""

import argparse
import html
import pathlib
from urllib.parse import quote

import segno

# Real-world UPI QRs leave `@`, `.`, `-` and `_` unencoded in the `pa` field. Some apps
# parse the query string naively, so match the convention rather than the RFC.
VPA_SAFE = "@.-_"

# 5411 = grocery stores / supermarkets. Setting a merchant category on a personal VPA is
# one of the things we are testing, not something we know to be valid.
KIRANA_MCC = "5411"


def build_uri(vpa, name, *, am=None, tn=None, tr=None, mc=None):
    """Assemble a upi://pay deep link. Values are percent-encoded; ordering is fixed so
    the generated URIs are diffable across runs."""
    params = [("pa", quote(vpa, safe=VPA_SAFE)), ("pn", quote(name, safe=""))]
    if am is not None:
        params += [("am", am), ("cu", "INR")]
    if tn is not None:
        params.append(("tn", quote(tn, safe="")))
    if tr is not None:
        params.append(("tr", quote(tr, safe="")))
    if mc is not None:
        params.append(("mc", mc))
    return "upi://pay?" + "&".join(f"{k}={v}" for k, v in params)


def variants(vpa, name):
    """The eight scans. Each isolates one thing; the control exists so that a failure can
    be attributed to the amount rather than to the QR or the VPA."""
    note, ref = "Bill 41", "VAAKKU20260730A041"
    full = dict(tn=note, tr=ref)
    return [
        ("v1-control-no-amount", "Control: no amount",
         "Baseline. Proves the VPA and the QR itself are fine. If this fails, nothing "
         "below is interpretable.",
         build_uri(vpa, name)),
        ("v2-amount-5", "Amount ₹5.00",
         "Smallest realistic bill.",
         build_uri(vpa, name, am="5.00")),
        ("v3-amount-247-50", "Amount ₹247.50",
         "Non-zero paise. The common case for a kirana bill.",
         build_uri(vpa, name, am="247.50")),
        ("v4-amount-4999", "Amount ₹4,999.00",
         "Larger amount — some apps apply extra friction above thresholds.",
         build_uri(vpa, name, am="4999.00")),
        ("v5-full", "Full: amount + note + ref",
         "What the app would actually generate. `tr` is the handle any future "
         "reconciliation would depend on.",
         build_uri(vpa, name, am="247.50", **full)),
        ("v6-merchant-category", "Full + merchant category (mc=5411)",
         "Declares a grocery MCC on a personal VPA. May be ignored, may be rejected as "
         "an invalid merchant — that is the finding.",
         build_uri(vpa, name, am="247.50", mc=KIRANA_MCC, **full)),
        ("v7-tamil-note", "Full, note in Tamil",
         "UTF-8 in `tn` percent-encodes to 3× the bytes. Tests both encoding handling "
         "and the note length limit.",
         build_uri(vpa, name, am="247.50", tn="பில் 41", tr=ref)),
        ("v8-amount-one-decimal", "Amount written as 247.5",
         "Spec says two decimals. Tests whether a sloppy amount is coerced, rejected, or "
         "silently misread as something else.",
         build_uri(vpa, name, am="247.5")),
    ]


def self_check(vpa, name):
    """Guard the encoding rules a bug would silently break, since a malformed URI would
    look exactly like an app incompatibility during the scan."""
    uri = build_uri(vpa, name, am="247.50", tn="பில் 41", tr="X-1")
    assert "@" in uri.split("&")[0], "VPA `@` must stay unencoded"
    assert "%20" in build_uri(vpa, "Test Shop"), "spaces must encode as %20, not +"
    assert "%E0%AE" in uri, "Tamil note must percent-encode as UTF-8"
    assert "cu=INR" in uri, "currency must accompany an amount"


CSS = """
body{font:15px/1.5 -apple-system,sans-serif;margin:0;padding:32px;background:#fff;color:#111}
h1{font-size:20px;margin:0 0 4px}
.lede{color:#555;margin:0 0 28px;max-width:60ch}
.card{page-break-inside:avoid;border:1px solid #ddd;border-radius:10px;padding:20px;
      margin:0 0 20px;display:flex;gap:22px;align-items:flex-start;background:#fff}
.qr{flex:0 0 auto;background:#fff;padding:10px}
.qr img{display:block;width:300px;height:300px;image-rendering:pixelated}
.meta{min-width:0}
.tag{font:600 11px ui-monospace,monospace;color:#777;letter-spacing:.06em;text-transform:uppercase}
h2{font-size:17px;margin:6px 0 8px}
.why{color:#444;margin:0 0 12px;max-width:52ch}
code{display:block;font:12px/1.6 ui-monospace,monospace;background:#f6f6f6;padding:10px;
     border-radius:6px;word-break:break-all;color:#222}
@media print{body{padding:0}.card{border-color:#999}}
"""


def write_sheet(path, cards, vpa):
    body = "".join(
        f'<div class="card"><div class="qr"><img src="{png}" alt=""></div>'
        f'<div class="meta"><div class="tag">{html.escape(slug)}</div>'
        f'<h2>{html.escape(title)}</h2><p class="why">{html.escape(why)}</p>'
        f"<code>{html.escape(uri)}</code></div></div>"
        for slug, title, why, uri, png in cards
    )
    path.write_text(
        "<!doctype html><meta charset=utf-8><title>UPI dynamic QR test sheet</title>"
        f"<style>{CSS}</style>"
        "<h1>UPI dynamic QR — scan sheet</h1>"
        f'<p class="lede">Payee <b>{html.escape(vpa)}</b>. Scan each with GPay, PhonePe, '
        "Paytm and BHIM. Screen brightness to maximum. <b>Stop at the confirmation "
        "screen — do not complete any payment.</b> Record results in "
        "docs/experiments/upi-qr-test.md.</p>" + body,
        encoding="utf-8",
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vpa", default="test@okhdfcbank", help="payee VPA (personal/P2P)")
    ap.add_argument("--name", default="Test Shop", help="payee name shown to the customer")
    ap.add_argument("--out", default=pathlib.Path(__file__).parent / "out")
    args = ap.parse_args()

    self_check(args.vpa, args.name)
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    cards = []
    for slug, title, why, uri in variants(args.vpa, args.name):
        png = f"{slug}.png"
        # Error level M and a 4-module quiet zone match what UPI QRs use in the wild.
        qr = segno.make(uri, error="m")
        qr.save(out / png, scale=16, border=4)
        cards.append((slug, title, why, uri, png))
        # Symbol version is worth watching: a longer URI means more modules, which at a
        # fixed display size means smaller ones and a harder scan in bad light.
        print(f"{slug:26} v{qr.version:<3} {len(uri):>3}ch  {uri}")

    write_sheet(out / "scan-sheet.html", cards, args.vpa)
    print(f"\n{len(cards)} QRs + scan sheet written to {out}")


if __name__ == "__main__":
    main()
