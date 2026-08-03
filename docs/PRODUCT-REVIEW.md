# Product review — Bolo Bill

Full PM evaluation of the product and the proposed PoC. Written against `PLAN.md`,
`DECISIONS.md`, `RISKS.md` and the nine-point PoC design.

## 1. Idea summary

A voice-first billing app for Tier-1 Indian retail — shops that bill on a scrap of paper
with a calculator. The shopkeeper speaks items as he hands over goods; the app builds an
itemised bill, announces the total, and shows a UPI QR carrying the exact amount. Phase 4
reuses the same pipeline for inventory and distributor reordering. Hard constraints are
unusually well specified: on-device only, ₹10/shop/month marginal cost with ₹0 cloud
inference, offline-first, Play-Store-only self-serve distribution, Tamil-English code-mixed.

**Positioning statement.** For a Chennai kirana owner who bills on paper and has no record
of what he sold, Bolo Bill is a voice billing app that turns what he already says out loud into
an itemised bill and an exact-amount UPI QR. Unlike paper and a calculator it leaves a record
without adding a step; unlike Vyapar or myBillBook it needs no catalog, no typing and no setup.

**Riskiest assumption.** Not the ASR — that's the riskiest *technical* assumption. The
riskiest *business* assumption is that a shopkeeper with forty years of muscle memory will
change how he bills at all. Everything else is downstream of that.

## 2. Verdict at a glance

| Dimension | Verdict | Why |
|---|---|---|
| Business Model Canvas completeness | **Partial** | Cost structure and segments are rigorous; revenue, partnerships and channels are thin or deferred. |
| Value proposition & positioning | **Partial** | Sharp and specific, but the core claim ("faster than paper") rests on a baseline nobody has measured. |
| ICP & path to PMF | **Partial** | Beachhead is narrow, named and reachable — but no threshold defines "they want this", only "they can use it". |
| Low-touch / viral adoption | **Partial** | Time-to-value is designed hard; the exposure mechanic doesn't exist until Phase 3. |
| Toothbrush test | **Strong** | 100+ uses per day. About as habitual as consumer software gets. |
| Competitive moat & defensibility | **Partial** | The corpus and per-shop learned catalog compound; the app itself is a quarter's work to copy. |
| Unit economics & riskiest assumption | **Partial** | Cost side is better than most seed-stage companies manage. Revenue side is deliberately blank, which creates a structural tension (see §9). |
| Legal, regulatory & trust | **Partial** | Play policy and bystander audio are handled thoughtfully. DPDP-Act exposure and the PoC's cloud-audio shift are not yet addressed. |
| Market & customer research | **Weak** | No sizing, no structured discovery, no churned-user research. The single biggest gap. |
| Product design | **Strong** | Explicit non-goals, principles that decide arguments, gates that can stop a phase. Above average. |
| New product development | **Partial** | Phasing and pre-launch metrics are strong; GTM and pricing are absent. |
| Post-sales engagement & support | **Weak** | Effectively unconsidered, and unusually hard for this user. See §11.4. |

## 3. Business Model Canvas

**Customer Segments — Strong.** "Tier-1 paper-billing kirana in Chennai, explicitly not
electronic registers or PC POS" is proper beachhead discipline. Most ideas at this stage say
"small retailers"; this one names who it is *not* for, which is the harder half.

**Value Propositions — Partial.** Specific and testable, but see §4: the headline benefit may
be the wrong one. Note also that there are two distinct segments hiding here — the owner
(wants records and no arithmetic errors) and, from PoC point 9, an employee at the counter
(wants speed and not being blamed). They need different value props and currently share one.

**Channels — Partial, trending Weak.** Play Store self-serve is the stated channel, but *how
a kirana owner discovers the app exists* is unanswered. This user does not browse the Play
Store for business software. The attribution footer that would solve it doesn't ship until
Phase 3, so Phases 1–2 have no discovery path at all beyond walking into shops.

**Customer Relationships — Strong.** Zero-touch self-serve, coherent with a free-to-₹10 price
point. Rare internal consistency — many plans assume white-glove onboarding at a price point
that can't fund it.

**Revenue Streams — Unknown, by choice.** Deferred deliberately. Fine for now, but see §9 for
why "free forever" is not cost-free as a decision.

