"""Instagram markup, against shapes real captured pages actually use.

The fixtures are trimmed from pages the Phase 0 probe saved, so nothing
here is invented: the profile-scoped permalinks, the
`"caption":{"text":...}` nesting, the `N likes, M comments - handle on
DATE:` prefix and the ISO `<time datetime>` are all copied from what
Instagram served. The captures themselves live under results/, which is
gitignored, so CI runs on these distillations.
"""

from __future__ import annotations

import datetime
import glob

import pytest

from crawlme.digest.feed import PageProblem
from crawlme.digest.feed import instagram as ig

_PROFILE = """<html><body>
<a href="/mollytea_canada/p/AAA111/"><img alt="Photo shared by MollyTeaCanada on August 13, 2026"></a>
<a href="/mollytea_canada/p/BBB222/">two</a>
<a href="/mollytea_canada/reel/CCC333/">reel</a>
<a href="/hellofoodbaby_/p/DDD444/">tagged</a>
<a href="/tofoodnoms/p/EEE555/">tagged</a>
<a href="/mollytea_canada/">profile</a>
</body></html>"""

_CAPTION = "\\ud83c\\udf47Top Up, Get More!\\n\\nTop up 50 CAD, get a free drink. Ends Saturday."

_POST = f"""<html><head>
<meta property="og:description" content="103 likes, 0 comments - mollytea_canada on August 13, 2026:
 &quot;\U0001f347Top Up, Get More! Top up 50 CAD, get a free drink. Ends Sat">
</head><body>
<script>{{"caption":{{"text":"{_CAPTION}"}},"other":1}}</script>
<time class="xdwrcjd" datetime="2026-08-13T20:03:29.000Z"></time>
</body></html>"""


def test_a_wrong_handle_is_detected_from_the_body():
    """Instagram answers 200 with a full page, so status codes lie."""
    assert ig.problem("<html>Sorry, this page isn't available.</html>") is PageProblem.UNAVAILABLE


def test_a_challenge_is_not_confused_with_content():
    assert ig.problem('<html>{"challenge_required":true}</html>') is PageProblem.BLOCKED


def test_a_login_wall_is_detected():
    assert ig.problem('<html><form id="loginForm"></form></html>') is PageProblem.LOGIN_REQUIRED


def test_a_healthy_post_reports_no_problem():
    assert ig.problem(_POST) is None


def test_listing_separates_own_posts_from_tagged_ones():
    lst = ig.parse_listing(_PROFILE, "mollytea_canada")
    assert len(lst.own) == 3, "reels are the account's posts too"
    assert len(lst.others) == 2
    assert all("mollytea_canada" not in i.permalink for i in lst.others)
    assert all(i.permalink.startswith("https://www.instagram.com/") for i in lst.all)


def test_listing_ignores_links_that_are_not_posts():
    assert not any(i.permalink.endswith("/mollytea_canada/") for i in ig.parse_listing(_PROFILE, "mollytea_canada").all)


def test_item_prefers_the_full_caption_over_the_truncated_meta():
    item = ig.parse_item(_POST, "https://www.instagram.com/p/AAA111/")
    assert item is not None
    assert item.text.endswith("Ends Saturday.")
    assert "\U0001f347" in item.text, "escaped emoji never decoded"


def test_item_reads_timestamp_author_and_likes():
    item = ig.parse_item(_POST, "https://www.instagram.com/p/AAA111/")
    assert item is not None
    assert item.published_at == datetime.datetime(2026, 8, 13, 20, 3, 29, tzinfo=datetime.timezone.utc)
    assert item.author == "mollytea_canada"
    assert item.signals["likes"] == 103
    assert item.item_id == "AAA111"


def test_item_falls_back_to_meta_when_the_json_shape_changes():
    """The JSON nesting is the fragile part; the meta tag outlives it."""
    item = ig.parse_item(_POST.replace('"caption"', '"captionX"'), "https://www.instagram.com/p/AAA111/")
    assert item is not None
    assert "Top Up, Get More!" in item.text


def test_a_profile_page_is_not_parsed_as_a_post():
    """Regression: a profile carries og:description too, so parsing one
    as a post produced an item whose caption was the account bio."""
    assert ig.parse_item(_PROFILE, "https://www.instagram.com/mollytea_canada/") is None


def test_an_unavailable_page_yields_no_item():
    assert ig.parse_item("<html>Sorry, this page isn't available</html>", "https://x/p/A/") is None


def test_against_real_captures_when_the_machine_has_any():
    """Guards the distillations above from drifting away from reality.

    Skips everywhere but a machine that has run the probe: CI stays
    hermetic, a developer gets the stronger check.
    """
    captures = sorted(glob.glob("results/phase0/*.html"))
    if not captures:
        pytest.skip("no probe captures on this machine")
    parsed = 0
    for path in captures:
        html = open(path, encoding="utf-8", errors="replace").read()
        if ig.problem(html):
            continue
        item = ig.parse_item(html, "https://www.instagram.com/p/Db_i7QgSJds/")
        if item is None:
            continue
        parsed += 1
        assert item.published_at is not None
        assert len(item.text) > 50
        assert item.to_candidate().text == item.text
    if not parsed:
        pytest.skip("captures held no post page")
