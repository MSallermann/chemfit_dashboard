"""
Minimal Qt thread-pool adapter for non-GUI callables.

``TaskRunner`` executes callables on Qt's global thread pool and delivers
completion callbacks to the GUI thread. Queued tasks can be cancelled before
execution; running tasks are not interrupted.
"""

from __future__ import annotations

import traceback
from collections.abc import Hashable
from dataclasses import dataclass, field
from threading import Event
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

    Cancelling a worker may arrive too late. Callers capture the token
    returned by :meth:`advance` and accept a callback only while
    :meth:`is_current` remains true.
    """

    _values: dict[GenerationKey, int] = field(default_factory=dict)

    def advance(self, key: GenerationKey) -> int:
        """Invalidate older work for ``key`` and return the new token."""
        token = self._values.get(key, 0) + 1
        self._values[key] = token
        return token

    def current(self, key: GenerationKey) -> int:
        """Return the current token without invalidating existing work."""
        # Observe the key so invalidate_all() includes this request family.
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
    """Deliver worker completion notifications to the GUI thread."""

    succeeded = Signal(object)
    failed = Signal(str)
    cancelled = Signal()


class _Task(QRunnable):
    """Execute a callable unless cancelled before it starts."""

    def __init__(self, function: Callable[[], Any]):
        super().__init__()
        self.function = function
        self.signals = _TaskSignals()
        self._cancelled = Event()

    def cancel(self) -> None:
        """Prevent execution if the task has not started."""
        self._cancelled.set()

    @Slot()
    def run(self) -> None:
        if self._cancelled.is_set():
            self.signals.cancelled.emit()
            return

        try:
            result = self.function()
        except Exception:
            self.signals.failed.emit(traceback.format_exc())
        else:
            self.signals.succeeded.emit(result)


class TaskRunner(QObject):
    """Submit background tasks to Qt's shared thread pool."""

    def __init__(self, parent: QObject | None = None):
        """Initialize the runner and track tasks until completion."""
        super().__init__(parent)
        self._pool = QThreadPool.globalInstance()
        self._active: set[_Task] = set()
        self._generations: GenerationTracker = GenerationTracker()

    def cancel_pending(self) -> None:
        """
        Cancel tasks that have not started executing.

        Running tasks are not interrupted. Cancelled tasks remain in the
        thread-pool queue until a worker picks them up and skips execution.
        """
        for task in tuple(self._active):
            task.cancel()

    def submit(
        self,
        function: Callable[[], Any],
        on_success: Callable[[Any], None],
        on_error: Callable[[str], None],
    ) -> None:
        """Submit a callable and register its completion callbacks."""
        task = _Task(function)
        self._active.add(task)

        def succeeded(result: Any) -> None:
            self._active.discard(task)
            try:
                on_success(result)
            except Exception:
                on_error(traceback.format_exc())

        def failed(details: str) -> None:
            self._active.discard(task)
            on_error(details)

        def cancelled() -> None:
            self._active.discard(task)

        task.signals.succeeded.connect(succeeded)
        task.signals.failed.connect(failed)
        task.signals.cancelled.connect(cancelled)

        self._pool.start(task)

    def submit_latest(
        self,
        key: str,
        function: Callable[[], Any],
        on_success: Callable[[Any], None],
        on_error: Callable[[str], None],
    ) -> None:
        """
        Submit a task, superseding previous tasks with the same key.

        Only callbacks belonging to the most recently submitted task for
        a given key are invoked. Results and errors from older tasks are
        silently discarded.

        Different keys are independent and may execute concurrently.
        Running tasks are not interrupted.
        """
        generation = self._generations.advance(key)

        def succeeded(result: Any) -> None:
            if self._generations.is_current(key, generation):
                on_success(result)

        def failed(details: str) -> None:
            if self._generations.is_current(key, generation):
                on_error(details)

        self.submit(function, succeeded, failed)

    def invalidate(self, key: str) -> None:
        """Discard callbacks from outstanding tasks associated with key."""
        self._generations.invalidate(key)
