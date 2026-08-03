# Risks

Ranked by expected damage × likelihood, with the cheapest experiment that de-risks each.
"Cheapest" is deliberate: most of the top risks are answerable for under ₹20,000 and a
weekend, and none of them should be answered by building the product first.

---

## 1. UPI deep link with a pre-filled amount may not work reliably against a personal VPA
**Damage: product-ending. Likelihood: moderate and under-appreciated.**

The whole payment story assumes `upi://pay?pa=…&am=…&tn=…&tr=…` renders as a QR that any UPI
app honours with the amount locked in. NPCI has progressively tightened P2P-with-amount flows
for fraud reasons, and individual apps differ in whether they pre-fill, allow editing, or warn.
If a shopkeeper's *personal* VPA can't take a dynamic amount, we need merchant onboarding —
which collides head-on with the self-serve constraint and reshapes D5 entirely.

**Experiment (₹0, one afternoon, do this first — before the field trip):** generate the QR
by hand against a real personal VPA and scan it with GPay, PhonePe, Paytm and BHIM. Record
for each: does the amount pre-fill, is it editable, is there a warning, does `tn`/`tr` survive
to the payer's screen and the payee's statement. Try ₹5, ₹247.50 and ₹4,999.

---

## 2. Payment confirmation has no automatic path under Play-Store-only distribution
**Damage: high. Likelihood: certain — this is a constraint, not a hypothesis.**

Notification listener and SMS reading are both closed off by Play policy (D5). Manual
confirmation is what we ship. The residual risk is not technical but behavioural: will
shopkeepers accept a bill flow that doesn't know whether money arrived?

**Experiment (₹0, folded into the Phase 1 field trip):** sit in 5 shops for two hours each and
count how every UPI payment is verified today — sound-box, phone glance, customer's screen,
or not at all. If sound-boxes dominate, our audio confirmation must match that experience and
its priority rises. If shopkeepers already just trust the customer's screen, manual
confirmation is a non-issue and we can stop worrying about it.

**Contingency held open:** `PaymentConfirmer` stays an interface with exactly one shipped
implementation, so a sideloaded channel or a self-serve PSP can be dropped in later.

---

## 3. ASR accuracy on code-mixed Tamil in a loud shop, on-device, at ₹0
**Damage: product-ending. Likelihood: high.**

This is the technical core. On-device-only (D1) is forced by cost, and on-device Tamil ASR
with English code-mixing at 70–80 dB(A) background is genuinely hard. Whisper hallucinating on
noise is a specific named danger: it produces fluent, confident, wrong text, which is exactly
the silent-error failure mode we said is fatal.

**Experiment (~₹20,000 + 2–3 weekends):** the Phase 1 eval harness — 200 field-recorded
utterances with gold labels, scored across Vosk (grammar-constrained), whisper.cpp tiny and
small-q5, and Android `SpeechRecognizer`. Metric is silent error rate and auto-accept rate,
not WER. See `experiments/phase-1-spike.md`. Cost is corpus collection and labelling; the
bakeoff itself is free.

**Leading indicator that the risk is retiring:** grammar-constrained Vosk beating unconstrained
Vosk by a wide margin, which would confirm that closed-vocabulary decoding is the lever we
think it is.

---

## 4. Silent mis-recognition destroys trust faster than we can build it
**Damage: permanent and unrecoverable per shop. Likelihood: high without explicit design.**

A shopkeeper who bills a customer ₹450 instead of ₹45 once will never open the app again, and
will tell the street. Raw accuracy improvements don't fix this — the confidence gate does.

**Experiment (free, part of Phase 1):** rather than tuning for accuracy, tune the accept /
confirm / reject thresholds on the corpus and plot the trade-off curve — silent error rate
against auto-accept rate. Pick the operating point at silent error ≤ 0.5%, then check what
auto-accept rate that costs. If holding 0.5% drops auto-accept below ~70%, the app is slower
than paper and the ASR backend is not good enough, regardless of its headline accuracy.

**Design commitments already made:** amount always spoken aloud in pre-recorded audio before
the QR appears; "cancel last" and "remove sugar" as first-class voice commands, not menu
items; every line shows its own price so the shopkeeper can spot a bad one at a glance.

---

## 5. Setup abandonment — install to first bill in under 5 minutes, unassisted
**Damage: high (no field force means setup is the entire funnel). Likelihood: moderate.**

Every step is a cliff: Play install on a slow connection, mic permission, UPI VPA entry (the
one unavoidable typing step), language choice, first utterance. The target user has never
used an app beyond WhatsApp and a UPI app.

