"""Pioneer: the path a candidate takes from discovery to a fetch.

    prefilter    cheap per-candidate rules, before anything is stored
    buffer       candidates waiting to be scored, a turn from each seed
    ranker       what turns a candidate into a priority
    queue        candidates that have a score, waiting for a fetch slot
    frontier     owns both halves, plus gating, budgets and checkpoints

    canonicalizer, robots, sources, goal_enhancer  --  supporting parts

Each module holds one concept and, where that concept has a contract,
the contract sits with it.  A second implementation is what earns a
concept its own package; none has one yet.
"""
