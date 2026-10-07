"""Transient terminal activity, kept out of persistent log records."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Coroutine
from functools import wraps
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar

from rich.console import Console, Group
from rich.live import Live
from rich.spinner import Spinner
from rich.text import Text

P = ParamSpec("P")
T = TypeVar("T")
if TYPE_CHECKING:
    _StreamHandler = logging.StreamHandler[Any]
else:
    _StreamHandler = logging.StreamHandler


class ProgressHandler(_StreamHandler):
    """Keep active stages below ordinary console logs."""

    def __init__(self, stream: Any, *, color: bool = False) -> None:
        super().__init__(stream)
        self.console = Console(file=stream, no_color=not color, highlight=False)
        self._style = "#e07ea4" if color else ""
        self.active: dict[object, tuple[str, float]] = {}
        self._spinners: dict[str, Spinner] = {}
        self.live = Live(
            console=self.console,
            get_renderable=self._render,
            transient=True,
            refresh_per_second=6,
        )
        self._stopped = False

    def begin(self, stage: str) -> object:
        token = object()
        self.active[token] = (stage, time.monotonic())
        if not self._stopped:
            if not self.live.is_started:
                self.live.start(refresh=True)
            else:
                self.live.refresh()
        return token

    def end(self, token: object) -> None:
        self.active.pop(token, None)
        if self.live.is_started:
            if self.active:
                self.live.refresh()
            else:
                self.live.stop()
                self._spinners.clear()

    def _render(self) -> Group:
        stages: dict[str, list[float]] = {}
        for stage, started in self.active.copy().values():
            stages.setdefault(stage, []).append(started)
        now = time.monotonic()
        rows = []
        for stage, starts in stages.items():
            elapsed = int(now - min(starts))
            duration = f"{elapsed // 60}m {elapsed % 60:02d}s" if elapsed >= 60 else f"{elapsed}s"
            spinner = self._spinners.setdefault(stage, Spinner("line", style=self._style))
            spinner.update(
                text=Text(
                    f"{stage}  {len(starts)} running · oldest {duration}",
                    style=self._style,
                    no_wrap=True,
                    overflow="ellipsis",
                )
            )
            rows.append(spinner)
        return Group(*rows)

    def emit(self, record: logging.LogRecord) -> None:
        if not self.live.is_started:
            super().emit(record)
            return
        try:
            self.console.print(Text.from_ansi(self.format(record)), soft_wrap=True)
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        self.acquire()
        try:
            self._stopped = True
            self.active.clear()
            self.live.stop()
            self._spinners.clear()
        finally:
            self.release()
            super().close()


def activity(stage: str) -> Callable[[Callable[P, Coroutine[Any, Any, T]]], Callable[P, Coroutine[Any, Any, T]]]:
    """Track an async operation through completion, failure or cancellation."""

    def decorate(fn: Callable[P, Coroutine[Any, Any, T]]) -> Callable[P, Coroutine[Any, Any, T]]:
        @wraps(fn)
        async def wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
            root = logging.getLogger()
            tokens = [
                (handler, handler.begin(stage))
                for handler in root.handlers
                if isinstance(handler, ProgressHandler) and root.isEnabledFor(logging.INFO)
            ]
            try:
                return await fn(*args, **kwargs)
            finally:
                for handler, token in tokens:
                    handler.end(token)

        return wrapped

    return decorate