**Key Resources — Partial, and under-recognised.** The plan treats the Tamil billing-speech
corpus as a *test fixture*. It is actually the most valuable asset in the business: nobody
has one, it can't be bought, and it compounds with every corrected utterance. Reclassify it.

**Key Activities — Partial.** Described at the "build the app" level. The recurring operating
loop that actually matters — collect corrections, retrain/tune, ship, measure silent error
rate — isn't named as the core activity it is.

**Key Partnerships — Weak.** Two unacknowledged single points of failure owned by others:
Google Play (which has *already* removed the notification-listener option) and the UPI apps
honouring `upi://pay` deep links against a personal VPA (Risk 1, still untested). Neither is
framed as platform risk in the plan; both should be.

**Cost Structure — Strong.** The ₹10/₹0 ceiling derived bottom-up from 24,000 utterances per
shop per month, then used to eliminate options rather than decorate them, is the best-reasoned
part of the whole plan.

## 4. Value proposition & positioning

**Jobs.** Functional: total a basket correctly and take payment. Social: look competent and
unhurried in front of a queue. Emotional: not lose money to arithmetic, not be cheated by an
employee, know what sold today.

**Pains today.** Arithmetic errors under time pressure. No record of what sold, so reordering
is memory. Reading the total aloud and hoping the customer types it into their UPI app
correctly. Disputes over a total the customer can't verify.

**The challenge to your headline.** Paper is *fast*. An experienced shopkeeper with a
calculator is genuinely quick, and your own `PLAN.md` concedes the baseline is unmeasured.
Betting the product on "faster than paper" is betting on the one claim most likely to be
false. Meanwhile two benefits are undeniable, immediate, and don't depend on beating a
stopwatch:

1. **The exact-amount UPI QR** removes a real, daily, irritating failure — the customer
   typing the wrong amount, or the shopkeeper repeating the total three times over traffic.
2. **A record of the day appears for free**, as a by-product of billing rather than a second
   chore. Nothing on paper does this.

**Painkiller or vitamin?** On speed: vitamin, possibly, and unproven. On the UPI amount and
the sales record: painkiller. **I'd lead with those and treat speed as a constraint to not
violate rather than the promise.** Principle 1 stays exactly as written — "faster than paper
or it doesn't ship" is the right *gate*. It's the wrong *pitch*.

**Evidence bar — Partial.** Informal conversations with shopkeepers you know. That's a real
starting point and better than nothing, but it is not Mom-Test discovery: it hasn't asked what
they did last time billing went wrong, what they've already tried and abandoned, or what they
currently pay for. Until it has, every pain above is inferred.

## 5. ICP & path to PMF

**Partial.** The beachhead is narrow, reachable and already accessible to you — genuinely
strong. What's missing is a definition of success that measures *desire* rather than
*usability*. The Phase 2 gate asks "can 4 of 5 people complete setup unassisted" and "is
silent error under 0.5%". Both are necessary; neither tells you anyone wants it.

Add a demand threshold before Phase 3: of the pilot shops, how many are still billing on it
on day 30 without being asked, and would they be "very disappointed" if it were taken away
(Sean Ellis, 40% bar)? Forty shopkeepers who reopen it every morning beats four hundred
installs. Retention curve flattening is the signal; installs are not.

## 6. Low-touch / viral adoption

**Partial.** Time-to-value is designed to be under five minutes and the plan takes that
seriously enough to gate on it. But there is no built-in exposure mechanic until the Phase 3
printed footer, which means Phase 2 growth data will be uninterpretable — the plan already
says this, correctly.

**A viral surface you're not using.** Every single bill ends with a customer looking at the
shopkeeper's screen to scan a QR. That is dozens of impressions per shop per day, from day
one, on a screen you fully control — and some of those customers own shops themselves or
know someone who does. The printed receipt is a *worse* version of an exposure mechanic you
already have and haven't claimed. Put a small, dignified attribution and a short link on the
QR screen in Phase 2. Cost: near zero. It moves the viral loop two phases earlier.

Buying complexity is a genuine strength: one person decides, no procurement, no integration.

## 7. Toothbrush test

