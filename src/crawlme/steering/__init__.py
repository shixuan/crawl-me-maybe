"""The steering half of the feedback loop: run-scoped signals and the
facade the scheduler talks to.

Analyzer output flows in; run-scoped signals aggregate it (signals);
the cross-task domain prior persists it (storage).  The scheduler only
ever talks to the SteeringSystem facade; the factory is the only place
that wires the pieces together.
"""

from crawlme.steering.loop import SteeringLoop, SteeringSystem
from crawlme.steering.signals import InflightSignals

__all__ = ["InflightSignals", "SteeringLoop", "SteeringSystem"]
