"""Transient terminal activity, kept out of persistent log records."""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from collections.abc import Callable, Coroutine
from functools import wraps
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")
_FRAMES = "|/-\\"
if TYPE_CHECKING:
    _StreamHandler = logging.StreamHandler[Any]
else:
    _StreamHandler = logging.StreamHandler


class ProgressHandler(_StreamHandler):
    """Keep active stages below ordinary console logs."""

    def __init__(self, stream: Any, *, color: bool = False) -> None:
        super().__init__(stream)
        self._color = color
        self.active: dict[object, tuple[str, float]] = {}
        self._timer: asyncio.TimerHandle | None = None
        self._lines = 0
        self._previous: list[str] = []
        self._frame = 0
        self._stopped = False

    def begin(self, stage: str) -> object:
        token = object()
        self.active[token] = (stage, time.monotonic())
        self._refresh()
        return token

    def end(self, token: object) -> None:
        self.active.pop(token, None)
        self._refresh()

    def _clear(self) -> None:
        if self._lines:
            self.stream.write(f"\033[{self._lines}A\r\033[J")
            self._lines = 0
            self._previous = []

    def _draw(self, message: str | None = None) -> None:
        stages: dict[str, list[float]] = {}
        for stage, started in self.active.values():
            stages.setdefault(stage, []).append(started)
        width, height = shutil.get_terminal_size()
        now = time.monotonic()
        lines = []
        for stage, starts in list(stages.items())[: max(1, height - 2)]:
            elapsed = int(now - min(starts))
            duration = f"{elapsed // 60}m {elapsed % 60:02d}s" if elapsed >= 60 else f"{elapsed}s"
            line = f"{_FRAMES[self._frame % 4]} {stage}  {len(starts)} running · oldest {duration}"
            lines.append(line[: max(1, width - 1)])
        output = [f"\033[{self._lines}A\r"] if self._lines else []
        if message is None and lines and [line[1:] for line in lines] == [line[1:] for line in self._previous]:
            # The text is unchanged: overwrite only each spinner, then return below the block.
            output.extend(self._paint(line[0]) + "\r\033[1B" for line in lines)
        else:
            if message is not None:
                output.append(message.replace("\n", "\033[K\n") + "\033[K\n")
            output.extend(self._paint(line) + "\033[K\n" for line in lines)
            if len(lines) < self._lines:
                output.append("\033[J")
        self._lines = len(lines)
        self._previous = lines
        if output:
            self.stream.write("".join(output))
            self.stream.flush()

    def _paint(self, text: str) -> str:
        return f"\033[38;2;217;119;87m{text}\033[0m" if self._color else text

    def _refresh(self) -> None:
        self.acquire()
        try:
            if self._stopped:
                return
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self._draw()
            if self.active:
                self._frame += 1
                self._timer = asyncio.get_running_loop().call_later(0.15, self._refresh)
        finally:
            self.release()

    def emit(self, record: logging.LogRecord) -> None:
        if not self._lines and not self.active:
            super().emit(record)
            return
        try:
            self._draw(self.format(record))
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        self.acquire()
        try:
            self._stopped = True
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self.active.clear()
            painted = self._lines > 0
            self._clear()
            if painted:
                self.flush()
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
