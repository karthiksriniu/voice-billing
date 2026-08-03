# Phase 1 spike — what it proves and how we judge it

**Shape:** audio file in → line items → total → UPI QR PNG out. Python, on a laptop, headless,
no Android app, no UI. Runs against a fixture corpus of real shop utterances.

**Purpose:** decide the ASR backend on evidence, and find out cheaply whether the product is
possible at all under the on-device-only constraint. This spike is designed to be able to
**kill the product**, and that is its most valuable outcome if the numbers say so.

---

## The three questions it answers

1. **Can any free, offline, on-device ASR backend understand code-mixed Tamil billing speech
   in a loud shop well enough to bill without silent errors?** (Risk 3, D1.)
2. **Does a deterministic grammar plus fuzzy catalog matching cover real phrasing**, or is
   real speech more varied than a grammar can absorb? (D3.)
3. **Does a dynamic `upi://pay` QR against a personal VPA actually work** across the four UPI
   apps that matter? (Risk 1, D5.)

Explicit non-goals: no Android, no UI, no printing, no TTS, no inventory, no on-device latency
measurement beyond the RTF proxy. Nothing in this spike is production code and none of it needs
to survive into Phase 2 except the eval harness and the parser, which do.

---

## Order of work

The eval harness is built **before** any pipeline code, and the field trip happens **before**
the harness. Both orderings matter and neither is negotiable — a harness written after the
parser will be shaped by the parser's assumptions, and a corpus recorded after the grammar
exists will be unconsciously biased toward what the grammar handles.

1. UPI QR check (₹0, one afternoon — do it first, it can invalidate D5)
2. Field trip: corpus + paper baseline + payment-verification observation
3. Labelling
4. Eval harness and scorer
5. ASR bakeoff
6. Parser, catalog matcher, N-best reranker
7. Report and gate decision

---

## The fixture corpus: 200 utterances, legally and cheaply

