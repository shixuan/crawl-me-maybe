"""The table that lists what a traversal chooses.

Its whole value is that a new row cannot quietly inherit a link graph's
answer, so most of these tests guard the table rather than exercise it.
"""

from __future__ import annotations

import dataclasses
import re
from unittest.mock import patch

import pytest

from crawlme.cli import main
from crawlme.config import Settings
from crawlme.pioneer.ranker.rule import FEED_FACTORS, GRAPH_FACTORS
from crawlme.scheduler.traversal import DEFAULT, TRAVERSALS, Traversal, feed_kinds, traversal_for


def test_every_row_answers_every_question():
    """A field with a default is a link graph's answer in disguise.

    Five of them were, and each was found by a run going wrong rather
    than by reading the code.
    """
    for field in dataclasses.fields(Traversal):
        assert field.default is dataclasses.MISSING, f"{field.name} may not have a default"
        assert field.default_factory is dataclasses.MISSING, f"{field.name} may not have a default"


def test_row_missing_a_field_fails_to_load():
    with pytest.raises(TypeError):
        Traversal(name="half", adapter=None, factors=GRAPH_FACTORS)  # type: ignore[call-arg]


def test_traversal_is_data_only():
    """Behaviour on a row is how a table becomes the place special cases go."""
    methods = [n for n in vars(Traversal) if not n.startswith("__") and callable(getattr(Traversal, n, None))]
    assert methods == [], f"Traversal grew behaviour: {methods}"


@pytest.mark.parametrize("name", list(TRAVERSALS))
def test_row_named_after_its_key(name):
    assert TRAVERSALS[name].name == name


def test_unknown_source_kind_is_a_link_graph():
    assert traversal_for("something-else") is DEFAULT
    assert traversal_for("") is DEFAULT


def test_feed_flag_offers_adapter_rows(capsys):
    """Otherwise the flag accepts a value the factory cannot build."""
    assert feed_kinds() == sorted(n for n, t in TRAVERSALS.items() if t.adapter is not None)
    with patch("sys.argv", ["crawl", "run", "--help"]), pytest.raises(SystemExit):
        main()
    offered = re.search(r"--feed \{([^}]*)\}", capsys.readouterr().out)
    assert offered is not None
    assert sorted(offered.group(1).split(",")) == feed_kinds()


def test_settings_default_names_a_row():
    assert Settings().source_kind in TRAVERSALS


#: the choices themselves --------------------------------------------------


def test_feed_has_no_domain_ceiling():
    """Every post shares the platform's host, so the ceiling is a total."""
    assert TRAVERSALS["instagram"].domain_budget == 0
    assert TRAVERSALS["links"].domain_budget > 0


def test_feed_is_two_levels_deep():
    """A listing and its posts, and there is no third."""
    assert TRAVERSALS["instagram"].depth_limit == 1
    assert TRAVERSALS["links"].depth_limit > 1


def test_feed_never_ends_on_age():
    """It is ordered per account and never as a whole."""
    assert TRAVERSALS["instagram"].time_horizon is False
    assert TRAVERSALS["links"].time_horizon is True


def test_each_kind_scores_on_its_own_signals():
    assert TRAVERSALS["instagram"].factors is FEED_FACTORS
    assert TRAVERSALS["links"].factors is GRAPH_FACTORS


def test_only_a_feed_asks_a_page_for_more_of_itself():
    assert TRAVERSALS["instagram"].scrolls > 0
    assert TRAVERSALS["links"].scrolls == 0
