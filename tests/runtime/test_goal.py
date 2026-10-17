from __future__ import annotations

import pytest
from pydantic import ValidationError

from crawlme.runtime import Goal


def test_goal_id_hashed():
    """A goal is named by its prompt: same text, same id."""
    a = Goal(prompt="find rust posts")
    b = Goal(prompt="find rust posts")
    c = Goal(prompt="find go posts")
    assert a.goal_id == b.goal_id
    assert a.goal_id != c.goal_id
    assert len(a.goal_id) == 12
    # An explicitly passed id still wins.
    d = Goal(prompt="find rust posts", goal_id="g1")
    assert d.goal_id == "g1"


def test_goal_defaults():
    g = Goal(prompt="find AI funding news")
    assert g.prompt == "find AI funding news"
    assert g.goal_id
    assert (g.max_pages, g.max_tokens, g.depth_limit, g.domain_budget) == (500, 500_000, 5, 50)


def test_goal_prompt_req():
    with pytest.raises(ValidationError):
        Goal()  # type: ignore[call-arg]
