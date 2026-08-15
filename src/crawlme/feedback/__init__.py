"""The feedback subsystem: optional, injectable, and self-contained.

Page analyses flow in (analyzer), run-scoped signals aggregate them
(signals), and cross-task domain reputation persists them
(domain_prior).  The scheduler only ever talks to the FeedbackSystem
facade; the factory is the only place that wires the pieces together.
"""
