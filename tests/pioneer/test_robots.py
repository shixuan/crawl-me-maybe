from __future__ import annotations

import datetime

import pytest

from crawlme.pioneer.robots import RobotsPolicy

ROBOTS_ALLOW_ALL = """User-agent: *
Disallow:"""

ROBOTS_BLOCK_ADMIN = """User-agent: *
Disallow: /admin
Disallow: /login"""


# -- robots policy ----------------------------------------------------------


def test_ignore_mode_allows_everything():
    rp = RobotsPolicy(ignore=True)
    rp.load_robots_txt("example.com", ROBOTS_BLOCK_ADMIN)
    assert rp.allow_fetch("https://example.com/admin") is True


def test_allow_all():
    rp = RobotsPolicy()
    rp.load_robots_txt("example.com", ROBOTS_ALLOW_ALL)
    assert rp.allow_fetch("https://example.com/any/path") is True


def test_disallow_specific_path():
    rp = RobotsPolicy()
    rp.load_robots_txt("example.com", ROBOTS_BLOCK_ADMIN)
    assert rp.allow_fetch("https://example.com/admin/secret") is False
    assert rp.allow_fetch("https://example.com/about") is True


def test_uncached_domain_allows():
    rp = RobotsPolicy()
    assert rp.allow_fetch("https://never.seen.before/page") is True


def test_record_429_triggers_backoff():
    rp = RobotsPolicy()
    now = datetime.datetime.now(datetime.timezone.utc)
    rp.record_response("example.com", 429)
    next_at = rp.next_allowed_at("example.com")
    # backoff = 2^1 = 2 seconds
    assert next_at > now


def test_circuit_breaker():
    rp = RobotsPolicy(circuit_threshold=3)
    for _ in range(3):
        rp.record_response("example.com", 429)
    # Circuit is open, fetch should be denied.
    rp.load_robots_txt("example.com", ROBOTS_ALLOW_ALL)
    assert rp.allow_fetch("https://example.com/page") is False


def test_success_resets_failure_counter():
    rp = RobotsPolicy(circuit_threshold=2)
    rp.record_response("example.com", 429)
    rp.record_response("example.com", 200)
    rp.record_response("example.com", 429)
    # Only 2 total failures, not consecutive, circuit stays closed.
    rp.load_robots_txt("example.com", ROBOTS_ALLOW_ALL)
    assert rp.allow_fetch("https://example.com/page") is True


@pytest.mark.parametrize(
    ("ttl", "stale"),
    [(datetime.timedelta(seconds=0), True), (datetime.timedelta(days=1), False)],
)
def test_cache_staleness(ttl, stale):
    rp = RobotsPolicy(cache_ttl=ttl)
    rp.load_robots_txt("example.com", ROBOTS_ALLOW_ALL)
    assert rp.is_cache_stale("example.com") is stale
