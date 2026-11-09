"""Focused equivalence tests for process-backed extraction exports."""

import json
import time

import pytest

from chemfit_dashboard.extraction import (
    ExtractionConfig,
    FieldSelector,
    OutputDescriptor,
    export_csv,
    export_json_file_process,
    export_jsonl,
)
from chemfit_dashboard.source import JsonFileSource
from chemfit_dashboard.workers import TaskRunner, TaskState


def _wait_for(qapp, predicate, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        qapp.processEvents()
        if time.monotonic() >= deadline:
            pytest.fail("Timed out waiting for process export")
        time.sleep(0.005)


@pytest.mark.parametrize("format_name", ["csv", "jsonl"])
def test_process_export_matches_existing_export(tmp_path, qapp, format_name):
    collection = tmp_path / "collection"
    collection.mkdir()
    for index in range(3):
        (collection / f"record_{index}.json").write_text(
            json.dumps({"output": index * 2}),
            encoding="utf-8",
        )
    source = JsonFileSource()
    records = tuple(source.discover([collection]))
    config = ExtractionConfig(
        ((FieldSelector((), ("output",)), OutputDescriptor("value")),)
    )
    expected = tmp_path / f"expected.{format_name}"
    actual = tmp_path / f"actual.{format_name}"
    if format_name == "csv":
        export_csv(expected, source, records, config)
    else:
        export_jsonl(expected, source, records, config)

    runner = TaskRunner(process_workers=1)
    results = []
    errors = []
    events = []
    runner.lifecycle_changed.connect(events.append)
    runner.submit(
        function=export_json_file_process,
        on_success=results.append,
        on_error=errors.append,
        backend="process",
        args=(str(actual), format_name, records, config),
        with_context=True,
    )

    _wait_for(qapp, lambda: bool(results or errors))
    assert not errors
    assert results == [3]
    assert actual.read_bytes() == expected.read_bytes()
    assert any(event.state == TaskState.PROGRESS for event in events)
    runner.shutdown(wait_for_processes=True)


def test_failed_process_export_does_not_replace_destination(tmp_path, qapp):
    destination = tmp_path / "values.txt"
    destination.write_text("keep me", encoding="utf-8")
    runner = TaskRunner(process_workers=1)
    errors = []
    runner.submit(
        function=export_json_file_process,
        on_success=lambda _count: pytest.fail("invalid export unexpectedly succeeded"),
        on_error=errors.append,
        backend="process",
        args=(str(destination), "invalid", (), ExtractionConfig(())),
        with_context=True,
    )

    _wait_for(qapp, lambda: bool(errors))
    assert destination.read_text(encoding="utf-8") == "keep me"
    assert not tuple(tmp_path.glob(".values.txt.*.tmp"))
    runner.shutdown(wait_for_processes=True)
