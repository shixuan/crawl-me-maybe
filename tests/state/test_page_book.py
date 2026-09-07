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


def test_a_funnel_only_narrows():
    """Every gap between two stages means something, which is what lets
    a new question be answered without a new field."""
    from crawlme.state.context import Funnel

    f = Funnel(discovered=63, scored=11, wanted=6, fetched=7, judged=1, relevant=0)
    found, judged, fetched, wanted, scored, discovered = f.as_tuple()
    assert (found, judged, fetched, wanted, scored, discovered) == (0, 1, 7, 6, 11, 63)


def test_a_seed_starts_with_an_empty_funnel():
    from crawlme.state.context import SeedState

    st = SeedState()
    assert st.funnel.as_tuple() == (0, 0, 0, 0, 0, 0)
    assert st.listing_pages == 0
    assert not st.retired