**Source: 5 cooperating Chennai kirana/provision shops** (available per this session's inputs),
2–3 hours each across morning and evening rush.

**Collection method.** Record the *shopkeeper*, not customers, using a target-class Android
phone lying on the counter where the app would sit — the mic, the position, and the noise are
then all realistic rather than approximated. Ask them to bill exactly as they normally would
and speak the items as they hand them over. Alongside, record 20 minutes of continuous ambient
audio per shop with no billing, for the VAD test (Risk 7) and for noise augmentation.

**Consent and legality.** This is the part to get right, because it is the corpus everything
downstream is measured against.
- Written consent from each shopkeeper, in Tamil, plain language: what is recorded, that
  it will be used only to build and test this app, that it will not be published or sold,
  that they can withdraw and have it deleted.
- **Pay them.** ₹2,000–3,000 per shop for the session. It is the difference between a favour
  and an agreement, and it makes the withdrawal right meaningful.
- A visible sign at the counter in Tamil during recording ("Voice recording in progress for
  app testing"). Customers are in a public commercial space, but a sign plus the practice
  below is the honest standard.
- **Do not transcribe or retain customer speech.** Segment to shopkeeper utterances only and
  delete the rest of the continuous audio after the VAD test. If a customer's voice is
  audible inside a shopkeeper utterance, keep it — it is exactly the noise condition we need —
  but never label or index it.
- Corpus is stored encrypted, never leaves the dev machine, and is **not** the same data as any
  future opt-in production donation stream (D9). No conflation, ever.

**Cost:** ₹10,000–15,000 for five shops, plus ₹3,000–5,000 for labelling help if a second
Tamil-literate pair of ears is needed. Under ₹20,000 total.

**Target composition** — biased toward the hard cases, since the easy ones will be over-
represented naturally:

| Category | Target |
|---|---|
| Weight-led, whole units ("two kilo sugar") | 40 |
| Weight-led, colloquial fractions (arai, kaal, mukkal, pav) | 40 |
| Price-led ("ten rupees coriander", "ஐந்து ரூபாய் biscuit") | 30 |
| Count/packet-led ("rendu packet", "ek dozen") | 25 |
| Bare item, implied quantity | 15 |
| Corrections ("cancel last", "remove sugar", "no, three kilo") | 20 |
| Multi-item in one breath | 15 |
| Genuinely unintelligible / talking to the customer, not the app | 15 |

That last row is deliberate and important: the corpus must contain utterances the app is
**supposed to reject**, or the confidence gate cannot be evaluated at all.

**Gold labels.** For each utterance: verbatim transcript (Tamil script where Tamil, Latin where
English, mixed as spoken), plus structured expected output — item, quantity, unit, price basis,
or the explicit label `REJECT`. Labelled by a Tamil-literate person; a 30-utterance overlap
sample double-labelled to measure inter-annotator agreement, because if two humans disagree on
what was said, no model can be scored against it.

---

## Scoring

Every utterance is classified into one of four outcomes, and only these are reported:

| Outcome | Meaning |
|---|---|
| **ACCEPT-correct** | Line added automatically; item, quantity, unit and price basis all match gold. |
| **ACCEPT-wrong** | Line added automatically and wrong. **The silent error.** |
| **CONFIRM** | Below the confidence gate; app asks. Costs ~2 s. Acceptable. |
| **REJECT-correct** | Refused, and gold says it should have been refused. |

Reported metrics: **silent error rate** = ACCEPT-wrong / all; **auto-accept rate** =
ACCEPT-correct / all; **parse error rate** = same pipeline run on gold transcripts instead of
ASR output, isolating grammar failures from recognition failures; **decode RTF** on one core
pinned at 1.8 GHz.

Word error rate is computed but **not used to make any decision**. It is a diagnostic, not a
gate — an ASR that mangles filler words while getting every number and item right is a good
ASR for this product.

The headline artefact is not a table but a **curve**: for each backend, silent error rate
plotted against auto-accept rate as the confidence threshold sweeps. That curve is what the
backend decision is actually made on, because it shows what accuracy costs in speed.

---

## Gate

The spike passes when the winning backend clears **all** of:

| | Threshold |
|---|---|
| Silent error rate | ≤ 1.0% |
| Auto-accept rate at that threshold | ≥ 80% |
| Parse error given correct transcript | ≤ 1.5% |
| Decode RTF (1 core @ 1.8 GHz) | ≤ 0.3 |
| UPI QR: amount pre-filled and honoured | 4 of 4 apps (GPay, PhonePe, Paytm, BHIM) |

**Stop conditions — do not proceed to Phase 2:**
- No backend reaches silent error ≤ 2% at auto-accept ≥ 60% (i.e. 2× the gate). On-device-only
  is forced by cost and cannot be relaxed with a cloud patch we can't afford, so this means
  the product does not work yet. Report it and stop.
- Personal-VPA dynamic QR fails or is unreliable in more than one of the four apps. D5 has to
  be redesigned before any Android work, because merchant onboarding changes the whole product.
- Paper baseline for a 5-line bill comes in under ~25 s. The latency budget in `PLAN.md` is
  then wrong and gets rewritten before pipeline work continues.

**Partial pass, and what it means:** grammar-constrained Vosk clearing the gate while
unconstrained backends don't is the *expected good outcome* and confirms that closed-vocabulary
decoding is the main lever (D1, D3). Conversely, if constraining the vocabulary gives little
benefit, the accuracy problem is acoustic rather than linguistic — and the answer is a better
mic path or push-to-talk (D2), not a better parser.

---

## What survives into Phase 2

- **The eval harness and corpus** — permanently. Every parser and ASR change from here on is
  measured against it, and it becomes the CI gate for the latency and accuracy thresholds in
  `PLAN.md`.
- **The parser, grammar and catalog matcher** — ported, not rewritten. Language data
  (numerals, units, aliases, phonetic normalisation rules) stays as data files so language 2
  is configuration.
- **The `AsrBackend` interface** — the shape, if not the Python implementation.

Everything else in the spike is disposable, and should be written as though it is.

---

## Deliverable

One report, 2–3 pages: the four-outcome table and threshold curve per backend, the chosen
backend with the evidence for it, the measured paper baseline, the UPI QR results across four
apps, the VAD false-endpoint rate on continuous shop audio, and a plain statement of whether
the gate passed. Plus the corpus itself, labelled and encrypted, and the harness.
