"""State contract shared by checkpoint participants."""

from abc import abstractmethod
from typing import Any, Protocol


class Checkpointable(Protocol):
    @abstractmethod
    def dump(self) -> dict[str, Any]: ...

    @abstractmethod
    def restore(self, state: dict[str, Any]) -> None: ...
