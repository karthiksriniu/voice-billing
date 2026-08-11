"""Where one trading day ends and the next begins.

Postgres stores `created_at` in UTC. A shop reads its own numbers in IST. Those two facts
are five and a half hours apart, and the gap lands squarely inside the hours a shop is
open: a bill rung up at 00:30 IST happened at 19:00 UTC the day before. Compute a day
boundary in UTC and every late-evening sale gets filed under yesterday, which understates
today and overstates a day that is already closed. Nobody notices until a shopkeeper counts
his own drawer and it disagrees with the app.

So every window here is built in IST and only then converted to the UTC instants the query
needs. Windows are half-open — `start <= t < end` — so a bill can never be counted twice by
two adjacent periods, which is what makes the daily figures sum to the monthly one.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))


def now_ist() -> datetime:
    return datetime.now(IST)


def _utc(d: date, days: int = 0) -> str:
    """An IST midnight, as the UTC instant a timestamptz comparison wants."""
    start = datetime(d.year, d.month, d.day, tzinfo=IST) + timedelta(days=days)
    # "Z" rather than "+00:00": the same instant, but a "+" travelling through a query
    # string decodes as a space unless every layer escapes it, and one that forgets turns a
    # date filter into a parse error at the far end.
    return start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def day_bounds(when: datetime | None = None) -> tuple[str, str]:
    d = (when or now_ist()).astimezone(IST).date()
    return _utc(d), _utc(d, 1)


def week_bounds(when: datetime | None = None) -> tuple[str, str]:
    """Monday to Monday. Not a universal convention, but it is the one Indian retail
    reporting and every accountant a shop deals with already uses."""
    d = (when or now_ist()).astimezone(IST).date()
    monday = d - timedelta(days=d.weekday())
    return _utc(monday), _utc(monday, 7)


def month_bounds(when: datetime | None = None) -> tuple[str, str]:
    d = (when or now_ist()).astimezone(IST).date()
    first = d.replace(day=1)
    nxt = (first + timedelta(days=32)).replace(day=1)
    return _utc(first), _utc(nxt)


def parse_day(s: str) -> date | None:
    try:
        return datetime.strptime((s or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def range_bounds(frm: str, to: str) -> tuple[str, str] | None:
    """A custom range, inclusive of both dates the shopkeeper picked.

    Inclusive because that is what the picker says: a shopkeeper who chooses the 1st to the
    31st means the whole month, and a half-open reading would silently drop the last day's
    takings — the single most likely day for them to check the figure against.
    """
    a, b = parse_day(frm), parse_day(to)
    if not a or not b:
        return None
    if b < a:
        a, b = b, a
    return _utc(a), _utc(b, 1)


def _read(iso: str) -> datetime:
    """`fromisoformat` did not learn to read a trailing Z until 3.11, and this runs on
    3.9 — so the suffix these functions emit has to be undone before parsing it back."""
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def span_ist(start_iso: str, end_iso: str) -> tuple[str, str]:
    """The window back in IST dates, for labelling a screen.

    The end is exclusive, so the last day inside the window is one second before it — a
    range labelled with the raw end date would claim a day it does not include.
    """
    a = _read(start_iso).astimezone(IST).date()
    b = (_read(end_iso).astimezone(IST) - timedelta(seconds=1)).date()
    return a.isoformat(), b.isoformat()


def earliest(*windows: tuple[str, str]) -> str:
    """The furthest-back start among several windows.

    Today, this week and this month are three overlapping periods, and on the 1st of a
    month falling mid-week the week reaches back further than the month does. Fetching from
    whichever starts first covers all three in one query instead of three.
    """
    return min(w[0] for w in windows)