**Strong.** 100+ bills a day is the top of the frequency scale. The risk here is inverted —
not "will they forget it exists" but "every one of those interactions is a chance to break
trust". At that frequency a 1% silent error rate means roughly one wrong bill per shop per
day, which is why the ≤0.5% target is the right obsession.

## 8. Competitive moat & defensibility

**Partial.** The app is copyable in a quarter. Two things aren't:

- **The corrected-utterance corpus.** Tamil code-mixed billing speech in real shop noise, with
  gold labels supplied by the user's own corrections. It compounds with usage, and a
  competitor can't buy it.
- **The learned per-shop catalog.** After a few weeks a shop's aliases, prices and phrasing
  live in the app. That's a real switching cost that builds silently.

**The threat isn't another startup.** It's PhonePe or Paytm. They already have hardware on
these counters, an existing trust relationship, the merchant onboarding we can't do, and — via
the soundbox — they already own the payment-confirmation moment we've had to concede. If
voice billing works, extending a soundbox to do it is a natural move for them. What they don't
have is the *billing* moment or item-level data. That's the ground to take and hold quickly.

## 9. Unit economics & riskiest assumption

**Partial.** The cost side is rigorous. The revenue side is blank by choice, and that has a
consequence worth stating: **at ₹0 revenue, any non-zero CAC gives an unrecoverable LTV:CAC
ratio.** "Free forever, no field force" is only coherent if growth is genuinely viral — which
makes the §6 exposure mechanic a survival requirement, not a nice-to-have. The plan currently
defers monetisation *and* defers the viral loop to Phase 3. One of those has to move.

**Riskiest assumption and its cheapest test.** "A shopkeeper will change a 40-year habit."
The cheapest test is not an app — it's a Wizard of Oz. Sit in a shop with the PoC and, if
the ASR stumbles, have a human silently correct the transcript. The shopkeeper can't tell the
difference. What you learn is whether he keeps picking the phone up on day three, which is the
only question that matters and is completely independent of whether the technology works yet.
Cost: two afternoons.

## 10. Legal, regulatory & trust

**Partial.** Handled well: Play policy constraints (correctly treated as fatal, not
negotiable), no-audio-retention by default, opt-in donation limited to corrected utterances,
and separate paid consent for the field corpus. That's a more careful privacy posture than
most products this size.

Not yet addressed:

- **The PoC inverts the privacy story.** A webapp streaming audio to Sarvam is the opposite of
  "no audio leaves the device". That's acceptable for a demo, but the consent screen must say
  so in plain Tamil, and the production consent copy must not be reused.
- **Voice biometrics.** If speaker recognition (PoC point 3) is ever built, voiceprints are
  plausibly sensitive personal data under India's DPDP Act 2023 and carry storage and consent
  obligations the rest of the design avoids. Another reason to cut it — see §11.2.
- **SMS/OTP.** Commercial SMS in India requires TRAI DLT registration of entity, sender ID and
  templates. Verify current requirements before building any OTP flow.
- **DPDP Act status.** The Act passed in 2023 with rules phasing in; confirm the current
  compliance position before the pilot, since the corpus involves recorded speech from
  identifiable people.

## 11. PM 101 deep dive

### 11.1 Market & customer research — **Weak**

