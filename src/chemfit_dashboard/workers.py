"""
Minimal Qt thread-pool adapter for non-GUI callables.

``TaskRunner`` executes a callable on Qt's global pool and always delivers its
success or traceback callback on the owning GUI thread. Running tasks require
their own cooperative cancellation token.
"""

from __future__ import annotations

import traceback
from collections.abc import Hashable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Generic, TypeVar

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

GenerationKey = TypeVar("GenerationKey", bound=Hashable)


@dataclass(slots=True)
class GenerationTracker(Generic[GenerationKey]):
    """
    Issue freshness tokens for independent families of asynchronous work.

    Cancelling a worker is cooperative and may arrive too late. Callers capture
    the token returned by :meth:`advance` and accept a callback only while
    :meth:`is_current` remains true. Keeping this policy here avoids parallel
    ``*_generation`` counters whose relationship is otherwise undocumented.
    """

    _values: dict[GenerationKey, int] = field(default_factory=dict)

    def advance(self, key: GenerationKey) -> int:
        """Invalidate older work for ``key`` and return the new token."""
        token = self._values.get(key, 0) + 1
        self._values[key] = token
        return token

    def current(self, key: GenerationKey) -> int:
        """Return the current token without invalidating existing work."""
        # Reading observes the request family so ``invalidate_all`` also covers
        # work whose first token is the implicit zero generation.
        return self._values.setdefault(key, 0)

    def is_current(self, key: GenerationKey, token: int) -> bool:
        """Return whether ``token`` may still update state for ``key``."""
        return self.current(key) == token

    def invalidate(self, *keys: GenerationKey) -> None:
        """Invalidate outstanding work for each supplied request family."""
        for key in keys:
            self.advance(key)

    def invalidate_all(self) -> None:
        """Invalidate every request family observed by this tracker."""
        self.invalidate(*tuple(self._values))


class _TaskSignals(QObject):
    """Marshal worker completion back to the owning Qt thread."""

    succeeded = Signal(object)
    failed = Signal(str)


class _Task(QRunnable):
    """Execute one callable and convert failures to printable tracebacks."""

    def __init__(self, function: Callable[[], Any]):
        """Store one function for execution on a worker thread."""
        super().__init__()
        self.function = function
        self.signals = _TaskSignals()

    @Slot()
    def run(self) -> None:
        try:
            result = self.function()
        except Exception:
            self.signals.failed.emit(traceback.format_exc())
        else:
            self.signals.succeeded.emit(result)


class TaskRunner(QObject):
    """Run source operations on Qt's global worker pool."""

    def __init__(self, parent: QObject | None = None):
        """Use Qt's shared pool and retain tasks until their signals arrive."""
        super().__init__(parent)
        self._pool = QThreadPool.globalInstance()
        self._active: set[_Task] = set()

    def cancel_pending(self) -> None:
        """Discard queued work; running tasks must stop cooperatively."""
        self._pool.clear()

    def submit(
        self,
        function: Callable[[], Any],
        on_success: Callable[[Any], None],
        on_error: Callable[[str], None],
    ) -> None:
        task = _Task(function)
        self._active.add(task)

        def succeeded(result: Any) -> None:
            self._active.discard(task)
            on_success(result)

        def failed(details: str) -> None:
            self._active.discard(task)
            on_error(details)

        task.signals.succeeded.connect(succeeded)
        task.signals.failed.connect(failed)
        self._pool.start(task)
