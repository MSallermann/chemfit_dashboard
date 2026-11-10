"""Small Qt bridge for keyed thread and process work."""

from __future__ import annotations

import os
import struct
import traceback
from collections.abc import Hashable
from concurrent.futures import (
    CancelledError,
    Future,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
)
from contextlib import suppress
from dataclasses import dataclass, field
from enum import Enum
from multiprocessing import get_context
from multiprocessing.shared_memory import SharedMemory
from threading import Event
from typing import TYPE_CHECKING, Generic, Literal, TypeVar

from PySide6.QtCore import QObject, QTimer, Signal, Slot

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

GenerationKey = TypeVar("GenerationKey", bound=Hashable)
TaskBackend = Literal["thread", "process"]
_PROCESS_PROGRESS = struct.Struct("<QQ")
_PROCESS_SEQUENCE = struct.Struct("<Q")
_PROCESS_CANCEL_OFFSET = _PROCESS_SEQUENCE.size + _PROCESS_PROGRESS.size
_PROCESS_STATE_SIZE = _PROCESS_CANCEL_OFFSET + 1


@dataclass(slots=True)
class GenerationTracker(Generic[GenerationKey]):
    """Issue freshness tokens for independent request families."""

    _values: dict[GenerationKey, int] = field(default_factory=dict)

    def advance(self, key: GenerationKey) -> int:
        token = self._values.get(key, 0) + 1
        self._values[key] = token
        return token

    def current(self, key: GenerationKey) -> int:
        return self._values.setdefault(key, 0)

    def is_current(self, key: GenerationKey, token: int) -> bool:
        return self.current(key) == token

    def invalidate(self, *keys: GenerationKey) -> None:
        for key in keys:
            self.advance(key)

    def invalidate_all(self) -> None:
        self.invalidate(*tuple(self._values))


class TaskState(str, Enum):
    SUBMITTED = "submitted"
    RUNNING = "running"
    PROGRESS = "progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INVALIDATED = "invalidated"


@dataclass(frozen=True, slots=True)
class TaskInfo:
    id: int
    key: str | None
    label: str | None
    backend: TaskBackend
    display_delay_ms: int


@dataclass(frozen=True, slots=True)
class TaskEvent:
    task: TaskInfo
    state: TaskState
    progress: tuple[int, int] | None = None
    error: str | None = None


class TaskContext:
    """Opt-in progress and cooperative cancellation passed to worker code."""

    def __init__(
        self,
        report: Callable[[int, int], None],
        cancelled: Callable[[], bool],
    ) -> None:
        """Create a context from lightweight reporting and cancellation hooks."""
        self._report = report
        self._cancelled = cancelled

    @property
    def cancelled(self) -> bool:
        return self._cancelled()

    def report_progress(self, value: int, total: int) -> None:
        self._report(value, total)

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise CancelledError


class _ProcessContext(TaskContext):
    def __init__(self, shared_memory_name: str) -> None:
        self._memory = SharedMemory(name=shared_memory_name)
        self._sequence = 0
        super().__init__(self._send_progress, self._is_cancelled)

    def _send_progress(self, value: int, total: int) -> None:
        # A tiny seqlock keeps the GUI from observing a partly written pair.
        self._sequence += 1
        _PROCESS_SEQUENCE.pack_into(self._memory.buf, 0, self._sequence)
        _PROCESS_PROGRESS.pack_into(
            self._memory.buf,
            _PROCESS_SEQUENCE.size,
            max(0, value),
            max(1, total),
        )
        self._sequence += 1
        _PROCESS_SEQUENCE.pack_into(self._memory.buf, 0, self._sequence)

    def _is_cancelled(self) -> bool:
        return bool(self._memory.buf[_PROCESS_CANCEL_OFFSET])

    def close(self) -> None:
        self._memory.close()


def _run_process(
    function: Callable[..., Any],
    args: tuple[Any, ...],
    shared_memory_name: str | None,
) -> Any:
    """Serializable process-pool entry point."""
    context = (
        _ProcessContext(shared_memory_name) if shared_memory_name is not None else None
    )
    try:
        if context is None:
            return function(*args)
        return function(*args, task_context=context)
    finally:
        if context is not None:
            context.close()


class _Bridge(QObject):
    """Marshal all worker notifications onto the runner's Qt thread."""

    worker_event = Signal(int, object, object)


@dataclass(slots=True)
class _TaskRecord:
    info: TaskInfo
    on_success: Callable[[Any], None]
    on_error: Callable[[str], None]
    cancellation: Event | None = None
    future: Future[Any] | None = None
    process_state: SharedMemory | None = None
    process_sequence: int = 0
    invalidated: bool = False