**Cheap proxy now (₹0, during the field trip):** a paper prototype walkthrough — show 5
shopkeepers the screens printed out and ask them what they'd tap. Catches the worst confusions
before any code.

**Real experiment (Phase 2 gate):** 5 unassisted installs, screen-recorded, nobody in the
room, timed to first correct bill. No coaching, no explaining. Fail the phase if fewer than
4 of 5 finish in 5 minutes.

**Known-hard step to design around:** entering the UPI VPA. Options worth testing — scan the
shop's existing static QR sticker with the camera and decode the VPA from it (best; no typing
at all), or read it from an installed UPI app's share sheet.

---

## 6. Latency budget missed on a 3 GB device
**Damage: high — principle 1 says it doesn't ship. Likelihood: moderate.**

p95 of 1150 ms speech-end to rendered item is tight for on-device ASR on a Helio G-series with
a shop's worth of thermal throttling.

**Experiment (~₹8,000):** buy one representative target phone now and measure decode real-time
factor for each candidate backend before committing (Phase 1 uses RTF ≤ 0.3 on a single
1.8 GHz core as the laptop-side proxy; this validates the proxy). One phone, not three, at
this stage.

---

## 7. VAD endpointing fails in shop noise
**Damage: moderate-to-high — cuts items mid-word or never closes. Likelihood: moderate.**

A ceiling fan and a TV keep energy-based VAD permanently open; a loud street clips speech onsets.

**Experiment (₹0, reuses the corpus):** record 20 minutes of *continuous* shop audio during
the field trip alongside the utterance corpus, and run Silero VAD over it offline. Measure
false-endpoint rate and missed-onset rate directly. If bad, D2 flips to push-to-talk.

---

## 8. Android `SpeechRecognizer` fragmentation, if it wins the bakeoff
**Damage: moderate. Likelihood: high if chosen.**

On-device recognition is API 31+; our floor is Android 10, where offline Tamil depends on an
OEM-dependent Google app language pack. A backend that works on a test device and not on a
shopkeeper's ₹6k phone is worse than one that is uniformly mediocre.

**Experiment (₹0, during the field trip):** check offline Tamil recognition on the actual
phones our pilot shopkeepers own. Five real devices beat any spec sheet.

---

## 9. Solo part-time capacity versus the scope
**Damage: moderate (slippage, not failure). Likelihood: high.**

Phases 1–3 are ~24–32 part-time weeks before there is a printer or spoken confirmation. The
mitigation is already in the plan: hard gates that stop a phase rather than carrying debt
forward, and a Phase 1 that is designed to kill the product early and cheaply if the ASR
doesn't hold up. Worth restating because the temptation under solo constraints is to skip the
eval harness and start on the Android app — which would make every later decision anecdotal.

---

## 10. Price volatility in vegetable shops outruns the catalog
**Damage: moderate — a stale price is a wrong bill. Likelihood: high in the vegetable segment.**

**Mitigation, not experiment:** a one-utterance spoken price change (D4), plus flagging any
price older than N days at the first bill of the day. Test it in the pilot; if vegetable shops
still struggle, they are a later segment and kirana/provision shops are the beachhead.

---

## 11. Thermal printer pairing does not survive restarts
**Damage: moderate. Likelihood: high — cheap ESC/POS printers are individually quirky.**

**Experiment (₹5,000, deferred to Phase 3):** buy 3 printer models and run 10 restart /
power-cycle / out-of-range cycles each, requiring zero user action to reconnect. Do not buy
printers before Phase 3.

---

## 12. Play Store review friction on `RECORD_AUDIO` and foreground mic use
**Damage: moderate (delay). Likelihood: low-to-moderate.**

Continuous mic access needs a prominent disclosure and a clear in-app justification. Our story
is good — mic is on only during an open bill, nothing is recorded, nothing is uploaded — and
D2's session scope is what makes it truthful.

**Mitigation:** submit an internal-testing track build early in Phase 2, well before the app is
finished, purely to surface policy objections while they are cheap to fix.

---

## 13. The paper baseline turns out to be faster than we assume
**Damage: high (invalidates the core claim). Likelihood: low, but entirely unmeasured.**

Forty years of muscle memory is a real competitor and we have never timed it.

**Experiment (₹0, field trip, day one):** time 30 real bills on paper across 5 shops — from
first item spoken by the customer to money changing hands. If a 5-line paper bill takes 20 s
rather than 40 s, the latency budget in `PLAN.md` is wrong and needs rewriting before any
pipeline work starts.
