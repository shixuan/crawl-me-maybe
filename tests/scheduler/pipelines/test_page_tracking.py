"""Verify results processing through its owning component."""

from __future__ import annotations

import dataclasses
import datetime

import pytest

from crawlme.analysis import AnalysisResult, AnalyzerFeedback
from crawlme.runtime import MAX_STALE_STREAK, RELEVANCE_WINDOW
from tests.scheduler.helpers import (
    _FRESH,
    _SINCE,
    _STALE,
    _judge,
    _make_sched,
    _page_published,
    _set_context,
    _vote,
)


def test_backfills_context():
    """2.9: the ranker reads the source page's verdict from here."""
    sched = _make_sched()
    sched.run_state.page_contexts["k1"] = {"title": "Existing", "link_count": 7}
    result = AnalysisResult(
        page_id="p1",
        url_key="k1",
        classification="RELEVANT",
        relevance_score=0.87,
        summary="Borrow checker deep dive.",
    )

    sched.pipeline._analysis.apply(result)

    ctx = sched.run_state.page_contexts["k1"]
    assert ctx["classification"] == "RELEVANT"
    assert ctx["relevance"] == 0.87
    assert ctx["summary"] == "Borrow checker deep dive."
    assert ctx["title"] == "Existing"
    assert ctx["link_count"] == 7


def test_context_keeps_older():
    """analyze runs before link extraction, so the later write must merge."""
    sched = _make_sched()
    sched.pipeline._analysis.apply(
        AnalysisResult(page_id="p1", url_key="k1", classification="IRRELEVANT", relevance_score=0.4)
    )

    sched.pipeline._tracking.record_page_context("k1", {"title": "T", "link_count": 3})

    ctx = sched.run_state.page_contexts["k1"]
    assert ctx["classification"] == "IRRELEVANT"
    assert ctx["title"] == "T"


@pytest.mark.parametrize(
    ("since", "published", "expected"),
    [
        # No window asked for: the streak stays dormant whatever arrives.
        (None, [_STALE], 0),
        (_SINCE, [_STALE] * 3, 3),
        (_SINCE, [_STALE, _FRESH], 0),
        # Silence is not evidence: it neither advances nor resets.
        (_SINCE, [_STALE, None], 1),
    ],
)
def test_stale_streak(since, published, expected):
    """Per seed, because a feed is time-ordered per account and never as
    a whole. Counted globally it could only ever arm for one seed."""
    sched = _make_sched()
    sched.run_state.limits = dataclasses.replace(sched.run_state.limits, since=since)
    for at in published:
        sched.pipeline._page.published(_page_published(at), "seedA")
    assert sched.run_state.seeds["seedA"].stale == expected


def test_window_fed():
    """A judged content page is what a source's window counts."""
    sched = _make_sched()
    sched.run_state.limits = dataclasses.replace(sched.run_state.limits, relevance_threshold=0.7)

    for key, score in (("k1", 0.9), ("k2", 0.2)):
        sched.pipeline._analysis.apply(AnalysisResult(page_id="p", url_key=key, relevance_score=score))
        sched.run_state.pages.of(key).listing = False
        sched.pipeline._page.vote(key)

    assert list(sched.run_state.seeds[""].window) == [True, False]


def test_window_threshold():
    """relevance_threshold stops being dead config here."""
    sched = _make_sched()
    sched.run_state.limits = dataclasses.replace(sched.run_state.limits, relevance_threshold=0.95)

    sched.pipeline._analysis.apply(AnalysisResult(page_id="p1", url_key="k1", relevance_score=0.9))
    sched.run_state.pages.of("k1").listing = False
    sched.pipeline._page.vote("k1")

    assert list(sched.run_state.seeds[""].window) == [False]


