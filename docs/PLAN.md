# Build plan — voice-first billing for small Indian retail

Working name: **Vaakku** (placeholder). Beachhead: Tier-1 kirana/vegetable shops in Chennai
that bill on paper today. Language pair 1: Tamil + English.

## Session inputs that shape everything below

| Input | Value | What it constrains |
|---|---|---|
| Distribution | **Play Store only** | Kills the notification-listener path for payment confirmation (D5). Kills SMS-reading too. |
| Field access | **Cooperating Chennai shops** | Corpus is field-recorded, not synthesised. Paper baseline is measurable, not assumed. |
| Team | **Solo, part-time, no deadline** | Effort-sized phases, not dates. No model training. Off-the-shelf or nothing. |
| Business model | **Undecided; optimise for adoption** | Phase 5 covers loops and retention only; pricing is deferred, not designed. |

## The one number: marginal cost ceiling

**₹10 per shop per month, all-in, of which cloud inference is ₹0.**

Derivation: a kirana shop's realistic software willingness-to-pay is ₹50–100/month. A 10%
COGS ceiling is the usual shape for that kind of product, and we may never charge at all,
so the ceiling has to survive an unfunded 10,000-shop base (₹12L/year — it does not, at
₹100/shop; it does at ₹10).

At 100 bills/day and ~8 utterances/bill, a shop generates **~24,000 utterances/month**.
₹10 buys ₹0.0004 per utterance. No commercial cloud ASR is within two orders of magnitude
of that. Even a 5%-escalation cloud fallback is ~1,200 utterances/month, which at Sarvam or
Google STT rates lands at ₹40–150/shop/month — over budget on its own.

**Conclusion, stated up front because it removes options rather than adding them: cloud ASR
is out of the MVP entirely.** Not "on-device with cloud fallback" — on-device, full stop.
The ₹10 covers Play developer fee amortised, crash reporting, and nothing else. Bhashini's
free government tier is the only cloud path worth re-testing later (D1).

## Latency budget

Measured **VAD endpoint (speech end) → line item rendered on screen**, on a Redmi
A-series-class device (Helio G-series / Snapdragon 4xx, 3 GB RAM).

| Stage | p50 | p95 |
|---|---|---|
| VAD endpoint decision | 250 ms | 300 ms |
| ASR final decode (streaming; most work overlaps speech) | 300 ms | 650 ms |
| Parse + catalog match + N-best rerank | 30 ms | 80 ms |
| Render + audio confirmation start | 70 ms | 120 ms |
| **Total** | **≤ 700 ms** | **≤ 1150 ms** |

**Build-breaking threshold: p95 > 1500 ms.** CI fails; nothing ships until it is back under.

Bill-level target: a 5-line bill from tap-open to QR displayed in **≤ 25 s**, against the
paper baseline measured in Phase 1 (expect 35–45 s; if paper turns out to be faster than we
think, that is a Phase 1 finding that changes the product, not a number to explain away).

## Accuracy thresholds

Raw word error rate is the wrong metric. What matters is what the shopkeeper experiences,
so every utterance lands in exactly one of three buckets:

- **ACCEPT** — line added automatically, and it is correct.
- **CONFIRM** — confidence below the gate; app says "didn't catch that" or shows the item
  for a one-word/one-tap confirmation. Costs ~2 s. Honest and survivable.
- **SILENT ERROR** — line added automatically, and it is wrong. This is the one that ends
  the relationship. Principle 2 in the brief is right and it is the metric we optimise.

| Metric | Phase 1 gate | Phase 2 ship |
|---|---|---|
| Silent error rate | ≤ 1.0% | ≤ 0.5% |
| Auto-accept rate | ≥ 80% | ≥ 90% |
| Parse error given a correct transcript | ≤ 1.5% | ≤ 1.0% |
| Corrections per bill | — | ≤ 0.3 |

A line is "correct" only if item, quantity, unit, **and** unit price all match. Getting the
item right and the quantity wrong is a wrong bill.

## Phases

Effort is in solo part-time weeks (~10 h/week). Each phase has a gate; a failed gate stops
the next phase rather than being carried forward as debt.

### Phase 0 — Planning (this session)
**Done when:** `PLAN.md`, `DECISIONS.md`, `RISKS.md`, `experiments/phase-1-spike.md` agreed.
No repo scaffolding, no dependency choices, no UI framework.

### Phase 1 — Headless spike + evaluation harness (~6–8 weeks)
Audio file in → line items → total → UPI QR PNG out. Python, laptop, no Android.

