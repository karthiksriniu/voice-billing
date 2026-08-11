"""What goes into an Americano.

A shop that sells prepared food has a stock problem no per-SKU ledger can see. Nobody buys
an Americano off a shelf — it is assembled at the moment of sale out of beans, water, ice,
a cup and a lid, and those are the things that run out, get spilled and walk out of the
door. Until a sale can be exploded into what it consumed, the shrinkage screen is blind to
the entire input side of a café.

The recipe is the missing link, and it is genuinely tedious to enter: twenty menu items
times five components each, in grams, on a phone. But it is also close to general
knowledge — an Americano is a double shot and hot water almost everywhere — so a model can
draft it and the shopkeeper can correct the two lines that are wrong for their shop. That
is a far better trade than an empty screen.

The same three rules as document import apply, for the same reason:

  1. **It proposes against the shop's own shelf.** The model is handed the exact component
     list with ids and units and may only return those ids; the schema constrains it, and
     the server drops anything that still slips through. It cannot invent an ingredient
     the shop does not stock.
  2. **It writes nothing.** A draft is returned for editing. A recipe applied silently
     would start consuming stock on every subsequent sale from numbers nobody read.
  3. **Quantities come back in the component's own stock unit.** No unit conversion at
     consumption time — the sale path is a multiplication. Asking for "0.018 kg" rather
     than "18 g" reads oddly in a prompt but removes the class of bug that once billed
     500 g of a Rs50/kg item as Rs25,000.
"""

from __future__ import annotations

import json
import os

MODEL = os.environ.get("RECIPE_MODEL", "claude-opus-5")

IN_PAISE = 5 * 88 * 100 / 1_000_000
OUT_PAISE = 25 * 88 * 100 / 1_000_000

MAX_COMPONENTS = 400


def schema_for(ids: list[str]) -> dict:
    """An enum of the shop's real component ids. This is the guardrail that matters: a
    hallucinated ingredient cannot be expressed in the response at all."""
    return {
        "type": "object",
        "properties": {
            "components": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "component_id": {"type": "string", "enum": ids},
                        "qty": {"type": "number"},
                        "why": {"type": "string"},
                    },
                    "required": ["component_id", "qty", "why"],
                    "additionalProperties": False,
                },
            },
            "note": {"type": "string"},
        },
        "required": ["components", "note"],
        "additionalProperties": False,
    }


PROMPT = """You are drafting a recipe — a bill of materials — for one item on a small
Indian café or eatery's menu, so the shop can work out what each sale takes off its shelf.

Menu item: {item}
{hint}

These are the only things this shop stocks. Use their ids exactly. `unit` is the unit that
component's stock is counted in, and your `qty` must be a number in THAT unit:

{components}

Rules:

- `qty` is what ONE serving of the menu item consumes, in the component's own unit. If
  beans are stocked in kg and a double shot is 18 grams, return 0.018 — not 18.
- Include the packaging a takeaway order actually uses: a cup, a lid, a straw, a stirrer, a
  napkin — but only where such a component exists in the list above, and only where that
  item would really use it. An espresso does not get a straw.
- Leave out anything you are unsure about, and say so in `note`. A missing component is a
  gap the shopkeeper can see and fill. A wrong one silently drains their stock on every
  sale, and they will not find it until they count.
- Do not include the menu item itself, or any other prepared item, as a component.
- Water from the tap, gas, and electricity are not stocked components. Skip them.
- Use ordinary Indian café portions. If the item's name states a size or a count ("large",
  "300 ml", "two piece"), scale to that.
- `note` is one short sentence for the shopkeeper, in plain English: what you assumed, and
  what you left out for them to add. Empty string if there is nothing worth saying."""


