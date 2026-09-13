"""Parse absolute or relative CLI cutoffs for publication and event windows."""

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
    """Read an ISO date or a day/week/month/year offset.

    Offsets point backward unless ahead=True. Naive dates are interpreted as UTC."""
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
