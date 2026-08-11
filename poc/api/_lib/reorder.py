"""When to buy more, and how close the shop is to needing to.

The stock screen could already say what was on the shelf. It could not say whether that was
a lot or nearly nothing, which is the only question a shopkeeper actually asks of it. Four
kilos of beans and four hundred cups are the same word — "four hundred" — until you know
one is a fortnight's trade and the other is a day's.

So everything here is expressed in **days of cover**: how long the shelf lasts at the rate
this shop is actually selling. That makes beans and cups and milk directly comparable, and
it makes the reorder point a number in the same unit, which is what lets one bar carry both.

The model is the standard one (see DECISIONS D12 for sources):

    reorder point = average daily demand x lead time + safety stock

with two departures, both forced by what a shop this size can be asked for:

  **Demand is measured, never assumed.** It comes from this shop's own movement ledger —
  sales, and the components those sales consumed — divided by the number of days the ledger
  has actually been running. Not 90 days. A shop three days into using the app would
  otherwise have its demand divided by ninety and be told its beans will last a year.

  **Lead time is assumed, and said to be.** It belongs to a supplier relationship the app
  knows nothing about. A default per category is better than a single global number and far
  better than a blank, but it is a guess, and the screen says so rather than presenting it
  as measured. Per-item lead time is the premium configuration this stands in for.

Safety stock scales with ABC class, in the direction the classic analysis prescribes: an
A item is a large part of the money tied up on the shelf, so it is held tight and reordered
close to the wire; a C item is cheap to over-hold and expensive to run out of, so it carries
a fatter buffer. Expressed as a multiple of lead time rather than a Z-score, because the
demand variance a Z-score needs is not something this data can honestly supply yet.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

# Days from placing an order to it being on the shelf. Grounded in how Indian general trade
# actually replenishes: dairy and fresh goods daily, dry goods and packaging on a weekly
# distributor cycle. Ingredients sit between the two because the category holds both milk
# and coffee beans; packaging and resale goods come through the slower channel.
LEAD_DAYS = {"raw": 3, "consumable": 7, "resale": 7, "menu": 0}
DEFAULT_LEAD = 5

# Safety stock as a multiple of lead time, by ABC class. A items are held tight — they are
# where the shop's money is. C items are cheap to hold and painful to be without.
SAFETY_MULTIPLE = {"A": 0.5, "B": 1.0, "C": 1.5}

# Standard Pareto cut points on cumulative annual consumption value.
ABC_CUTS = ((0.80, "A"), (0.95, "B"))

# Below this the demand figure is noise, not a rate. Two days of trade cannot tell a shop
# how long its beans will last, and a bar drawn from it would be a confident fiction.
MIN_DAYS_OBSERVED = 3


def observed_days(ledger: list[dict], now: datetime | None = None) -> float:
    """How long the ledger has actually been running.

    The window the movements were fetched over is not the window they cover. Dividing a
    week's sales by ninety days understates demand by an order of magnitude, and understated
    demand reads as "you have plenty" — the one error this screen must not make.
    """
    now = now or datetime.now(timezone.utc)
    earliest = None
    for m in ledger:
        raw = (m.get("occurred_at") or "").replace("Z", "+00:00")
        if not raw:
            continue
        try:
            when = datetime.fromisoformat(raw)
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if earliest is None or when < earliest:
            earliest = when
    if earliest is None:
        return 0.0
    return max((now - earliest).total_seconds() / 86400.0, 0.0)


def classify(rows: list[dict]) -> dict[str, str]:
    """ABC by annual consumption value — demand x price, not price alone.

    A pack of straws at one rupee that turns over five hundred times a week is not a C item,
    and an expensive pot that sells twice a year is not an A. What matters is how much money
    flows through the line, which is the whole point of the classification.
    """
    valued = [(r["id"], float(r.get("daily_demand") or 0) * 365.0
               * float(r.get("unit_price") or 0)) for r in rows]
    total = sum(v for _, v in valued)
    if total <= 0:
        return {pid: "C" for pid, _ in valued}
    out, running = {}, 0.0
    for pid, value in sorted(valued, key=lambda x: -x[1]):
        running += value / total
        out[pid] = next((label for cut, label in ABC_CUTS if running <= cut), "C")
    return out


def annotate(rows: list[dict], days: float) -> list[dict]:
    """Add days-of-cover and a reorder point to each stock row.

    Rows are mutated in place and returned. A row that cannot honestly carry these numbers
    gets `state: "unknown"` and no bar, rather than a plausible-looking one.
    """
    enough = days >= MIN_DAYS_OBSERVED
    for r in rows:
        r["daily_demand"] = round(float(r.get("sold") or 0) / days, 6) if enough and days else 0.0
    abc = classify(rows)

    for r in rows:
        category = r.get("category") or "resale"
        lead = LEAD_DAYS.get(category, DEFAULT_LEAD)
        klass = abc.get(r["id"], "C")
        # Days of trade the shelf must cover: the wait for the order, plus a buffer sized by
        # how much of the shop's money this line represents.
        reorder_days = round(lead * (1 + SAFETY_MULTIPLE[klass]), 1)
        demand = r["daily_demand"]
        stock = float(r.get("stock") or 0)

        r["abc"] = klass
        r["lead_days"] = lead
        r["reorder_days"] = reorder_days
        r["reorder_level"] = round(demand * reorder_days, 3)

        if category == "menu":
            # Assembled at the moment of sale; there is no shelf to run down, so there is
            # no reorder point either. Nulled rather than left at zero — a zero would draw
            # as a bar with its flag jammed against the left edge, which reads as "order
            # this immediately" about a thing that is never ordered.
            r["state"] = "made_to_order"
            r["days_cover"] = None
            r["reorder_days"] = None
            r["reorder_level"] = None
            r["horizon_days"] = None
            continue
        elif not enough or demand <= 0:
            # No rate, so no forecast. Said plainly instead of drawn as a full green bar,
            # which is what "infinite cover" would otherwise look like.
            r["state"] = "unknown"
            r["days_cover"] = None
        else:
            cover = round(stock / demand, 1)
            r["days_cover"] = cover
            r["state"] = ("out" if stock <= 0
                          else "low" if cover <= reorder_days
                          else "ok")
        # The bar's full width. Anchored on the reorder point rather than a fixed number of
        # days so the flag lands in the same place on every row — the eye then reads one
        # question, "is the marker left of the flag", instead of re-scaling per item.
        r["horizon_days"] = round(max(reorder_days, 1) * 2.5, 1)
    return rows


def summarise(rows: list[dict]) -> dict:
    """What to buy, worst first. The screen's headline when nothing has gone missing."""
    need = [r for r in rows if r.get("state") in ("out", "low")]
    need.sort(key=lambda r: (r["state"] != "out", r.get("days_cover") if
                             r.get("days_cover") is not None else 0))
    return {"count": len(need),
            "items": [{"id": r["id"], "name": r["name"], "days_cover": r["days_cover"],
                       "state": r["state"],
                       # How much to buy: enough to clear the reorder point with the order
                       # cycle's worth on top, less what is already there.
                       "suggest": round(max(r["reorder_level"] * 2 - float(r.get("stock") or 0),
                                            r["reorder_level"]), 2),
                       "unit": r["unit"]} for r in need[:12]]}
