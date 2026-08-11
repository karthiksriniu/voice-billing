# Decisions

One entry per open decision in the brief. Each: options, recommendation, reasoning, and the
condition that reverses it. Decisions marked **[data]** are provisional and settled by the
Phase 1 spike.

---

## D1. Speech recognition — on-device only, backend chosen by bakeoff **[data]**

**Options**
| | Offline | Marginal cost @24k utt/shop/mo | Size | Tamil+English code-mix |
|---|---|---|---|---|
| Vosk (small Indic, constrained grammar) | Yes | ₹0 | ~50 MB | Weak base model, but grammar-restricted decoding compensates hard |
| whisper.cpp tiny-q5 / small-q5 | Yes | ₹0 | 75 / 250 MB | Tiny is poor at Tamil; small is too slow on 3 GB and **hallucinates on noise** |
| Android `SpeechRecognizer` (on-device) | Partly | ₹0 | 0 MB in APK | Google's own Tamil pack; single-locale, so code-mix degrades; availability varies by OEM |
| AI4Bharat IndicConformer | Yes | ₹0 | ~120 MB ONNX | Best published Tamil accuracy; NeMo→ONNX→Android is a multi-week project |
| Bhashini / ULCA, Sarvam, Google Cloud STT, IndicWhisper | No | ₹40–12,000 | — | Good accuracy, wrong cost curve |

**Recommendation: on-device only. No cloud fallback at MVP.** All candidates sit behind one
`AsrBackend` interface (audio frames in → N-best hypotheses with per-token confidence out),
and Phase 1 picks the winner on the field corpus. Provisional favourite is **Vosk with a
grammar restricted to the shop's catalog + numerals + units**, because a closed vocabulary is
the single biggest accuracy lever we have and only Kaldi-family decoders expose it.

**Reasoning.** The brief's leaning was on-device with cloud fallback for low-confidence
utterances. The cost arithmetic in `PLAN.md` kills the fallback: even at 5% escalation the
cheapest Indic cloud ASR is 4–15× the ₹10/shop/month ceiling. Since the fallback can't pay
for itself, the on-device path must carry 100% of traffic anyway — so build for that and
skip the dual-path complexity. Offline-non-negotiable points the same way. Whisper deserves
a specific warning rather than a benchmark: it fabricates fluent text from noise, which
manufactures exactly the silent errors we have decided are fatal.

**Evidence needed:** the 200-utterance field corpus, scored on silent error rate and
auto-accept rate per backend. Also required: does `SpeechRecognizer` actually do offline
Tamil on the ₹6–8k phones our pilot shopkeepers own? Android 12+ has
`createOnDeviceSpeechRecognizer`, but our floor is Android 10, where offline recognition
depends on an OEM-dependent Google app language pack. Check it on their phones during the
field trip — costs nothing.

**Practical note:** `SpeechRecognizer` only reads the mic, so it cannot be fed WAV files.
Evaluate that arm by playing the corpus through a speaker into the phone in a quiet room.
Acoustically imperfect, but comparable across devices and closer to real conditions than
file injection would be.

