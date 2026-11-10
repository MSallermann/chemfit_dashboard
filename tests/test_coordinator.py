from __future__ import annotations

import json
import time

import pytest

from chemfit_dashboard.source import JsonFileSource


def _wait_for_record(coordinator, qapp, record_id: str) -> None:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        document = coordinator.selected_document
        if document is not None and document.record.id == record_id:
            return
        qapp.processEvents()
        time.sleep(0.005)
    pytest.fail(f"Timed out selecting record {record_id}")


def test_record_selection_rejects_records_outside_current_collection(
    tmp_path, coordinator
):
    current_path = tmp_path / "current.json"
    outside_path = tmp_path / "outside.json"
    current_path.write_text("{}", encoding="utf-8")
    outside_path.write_text("{}", encoding="utf-8")
    source = JsonFileSource()
    current = source.discover([current_path])[0]
    outside = source.discover([outside_path])[0]
    coordinator.collection.replace((current,))

    with pytest.raises(ValueError, match="not part of the current collection"):
        coordinator.select_record(outside)
    with pytest.raises(ValueError, match="not part of the current collection"):
        coordinator.select_location(outside, ())

    assert coordinator.selected_document is None


def test_clearing_field_prevents_restoration_in_another_record(
    tmp_path, coordinator, qapp
):
    for index in range(2):
        (tmp_path / f"step_{index}.json").write_text(
            json.dumps({"output": {"energy": index}}),
            encoding="utf-8",
        )
    records = JsonFileSource().discover([tmp_path])
    coordinator.collection.replace(records)
    coordinator.select_record(records[0])
    _wait_for_record(coordinator, qapp, records[0].id)
    coordinator.select_field(("output", "energy"))

    coordinator.select_field(None)
    coordinator.select_record(records[1])
    _wait_for_record(coordinator, qapp, records[1].id)

    assert coordinator.selected_field is None
    assert coordinator.selected_field_value is None
