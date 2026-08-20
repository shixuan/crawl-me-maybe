"""The path a candidate takes from discovery to a fetch.

Everything here is one pipeline, and the modules are its stages:

    prefilter   cheap per-candidate rules, before anything is stored
    buffer      candidates waiting to be scored, a turn from each seed
    queue       candidates that have a score, waiting for a fetch slot
    gated       the frontier itself: budgets, dedup, checkpoints

They were spread across the package and read as four unrelated things,
which is part of how a fairness rule ended up on the wrong stage.
"""

from crawlme.pioneer.frontier.buffer import Buffer, RoundRobinBuffer
from crawlme.pioneer.frontier.gated import Frontier, GatedFrontier
from crawlme.pioneer.frontier.prefilter import Decision, PreFilter, PreFilterContext
from crawlme.pioneer.frontier.queue import Gate, GateFn, PriorityQueue

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