**Reverses if:** Bhashini's free tier proves genuinely unmetered and low-latency at our
volume (then a fallback becomes free and we take it); or no on-device backend clears the
Phase 1 gate (then the product needs rethinking, not a cloud patch we can't afford); or we
later acquire ML capacity, at which point IndicConformer fine-tuned on our own corpus
becomes the obvious upgrade.

---

## D2. Capture model — session-scoped VAD

**Options:** push-to-talk per item · session with VAD segmenting continuous speech ·
always-on with wake word.

**Recommendation: session-scoped VAD.** Tap once to open a bill, speak items freely, say
"total" (or tap) to close. Use **Silero VAD** (~1 MB ONNX, runs fine on low-end) rather than
WebRTC VAD, which collapses in shop noise. Two escape hatches: a large hold-to-talk button,
and **volume-down as a hardware push-to-talk** so the phone can sit on the counter and be
pressed without looking.

**Reasoning.** Agrees with the brief's leaning. Push-to-talk per item costs a tap per line
and breaks the rhythm of handing over goods with both hands full. Wake words are a false-
trigger and privacy-comfort problem in a room full of strangers, and a permanently hot mic on
a 2 GB phone is a battery and Play-policy problem for no gain. The session scope also bounds
mic-on time to the duration of an actual bill, which is the honest thing to put on the
consent screen.

**Reverses if:** Phase 1 shows VAD endpointing is unreliable at shop noise levels (ceiling
fan, TV, traffic) — specifically if false endpoints cut items mid-utterance more than ~3% of
the time. Then push-to-talk becomes the default and VAD becomes the power-user mode.

---

## D3. Utterance → line item — deterministic grammar, N-best reranked

**Options:** deterministic grammar + fuzzy catalog match · small on-device LLM · cloud LLM ·
hybrid with escalation.

**Recommendation: deterministic, with no LLM anywhere in the MVP.**

Pipeline: normalise numerals → parse grammar → fuzzy-match item against this shop's catalog
→ score. Run it over **every ASR hypothesis in the N-best list**, and rank by
(parse validity × catalog match score × ASR confidence). This recovers a large fraction of
ASR errors for free and is the highest-value cheap trick in the whole design.

Grammar must cover, at minimum:
- weight-led: `[qty][unit][item]` — "two kilo sugar", "arai kilo thuvaram paruppu"
- price-led: `[amount] rupees [of] [item]` — "ten rupees coriander", "ஐந்து ரூபாய் biscuit"
- count-led: "one packet", "rendu dozen", "ek packet"
- fractional/colloquial measures: arai (½), kaal (¼), mukkal (¾), pav (250 g), dozen (12)
- bare item with implied unit quantity, resolved from the catalog's default unit

Item matching needs phonetic normalisation for Tamil–English transliteration variance
(*thuvaram / tuvaram / thuar*). Soundex and Metaphone are English-only and will mislead;
build a small romanisation-normalising key (collapse aspirates, t/th, retroflex/dental,
vowel length) and combine it with edit distance over both the romanised and Tamil-script
forms. Catalogs are ~100–400 SKUs, so an exhaustive scan is free.

**Reasoning.** Agrees with the brief. Deterministic is debuggable, free, offline, testable
against the fixture corpus, and — the point that matters most — its failures are *legible*.
When it can't parse, it knows it can't, which feeds the confidence gate. A small on-device
LLM on a 3 GB phone burns our entire latency budget and produces confident wrong answers,
which is the exact failure mode we are designing against.

**Reverses if:** parse error given a correct transcript stays above ~3% after two grammar
iterations, meaning real phrasing is more varied than the grammar can absorb. Even then the
first move is a larger grammar with a learned alias table, not an LLM. An LLM only becomes
right if we later need free-form utterances ("give me the usual") that have no grammar.

---

## D4. Catalog bootstrap — seeded list, learn prices as you go

**Options:** seeded regional SKU list with typical prices · learn-as-you-go · voice dictation
of the whole catalog · OCR a photographed price list.

**Recommendation: ship a seeded ~300-SKU Chennai kirana list carrying names, Tamil/English
aliases and default units — but no prices. Price is learned on first sale**, asked aloud
once ("சர்க்கரை என்ன விலை?"), then remembered forever.

**Reasoning.** The brief's leaning, with one change: seeding prices is actively harmful. A
seeded name costs nothing if wrong — the shopkeeper says something else and the alias is
learned. A seeded *price* that is wrong produces a wrong bill, which is the one thing we
said destroys trust permanently. Vegetable prices move daily and vary by street, so a seeded
price is wrong more often than right. Seeding names and aliases still buys the entire ASR
benefit (a populated vocabulary for grammar-restricted decoding on day one) with none of the
risk, and setup time stays at zero per principle 4.

Also needed: a fast spoken price-change path ("சர்க்கரை நாற்பத்தி ஐந்து ரூபாய்"), because
in a vegetable shop prices change every morning and any friction here abandons the app.

**Reverses if:** pilot shops turn out to keep stable enough prices that seeding them saves
real time and the "confirm this price" step is judged cheap. Test it by seeding prices in a
*confirm-before-first-use* state for one pilot shop and comparing setup time and error rate.
Voice dictation of a full catalog and price-list OCR both stay available as optional
accelerators later; neither should ever be on the critical path to the first bill.

---

## D5. Payment confirmation — dynamic QR, manual confirmation. **Highest-risk decision.**

**Options**
1. **Android `NotificationListenerService`** reading the shopkeeper's own UPI/bank app
   notifications.
2. **Reading payment SMS** (`READ_SMS`).
3. **PSP / aggregator webhook** (Razorpay, Cashfree, PhonePe merchant).
4. **Manual confirmation** by the shopkeeper.

**Recommendation: generate a dynamic `upi://pay` QR carrying the exact amount, and have the
shopkeeper confirm receipt with one tap. No automatic confirmation at MVP.**

**Reasoning.** Play-Store-only distribution removes options 1 and 2 outright, and this is not
a grey area. `NotificationListenerService` is restricted to a short list of approved use
cases (accessibility, companion devices, and similar); payment reconciliation is not among
them, and an app whose core loop depends on it gets rejected or removed. `READ_SMS` is
governed by the SMS/Call Log policy, which permits it essentially only for default SMS
handlers — financial reconciliation is again not an exemption. Neither is worth a rejection
that takes the whole product offline.

Option 3 is technically clean and directly contradicts the distribution constraint: PSP
merchant onboarding means PAN, bank proof and a review queue, which is not something a
shopkeeper completes unassisted in under 5 minutes at 8 p.m. It also puts us adjacent to the
flow of funds, against principle 5.

That leaves manual confirmation — and it is a much better answer than it first appears,
because **it is exactly what shopkeepers do today.** With a static QR sticker there is no
automatic confirmation at all: they glance at their phone, hear the sound-box, or look at the
customer's screen. We are not asking them to accept a downgrade. Meanwhile the dynamic QR
already removes the two real failure modes of the sticker — the customer typing the amount
wrong, and the shopkeeper having to read the total aloud and hope.

Design the confirmation to ride on the habit rather than fight it: QR on screen, amount large
above it, one full-width **"வந்தது / Received"** button. Unconfirmed bills stay in a
day-end review list, never blocking the next customer.

**What settles the residual doubt (₹0):** on the Phase 1 field trip, sit in 5 shops for two
hours and count how each UPI payment is actually verified today. If it turns out that
essentially every shop already relies on a sound-box, then a *matching* audio confirmation
from us is the differentiator and the priority changes.

**Two things to verify before Phase 2, both cheap and both genuinely uncertain:**
- Does a `upi://pay` deep link with `am` set against a **personal (P2P) VPA** work
  consistently across GPay, PhonePe, Paytm and BHIM? NPCI has progressively tightened
  P2P-with-amount flows for fraud reasons, and some apps make the amount editable or warn.
  Test with a real VPA on four apps. If P2P-with-amount is unreliable, the product needs a
  merchant VPA, and D5 changes shape entirely.
- Does the `tr` (transaction ref) we set survive to the shopkeeper's own statement? If yes, a
  future reconciliation path exists without a PSP.

### Update — verified payment becomes the paid tier

The owner's intent, recorded: **automatic** confirmation is the goal, done properly —
verify the payment against the bank/PSP in the backend, and only then trigger a receipt.
Manual confirmation stays the free-tier behaviour; automation is an opt-in **premium**
feature. See D10.

This resolves the tension that made D5 hard. Merchant onboarding was rejected because PAN,
bank proof and a review queue cannot happen inside install-to-first-bill-in-5-minutes. But
that constraint applies to a shop that has not yet decided we are worth anything. A shop
choosing to pay for automation, weeks in, is a completely different proposition: it has
already converted, and KYC is a reasonable price for something it asked for. **The
self-serve constraint governs the free tier, not the upgrade.**

What still has to be true, and none of it is proven yet:
- A PSP that will onboard a sub-GST-threshold kirana at all, self-serve, on a phone.
- Settlement staying shopkeeper ↔ customer. Principle 5 does not bend for a paid tier.
- Webhook latency fast enough to matter at the counter — a confirmation that lands 30
  seconds later is worse than the sound-box the shop already has.
- The economics: per-conversation WhatsApp pricing and PSP fees come out of the tier's
  own revenue, not the ₹10/shop/month free-tier ceiling, which was never meant to carry
  them.

**Not building:** the phone's share sheet into WhatsApp. It was the cheapest honest path to
a receipt and it works today at ₹0, but it is manual, and shipping it would set the
expectation that receipts are something the shopkeeper does rather than something that
happens. Rejected on product grounds, not technical ones.

**Reverses if:** (a) we ever add a sideloaded distribution channel — the notification listener
becomes available immediately and is the right answer, so keep the confirmation logic behind
a `PaymentConfirmer` interface with a manual implementation as the only one shipped; (b) a
PSP appears with genuinely instant self-serve merchant onboarding (some now offer VPA-only
onboarding against an existing UPI ID); (c) pilot data shows unconfirmed-bill disputes
happening more than ~1 per shop per week.

---

## D6. Spoken confirmation — pre-recorded clips for numbers, TTS for item names only

**Options:** Android built-in TTS · pre-recorded numeral and phrase audio concatenated
locally · hybrid.

**Recommendation: hybrid, weighted to pre-recorded.** Record a professional Tamil voice for
0–99, hundred / thousand, rupee / rupees / paise, and ~20 stock phrases ("didn't catch that",
"total", "removed"). Concatenate locally. The **running total and final total are always
pre-recorded** — they must be unmistakable at 75 dB(A). Item names, which cannot be
pre-recorded because the catalog grows, use Android TTS when a Tamil voice is installed and
are **silently skipped when it isn't** — the screen already shows the item, and the number is
what matters.

**Reasoning.** Agrees with the brief's leaning, and identifies where it stops stretching: the
catalog is open-ended, so pure pre-recording cannot cover item names. Indic TTS on a ₹7k
phone is uneven in quality and slow to initialise, which is survivable for a secondary cue and
not for the amount the customer is about to pay. Clip inventory is ~130 files, a few MB at
low bitrate.

**Reverses if:** Phase 3 testing shows shopkeepers rely on hearing the item name to catch
errors (rather than glancing at the screen), which would make TTS availability critical
rather than optional. In that case, pre-record the top ~300 seeded SKU names too and use TTS
only for shop-added items.

---

## D7. Output — screen-only at MVP, print in Phase 3, share link later

**Recommendation:** Phase 2 is **screen-only**. Thermal printing lands in **Phase 3**. A
WhatsApp/SMS bill link is **out of MVP** and probably out of the product for now.

**Reasoning.** Screen-only is enough to replace the scrap of paper, and every hardware
dependency added before the core loop is proven is a reason for the pilot to fail for reasons
unrelated to the pilot. Printing matters for a different reason than customer receipts —
**the printed footer is the primary attribution surface**, which is why it is Phase 3 and not
Phase 4, and why growth measurement can't start before it.

The share link was rejected on a principle, not a cost: it required the customer's phone
number, which meant the shopkeeper types — violating principle 3 at the busiest moment of
the interaction.

**Updated.** That objection does not survive the obvious fix: the phone is already being
handed to the customer to scan the QR, so *the customer* types their own number, on the
screen in front of them. The shopkeeper still never types. The payment screen now captures
an optional customer number beside the QR, and "Send Receipt" records it against the bill.

What remains unresolved is delivery, and it is the same shape as D5. There is no messaging
provider wired up, and adding one is not free: WhatsApp Business pricing is per-conversation
and template approval is required, while SMS needs TRAI DLT registration. Both break the
₹10/shop/month ceiling at 100 bills a day. So a captured number is stored as `requested`,
never `sent`, and the shopkeeper is told so plainly. The share sheet into WhatsApp would work today at ₹0, and is
deliberately **not** being built: it is manual, and it would teach shopkeepers that a
receipt is something they do. The intended path is automatic — payment verified in the
backend, receipt triggered by that verification — as a premium feature. See D5 and D10.

**Pairing requirement (non-negotiable for Phase 3):** the printer must reconnect with zero
user action after a phone restart, a printer power-cycle, and a day out of range. Bond once
during setup, store the MAC, reconnect on app foreground with exponential backoff, and never
show a Bluetooth device picker again after the first successful print. Budget real time for
this — cheap ESC/POS printers are individually quirky, so test on 3 models.

---

## D8. Data and sync — local-first SQLite, no backend at MVP

**Recommendation: no backend at all in Phases 1–4.** SQLite on device; Android Auto Backup
(25 MB free, Google-provided) for device-loss recovery; plus a user-triggered encrypted
export file they can send themselves on WhatsApp.

Schema, sketched so later sync is not a rewrite:

```
shop(id, name, upi_vpa, payee_name, lang)
product(id, name, name_ta, default_unit, aliases[], is_seeded)
price(id, product_id, unit, amount_paise, effective_from)      -- append-only, never updated
bill(id, opened_at, closed_at, total_paise, payment_state, upi_tr_ref)
line_item(id, bill_id, product_id, qty_milli, unit, unit_price_paise, amount_paise,
          asr_confidence, was_corrected, source_utterance_id)
stock_movement(id, product_id, delta_milli, reason, bill_id, occurred_at)   -- Phase 4
utterance(id, bill_id, transcript, nbest_json, accepted_hypothesis, at)     -- no audio
```

Everything that changes over time (`price`, `stock_movement`) is an **append-only event log**
keyed by a UUID generated on-device, so a later sync is append-mostly with
last-writer-wins only on the few mutable rows. That is the whole cost of keeping the option
open, and it is worth paying now.

**Reasoning.** Agrees with the brief's leaning. No backend removes cost, latency, privacy
exposure, uptime, and an entire compliance surface in one move. The one real objection is
device loss = total data loss, and Android Auto Backup answers it for ₹0. Nothing downstream
is blocked: inventory (Phase 4) is single-shop and single-device by the stated non-goals, and
growth loops (Phase 5) need attribution counters, not a synced database.

**Reverses if:** multi-counter or multi-device shows up (an explicit non-goal today), or a
monetisation model appears that needs server-side data. Both would land after Phase 4, by
which point the event-log schema makes the change additive.

---

## D9. Privacy — no audio retained by default, opt-in donation of corrections only

**Recommendation.**
- **Default: zero audio retention.** Frames are consumed by the recogniser and discarded; the
  only persisted artefact is the transcript and N-best list in `utterance`, kept 30 days for
  the shopkeeper's own correction history and then deleted.
- **No telemetry upload of any kind by default.** With no backend (D8) there is nowhere to
  send it anyway, which is a useful forcing function.
- **Model improvement without a corpus we can't keep:** an explicit opt-in toggle, off by
  default, that donates **only utterances the shopkeeper corrected** — audio plus their
  correction as the gold label. Corrected utterances are both the highest-value training data
  and the ones where the user has already looked at the transcript and knows what it says.
  Wi-Fi only, reviewable and deletable in-app, revocable with retroactive deletion.
- **The Phase 1 field corpus is collected under separate written consent** with payment, and
  never conflated with production data.

**Consent screen wording (Tamil + English, plain, one screen, shown before first bill):**
> This app listens only while you are making a bill. Your voice is not saved and never leaves
> this phone. Your bills, prices and stock stay on this phone.
> *(Optional, off by default)* When you correct something the app heard wrong, you can choose
> to send that one recording to help it understand Tamil better. You can turn this off or
> delete what you sent at any time.

**Reasoning.** Retaining shop audio would capture bystander conversation in a public space
from people who never consented — a problem no consent screen we can show the shopkeeper
solves. Restricting donation to corrected utterances is the compromise that gets us a
training set that is small, high-signal, and honestly consented.

**Reverses if:** the correction-donation stream proves too sparse to be useful (likely below
~50 opted-in shops). The answer then is paid field collection, not looser defaults.

---

## D10. Growth and monetisation — loops instrumented, pricing deferred

Per this session's input, pricing is not being designed now. What Phase 5 builds:

- **Printed / on-screen bill footer attribution** — a short line and a WhatsApp-shareable
  install link. This is the main loop and it does not exist before Phase 3 (D7).
- **Referral with a concrete reward** — but the reward can't be a discount on a free product.
  The natural candidate is the thermal printer at cost or free after N referred shops, which
  also seeds the hardware channel if we ever want it.
- **Market-cluster seeding** — kirana shops in a market street watch each other constantly.
  Seed a whole street rather than scattered shops; measure whether adoption is contagious
  within a cluster. This is the highest-leverage distribution experiment available and it
  costs only how we choose pilot locations.
- **Distributor partnerships** — deferred to Phase 4+, since the reorder draft is the hook and
  it doesn't exist until then. Flagged now because it is the one loop with data-consent
  implications (D9) and those must not be retrofitted.

**On charging:** billing itself almost certainly has to stay free — the competitor is a free
piece of paper. If revenue is ever needed, the defensible lines are hardware margin on the
printer and the Phase 4 inventory/reorder tier, in that order. Payments monetisation is the
one to be most careful about, since it contradicts principle 5 and D5.

### Update — the first revenue thesis

Recorded from the owner: **automation is the paid tier.** Billing stays free forever; what
a shop pays for is the work it no longer has to do.

- **Verified payment confirmation** — the backend confirms the money actually arrived,
  instead of the shopkeeper glancing at his phone (D5).
- **Automatic WhatsApp receipts** — triggered by that verification, not by a manual tap
  (D7).

This is the first answer to the structural problem in `PRODUCT-REVIEW.md` §9: at ₹0 revenue
any non-zero CAC is unrecoverable, so "free forever, no field force" only worked if growth
was purely viral. A paid tier does not remove that requirement for the free tier, but it
does mean the business has somewhere for money to come from.

It is also the right shape of thing to charge for. It is not a feature gate on billing —
which would break Principle 1 and the whole beachhead argument — it is work being done on
the shopkeeper's behalf, and it costs us real money per shop (PSP fees, per-conversation
WhatsApp pricing), so the price has a floor that is easy to explain.

