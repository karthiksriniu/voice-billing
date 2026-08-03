# Positioning — Bolo Bill

Built with the three-track method: customer deep-dive, competitor moat deconstruction,
ecosystem pulse — then the intersection where a real unaddressed pain meets unclaimed
competitive space.

The baseline everyone shares: a UPI QR at the counter, a cheap Android phone, and a
shopkeeper who already knows exactly what he's selling. Nobody has a technology advantage
here. The wedge has to be operational, not technical.

---

## Track 1 — Customer deep-dive

**Prospective (the beachhead).** Tier-1 kirana, provision and vegetable shops in Chennai
billing on paper. One owner, sometimes one helper. 60–120 bills a day, mostly under ₹300.
Below the GST registration threshold, so compliance products are aimed over their heads.

**Churned — the group nobody has studied and the most valuable one.** Shops that bought an
electronic register or installed a billing app and went back to paper. They already tried to
solve this and rejected the solution. Whatever made them revert is the exact thing that will
make our pilots revert. **This is the highest-value research available and it is free** — they
are on the same street as the pilot shops. Go and find three.

**What triggers "paper is no longer enough".** Not speed — speed degrades gracefully and
they've adapted to it. The triggers are discrete, memorable failures:
- A dispute with a customer over a total nobody can reconstruct.
- Running out of a fast-moving SKU because reordering runs on memory.
- A customer typing the wrong amount into their UPI app, or paying and claiming they did.
- Putting a helper on the counter and having no way to know what was sold.

**What they say vs what they actually care about.** They say *"I want billing to be faster."*
What they act on is *not losing money* and *not looking foolish in front of a queue*. Speed is
what they can articulate; risk is what they respond to. **A pitch built on speed is arguing
with forty years of muscle memory. A pitch built on "you'll have a record and the customer
pays the exact amount" is arguing with nothing.**

---

## Track 2 — Competitor moat deconstruction

| Alternative | Their narrative | Where it rings hollow |
|---|---|---|
| **Pen, paper, calculator** | Free, instant, no battery, universally understood, zero learning | Leaves no record at all. Arithmetic errors under pressure. The total has to be shouted over traffic and hand-typed by the customer. |
| **Electronic register** (₹3–8k) | Prints a receipt, feels like a real shop | Item *index numbers*, not names — a memorised code per SKU. Setup is a day of work someone has to do for you. Nobody self-serves onto one. |
| **Billing apps** (Vyapar, myBillBook, OkCredit and similar) | "GST billing made easy", inventory, ledgers | **All of them require typing, and all require a catalog before the first bill.** Their narrative is aimed at a shop with a computer-literate owner and GST registration — one tier above our beachhead. For a Tier-1 shop the cold-start wall is the product. |
| **Paytm / PhonePe soundbox** | Confirms payment out loud, instantly, in the local language | Knows the *total* and nothing else. No items, no record, no reorder signal. It solves the last two seconds of the transaction and ignores the two minutes before it. |

**Read the table again and the crack is obvious.** Every software competitor has decided the
customer is someone who will type and who will set up a catalog first. That decision is what
puts Tier-1 shops out of reach for all of them simultaneously — and it isn't a feature gap
they can close with a sprint, because it's the assumption their whole product rests on.

**The competitor that matters is the soundbox.** Not because it competes on billing — it
doesn't — but because Paytm and PhonePe are *already on the counter*, already trusted with
money, already have merchant onboarding we can't do, and already own the payment-confirmation
moment `DECISIONS.md` D5 had to concede. If voice billing works, extending a soundbox is a
natural move for them. What they do not have is the billing moment or item-level data.

---

## Track 3 — Ecosystem & regulation pulse

- **UPI is now the counter's default**, which is what makes an exact-amount QR valuable rather
  than a novelty. This is the "why now" — it would not have worked in 2019.
- **Soundboxes are proliferating into exactly our shops**, funded by PSPs. They are
  normalising "the phone talks to me about money" — which softens the ground for spoken
  confirmation and simultaneously plants a competitor on the counter.
- **On-device ASR only recently became viable** on ₹7k hardware. The second half of the why-now.
- **ONDC and distributor digitisation** are pushing to instrument kirana supply, but from the
  distributor's side inward. Nobody has instrumented the shop's own sales.
- **DPDP Act 2023** raises the bar on recorded voice and any biometric derived from it — an
  argument for the no-retention default, and against ever building voiceprint recognition.
- **GST thresholds** keep Tier-1 shops out of compliance software's addressable market, which
  is precisely why the incumbents aim above them and why the segment stays unserved.

**The next battleground is the counter interface itself.** Whoever owns the moment of billing
owns item-level demand data, and therefore reordering, and eventually credit. Payments
companies own the *payment* moment. Distributor apps own the *supply* moment. **The billing
moment — where the shopkeeper says what he's selling out loud — is unowned.**

---

## Positioning intersection matrix

**Pain points ignored completely by everyone**
- Billing with **zero setup and zero typing**, for a shop whose catalog doesn't exist yet.
- Tamil–English code-mixed speech with colloquial measures as a first-class input.
- Operating at genuinely ₹0 marginal cost, which is what makes "free forever" survivable.

**Partially addressed, missing depth**
- *Record of sales* — billing apps do it, but only after you type a catalog and then every item.
- *Payment confirmation* — soundboxes do it, but for the total only, with no idea what was sold.
- *Reordering* — distributor apps do it, disconnected from what actually left the shelf.

**The intersection — the position to take**

> The moment a shopkeeper says *"rendu kilo sugar"* out loud is the only moment item-level data
> exists in a Tier-1 shop. Paper doesn't capture it. Soundboxes capture only the total.
> Billing software captures it only if you stop and type. **That moment is unowned, and it is
> the wedge.**

Everything defensible follows from owning it: the corrected-utterance corpus (which compounds
and can't be bought), the learned per-shop catalog (which becomes a switching cost within
weeks), and eventually the reorder signal that distributor partnerships would need.

## Positioning statement

> For **a Chennai kirana owner who bills on paper and has no record of what he sold**,
> **Bolo Bill** is a **voice billing app** that **turns what he already says out loud into an
> itemised bill and an exact-amount UPI QR**. Unlike **paper and a calculator**, it **leaves a
> record without adding a single step** — and unlike **Vyapar or myBillBook**, it needs **no
> catalog, no typing and no setup**.

## What to say, and what to stop saying

**Lead with:** "It writes the bill while you talk. The customer scans and pays the exact
amount. At closing you know what you sold." Three concrete, verifiable claims, none of which
requires beating a stopwatch.

**Stop leading with "faster than paper."** Keep it as an engineering gate — `PLAN.md`
principle 1 is correct and should not change — but it is a bad promise: it's the one claim
most likely to be false, it invites a comparison the customer will run in his head against
forty years of practice, and losing that comparison in the first thirty seconds loses the demo.

**Never claim payment confirmation.** We can't do it (D5), the soundbox on the same counter
can, and being caught overstating it next to a device that actually works would be
unrecoverable. Say plainly: "You tap when the money arrives, the same as you check today."