class TaskRunner(QObject):
    """Run keyed work and report its lifecycle on the GUI thread."""

    lifecycle_changed = Signal(object)
    thread_workers_changed = Signal(int)
    process_workers_changed = Signal(int)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        thread_workers: int | None = None,
        process_workers: int | None = None,
    ) -> None:
        """Create a runner with a lazy spawn-based process pool."""
        super().__init__(parent)
        self.thread_workers = max(
            1, thread_workers or min(32, (os.cpu_count() or 1) + 4)
        )
        self.process_workers = max(1, process_workers or min(2, os.cpu_count() or 1))
        self._thread_pool = ThreadPoolExecutor(
            max_workers=self.thread_workers,
            thread_name_prefix="chemfit-dashboard",
        )
        self._process_pool: ProcessPoolExecutor | None = None
        self._tasks: dict[int, _TaskRecord] = {}
        self._latest: dict[str, int] = {}
        self._next_id = 1
        self._closed = False

        self._bridge = _Bridge(self)
        self._bridge.worker_event.connect(self._worker_event)
        self._progress_timer = QTimer(self)
        self._progress_timer.setInterval(40)
        self._progress_timer.timeout.connect(self._poll_progress)

    @property
    def process_executor(self) -> ProcessPoolExecutor:
        """Expose the shared pool to the search chunk coordinator."""
        if self._closed:
            message = "TaskRunner is shut down"
            raise RuntimeError(message)
        if self._process_pool is None:
            self._process_pool = ProcessPoolExecutor(
                max_workers=self.process_workers,
                mp_context=get_context("spawn"),
            )
        return self._process_pool

    def set_thread_workers(self, count: int) -> None:
        """Use a new thread pool size without interrupting already-running work."""
        count = max(1, count)
        if self._closed or count == self.thread_workers:
            return
        previous = self._thread_pool
        self.thread_workers = count
        self._thread_pool = ThreadPoolExecutor(
            max_workers=count,
            thread_name_prefix="chemfit-dashboard",
        )
        previous.shutdown(wait=False)
        self.thread_workers_changed.emit(count)

    def set_process_workers(self, count: int) -> None:
        """Replace the lazily created process pool with the requested size."""
        count = max(1, count)
        if self._closed or count == self.process_workers:
            return
        previous = self._process_pool
        self._process_pool = None
        self.process_workers = count
        self.process_workers_changed.emit(count)
        if previous is not None:
            previous.shutdown(wait=False)

    def submit(
        self,
        *,
        function: Callable[..., Any],
        on_success: Callable[[Any], None],
        on_error: Callable[[str], None],
        backend: TaskBackend = "thread",
        label: str | None = None,
        args: tuple[Any, ...] = (),
        with_context: bool = False,
        display_delay_ms: int = 0,
    ) -> None:
        self._submit(
            None,
            function,
            on_success,
            on_error,
            backend,
            label,
            args,
            with_context,
            display_delay_ms,
        )

    def submit_latest(
        self,
        *,
        key: str,
        function: Callable[..., Any],
        on_success: Callable[[Any], None],
        on_error: Callable[[str], None],
        backend: TaskBackend = "thread",
        label: str | None = None,
        args: tuple[Any, ...] = (),
        with_context: bool = False,
        display_delay_ms: int = 0,
    ) -> None:
        self.invalidate(key)
        self._submit(
            key,
            function,
            on_success,
            on_error,
            backend,
            label,
            args,
            with_context,
            display_delay_ms,
        )

    def _submit(
        self,
        key: str | None,
        function: Callable[..., Any],
        on_success: Callable[[Any], None],
        on_error: Callable[[str], None],
        backend: TaskBackend,
        label: str | None,
        args: tuple[Any, ...],
        with_context: bool,
        display_delay_ms: int,
    ) -> None:
        if self._closed:
            message = "TaskRunner is shut down"
            raise RuntimeError(message)
        if backend not in ("thread", "process"):
            message = f"Unknown task backend: {backend}"
            raise ValueError(message)
        task_id = self._next_id
        self._next_id += 1
        info = TaskInfo(task_id, key, label, backend, max(0, display_delay_ms))
        record = _TaskRecord(info, on_success, on_error)
        self._tasks[task_id] = record
        if key is not None:
            self._latest[key] = task_id
        self._emit(record, TaskState.SUBMITTED)
        if backend == "thread":
            self._start_thread(record, function, args, with_context)
        else:
            self._start_process(record, function, args, with_context)

    def _start_thread(
        self,
        record: _TaskRecord,
        function: Callable[..., Any],
        args: tuple[Any, ...],
        with_context: bool,
    ) -> None:
        cancellation = Event()
        record.cancellation = cancellation
        bridge = self._bridge
        task_id = record.info.id

        def run() -> Any:
            if cancellation.is_set():
                raise CancelledError
            bridge.worker_event.emit(task_id, TaskState.RUNNING, None)
            if not with_context:
                return function(*args)
            context = TaskContext(
                lambda value, total: bridge.worker_event.emit(
                    task_id, TaskState.PROGRESS, (value, total)
                ),
                cancellation.is_set,
            )
            return function(*args, task_context=context)

        self._watch(record, self._thread_pool.submit(run))

    def _start_process(
        self,
        record: _TaskRecord,
        function: Callable[..., Any],
        args: tuple[Any, ...],
        with_context: bool,
    ) -> None:
        shared_memory_name = None
        if with_context:
            record.process_state = SharedMemory(create=True, size=_PROCESS_STATE_SIZE)
            record.process_state.buf[:] = bytes(_PROCESS_STATE_SIZE)
            shared_memory_name = record.process_state.name
            self._progress_timer.start()
        try:
            future = self.process_executor.submit(
                _run_process, function, args, shared_memory_name
            )
        except Exception:
            self._worker_event(record.info.id, TaskState.FAILED, traceback.format_exc())
            return
        self._emit(record, TaskState.RUNNING)
        self._watch(record, future)

    def _watch(self, record: _TaskRecord, future: Future[Any]) -> None:
        """Retain a future and route its outcome through the Qt event bridge."""
        record.future = future
        bridge = self._bridge
        task_id = record.info.id

        def completed(done: Future[Any]) -> None:
            try:
                result = done.result()
            except CancelledError:
                bridge.worker_event.emit(task_id, TaskState.CANCELLED, None)
            except Exception:
                bridge.worker_event.emit(
                    task_id, TaskState.FAILED, traceback.format_exc()
                )
            else:
                bridge.worker_event.emit(task_id, TaskState.COMPLETED, result)

        future.add_done_callback(completed)

    @Slot(int, object, object)
    def _worker_event(
        self, task_id: int, state: TaskState, payload: object | None
    ) -> None:
        record = self._tasks.get(task_id)
        if record is None:
            return
        if state == TaskState.RUNNING:
            self._emit(record, TaskState.RUNNING)
            return
        if state == TaskState.PROGRESS:
            if not record.invalidated:
                assert isinstance(payload, tuple)
                value, total = payload
                self._emit(
                    record,
                    state,
                    (max(0, int(value)), max(1, int(total))),
                )
            return

        details = str(payload) if state == TaskState.FAILED else None
        record = self._finish(task_id, state, details)
        if record is None or record.invalidated:
            return
        if state == TaskState.FAILED:
            record.on_error(details or "Unknown task failure")
        elif state == TaskState.COMPLETED:
            try:
                record.on_success(payload)
            except Exception:
                record.on_error(traceback.format_exc())

    def _cancel(self, task_id: int) -> None:
        record = self._tasks.get(task_id)
        if record is None:
            return
        if record.cancellation is not None:
            record.cancellation.set()
        if record.process_state is not None:
            record.process_state.buf[_PROCESS_CANCEL_OFFSET] = 1
        if record.future is not None:
            record.future.cancel()

    def invalidate(self, key: str) -> None:
        task_id = self._latest.pop(key, None)
        record = self._tasks.get(task_id) if task_id is not None else None
        if record is None or record.invalidated:
            return
        record.invalidated = True
        self._emit(record, TaskState.INVALIDATED)
        self._cancel(record.info.id)

    def _poll_progress(self) -> None:
        has_process_state = False
        for task_id, record in tuple(self._tasks.items()):
            if record.process_state is None:
                continue
            has_process_state = True
            self._poll_one(task_id, record)
        if not has_process_state:
            self._progress_timer.stop()

    def _finish(
        self,
        task_id: int,
        state: TaskState,
        error: str | None = None,
    ) -> _TaskRecord | None:
        record = self._tasks.get(task_id)
        if record is None:
            return None
        self._poll_one(task_id, record)
        self._tasks.pop(task_id, None)
        if record.info.key is not None and self._latest.get(record.info.key) == task_id:
            self._latest.pop(record.info.key, None)
        self._close_process_state(record)
        self._emit(record, state, error=error)
        return record

    def _poll_one(self, task_id: int, record: _TaskRecord) -> None:
        memory = record.process_state
        if memory is None:
            return
        sequence_before = _PROCESS_SEQUENCE.unpack_from(memory.buf, 0)[0]
        if sequence_before == 0 or sequence_before % 2:
            return
        value, total = _PROCESS_PROGRESS.unpack_from(memory.buf, _PROCESS_SEQUENCE.size)
        sequence_after = _PROCESS_SEQUENCE.unpack_from(memory.buf, 0)[0]
        if (
            sequence_before != sequence_after
            or sequence_after == record.process_sequence
        ):
            return
        record.process_sequence = sequence_after
        self._worker_event(task_id, TaskState.PROGRESS, (value, total))

    @staticmethod
    def _close_process_state(record: _TaskRecord) -> None:
        memory = record.process_state
        if memory is None:
            return
        record.process_state = None
        memory.close()
        with suppress(FileNotFoundError):
            memory.unlink()

    def _emit(
        self,
        record: _TaskRecord,
        state: TaskState,
        progress: tuple[int, int] | None = None,
        error: str | None = None,
    ) -> None:
        self.lifecycle_changed.emit(TaskEvent(record.info, state, progress, error))

    def shutdown(self, *, wait_for_processes: bool = False) -> None:
        if self._closed:
            return
        for task_id in tuple(self._tasks):
            self._cancel(task_id)
        self._closed = True
        self._progress_timer.stop()
        for record in self._tasks.values():
            self._close_process_state(record)
        self._tasks.clear()
        self._latest.clear()
        self._thread_pool.shutdown(wait=False, cancel_futures=True)
        if self._process_pool is not None:
            self._process_pool.shutdown(
                wait=wait_for_processes,
                cancel_futures=True,
            )
            self._process_pool = None
