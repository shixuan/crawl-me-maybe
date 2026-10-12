"""Pioneer: the path a candidate takes from discovery to a fetch.

    prefilter    cheap per-candidate rules, before anything is stored
    buffer       candidates waiting to be scored, a turn from each seed
    ranker       what turns a candidate into a priority
    queue        candidates that have a score, waiting for a fetch slot
    frontier     owns both halves, plus gating, budgets and checkpoints

    canonicalizer, robots, sources, goal_enhancer  --  supporting parts

Frontier is exported here; interchangeable components keep their contracts.
"""

from crawlme.pioneer.frontier import Frontier

__all__ = ["Frontier"]
