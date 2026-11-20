"""Monotonic time budget shared by all stages of one run."""

from time import monotonic


class RunClock:
    def __init__(self) -> None:
        self._start: float | None = None
        self.limit = 0

    def start(self, limit: int) -> None:
        """Start once, including when the crawl runs without optional startup."""
        if self._start is None:
            self.limit = limit
            self._start = monotonic()

    def restore(self, elapsed: float) -> None:
        self._start = monotonic() - elapsed

    def elapsed(self) -> float:
        return 0.0 if self._start is None else monotonic() - self._start

    def remaining(self) -> float | None:
        if self.limit <= 0 or self._start is None:
            return None
        return max(0.0, self.limit - self.elapsed())

    def expired(self) -> bool:
        return self.remaining() == 0
