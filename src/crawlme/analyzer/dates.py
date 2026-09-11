"""Reading dates out of what a page wrote, as a range to compare against.

A page carries two times. One is when it was published, which bounds
what is worth fetching and is known before anything is read. The other
is when the thing it describes applies -- an end it runs until, a date
it happens on, a span it covers -- which is knowable only after
extraction, so it can never bound a fetch, only sort what was found.

Two steps, kept apart because they answer different questions. Reading
dates out of a string is about text, and the shapes are a language's
rather than any one crawl's. Turning those dates into a range is about
what the goal meant by the field it declared: a field marking an end
leaves the start open, a field marking an occurrence does not, and the
same pair of dates has to become different ranges.

Relative wording is refused on purpose. "next week" in a post from
three weeks ago means two weeks ago, and a date that is wrong by a
fortnight while looking perfectly reasonable is worse than no date.
"""

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
    r"\b(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\.?\s*(\d{1,2})(?:st|nd|rd|th)?"
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
    r"\b(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\.?\s+(\d{1,2})\s*[-\u2013\u2014]\s*(\d{1,2})\b",
    re.IGNORECASE,
)


def read_dates(text: str, *, said_on: datetime.datetime | None = None) -> tuple[datetime.date, datetime.date] | None:
    """The span of dates this text names, or None when it names none.

    A single date comes back as a span of one day. *said_on* fixes the
    year when the text leaves it out, which most posts do: the year
    chosen is whichever puts the date nearest to when the page was
    published, so "Aug 30" on a page from September is this year's
    August rather than next year's.
    """
    if not text or not text.strip():
        return None
    found: list[datetime.date] = []

    for y, m, d in _ISO.findall(text):
        if (day := _day(int(y), int(m), int(d))) is not None:
            found.append(day)

    if not found:
        for mon, d1, d2 in _SAME_MONTH_RANGE.findall(text):
            month = _MONTHS[mon.lower()]
            for d in (d1, d2):
                if (day := _day(_year_for(month, int(d), said_on), month, int(d))) is not None:
                    found.append(day)

    if not found:
        for mon, d, year in _MONTH_DAY.findall(text):
            month = _MONTHS[mon.lower()]
            y = int(year) if year else _year_for(month, int(d), said_on)
            if (day := _day(y, month, int(d))) is not None:
                found.append(day)
        for d, mon, year in _DAY_MONTH.findall(text):
            month = _MONTHS[mon.lower()]
            y = int(year) if year else _year_for(month, int(d), said_on)
            if (day := _day(y, month, int(d))) is not None:
                found.append(day)

    if not found and (m := _MONTH_ONLY.match(text)):
        month = _MONTHS[m.group(1).lower()]
        y = int(m.group(2)) if m.group(2) else _year_for(month, 1, said_on)
        last = calendar.monthrange(y, month)[1]
        return datetime.date(y, month, 1), datetime.date(y, month, last)

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
    """Two ends, either of which may be open.

    An end alone is a deadline; a start alone is something with no
    stated finish; the same date at both ends is a single day.
    """

    start: datetime.date | None
    end: datetime.date | None

    def overlaps(self, first: datetime.date, last: datetime.date) -> bool:
        """Whether any of this range falls inside *first*..*last*.

        Overlap rather than containment, because the question a reader
        asks is "is any of it still ahead of me", and something that
        began a fortnight ago and closes on Wednesday answers yes.
        """
        if self.end is not None and self.end < first:
            return False
        if self.start is not None and self.start > last:
            return False
        return True

    @property
    def expired(self) -> bool:
        return self.end is not None and self.end < datetime.datetime.now(datetime.timezone.utc).date()


def read_range(text: str, *, kind: str = "until", said_on: datetime.datetime | None = None) -> DateRange | None:
    """What *text* says about when the thing is on, or None if nothing.

    *kind* is what the goal meant by the field it came from. "until"
    marks the end of something, so one date leaves the start open and
    shuts on that day. "on" marks when it happens, so one date is a
    single day. The same pair of dates has to become different ranges,
    which is why the goal has to say which it declared.
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
