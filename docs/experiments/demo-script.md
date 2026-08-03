# Shopkeeper demo — script and what to measure

The demo is not a sales pitch. It is the cheapest available test of the riskiest assumption
in the whole product: **that a shopkeeper will change how he bills at all.** Everything below
is arranged around learning that, not around impressing anyone.

## Before you go

1. **Preload his catalog.** Get 15–20 of his actual fast movers with his actual prices into
   `poc/api/_lib/seed/catalog.csv` beforehand. A demo on his own SKUs at his own prices is worth more
   than any amount of polish, and it removes the "your app doesn't know my shop" objection
   before it's raised.
2. **Warm the backend.** Open the app once on the way — the first request pays a cold start.
3. **Check the network.** If his shop has bad signal, tether to your own phone. The PoC needs
   connectivity; the product won't, and say so when it comes up.
4. **Run the UPI QR test first** (`upi-qr-test.md`). If a dynamic amount doesn't work against
   a personal VPA, the last third of this demo is a lie and you need to know before you go.

## What to say at the start

Say it in Tamil. Three sentences, no product tour:

> "Neenga eppadi normal-a sollureengalo, appadiye sollunga. Naan onnum sollamaatten.
> Kadaisila bill vandhurum."
> *(Say it exactly the way you normally would. I won't guide you. The bill appears at the end.)*

Then **hand him the phone and stop talking.** The instinct will be to coach him through it.
Coaching destroys the only data worth collecting. If he gets stuck, note where — that's the
finding.

## The flow

1. Consent screen → shop name → his UPI ID → Start.
2. He holds the button and speaks items as he'd hand them over.
3. Amber items need a tap to confirm; Finalise stays hidden until they're resolved.
4. Finalise → QR appears with the exact amount → he shows it to a customer.
5. He taps "Received" → receipt → next customer.

**Admin mode:** say "விலை வாசி" (or "owner mode"), then "sugar நாற்பத்தி அஞ்சு ரூபாய்" to
change a price. Say "பில் மோட்" to go back.

## What to measure — write these down in the shop

| | |
|---|---|
| Seconds per bill, app vs his paper (time 10 of each) | \_\_\_ / \_\_\_ |
| Items he had to repeat | \_\_\_ / \_\_\_ |
| Amber (confirm) rate | \_\_\_ % |
| **Silent errors — wrong line he did *not* notice** | \_\_\_ |
| Did he find the button without being told? | Y / N |
| Where did he hesitate? | |
| What did he say unprompted? (verbatim) | |

The verbatim quotes are the most valuable row on this table. Write them in his words, not
your paraphrase.

## The three questions to ask afterwards

Mom Test rules — ask about the past, never about the hypothetical future. "Would you use
this?" produces a polite yes and tells you nothing.

1. "Last time a bill went wrong — what happened?"
2. "Have you tried anything before this? A machine, an app? What happened to it?"
3. "How do you know today whether the UPI money actually came?"

Question 3 settles Risk 2 for free. If he says he relies on a soundbox, our manual
confirmation is a step backwards from what he has and the priority changes.

## What not to do

- **Don't claim it detects payment.** It doesn't, a soundbox on the same counter does, and
  being caught overstating that is unrecoverable.
- **Don't lead with speed.** Paper is fast and he knows it better than you do. Lead with the
  exact-amount QR and the record of the day — see `POSITIONING.md`.
- **Don't hide that the demo uses the internet.** The consent screen says so. The honest
  version — "this demo sends your voice out, the real one won't" — buys more credibility than
  it costs.
- **Don't oversell the accuracy.** This runs on cloud ASR that the shipped product can't
  afford. Say plainly that the offline version will be a little less accurate at first and
  will improve as it learns his shop. If you skip this, you've set an expectation against
  the very people you want as pilots.

## Consent for recording

If you want the utterances for the Phase 1 corpus — and you do — ask before you start,
in Tamil, and pay for the session. `phase-1-spike.md` has the terms: what's recorded, used
only to build and test this app, never published or sold, deletable on request. A demo that
quietly becomes a data collection exercise is the one way to lose a pilot shop permanently.

## What a good outcome looks like

Not "he was impressed". Impressed is free and means nothing.

A good outcome is **he asks to keep it**, or asks when he can have it, or picks the phone up
again for the next customer without being prompted. If he hands it back after one bill and
returns to his pad, that is also a result — and a cheaper one to learn now than after Phase 2.
