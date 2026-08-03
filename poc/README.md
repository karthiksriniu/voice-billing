# Vaakku PoC

Demo-grade voice billing: hold the button, speak items, get an exact-amount UPI QR.
Python + FastAPI on Vercel, Sarvam for speech, Supabase for storage.

**This is not the product architecture.** Cloud ASR and a server are exactly what the ₹10/₹0
cost ceiling and the offline requirement rule out (`docs/PLAN.md`). It's a demo vehicle and a
corpus collection instrument. What survives into Phase 1 is `api/_lib/parser.py`,
`api/_lib/lang/*.json` and `tests/test_parser.py` — nothing else.

## Run locally

```bash
cd poc && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt uvicorn
```

```bash
cd poc && .venv/bin/uvicorn api.index:app --reload --port 8077
```

Open http://localhost:8077. With no API key it runs in text mode — the pipeline works, the
mic doesn't. `GET /api/health` reports which backends are live.

```bash
cd poc && python3 tests/test_parser.py
```

## Environment

| Variable | Needed | Notes |
|---|---|---|
| `SARVAM_API_KEY` | for voice | Without it the app falls back to text mode rather than failing. |
| `SARVAM_MODEL` | no | Defaults to `saarika:v2.5`. **Verify against current Sarvam docs** — the API surface moves and the default may be stale. |
| `SARVAM_LANGUAGE` | no | Defaults to `ta-IN`. |
| `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` | no | Without them the catalog loads from `seed/catalog.csv` in memory and bills aren't persisted. The demo still runs end to end. |
| `ASSUMED_ASR_CONFIDENCE` | no | Sarvam returns no confidence score, so this stands in. Lower it to make the confirm gate stricter. |

## Deploy

Supabase first: paste `supabase/schema.sql` into the SQL editor. RLS is on with no public
policy — the service key bypasses it, and that key must stay server-side only.

Then, from `poc/`:

```bash
npx vercel --prod
```

Set the environment variables in the Vercel project settings before the first real use. I
can't deploy this for you — it needs your account credentials.

## How it works

```
hold button → MediaRecorder (webm/opus) → POST /api/transcribe
            → Sarvam → transcript → parser → line items + confidence verdict → UI
```

**Parser** (`api/_lib/parser.py`) is a deterministic grammar, no LLM. It handles weight-led
("two kilo sugar"), price-led ("ten rupees coriander"), count-led ("rendu packet biscuit"),
colloquial fractions (arai, kaal, mukkaal, pav), compound Tamil numerals ("irubathi anju" =
25), unit conversion (500 g against a per-kg price), and multi-item utterances. All language
data is in `lang/ta-en.json` — a second language is a sibling file, not a code change.

**Confidence gate.** Every line lands in accept / confirm / reject. Confirm items render
amber and **block the Finalise button** until resolved. This is the PoC's most important
behaviour: an unresolved item must never quietly become part of an amount a customer is
asked to pay.

**Payment is confirmed manually** — the shopkeeper taps "Received". The app cannot detect
UPI payment and the UI says so in Tamil and English. See `docs/DECISIONS.md` D5.

## Deliberately not built

- **Speaker recognition.** Push-to-talk already scopes capture, and while the button is held
  the owner is the closest, loudest source. Verification from short enrollment in 70–80 dB
  noise is research-grade, and its failure mode — rejecting the owner — is worse than the
  problem it solves. It also drags voiceprints into DPDP-Act territory.
- **Phone/OTP auth and multi-user enrollment.** Commercial SMS in India needs TRAI DLT
  registration of entity, sender ID and templates. Days of compliance work that demonstrates
  nothing about whether voice billing works.
- **Excel catalog upload as a user feature.** It contradicts "no PC". Keep it as an *ops*
  tool: preload the shop's real SKUs into `seed/catalog.csv` before the meeting, so the demo
  runs on their own products. That's worth more than any polish.

## Known limits

- Sarvam returns no confidence score, so the gate leans almost entirely on catalog match
  quality. Worth noting for Phase 1: Vosk *does* expose per-token confidence, which is a
  point in its favour that has nothing to do with accuracy.
- Serverless cold start adds ~1–2 s to the first utterance. The page pings `/api/health` on
  load to warm it; do that before walking into the shop too.
- Admin mode currently updates the price of an item **already in the catalog**
  ("sugar nooru rubai"). Creating a brand-new SKU by voice needs a name-capture flow that
  doesn't exist yet.
- Latency here is not the product's latency. `/api/transcribe` reports `asr_ms` and
  `parse_ms` separately for that reason — the parse stage is the only number comparable to
  the budget in `PLAN.md`, and it runs in ~1 ms.