Both are **optional**. The free tier must stay complete on its own: a shop that never pays
still bills by voice, still shows an exact-amount QR, still keeps its record. If the free
tier starts feeling deliberately crippled, the beachhead argument dies with it.

Open, and worth settling before building: what it costs, whether it is per-shop-per-month
or per-transaction, and whether verified payment alone is enough to charge for or only
becomes compelling bundled with receipts and the Phase 4 reorder draft.

**Reverses if:** a different monetisation thesis is chosen, at which point this entry is
rewritten rather than amended.

---

## D11. A vision model reads paper, and only paper

**Decision.** Document import — a photographed rate card, menu board or supplier delivery
note turned into catalog rows or stock inward — runs on Claude Opus 5 with vision. Nothing
else in the product calls a large model. The utterance path stays the deterministic grammar
parser it was.

**Why this does not break the ₹10 ceiling.** The constraint that rules out cloud ASR is
arithmetic, not principle: ~24,000 utterances a shop a month leaves no room for per-call
inference at any price. A catalog import happens **once**, when a shop signs up; an invoice
perhaps weekly. Measured cost is about **7 paise a page** — call it ₹1–2 a shop a year for
the catalog and a few rupees a year for inward. That is two orders of magnitude away from
the number that decided the billing path, which is why the same reasoning lands in the
opposite place.

