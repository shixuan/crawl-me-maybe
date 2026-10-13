from __future__ import annotations

from crawlme.pioneer import Candidate, FrontierItem
from crawlme.urls import URL


def make_url() -> URL:
    return URL(
        raw="https://example.com/page?a=1",
        canonical="https://example.com/page",
        url_key="abc123",
    )


def test_candidate_defaults():
    c = Candidate(url=make_url())
    assert c.candidate_id
    assert c.status == "INGESTED"
    assert c.depth == 0


def test_item_defaults():
    u = make_url()
    fi = FrontierItem(url=u, url_key=u.url_key)
    assert fi.item_id
    assert (fi.status, fi.score_source, fi.priority) == ("QUEUED", "seed", 0.0)


def test_candidate_text():
    """A feed post brings a caption; a link brings neither."""
    from crawlme.pioneer import Candidate
    from crawlme.urls import URL

    url = URL(raw="https://x.com/p", canonical="https://x.com/p", url_key="k1")
    link = Candidate(url=url, anchor="click here")
    assert link.text == ""
    assert link.signals == {}

    post = Candidate(url=url, text="free tea today", signals={"account": "molly", "hashtags": ["tea"]})
    assert post.text == "free tea today"
    assert post.signals["account"] == "molly"
