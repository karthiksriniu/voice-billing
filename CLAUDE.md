# Project: Voice-first billing for small Indian retail (working name: Vaakku)

## What this is

A voice-first billing app for small Indian retailers — kirana, provision and vegetable
shops. The shopkeeper speaks naturally while handing over goods ("two kilo sugar", "arai
kilo thuvaram paruppu", "ஐந்து ரூபாய் biscuit"); the app builds the bill line by line in
real time, announces the running total aloud, then shows a UPI QR carrying the exact
amount. The customer scans and pays. A later phase reuses the same voice pipeline for
inventory: sales decrement stock, spoken goods-inward entries increment it, low stock is
flagged and a reorder message drafted.

**Who we're replacing:** a scrap of paper, a calculator, and forty years of muscle memory.
Tier-1 paper-billing shops are the beachhead — not electronic registers, not PC POS. Any
flow slower or fussier than paper loses, regardless of how good the technology is.

We are pre-product. Phase 0 (planning) is complete; no application code exists yet. See
`docs/PLAN.md` for the phased build plan and its gates, `docs/DECISIONS.md` for the ten
resolved design decisions, `docs/RISKS.md` for the ranked risk list, and
`docs/experiments/phase-1-spike.md` for what the first build has to prove.

## How I want you to work on this

Deliver what was asked, at the scope intended. Make routine judgment calls yourself and
keep moving; check in with me only when two readings of a request would lead to
materially different work. If you think a request is mistaken or there's a better
approach, say so in a sentence and then proceed as asked — don't quietly redesign it.

Before your first tool call, say in one sentence what you're about to do. While working,
speak up only when you find something important or you're changing direction. When you
finish, lead with the outcome — the first sentence should say what happened or what you
found, with detail after it.

Keep responses focused and concise. Short caveats, most of the response on the answer
itself. For explanations, give a high-level summary unless I ask for depth.

For design docs and written deliverables: match length to what the task needs. Cover the
substance and skip filler sections, redundant summaries, and boilerplate. A tight 2-page
design doc I'll actually read beats an exhaustive 10-page one I won't.

Delegate to a subagent only for large, genuinely independent tracks of work — a wide
multi-file investigation, or researching two unrelated domains in parallel. Don't
delegate anything you could finish in a handful of tool calls. If one subagent can do
it, use one, not three.

Only flag a correction to something you said earlier if it would change my decisions or
the code. Otherwise just fix it and move on.

## Product principles (these decide arguments)

1. **Faster than paper, or it doesn't ship.** p95 speech-end → line item on screen is
   1150 ms; p95 above 1500 ms is build-breaking, not a regression to triage later.
2. **Wrong bills destroy trust permanently.** Silent mis-recognition is far worse than an
   honest "didn't catch that". We optimise silent error rate (≤0.5% at ship), not word
   error rate. Confidence gating and cheap correction come before raw accuracy.
3. **The shopkeeper never types.** Correction, catalog setup and inventory entry all have
   a voice path. Typing is the fallback, never the default.
4. **No cold-start wall.** A shop bills on day one with an empty catalog; it fills in as
   they trade.
5. **We touch money but don't hold it.** UPI goes shopkeeper ↔ customer directly. We are
   not in the flow of funds.

## Hard constraints (these are real, not aspirational)

- **Hardware is a low-end Android phone the shopkeeper already owns** — Android 10+,
  2–3 GB RAM, mediocre mic, one speaker. Plus their existing UPI QR and UPI app. Optional
  ₹1,500–2,500 Bluetooth ESC/POS printer. Assume no PC, no tablet, no dedicated mic, no
  reliable Wi-Fi, no steady 4G.
- **Marginal cost ceiling is ₹10 per shop per month, of which cloud inference is ₹0.**
  At ~24,000 utterances/shop/month this rules out cloud ASR entirely, including as a
  low-confidence fallback. This single number constrains more design than anything else.
- **Offline billing is non-negotiable.** Network is a bonus, never a dependency.
- **Distribution is Play Store only, self-serve, no field force.** Install → first
  successful bill in under 5 minutes, unassisted. This kills `NotificationListenerService`
  and `READ_SMS` for payment confirmation — both are Play policy violations for our use
  case. Don't propose them.
- **Language pair 1 is Tamil + English (Chennai), code-mixed.** Regional numerals and
  colloquial measures (arai, kaal, mukkal, pav, dozen, ek packet) are first-class. A
  second language must be configuration — numerals, units, aliases and phonetic rules live
  in data files, not code.
- **Environment is loud** — traffic, customers, ceiling fans, a TV. Design for 70–80 dB(A)
  background, not a quiet room.
- **Privacy: no audio retained by default.** Frames are consumed and discarded. Any model
  improvement runs through opt-in donation of corrected utterances only. Never conflate the
  consented field corpus with production data.

## Non-goals

GST filing and compliance reporting, multi-store or multi-counter, employee accounts,
loyalty programmes, credit/khata ledgers, barcode scanning, e-commerce or delivery, iOS.

## Decisions

Recorded in `docs/DECISIONS.md`, one entry per decision with options, reasoning and the
condition that reverses it. Decisions marked **[data]** are provisional and settled by the
Phase 1 spike. Don't change a recorded decision silently — if you think one is wrong, say
so and let me decide.