**Why a model at all, when the parser exists.** The parser reads speech in a grammar the
shop has been taught. A rate card is somebody's handwriting, in a layout nobody agreed on,
with abbreviations only that shop uses, in a script that may not be Latin. There is no
grammar to write. This is the shape of problem a large model is actually for.

**What it is not allowed to do.**

- **It transcribes; it does not decide.** Which SKU a row *is* — whether "Sug." is the Sugar
  already on the shelf — is settled afterwards by the same phonetic matcher the voice path
  uses, at a higher bar (0.90) than billing, because billing has the shopkeeper's ear a
  second later and an import does not. The model is never in a position to overwrite a price
  by concluding two names are the same thing.
- **It cannot write.** The read endpoint returns proposals. A separate, explicit save writes
  them. A matched row keeps its aliases, its stock and its description: a rate card names a
  price and nothing else, and an import that quietly dropped the spoken names a shop had
  taught it would be worse than not importing at all.
- **It may not guess a number.** Blank, smudged and ambiguous prices are dropped and counted,
  and the count is shown. A missing row is an annoyance; an invented price is a wrong bill,
  and principle 2 says which of those we optimise against.
- **Owner only.** A worker importing a rate card would be repricing the shop from a
  photograph.

**Cost of this decision.** It adds a cloud dependency to a product that is otherwise
offline-first, and an API key to a deployment that had two. Import is therefore the one
screen that requires a network — acceptable because it is a setup activity, done once,
usually sitting down, and the shop bills perfectly well without ever using it (D4).

