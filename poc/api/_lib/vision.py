"""Reading a shop's paperwork: a price list, a menu board, a supplier's invoice.

This is the one place in the product where a large model earns its cost, and the reason is
arithmetic rather than taste. The billing path runs ~24,000 utterances a shop a month
against a ₹10 ceiling, which puts cloud inference at zero rupees and keeps the parser
deterministic. A catalog import happens *once* when the shop signs up, and an inward
invoice perhaps weekly. Two orders of magnitude fewer calls buys the accuracy that reading
somebody's handwriting actually needs.

Three rules hold the blast radius down:

  1. **It extracts, it does not decide.** The model transcribes what is on the paper into
     rows. Which SKU a row *is* — whether "Sunflower Oil 1L" is the "Refined Oil" already
     in the catalog — is decided afterwards by the same phonetic matcher the voice path
     uses. The model never touches an existing price.
  2. **Nothing is written until the shopkeeper says so.** Every row comes back as a
     proposal with what it matched and how confidently. Import is a review screen, not an
     upload button. A price list read slightly wrong and applied silently would mis-bill
     every sale afterwards, which is precisely the failure this product cannot survive.
  3. **A number that cannot be read is dropped, never guessed.** Blank prices are counted
     and reported rather than filled in. A missing row is an annoyance; an invented price
     is a wrong bill.
"""

from __future__ import annotations

import base64
import os

MODEL = os.environ.get("VISION_MODEL", "claude-opus-5")

# Opus 5 pricing, in paise per token, so the shopkeeper can be shown what an import cost
# instead of being asked to trust that it was cheap. $5/$25 per million at ₹88/$.
IN_PAISE = 5 * 88 * 100 / 1_000_000
OUT_PAISE = 25 * 88 * 100 / 1_000_000

MAX_FILES = 5
MAX_BYTES = 12 * 1024 * 1024

# Units the catalog understands. Constraining the model to this list is what stops a menu
# reading "per plate" from creating a unit nothing downstream can convert.
UNITS = ["kg", "g", "litre", "ml", "piece", "packet", "dozen", "plate", "cup"]

CATALOG_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "unit": {"type": "string", "enum": UNITS},
                    "price": {"type": "number"},
                    "verbatim": {"type": "string"},
                },
                "required": ["name", "unit", "price", "verbatim"],
                "additionalProperties": False,
            },
        },
        "skipped": {"type": "integer"},
    },
    "required": ["items", "skipped"],
    "additionalProperties": False,
}

INWARD_SCHEMA = {
    "type": "object",
    "properties": {
        "supplier": {"type": "string"},
        "invoice_no": {"type": "string"},
        "invoice_date": {"type": "string"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "qty": {"type": "number"},
                    "unit": {"type": "string", "enum": UNITS},
                    "rate": {"type": "number"},
                    "verbatim": {"type": "string"},
                },
                "required": ["name", "qty", "unit", "rate", "verbatim"],
                "additionalProperties": False,
            },
        },
        "skipped": {"type": "integer"},
    },
    "required": ["supplier", "invoice_no", "invoice_date", "items", "skipped"],
    "additionalProperties": False,
}

# Written as a transcription brief, not an analysis brief. Every sentence that invites the
# model to be helpful — to tidy a name, to work out a missing price, to convert a unit it
# was not given — is a sentence that puts a number on a bill that nobody wrote down.
COMMON = """You are reading paperwork from a small Indian retail shop — a kirana, a
provision store, a small eatery. It may be a printed price list, a handwritten register
page, a photographed menu board, or a supplier's invoice. Names may be in English, Tamil,
Hindi, Malayalam, Telugu or Kannada, or a mix, and are often abbreviated the way the shop
itself writes them.

Transcribe. Do not interpret.

- Copy each product name as written. Do not expand abbreviations, correct spellings,
  translate, or make names consistent with each other. "Sug." stays "Sug.", not "Sugar".
- `verbatim` is the raw line exactly as it appears on the paper, including the numbers.
  `name` is just the product part of that line.
- Never invent a number. If a price or quantity is smudged, cut off, ambiguous, or simply
  absent, leave that row out entirely and add one to `skipped`. A missing row is fine. A
  guessed number is not.
- Numbers may use Indian digit grouping (1,20,000) or a regional numeral script. Return
  them as plain decimals.
- Prices are in rupees. Drop the ₹ or Rs. Ignore any tax column, discount column, or
  running total — only the per-unit figure matters.
- Pick the closest unit from the allowed list. If the paper states no unit at all, use
  "piece" for countable goods and "plate" or "cup" for prepared food.
- Skip anything that is not a sellable line: headers, column titles, subtotals, GST rows,
  page numbers, the shop's own name and address, "thank you" lines."""