The biggest gap in an otherwise strong plan. No TAM/SAM/SOM, even bottom-up. No structured
discovery. No competitive analysis of the actual software incumbents (Vyapar, myBillBook,
OkCredit and the rest reach millions of Indian retailers — the plan doesn't mention them, and
"we compete with paper" is only true for the beachhead, not for the market you'd expand into).

Most valuable and completely unexplored: **churned users.** Shops that bought an electronic
register or installed a billing app and went back to paper. They already tried to solve this
and rejected the solution. Why they reverted is the single most informative thing available,
and it's free — they're on the same street as your pilot shops.

"Why now" is genuinely strong and worth stating explicitly in any pitch: UPI is now universal
at the counter, cheap Android is ubiquitous, and on-device ASR only recently became viable.
This would not have worked in 2019.

### 11.2 Product design — **Strong**

Explicit non-goals, principles framed to settle arguments, phase gates that can stop work,
decisions recorded with reversal conditions. This is better than most funded teams manage.

On the PoC design specifically: points 1, 2, 4, 5, 6, 7 are well-judged. Push-to-talk is the
right call for a demo even though `DECISIONS.md` D2 prefers session VAD in the product — PTT
is the documented fallback and it removes a variable from the demo. The mode keyword (point 4)
is cheap and good. Excel catalog upload (point 5) contradicts "no PC" as a *user* feature, but
as an *ops* tool it's excellent: preload the shop's real SKUs before the meeting so the demo
runs on their own products, which is worth more than any polish.

Two to change:

- **Speaker recognition (point 3) — cut it.** Speaker verification from short enrollment, in
  70–80 dB noise, with overlapping speech, is research-grade. Push-to-talk already scopes
  capture, and while the button is held the owner is the closest, loudest source by a wide
  margin. What it adds is a failure mode strictly worse than the one it fixes: falsely
  rejecting the owner. It also drags in voice-biometric data obligations (§10).
- **OTP auth (point 1) — skip for the PoC.** DLT registration is a multi-day compliance
  detour and authentication demonstrates nothing about whether voice billing works.

Point 8's "Paytm-style receipt confirmation" deserves a flag: the soundbox works *because
Paytm is the PSP*. We are not, so the PoC must confirm manually. Your instinct to want that
moment is exactly right, and it confirms Risk 2's ranking.

### 11.3 New product development — **Partial**

Phasing is realistic and gated. Pre-launch success metrics are defined, which most teams skip.
Missing: any GTM plan beyond "Play Store", and pricing (deferred by choice). The phase
sequencing has one ordering problem already noted — the viral loop sits behind the printer.

### 11.4 Post-sales engagement & support — **Weak**

Essentially unconsidered, and harder here than almost anywhere. The support model has to work
for a user who **cannot type, may not read comfortably, has no PC, and has no field agent** —
while the business model funds no support staff at all. "Self-serve docs" is not an answer for
this user.

Concretely unanswered: what does a shopkeeper do at 9 a.m. when the app won't open and there's
a queue? He reverts to paper permanently — that's the churn event, and it will never generate
a support ticket, so you won't even know it happened.

Worth designing early, and cheaply: an in-app voice-note help button that records a complaint
and queues it (no typing, no reading), a visible "paper mode" fallback that doesn't feel like
failure, and a retention alarm that fires when a shop's daily bill count drops to zero — the
only churn signal you'll get.

## 12. Key gaps & open questions

1. What is the actual paper baseline — seconds per bill, measured, not assumed? The headline
   claim depends on it and it's still unmeasured.
2. Why did shops that bought an electronic register or a billing app go back to paper?
3. Does `upi://pay` with a pre-filled amount work against a personal VPA across the four major
   apps? (Risk 1 — the test sheet is built and unrun.)
4. Would a pilot shopkeeper be "very disappointed" if this were taken away at day 30?
5. How does a shopkeeper discover this app exists, before Phase 3 attribution?
6. What is the bottom-up SOM — how many Tier-1 shops in Chennai, realistically reachable?
7. How does a user who can't type get help when the app breaks?
8. If growth must be free (₹0 revenue), what carries acquisition before the printed footer?

## 13. Recommended next steps

Ordered by validation value per unit of effort.

1. **Run the UPI QR test.** Already built, ₹0, one afternoon, and it can invalidate D5 before
   any further work. Nothing should be built on top of an unverified payment path.
2. **Interview 10 shopkeepers, including 3 who tried something and reverted.** Mom Test rules:
   ask what happened last time a bill went wrong, what they've already tried, what they pay for
   today. Do not describe the app until the end. This closes the largest gap in the plan and
   costs a day.
3. **Time 30 real paper bills while you're there.** Settles whether "faster than paper" is a
   promise or a liability, and it's free once you're in the shop.
4. **Wizard-of-Oz the habit question with the PoC.** Two afternoons in one shop, human backstop
   behind the ASR. Measures whether he picks the phone up on day three — the riskiest
   assumption, tested independently of whether the technology is ready.
5. **Move attribution onto the QR screen in Phase 2.** Near-zero cost, and it pulls the only
   viable acquisition loop two phases forward, which the ₹0-revenue model structurally needs.