def test_relevant_count():
    """One judgement answers two questions: the run's tally of what it
    found, and the source's own window of whether it is still paying."""
    sched = _make_sched()
    _set_context(sched, relevance_threshold=0.7)
    for key, score, cls in (("a", 0.9, "RELEVANT"), ("b", 0.2, "IRRELEVANT"), ("c", 0.75, "RELEVANT")):
        sched.pipeline._analysis.apply(AnalysisResult(url_key=key, relevance_score=score, classification=cls))
        sched.run_state.pages.of(key).listing = False
        sched.pipeline._page.vote(key)
    assert sched.run_state.progress.relevant_found == 2
    assert list(sched.run_state.seeds[""].window) == [True, False, True]


def test_seen_bounded():
    """Unbounded, it would grow for a whole run only to be sliced away
    at the prompt every time."""
    from crawlme.runtime.state import _SEEN_SO_FAR

    sched = _make_sched()
    for i in range(_SEEN_SO_FAR + 4):
        sched.pipeline._analysis.apply(
            AnalysisResult(
                page_id=f"p{i}",
                url_key=f"k{i}",
                classification="RELEVANT",
                relevance_score=0.9,
                feedback=AnalyzerFeedback(classification="RELEVANT", relevance_score=0.9, url=f"https://x/{i}"),
            )
        )

    assert len(sched.run_state.relevant_pages) == _SEEN_SO_FAR
    assert sched.run_state.relevant_pages[-1]["url"] == f"https://x/{_SEEN_SO_FAR + 3}"


@pytest.mark.asyncio
async def test_a_proposed_seed_is_credited_for_what_it_found():
    """The report ranks them by this, and it is the whole reason to
    print them: a seed that earned its place is one to keep."""
    from crawlme.analysis import AnalyzerFeedback
    from crawlme.urls import Canonicalizer

    canon = Canonicalizer()
    seed = "https://ig.test/acct/"
    seed_key = canon.canonicalize(seed, seed).url_key
    post = "https://ig.test/acct/p/1/"
    post_key = canon.canonicalize(post, post).url_key

    sched = _make_sched(canonicalizer=canon)
    sched.run_state.pages.open(post_key, post, seed_key)
    sched.run_state.proposed_seeds = {seed_key: (seed, "why")}
    sched.run_state.limits = dataclasses.replace(sched.run_state.limits, relevance_threshold=0.7)

    sched.pipeline._analysis.apply(
        AnalysisResult(classification="RELEVANT", relevance_score=0.9, feedback=AnalyzerFeedback(url=post, title="t"))
    )
    why, tally = sched.summary()["proposed_seeds"][seed]
    assert (why, tally[0]) == ("why", 1)


def test_a_listing_does_not_vote():
    """It is read for its links and can never be an answer. Counting it
    asks a page that was never in the running whether the run is
    working, and one run stopped for diminishing returns because five
    seeds put seven certain misses into a window of twenty."""
    sched = _make_sched()
    _judge(sched, "k1", 0.0)
    sched.run_state.pages.of("k1").listing = True
    sched.pipeline._page.vote("k1")
    assert list(sched.run_state.seeds[""].window) == []


def test_a_page_votes_once_judged():
    sched = _make_sched()
    _judge(sched, "k1", 0.9)
    sched.run_state.pages.of("k1").listing = False
    sched.pipeline._page.vote("k1")
    assert list(sched.run_state.seeds[""].window) == [True]


def test_a_late_verdict_still_votes():
    """A retried analysis lands long after link extraction, so the
    harvester's half is already in when the verdict arrives."""
    sched = _make_sched()
    sched.run_state.pages.of("k1").listing = False
    sched.pipeline._page.vote("k1")
    assert list(sched.run_state.seeds[""].window) == []
    _judge(sched, "k1", 0.9)
    assert list(sched.run_state.seeds[""].window) == [True]


def test_a_vote_is_cast_once():
    sched = _make_sched()
    _judge(sched, "k1", 0.9)
    sched.run_state.pages.of("k1").listing = False
    sched.pipeline._page.vote("k1")
    sched.pipeline._page.vote("k1")
    assert list(sched.run_state.seeds[""].window) == [True]


