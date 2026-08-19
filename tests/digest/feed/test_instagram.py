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
import json
from pathlib import Path

import pytest

from crawlme.digest.feed import PageProblem
from crawlme.digest.feed import instagram as ig
from crawlme.schemas import Payload

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
    lst = ig.parse_listing(_PROFILE, "https://www.instagram.com/mollytea_canada/", [])
    assert len(lst.own) == 3, "reels are the account's posts too"
    assert len(lst.others) == 2
    assert all("mollytea_canada" not in i.permalink for i in lst.others)
    assert all(i.permalink.startswith("https://www.instagram.com/") for i in lst.all)


def test_listing_ignores_links_that_are_not_posts():
    assert not any(
        i.permalink.endswith("/mollytea_canada/")
        for i in ig.parse_listing(_PROFILE, "https://www.instagram.com/mollytea_canada/", []).all
    )


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


#: what the grid never shows ---------------------------------------------

_FIXTURE = Path(__file__).parent / "fixtures" / "ig_timeline.json"

_GRID = b"""<html><body>
<a href="/mrsurprisetoys/p/DcFMbOThnuH/"><img
 alt="Photo shared by Mr. Surprise Toys on August 16, 2026. May be pop art of slow loris."></a>
<a href="/mrsurprisetoys/p/DbbjpjPD2e0/"><img alt="Photo shared by Mr. Surprise Toys. May be a graphic of penguin."></a>
</body></html>"""


def _payload() -> Payload:
    return Payload(
        url="https://www.instagram.com/graphql/query", content_type="application/json", body=_FIXTURE.read_bytes()
    )


def test_the_payload_supplies_the_text_the_grid_withholds():
    """The grid describes the picture; the payload says what the post says.

    A run that ranked on the description alone fetched the two least
    relevant posts of twelve and left four giveaways unread.
    """
    lst = ig.parse_listing(_GRID.decode(), "https://www.instagram.com/mrsurprisetoys/", [_payload()])
    by_code = {i.item_id: i for i in lst.all}
    text = by_code["DcFMbOThnuH"].text
    assert "Come4Free" in text and "#giveaway" in text
    assert "May be pop art" not in text


def test_the_payload_supplies_an_exact_time():
    """The grid states a day at best, and often nothing at all."""
    lst = ig.parse_listing(_GRID.decode(), "https://www.instagram.com/mrsurprisetoys/", [_payload()])
    posted = {i.item_id: i.published_at for i in lst.all}
    assert posted["DcFMbOThnuH"] == datetime.datetime(2026, 8, 16, 0, 46, 9, tzinfo=datetime.timezone.utc)
    # The grid gave this one no date at all; the payload does.
    assert posted["DbbjpjPD2e0"] is not None


def test_without_a_payload_the_listing_is_what_it_always_was():
    """A plain HTTP fetch, or a platform that changed shape underneath."""
    lst = ig.parse_listing(_GRID.decode(), "https://www.instagram.com/mrsurprisetoys/", [])
    by_code = {i.item_id: i for i in lst.all}
    assert "May be pop art" in by_code["DcFMbOThnuH"].text
    assert by_code["DbbjpjPD2e0"].published_at is None


def test_an_unreadable_payload_is_not_fatal():
    junk = Payload(url="x", content_type="application/json", body=b"{not json")
    lst = ig.parse_listing(_GRID.decode(), "https://www.instagram.com/mrsurprisetoys/", [junk])
    assert len(lst.all) == 2


def test_posts_are_found_by_shape_not_by_where_they_sit():
    """The connection is named for an API version and will be renamed."""
    moved = json.dumps(
        {"whatever": {"deeper": [{"code": "AAA111", "caption": {"text": "free tea"}, "taken_at": 1786841169}]}}
    )
    lst = ig.parse_listing(
        '<a href="/x/p/AAA111/"><img alt="alt"></a>',
        "https://www.instagram.com/x/",
        [Payload(url="u", content_type="application/json", body=moved.encode())],
    )
    assert lst.all[0].text == "free tea"


def test_the_adapter_keeps_only_the_response_that_carries_posts():
    assert ig.keeps_payload("https://www.instagram.com/graphql/query", "application/json") is True
    assert ig.keeps_payload("https://www.instagram.com/static/bundle.js", "application/javascript") is False
    assert ig.keeps_payload("https://www.instagram.com/graphql/query", "text/html") is False
