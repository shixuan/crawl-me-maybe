"""Reading a moment off the command line.

Two flags name one, in opposite directions: what was published since,
and what runs until. They take the same two forms, so they read it in
one place rather than each growing its own idea of what "1 week" is.
"""

from __future__ import annotations

import datetime

#: Months and years are the calendar-free approximations a crawl
#: budget can live with.
_UNITS = {
    "day": 1,
    "days": 1,
    "week": 7,
    "weeks": 7,
    "month": 30,
    "months": 30,
    "year": 365,
    "years": 365,
}


def read_cutoff(text: str, *, flag: str, ahead: bool = False) -> datetime.datetime:
    """A moment, written as an offset from now or as a date.

    *ahead* turns an offset around: "1 week" is a week back for what was
    published, a week forward for what is still to come. A written date
    is that date either way, because a date does not have a direction.
    """
    raw = text.strip().lower()
    parts = raw.split()
    if len(parts) == 2 and parts[0].isdigit() and parts[1] in _UNITS:
        days = int(parts[0]) * _UNITS[parts[1]]
        step = datetime.timedelta(days=days if ahead else -days)
        return datetime.datetime.now(datetime.timezone.utc) + step
    try:
        parsed = datetime.datetime.fromisoformat(raw)
    except ValueError:
        raise ValueError(f"cannot read {flag} {text!r}, use '1 week' or '2026-08-01'") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)