Contains four things, in this order:
1. **Field trip** (day 1, before any code): 3–5 Chennai shops. Record the 200-utterance
   corpus, time the paper baseline, and observe how shopkeepers verify UPI payment today.
   That last observation settles D5's remaining doubt for free.
2. **Eval harness first.** Corpus + gold labels + a scorer that reports the four metrics
   above. Written before pipeline code, per the brief.
3. **ASR bakeoff** behind one adapter interface. Vosk (constrained grammar) vs
   whisper.cpp tiny/small-q5 vs Android SpeechRecognizer (device-side, acoustic re-record
   — see D1). Decision by data, not by preference.
4. **Parser + catalog matcher + UPI QR generator.**

**Gate:** the winning backend clears silent error ≤ 1.0%, auto-accept ≥ 80%, parse error
≤ 1.5%, decode real-time factor ≤ 0.3 on one 1.8 GHz core. QR scans successfully in GPay,
PhonePe, Paytm and BHIM with the amount pre-filled and non-editable-by-accident.
**If no backend clears 2× these numbers, stop and reconsider the product**, because the
on-device-only constraint is not negotiable and neither is trust.

### Phase 2 — Android app, offline, screen-only, no backend (~10–14 weeks)
Single shop, single counter. Session-scoped VAD capture (D2). Seeded catalog + learn-as-you-go
(D4). Dynamic UPI QR, manual payment confirmation (D5). Local SQLite, Android Auto Backup,
no server (D8). APK target < 30 MB including ASR model.

**Done when:**
- p95 speech-end → item ≤ 1150 ms on target hardware; p99 ≤ 1500 ms.
- 5 people who have never seen the app install it unassisted and produce a correct first
  bill in **under 5 minutes**, measured by screen recording with nobody in the room.
- 3 pilot shops bill a full trading day on it with silent error ≤ 0.5% (audited against a
  parallel paper record kept by the shopkeeper for that day only).
- Median 5-line bill ≤ 25 s and demonstrably faster than the shop's own paper baseline.

### Phase 3 — Spoken confirmation, printing, payment confirmation (~8–10 weeks)
Pre-recorded numeral/phrase audio for totals (D6). Bluetooth ESC/POS printing with
auto-reconnect that survives a phone restart (D7). Payment confirmation UX (D5) — still
manual, but designed so the shopkeeper's existing verification habit is one tap.

**Done when:** total announced within 400 ms of bill close and intelligible at 75 dB(A)
background; printer reconnects without user action in ≥ 95% of restarts across 10 trials on
3 printer models; bill footer attribution live (this is when growth loops actually start —
see Phase 5).

### Phase 4 — Inventory (~10–12 weeks)
Sales decrement stock; spoken goods-inward increments it; low-stock flags; drafted reorder
message. Same voice pipeline, second grammar mode ("inward: 20 kilo sugar").

**Done when:** stock ledger reconciles to a manual count in a pilot shop within 2% over a
2-week window, and a reorder draft is sent by a shopkeeper unprompted.

### Phase 5 — Growth and monetisation (~6 weeks + ongoing)
Printed/WhatsApp footer attribution, referral, market-cluster seeding. Pricing deferred per
the session input; this phase instruments the loops rather than closing them.

**Done when:** D30 bills/shop/day ≥ 60% of D7, and ≥ 15% of new installs are attributable to
an existing shop.

## Metrics tracked from Phase 2 onward

Seconds per bill vs that shop's paper baseline · line-item error rate · silent error rate ·
corrections per bill · setup completion rate (install → first bill) · bills/shop/day at D7
and D30 · auto-accept rate. All computed on-device and shown to the shopkeeper as their own
numbers; aggregate telemetry only if the user opts in (D9).

## Sequencing notes

- **Attribution arrives in Phase 3, not Phase 2.** The printed bill footer is the main viral
  surface and there is no printer until Phase 3. Phase 2 pilots will spread by word of mouth
  or not at all; do not read Phase 2 growth numbers as a signal.
- **The paper baseline must be measured on the Phase 1 field trip.** Every "faster than
  paper" claim downstream depends on a number we do not yet have.
- **The ASR adapter interface is the one architectural commitment made now** (D1), because it
  is what lets the backend choice be made by evidence in Phase 1 and revisited in Phase 4
  without a rewrite. Likewise the parser's language pack must be data, not code, so a second
  language is configuration (numerals, units, aliases, phonetic normalisation rules).
