"""Parse explicit event dates and group date ranges. Relative phrases are not resolved."""

from __future__ import annotations

import calendar
import dataclasses
import datetime
import re

_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
_MONTHS |= {m.lower(): i for i, m in enumerate(calendar.month_name) if m}

_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
#: "August 29", "Aug 29", "Aug.3", "August 29, 2026" -- the day may carry
#: an ordinal suffix, and an abbreviation's dot may swallow the space.
_MONTH_DAY = re.compile(
    r"\b(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\.?\s*(\d{1,2})(?!\d)(?:st|nd|rd|th)?"
    r"(?:\s*,?\s*(\d{4}))?",
    re.IGNORECASE,
)
#: "27 Aug", "27 August 2026" -- the other order, common outside North America.
_DAY_MONTH = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\.?"
    r"(?:\s*,?\s*(\d{4}))?",
    re.IGNORECASE,
)
#: A bare month, which is a whole month rather than a day.
_MONTH_ONLY = re.compile(r"^\s*(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\.?\s*(\d{4})?\s*$", re.I)
#: "August 15-16", including the en dash and em dash forms: one month, two days.
_SAME_MONTH_RANGE = re.compile(
    r"\b(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\.?\s+(\d{1,2})\s*[-\u2013\u2014]\s*(\d{1,2})\b"
    r"(?:\s*,?\s*(\d{4}))?",
    re.IGNORECASE,
)


def read_dates(text: str, *, said_on: datetime.datetime | None = None) -> tuple[datetime.date, datetime.date] | None:
    """The span of dates this text names, or None when it names none.

    A single date comes back as a span of one day. When the text omits
    the year, *said_on* picks the one that puts the date nearest it.
    """
    if not text or not text.strip():
        return None
    if m := _MONTH_ONLY.fullmatch(text):
        month = _MONTHS[m.group(1).lower()]
        y = int(m.group(2)) if m.group(2) else _year_for(month, 1, said_on)
        last = calendar.monthrange(y, month)[1]
        first, end = _day(y, month, 1), _day(y, month, last)
        return (first, end) if first is not None and end is not None else None
    found: list[datetime.date] = []

    # Consume complete matches so another format cannot reinterpret part
    # of a range or its year, while still reading other dates in the text.
    for pattern in (_ISO, _SAME_MONTH_RANGE, _MONTH_DAY, _DAY_MONTH):
        for match in pattern.finditer(text):
            if pattern is _ISO:
                iso_year, iso_month, iso_day = map(int, match.groups())
                if (day := _day(iso_year, iso_month, iso_day)) is not None:
                    found.append(day)
                continue
            if pattern is _SAME_MONTH_RANGE:
                mon, d1, d2, year = match.groups()
                days = [d1, d2]
            else:
                mon, d, year = (
                    match.groups() if pattern is _MONTH_DAY else (match.group(2), match.group(1), match.group(3))
                )
                days = [d]
            month = _MONTHS[mon.lower()]
            for d in days:
                y = int(year) if year else _year_for(month, int(d), said_on)
                if (day := _day(y, month, int(d))) is not None:
                    found.append(day)
        text = pattern.sub(lambda match: " " * len(match.group()), text)

    if not found:
        return None
    return min(found), max(found)


def _day(year: int, month: int, day: int) -> datetime.date | None:
    try:
        return datetime.date(year, month, day)
    except ValueError:
        return None


def _year_for(month: int, day: int, said_on: datetime.datetime | None) -> int:
    """The year that puts this month and day nearest to publication."""
    if said_on is None:
        return datetime.datetime.now(datetime.timezone.utc).year
    base = said_on.date()
    candidates = [y for y in (base.year - 1, base.year, base.year + 1)]
    best = min(candidates, key=lambda y: abs(((_day(y, month, day) or base) - base).days))
    return best


@dataclasses.dataclass(frozen=True)
class DateRange:
    """Two ends, either of which may be open."""

    start: datetime.date | None
    end: datetime.date | None


def read_range(text: str, *, kind: str = "until", said_on: datetime.datetime | None = None) -> DateRange | None:
    """What *text* says about when something is on, or None if nothing.

    *kind* says what a lone date means. "until" is an end, so the start
    stays open; "on" is a single day.
    """
    if not text or not text.strip():
        return None
    dates = read_dates(text, said_on=said_on)
    if dates is None:
        return None
    first, last = dates
    if kind == "until" and first == last:
        return DateRange(None, last)
    return DateRange(first, last)


UNDATED = "undated"
OVER = "over"
OPEN = "open"
LATER = "later"


def group_of(
    starts: datetime.date | None,
    ends: datetime.date | None,
    today: datetime.date,
    horizon: datetime.date | None = None,
) -> str:
    """Which of undated, over, open and later a range falls in.

    A range with only an end is already running, so it stays open
    however far off that end is. Without a horizon nothing is later.
    """
    if starts is None and ends is None:
        return UNDATED
    if ends is not None and ends < today:
        return OVER
    if horizon is not None and starts is not None and starts > horizon:
        return LATER
    return OPEN
