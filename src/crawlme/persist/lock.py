"""An OS-owned lock prevents two crawl processes from writing one run."""

from __future__ import annotations

import fcntl
from pathlib import Path
from typing import IO


class Lock:
    def __init__(self, db_path: str) -> None:
        self.path = Path(db_path).with_suffix(".lock")
        self.file: IO[str] | None = None

    def acquire(self) -> None:
        self.file = self.path.open("a")
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.release()
            raise ValueError("this run is already active in another process") from exc

    def release(self) -> None:
        if self.file is not None:
            self.file.close()
            self.file = None
