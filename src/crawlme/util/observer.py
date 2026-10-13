"""Observe decorated mutations without adding notification state to their owners."""

from collections.abc import Awaitable, Callable
from functools import wraps
from inspect import iscoroutinefunction
from typing import Any, Concatenate, ParamSpec, TypeVar, cast
from weakref import ReferenceType, WeakMethod, ref

Owner = TypeVar("Owner")
Args = ParamSpec("Args")
Result = TypeVar("Result")
_Notifier = Callable[[], None]
_Reader = Callable[[Any], object]
_Listener = tuple[_Reader, Callable[[], _Notifier | None]]
_watchers: dict[int, tuple[ReferenceType[object], dict[object, _Listener]]] = {}


def subscribe(owner: object, read: _Reader, notify: _Notifier) -> Callable[[], None]:
    """Observe detached values by instance identity and return an unsubscribe function."""
    key = id(owner)
    entry = _watchers.get(key)
    if entry is None or entry[0]() is not owner:

        def remove(reference: ReferenceType[object]) -> None:
            current = _watchers.get(key)
            if current is not None and current[0] is reference:
                _watchers.pop(key, None)

        entry = (ref(owner, remove), {})
        _watchers[key] = entry
    reference, listeners = entry
    callback: Callable[[], _Notifier | None]
    try:
        callback = WeakMethod(notify)
    except TypeError:

        def callback() -> _Notifier:
            return notify

    token = object()
    listeners[token] = (read, callback)

    def unsubscribe() -> None:
        current = _watchers.get(key)
        if current is not None and current[0] is reference:
            current[1].pop(token, None)
            if not current[1]:
                _watchers.pop(key, None)

    return unsubscribe


def _capture(owner: object) -> dict[object, object]:
    entry = _watchers.get(id(owner))
    if entry is None or entry[0]() is not owner:
        return {}
    return {token: read(owner) for token, (read, callback) in list(entry[1].items()) if callback() is not None}


def _publish(owner: object, before: dict[object, object]) -> None:
    entry = _watchers.get(id(owner))
    if entry is None or entry[0]() is not owner:
        return
    for token, value in before.items():
        listener = entry[1].get(token)
        if listener is not None:
            read, callback = listener
            notify = callback()
            if notify is not None and read(owner) != value:
                notify()


def notify(method: Callable[Concatenate[Owner, Args], Result]) -> Callable[Concatenate[Owner, Args], Result]:
    """Publish watched updates synchronously, including after errors and cancellation."""
    if iscoroutinefunction(method):
        async_method = cast(Callable[Concatenate[Owner, Args], Awaitable[Any]], method)

        @wraps(method)
        async def run_async(owner: Owner, /, *args: Args.args, **kwargs: Args.kwargs) -> Any:
            before = _capture(owner)
            try:
                return await async_method(owner, *args, **kwargs)
            finally:
                _publish(owner, before)

        return cast(Callable[Concatenate[Owner, Args], Result], run_async)

    @wraps(method)
    def run(owner: Owner, /, *args: Args.args, **kwargs: Args.kwargs) -> Result:
        before = _capture(owner)
        try:
            return method(owner, *args, **kwargs)
        finally:
            _publish(owner, before)

    return run
