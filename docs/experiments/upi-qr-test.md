# UPI dynamic QR test — protocol and results

**Risk being retired:** `RISKS.md` #1. The entire payment story assumes a `upi://pay` deep
link with a pre-filled amount renders as a QR that any UPI app honours, against the
shopkeeper's **personal (P2P) VPA**. NPCI has progressively tightened P2P-with-amount flows
for fraud reasons and individual apps differ. If this doesn't hold, we need merchant
onboarding — which collides with the self-serve constraint and reshapes `DECISIONS.md` D5.

**Cost:** ₹0 and about 45 minutes. **Do this before the field trip.**

---

## Running it

```bash
cd "experiments/upi-qr" && .venv/bin/python generate.py --vpa YOUR_VPA --name "Test Shop"
```

Writes 8 PNGs and `out/scan-sheet.html`. Open the sheet on the laptop at **maximum screen
brightness** and scan from a phone. Don't commit `out/` once it carries a real VPA.

**Scan to the confirmation screen only. Do not complete any payment.** The one exception is
the optional `tr` test at the bottom, which needs a real ₹1 transfer and is your call.

## The eight variants

| | What it isolates |
|---|---|
| **v1** control, no amount | Baseline. Proves the VPA and QR are fine. If v1 fails, nothing else is interpretable. |
| **v2** ₹5.00 | Smallest realistic bill. |
| **v3** ₹247.50 | Non-zero paise — the common kirana case. |
| **v4** ₹4,999.00 | Larger amount; some apps add friction above thresholds. |
| **v5** amount + note + ref | What the app would really generate. `tr` is the handle any future reconciliation depends on. |
| **v6** + `mc=5411` | Grocery merchant category on a personal VPA. May be ignored, may be rejected as an invalid merchant. |
| **v7** note in Tamil | UTF-8 percent-encodes to 3× the bytes — tests encoding and the note length limit. |
| **v8** `am=247.5` | Spec says two decimals. Tests whether a sloppy amount is coerced, rejected, or misread. |

**Only v2–v5 are gating.** A v6/v7/v8 failure is a configuration finding (don't send `mc`,
keep notes ASCII, always format to two decimals), not a product problem.

## Recording

One code per cell:

| Code | Meaning |
|---|---|
| `✓` | Amount pre-filled and **not** editable |
| `E` | Amount pre-filled but the payer can edit it |
| `W` | Amount carried, but a warning or extra confirmation is shown — write the wording down |
| `✗` | QR rejected, doesn't open, or amount not carried at all |

### Results

| Variant | GPay | PhonePe | Paytm | BHIM |
|---|---|---|---|---|
| v1 control, no amount | | | | |
| v2 ₹5.00 | | | | |
| v3 ₹247.50 | | | | |
| v4 ₹4,999.00 | | | | |
| v5 full (amount+note+ref) | | | | |
| v6 + `mc=5411` | | | | |
| v7 Tamil note | | | | |
| v8 `am=247.5` | | | | |

### What v5 displays to the payer

| | GPay | PhonePe | Paytm | BHIM |
|---|---|---|---|---|
| Payee name shown as "Test Shop"? | | | | |
| Note (`tn`) visible? | | | | |
| Ref (`tr`) visible anywhere? | | | | |
| Scan attempts needed (1 = first try) | | | | |

### Warning wording and anything odd

> _(free text — exact warning text matters; "may be a fraud" reads very differently to a
> customer than "confirm the amount")_

---

## Gate

Feeds the Phase 1 gate line "UPI QR: amount pre-filled and honoured — 4 of 4 apps".

- **Pass —** v2–v5 are `✓` or `E` in all four apps. D5 stands. `E` everywhere is a partial
  win: it still removes the shopkeeper reading the total aloud, but not the customer typing
  it wrong, so the spoken confirmation in Phase 3 matters more than we assumed.
- **Redesign D5 —** any `✗` or `W` on v2–v5 in **more than one** app. The personal-VPA path
  isn't dependable and we need a merchant VPA, which means PSP onboarding and a direct
  conflict with install-to-first-bill-in-5-minutes. Stop before any Android work.
- **Single-app failure —** note it and continue, but the app matters: GPay and PhonePe are
  most of the volume, so a failure there is closer to a redesign than a footnote.

## Already known, before any scanning

The Tamil note (v7) pushes the symbol from **version 4 to version 8** — from 33×33 to 49×49
modules. At a fixed on-screen size that makes each module ~40% smaller and the QR
meaningfully harder for a cheap camera to read in bad light. **Keep `tn` short and ASCII in
production, and keep `tr` short too** — `VAAKKU20260730A041` alone pushes v4 → v6. The
production QR should aim to stay at version 4 or below.

## Optional: does `tr` survive to the statement? (needs real money)

Only worth doing if v5 passes. Scan v5, edit the amount down to ₹1, and pay to a second
account you own. Then check whether `VAAKKU...` appears in the payee bank statement,
UPI app history, or SMS. If it does, a future reconciliation path exists without a PSP and
D5's reversal condition (b) gets easier. Your money, your call — skip it without cost to the
main result.
