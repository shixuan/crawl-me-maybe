from __future__ import annotations

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
