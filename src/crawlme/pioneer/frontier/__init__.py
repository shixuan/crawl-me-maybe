"""The path a candidate takes from discovery to a fetch.

    prefilter   cheap per-candidate rules, before anything is stored
    buffer      candidates waiting to be scored, a turn from each seed
    queue       candidates that have a score, waiting for a fetch slot
    gated       the frontier: it owns both, plus budgets and checkpoints

base.py holds the contracts; everything else here satisfies one.
"""

from crawlme.pioneer.frontier.base import Buffer, Frontier, Gate, GateFn
from crawlme.pioneer.frontier.buffer import RoundRobinBuffer
from crawlme.pioneer.frontier.gated import GatedFrontier
from crawlme.pioneer.frontier.prefilter import Decision, PreFilter, PreFilterContext
from crawlme.pioneer.frontier.queue import PriorityQueue

__all__ = [
    "Buffer",
    "Decision",
    "Frontier",
    "Gate",
    "GateFn",
    "GatedFrontier",
    "PreFilter",
    "PreFilterContext",
    "PriorityQueue",
    "RoundRobinBuffer",
]
