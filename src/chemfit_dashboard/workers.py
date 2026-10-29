from __future__ import annotations

import traceback
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any


class _TaskSignals(QObject):
    succeeded = Signal(object)
    failed = Signal(str)


class _Task(QRunnable):
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