**Reverses if:** on-device OCR gets good enough at Indian handwriting and mixed scripts to
match this without a network — at which point the interface stays and the reader is swapped
behind it — or if measured cost per shop turns out an order of magnitude above 7 paise a
page, which would mean rate limiting imports rather than removing them.

---

## D12. Reorder forecasting — days of cover, measured demand, assumed lead time

**Decision.** The stock screen shows, per item, a green-to-red bar scaled in **days of
cover** with two markers: where the shelf is now, and the reorder point. Demand comes from
the shop's own ledger. Lead time is a category default and is labelled as an assumption.
Per-item lead time is deferred to the premium configuration tier.

**Why days of cover and not units.** "4.64 kg" and "484 pieces" cannot be compared, so a bar
scaled in an item's own unit answers nothing across a list. "9 days left" and "31 days left"
compare instantly, and the reorder point lands in the same unit — which is what lets one bar
carry both numbers. It also makes the flag's position constant across every row, so a
screenful of different products reduces to one repeated question: is the marker left of the
flag?

**The model.** The standard formula, unchanged:

    reorder point = average daily demand x lead time + safety stock

with two departures forced by what a shop this size can be asked for.

**Demand is measured.** From this shop's movement ledger — sales, and the components those
sales consumed — divided by *the days the ledger actually spans*, not the window it was
fetched over. This distinction is the single most dangerous number on the screen: a shop
three days in, fetched over ninety days, would have its demand divided by thirty and be told
its beans last a year. Errors here are asymmetric — overstating cover means running out
mid-service, understating it means buying early — so every judgement call is made toward
the pessimistic side, and the tests are written to catch the optimistic failure.

