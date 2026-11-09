"""Tests for asynchronous request coordination primitives."""

import threading
import time
from enum import Enum, auto

import pytest
from PySide6.QtCore import QThread
from PySide6.QtWidgets import QMainWindow

from chemfit_dashboard.activity import ActivityStatus
from chemfit_dashboard.workers import (
    GenerationTracker,
    TaskRunner,
    TaskState,
)


def _sum_values(left: int, right: int) -> int:
    return left + right


def _fail_task() -> None:
    message = "expected worker failure"
    raise ValueError(message)


def _slow_value(value: int, delay: float) -> int:
    time.sleep(delay)
    return value


def _wait_for(qapp, predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        qapp.processEvents()
        if time.monotonic() >= deadline:
            pytest.fail("Timed out waiting for background task")
        time.sleep(0.005)


class _Request(Enum):
    LOAD = auto()
    SEARCH = auto()


def test_generation_tracker_keeps_request_families_independent():
    generations = GenerationTracker[_Request]()

    load = generations.advance(_Request.LOAD)
    search = generations.advance(_Request.SEARCH)

    assert generations.is_current(_Request.LOAD, load)
    assert generations.is_current(_Request.SEARCH, search)
    generations.invalidate(_Request.LOAD)
    assert not generations.is_current(_Request.LOAD, load)
    assert generations.is_current(_Request.SEARCH, search)


def test_generation_tracker_can_invalidate_all_observed_requests():
    generations = GenerationTracker[_Request]()
    tokens = {
        request: generations.advance(request)
        for request in (_Request.LOAD, _Request.SEARCH)
    }

    generations.invalidate_all()

    assert all(
        not generations.is_current(request, token) for request, token in tokens.items()
    )


@pytest.mark.parametrize("backend", ["thread", "process"])
def test_task_runner_executes_backends_and_delivers_on_gui_thread(qapp, backend):
    runner = TaskRunner(process_workers=1)
    results = []
    callback_threads = []

    def succeeded(result):
        results.append(result)
        callback_threads.append(QThread.currentThread())

    runner.submit(
        function=_sum_values,
        on_success=succeeded,
        on_error=pytest.fail,
        backend=backend,
        args=(20, 22),
    )

    _wait_for(qapp, lambda: bool(results))
    assert results == [42]
    assert callback_threads == [qapp.thread()]
    runner.shutdown(wait_for_processes=True)


def test_task_runner_reports_failures(qapp):
    runner = TaskRunner(process_workers=1)
    errors = []
    runner.submit(
        function=_fail_task,
        on_success=lambda _result: pytest.fail("failure task unexpectedly succeeded"),
        on_error=errors.append,
        backend="thread",
    )

    _wait_for(qapp, lambda: bool(errors))
    assert "expected worker failure" in errors[0]
    runner.shutdown(wait_for_processes=True)


def test_submit_latest_invalidates_obsolete_result(qapp):
    runner = TaskRunner(process_workers=1)
    results = []
    events = []
    runner.lifecycle_changed.connect(events.append)
    runner.submit_latest(
        key="preview",
        function=_slow_value,
        on_success=results.append,
        on_error=pytest.fail,
        backend="thread",
        args=(1, 0.08),
    )
    runner.submit_latest(
        key="preview",
        function=_slow_value,
        on_success=results.append,
        on_error=pytest.fail,
        backend="thread",
        args=(2, 0.0),
    )

    _wait_for(qapp, lambda: results == [2])
    _wait_for(
        qapp,
        lambda: sum(
            event.state in {TaskState.COMPLETED, TaskState.CANCELLED}
            for event in events
        )
        == 2,
    )
    assert results == [2]
    assert any(event.state == TaskState.INVALIDATED for event in events)
    runner.shutdown(wait_for_processes=True)


def test_activity_status_tracks_labeled_runner_task(qapp):
    runner = TaskRunner()
    window = QMainWindow()
    activity = ActivityStatus(window)
    runner.lifecycle_changed.connect(activity.task_changed)
    release = threading.Event()
    completed = []
    runner.submit(
        function=lambda: release.wait(2),
        on_success=completed.append,
        on_error=pytest.fail,
        label="Waiting for data",
    )

    _wait_for(qapp, lambda: bool(activity.labels))
    assert tuple(activity.labels.values()) == ("Waiting for data",)
    release.set()
    _wait_for(qapp, lambda: completed == [True])
    _wait_for(qapp, lambda: not activity.labels)
    runner.shutdown()
    window.close()
