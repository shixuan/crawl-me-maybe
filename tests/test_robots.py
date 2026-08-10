from __future__ import annotations

import datetime

import pytest

from crawlme.pioneer.robots import RobotsPolicy

ROBOTS_ALLOW_ALL = """User-agent: *
Disallow:"""

ROBOTS_BLOCK_ADMIN = """User-agent: *
Disallow: /admin
Disallow: /login"""


class TestRobotsPolicy:
    def test_ignore_mode_allows_everything(self):
        rp = RobotsPolicy(ignore=True)
        rp.load_robots_txt("example.com", ROBOTS_BLOCK_ADMIN)
        assert rp.allow_fetch("https://example.com/admin") is True

    def test_allow_all(self):
        rp = RobotsPolicy()
        rp.load_robots_txt("example.com", ROBOTS_ALLOW_ALL)
        assert rp.allow_fetch("https://example.com/any/path") is True

    def test_disallow_specific_path(self):
        rp = RobotsPolicy()
        rp.load_robots_txt("example.com", ROBOTS_BLOCK_ADMIN)
        assert rp.allow_fetch("https://example.com/admin/secret") is False
        assert rp.allow_fetch("https://example.com/about") is True

    def test_uncached_domain_allows(self):
        rp = RobotsPolicy()
        assert rp.allow_fetch("https://never.seen.before/page") is True

    def test_record_429_triggers_backoff(self):
        rp = RobotsPolicy()
        now = datetime.datetime.now(datetime.timezone.utc)
        rp.record_response("example.com", 429)
        next_at = rp.next_allowed_at("example.com")
        # backoff = 2^1 = 2 seconds
        assert next_at > now

    def test_circuit_breaker(self):
        rp = RobotsPolicy(circuit_threshold=3)
        for _ in range(3):
            rp.record_response("example.com", 429)
        # Circuit is open, fetch should be denied.
        rp.load_robots_txt("example.com", ROBOTS_ALLOW_ALL)
        assert rp.allow_fetch("https://example.com/page") is False

    def test_success_resets_failure_counter(self):
        rp = RobotsPolicy(circuit_threshold=2)
        rp.record_response("example.com", 429)
        rp.record_response("example.com", 200)
        rp.record_response("example.com", 429)
        # Only 2 total failures, not consecutive, circuit stays closed.
        rp.load_robots_txt("example.com", ROBOTS_ALLOW_ALL)
        assert rp.allow_fetch("https://example.com/page") is True

    def test_cache_stale_after_ttl(self):
        rp = RobotsPolicy(cache_ttl=datetime.timedelta(seconds=0))
        rp.load_robots_txt("example.com", ROBOTS_ALLOW_ALL)
        assert rp.is_cache_stale("example.com") is True

    def test_cache_fresh(self):
        rp = RobotsPolicy(cache_ttl=datetime.timedelta(days=1))
        rp.load_robots_txt("example.com", ROBOTS_ALLOW_ALL)
        assert rp.is_cache_stale("example.com") is False
