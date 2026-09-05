"""What the run knows about one page, and when it knows it."""

from __future__ import annotations

from crawlme.state.context import PageBook


def test_a_record_waits_for_both_halves():
    """The seed arrives on dispatch, the listing flag after harvesting,
    and the verdict when the analyzer answers, which for a retried
    analysis is long after both."""
    book = PageBook()
    rec = book.open("k1", "https://x.test/p", "seedA")
    assert not rec.ready()

    rec.listing = False
    assert not rec.ready()

    rec.relevant = True
    assert rec.ready()


def test_either_order_completes_the_pair():
    book = PageBook()
    book.of("k1").relevant = False
    assert not book.of("k1").ready()
    book.of("k1").listing = True
    assert book.of("k1").ready()


def test_counting_it_once_is_the_caller_s_job():
    book = PageBook()
    rec = book.open("k1", "https://x.test/p", "s")
    rec.listing, rec.relevant = False, True
    assert rec.ready()
    rec.counted = True
    assert not rec.ready()


def test_a_page_is_found_by_key_or_by_address():
    book = PageBook()
    book.open("k1", "https://x.test/p", "seedA")
    assert book.by_url("https://x.test/p") is book.of("k1")
    assert book.seed_of("k1") == "seedA"
    assert book.key_of("https://x.test/p") == "k1"


def test_an_unknown_page_answers_with_the_default():
    """The analyzer's feedback names a URL the run may never have filed."""
    book = PageBook()
    assert book.by_url("https://nowhere.test/") is None
    assert book.seed_of("nope", "fallback") == "fallback"
    assert book.key_of("https://nowhere.test/") == ""