CATALOG_TASK = """
This is a price list or menu. Return one row per sellable item with its selling price."""

INWARD_TASK = """
This is a supplier invoice or a goods-received note. Return one row per item delivered,
with the quantity received and the per-unit purchase rate.

`qty` is how much arrived. If the invoice says "5 bags x 25 kg", that is 125 kg, not 5.
`rate` is the price of one unit, not the line total — if only a line total is printed,
divide it by the quantity. If the supplier's name, invoice number or date is not visible,
return an empty string for it; do not guess."""


def _block(data: bytes, mime: str) -> dict:
    b64 = base64.standard_b64encode(data).decode()
    if mime == "application/pdf":
        return {"type": "document",
                "source": {"type": "base64", "media_type": mime, "data": b64}}
    return {"type": "image",
            "source": {"type": "base64", "media_type": mime, "data": b64}}


async def read(files: list[tuple[bytes, str]], kind: str) -> dict:
    """Turn photographs or a PDF into proposed rows. Returns rows plus what it cost.

    Failure is returned, never raised or swallowed: an import that quietly produced nothing
    looks identical to a shop with no products, and the shopkeeper needs to be told which
    one happened.
    """
    if not files:
        return {"ok": False, "error": "No file"}
    if len(files) > MAX_FILES:
        return {"ok": False, "error": f"At most {MAX_FILES} pages at a time"}
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {"ok": False, "error": "ANTHROPIC_API_KEY not set"}

    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        return {"ok": False, "error": "anthropic package not installed"}

    schema = INWARD_SCHEMA if kind == "inward" else CATALOG_SCHEMA
    task = INWARD_TASK if kind == "inward" else CATALOG_TASK

    # Documents first, instruction last: a model reads the pages before it is told what to
    # do with them, and the task then applies to everything above it.
    content: list[dict] = [_block(data, mime) for data, mime in files]
    content.append({"type": "text", "text": COMMON + task})

    client = AsyncAnthropic(timeout=90.0, max_retries=1)
    try:
        # Streamed because a forty-line price list is a long generation and a non-streaming
        # request that outlives the HTTP timeout returns nothing at all — the one outcome
        # worse than a slow import.
        async with client.messages.stream(
            model=MODEL,
            max_tokens=16000,
            # Low effort on purpose. This is transcription, not reasoning: the answer is
            # on the page. Higher effort spends thinking tokens deliberating over a smudge
            # that the instructions already say to skip, and adds seconds to a request
            # that runs inside a 60-second serverless ceiling.
            output_config={"effort": "low",
                           "format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": content}],
        ) as stream:
            msg = await stream.get_final_message()
    except Exception as err:                                   # noqa: BLE001
        return {"ok": False, "error": f"{type(err).__name__}: {err}"[:200]}

    # A refusal arrives as a successful response with an empty body. Reading content[0]
    # without checking would raise an IndexError that reads like a bug in our code.
    if msg.stop_reason == "refusal":
        return {"ok": False, "error": "The model declined to read this document"}

    import json
    text = next((b.text for b in msg.content if b.type == "text"), "")
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return {"ok": False, "error": "Could not read the document"}

    u = msg.usage
    paise = round(u.input_tokens * IN_PAISE + u.output_tokens * OUT_PAISE)
    return {"ok": True, "kind": kind, "data": data, "cost_paise": paise,
            "truncated": msg.stop_reason == "max_tokens"}