**Lead time is assumed, and says so.** It belongs to a supplier relationship the app knows
nothing about. Defaults follow how Indian general trade actually replenishes — dairy and
fresh goods daily, dry goods and packaging on a weekly distributor cycle — so ingredients
assume 3 days and packaging and resale goods 7. The screen carries a line saying which
figures were measured and which assumed. A forecast presented without its basis gets trusted
further than it has earned.

**Safety stock scales with ABC class**, by annual consumption value (demand x price, not
price alone — a one-rupee straw turning over five hundred times a week is an A item and a
₹1,850 pot that sells twice a year is not). A items are held tight because that is where the
shop's money sits; C items carry a fatter buffer, being cheap to over-hold and painful to be
without. Expressed as a multiple of lead time rather than a Z-score, because the demand
variance a Z-score needs is not something this data can honestly supply yet.

**What it refuses to do.** An item with no sales history, or a shop with under three days of
trade, gets "not enough history yet" rather than a bar. Left to itself, zero demand computes
as infinite cover and draws as a full green bar — the most confident possible statement
about the product we know least about. A menu item gets no bar at all: it is assembled at the
till and never sat on a shelf.

**Sources.** Reorder point and safety stock: [Netstock](https://www.netstock.com/blog/reorder-point-formula/),
[Brightpearl](https://www.brightpearl.com/blog/how-to-calculate-reorder-points),
[GAINS](https://gainsystems.com/blog/reorder-point-vs-safety-stock-balancing-inventory-in-retail/).
ABC thresholds and the demand-x-value basis: [MRPeasy](https://www.mrpeasy.com/blog/abc-analysis/),
[EazyStock](https://www.eazystock.com/blog/calculate-apply-abc-classification-inventory/).
Indian replenishment cadence: [Kirana Club](https://kirana.club/resources/fmcg-distribution-india-guide),
[Crimson Cup](https://www.crimsoncup.com/whats-new/inventory-tips-to-keep-your-cafe-running-smoothly).

**Reverses if:** shops turn out to reorder on a fixed calendar rather than on stock level, in
which case the bar becomes a "will you make it to Friday" indicator instead — or once
per-item lead times exist, at which point the category default becomes a fallback rather than
the rule and the assumption line comes off the screen for any item that has been configured.