def test_the_tally_counts_listings():
    """Abstaining from the window is not abstaining from the run: a
    listing that somehow answers the goal is still an answer found."""
    sched = _make_sched()
    _judge(sched, "k1", 0.9)
    sched.run_state.pages.of("k1").listing = True
    sched.pipeline._page.vote("k1")
    assert sched.run_state.progress.relevant_found == 1


def test_a_cold_source_retires():
    """A full window of its own content with almost nothing to show."""
    sched = _make_sched()
    for i in range(RELEVANCE_WINDOW):
        _vote(sched, "seedA", f"k{i}", False)
    sched.pipeline._frontier.retire.assert_called_once_with("seedA")
    assert sched.run_state.seeds["seedA"].retired


def test_one_cold_source_leaves_the_others():
    """The whole reason this is per seed. Read globally, one quiet shop's
    back catalogue ended a run with three sources still producing."""
    sched = _make_sched()
    for i in range(RELEVANCE_WINDOW):
        _vote(sched, "cold", f"c{i}", False)
        _vote(sched, "hot", f"h{i}", i % 3 == 0)
    assert sched.run_state.seeds["cold"].retired
    assert not sched.run_state.seeds["hot"].retired


def test_a_source_past_the_window_retires():
    """A feed is time-ordered per account, so reading past --since means
    that account is walked out. It says nothing about the others."""
    sched = _make_sched()
    since = datetime.datetime(2026, 8, 1, tzinfo=datetime.timezone.utc)
    sched.run_state.limits = dataclasses.replace(sched.run_state.limits, since=since)
    old = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    for i in range(MAX_STALE_STREAK):
        sched.pipeline._page.published(_page_published(old), "seedA")
    assert sched.run_state.seeds["seedA"].retired
    sched.pipeline._frontier.retire.assert_called_once_with("seedA")


def test_one_hit_resets_the_stale_streak():
    sched = _make_sched()
    since = datetime.datetime(2026, 8, 1, tzinfo=datetime.timezone.utc)
    sched.run_state.limits = dataclasses.replace(sched.run_state.limits, since=since)
    for at in (datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),) * 4:
        sched.pipeline._page.published(_page_published(at), "seedA")
    sched.pipeline._page.published(
        _page_published(datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc)), "seedA"
    )
    assert sched.run_state.seeds["seedA"].stale == 0


def test_recall_retires_nothing():
    """Reading the tail is the point of the mode, not evidence a source
    is done."""
    sched = _make_sched()
    sched.run_state.limits = dataclasses.replace(sched.run_state.limits, recall=True)
    for i in range(RELEVANCE_WINDOW):
        _vote(sched, "seedA", f"k{i}", False)
    assert not sched.run_state.seeds["seedA"].retired
    sched.pipeline._frontier.retire.assert_not_called()


def test_a_source_retires_once():
    sched = _make_sched()
    for i in range(RELEVANCE_WINDOW * 2):
        _vote(sched, "seedA", f"k{i}", False)
    sched.pipeline._frontier.retire.assert_called_once_with("seedA")


def test_an_unfiled_page_cannot_retire_anything():
    """The streak used to be looked up by a key the page did not have
    yet, so it always landed under the empty seed -- where retirement
    ignores it. Five sources shared one streak, each other's fresh pages
    reset it, and a --since of one month came back full of year-old
    posts."""
    sched = _make_sched()
    since = datetime.datetime(2026, 8, 1, tzinfo=datetime.timezone.utc)
    sched.run_state.limits = dataclasses.replace(sched.run_state.limits, since=since)
    old = _page_published(datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc))
    for _ in range(MAX_STALE_STREAK * 2):
        sched.pipeline._page.published(old, "")
    assert not sched.run_state.seeds[""].retired
    sched.pipeline._frontier.retire.assert_not_called()
