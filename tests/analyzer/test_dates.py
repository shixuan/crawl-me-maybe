"""Reading dates out of what a page wrote."""

from __future__ import annotations

import datetime

import pytest

from crawlme.analyzer.dates import DateRange, read_dates, read_range

SAID_ON = datetime.datetime(2026, 9, 2, tzinfo=datetime.timezone.utc)
D = datetime.date


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("August 29, 2026", (D(2026, 8, 29), D(2026, 8, 29))),
        ("2026-07-01", (D(2026, 7, 1), D(2026, 7, 1))),
        ("Aug 30", (D(2026, 8, 30), D(2026, 8, 30))),
        ("Aug.3", (D(2026, 8, 3), D(2026, 8, 3))),
        ("27 Aug", (D(2026, 8, 27), D(2026, 8, 27))),
        ("August 29, 2026 at 11:59 PM ET", (D(2026, 8, 29), D(2026, 8, 29))),
        # Ranges, in the dash characters the pages actually use.
        ("August 15-16, 2026", (D(2026, 8, 15), D(2026, 8, 16))),
        ("August 15\u201316, 2026", (D(2026, 8, 15), D(2026, 8, 16))),
        ("August 14 \u2013 August 20", (D(2026, 8, 14), D(2026, 8, 20))),
        # Several days each with hours, which is how a two-day span gets written.
        ("July 24: 2:00 PM \u2013 9:00 PM; July 25: 10:00 AM \u2013 9:00 PM", (D(2026, 7, 24), D(2026, 7, 25))),
        # A bare month is the whole month, not a day in it.
        ("August", (D(2026, 8, 1), D(2026, 8, 31))),
    ],
)
def test_reads_the_shapes_pages_use(text, expected):
    assert read_dates(text, said_on=SAID_ON) == expected


@pytest.mark.parametrize("text", ["", "   ", "TODAY", "tomorrow", "next week", "stay tuned"])
def test_relative_wording_is_refused(text):
    """ "next week" on a post from three weeks ago means two weeks ago, and
    a date wrong by a fortnight that looks reasonable is worse than none."""
    assert read_dates(text, said_on=SAID_ON) is None


def test_year_follows_publication():
    """Most posts leave the year out. The one that puts the date nearest
    to publication is the one meant."""
    jan = datetime.datetime(2026, 1, 5, tzinfo=datetime.timezone.utc)
    assert read_dates("Dec 30", said_on=jan) == (D(2025, 12, 30), D(2025, 12, 30))
    assert read_dates("Jan 20", said_on=jan) == (D(2026, 1, 20), D(2026, 1, 20))


def test_impossible_dates_are_dropped():
    assert read_dates("February 30, 2026", said_on=SAID_ON) is None


def test_one_date_means_two_things():
    """A deadline shuts on its date with the start left open; an event
    date is a single day. The goal has to say which it declared."""
    assert read_range("September 15, 2026", kind="until", said_on=SAID_ON) == DateRange(None, D(2026, 9, 15))
    assert read_range("September 15, 2026", kind="on", said_on=SAID_ON) == DateRange(D(2026, 9, 15), D(2026, 9, 15))


def test_wording_without_a_date_is_not_a_date():
    """A page can say something about time without naming one. Reading a
    phrase as open-ended would mean listing the phrases one domain uses,
    which is a domain rule hidden inside a general reader."""
    assert read_range("while supplies last", said_on=SAID_ON) is None
    assert read_range("until further notice", said_on=SAID_ON) is None


def test_overlap_not_containment():
    """The question is whether it can still be caught this week, and an
    span that began a fortnight ago and closes on Wednesday answers yes."""
    w = read_range("September 16, 2026", kind="until", said_on=SAID_ON)
    assert w.overlaps(D(2026, 9, 12), D(2026, 9, 18))
    assert not w.overlaps(D(2026, 9, 20), D(2026, 9, 26))


def test_window_out_of_range_both_ways():
    past = read_range("August 20, 2026", kind="until", said_on=SAID_ON)
    far = read_range("October 30, 2026", kind="on", said_on=SAID_ON)
    assert not past.overlaps(D(2026, 9, 12), D(2026, 9, 18))
    assert not far.overlaps(D(2026, 9, 12), D(2026, 9, 18))