async def check() -> dict:
    """Is the key actually usable?

    Presence and validity are different questions, and only the first one is free to
    answer. A key that was rotated, mistyped, or set on the wrong Vercel environment reads
    as configured and then fails at the moment a shopkeeper taps a button — as an error
    message about an API they have never heard of. One real call, a handful of tokens, and
    the answer is definite.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {"ok": False, "state": "missing",
                "detail": "No key is set on this deployment"}
    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        return {"ok": False, "state": "missing", "detail": "anthropic package not installed"}
    try:
        client = AsyncAnthropic(timeout=20.0, max_retries=0)
        msg = await client.messages.create(
            model=MODEL, max_tokens=16,
            # Nothing to think about, so nothing is spent thinking. This is a reachability
            # probe, not a question.
            thinking={"type": "disabled"},
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": "Reply with the word: ready"}],
        )
    except Exception as err:                           # noqa: BLE001
        name = type(err).__name__
        # Told apart because they need different things from the shopkeeper: a bad key is
        # a key to replace, a rate limit is a minute to wait, and a network fault is
        # neither of those and not their fault at all.
        state = ("bad_key" if "Authentication" in name or "PermissionDenied" in name
                 else "rate_limited" if "RateLimit" in name
                 else "unreachable")
        return {"ok": False, "state": state, "detail": f"{name}: {err}"[:180]}
    u = msg.usage
    return {"ok": True, "state": "ready", "model": msg.model,
            "cost_paise": round(u.input_tokens * IN_PAISE + u.output_tokens * OUT_PAISE, 2)}


async def propose(item: str, components: list[dict], hint: str = "") -> dict:
    """Draft a bill of materials for one menu item. Returns the draft, never writes it."""
    if not item.strip():
        return {"ok": False, "error": "No item"}
    if not components:
        # Worth its own message: the honest answer is that the shelf is empty, not that
        # the model failed. A shop has to say what it stocks before anything can consume it.
        return {"ok": False, "error": "no_components"}
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {"ok": False, "error": "ANTHROPIC_API_KEY not set"}

    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        return {"ok": False, "error": "anthropic package not installed"}

    components = components[:MAX_COMPONENTS]
    ids = [c["id"] for c in components]
    listing = "\n".join(
        f"- id={c['id']} | name={c['name']} | unit={c['unit']}"
        + (f" | {c['category']}" if c.get("category") else "")
        for c in components)

    client = AsyncAnthropic(timeout=60.0, max_retries=1)
    try:
        async with client.messages.stream(
            model=MODEL,
            max_tokens=8000,
            # A recipe is recall plus a little arithmetic, not deliberation, and this runs
            # inside a serverless request the shopkeeper is watching. Low effort keeps it
            # to a few seconds; the shopkeeper's edit is the accuracy backstop, not depth
            # of reasoning about how much milk goes in a cappuccino.
            output_config={"effort": "low",
                           "format": {"type": "json_schema", "schema": schema_for(ids)}},
            messages=[{"role": "user", "content": PROMPT.format(
                item=item.strip(),
                hint=(f"What the shop calls it / how it is served: {hint.strip()}"
                      if hint.strip() else ""),
                components=listing)}],
        ) as stream:
            msg = await stream.get_final_message()
    except Exception as err:                           # noqa: BLE001
        return {"ok": False, "error": f"{type(err).__name__}: {err}"[:200]}

    if msg.stop_reason == "refusal":
        return {"ok": False, "error": "The model declined this one"}

    text = next((b.text for b in msg.content if b.type == "text"), "")
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return {"ok": False, "error": "Could not draft a recipe"}

    # Belt and braces over the enum: an id that is not on this shop's shelf is dropped
    # rather than carried, and a non-positive quantity is not a component.
    known = set(ids)
    out = []
    for c in data.get("components", []):
        cid = str(c.get("component_id") or "")
        try:
            qty = float(c.get("qty") or 0)
        except (TypeError, ValueError):
            continue
        if cid in known and qty > 0:
            out.append({"component_id": cid, "qty": qty, "why": (c.get("why") or "")[:120]})

    u = msg.usage
    return {"ok": True, "components": out, "note": (data.get("note") or "")[:300],
            "cost_paise": round(u.input_tokens * IN_PAISE + u.output_tokens * OUT_PAISE)}
