from __future__ import annotations

import csv
import json

import pytest

from chemfit_dashboard.extraction import (
    ExtractionConfig,
    ExtractionStatus,
    FieldSelector,
    export_csv,
    export_jsonl,
    extract_fields,
    load_config,
    save_config,
    selector_for_document,
)
from chemfit_dashboard.extraction_ui import DataExtractionPanel
from chemfit_dashboard.inspector import Inspector
from chemfit_dashboard.source import JsonFileSource
from chemfit_dashboard.traversal import CancellationToken, OperationCancelled


def _write_semantic_collection(tmp_path):
    structure = {
        "children": [
            {"index": 0, "name": "duplicate"},
            {"index": 1, "name": "duplicate"},
        ]
    }
    values = (
        {
            "meta": {"chemfit.structure": structure},
            "children": [
                {"output": {"items": [{"value": None}], "empty": {}}},
                {"output": {"items": [{"value": 4}], "a/b": 1, "a~b": 2}},
            ],
        },
        {
            "meta": {"chemfit.structure": structure},
            "children": [
                {"output": {"items": [{"value": 9}]}},
                {"output": {"skipped": True}},
            ],
        },
    )
    for index, value in enumerate(values):
        (tmp_path / f"step_{index}.json").write_text(
            json.dumps(value), encoding="utf-8"
        )


def test_extracts_mixed_paths_with_semantic_identity_and_statuses(tmp_path):
    _write_semantic_collection(tmp_path)
    source = JsonFileSource(cache_capacity=1)
    records = source.discover([tmp_path])
    first = source.load(records[0])
    selector = selector_for_document(
        first,
        FieldSelector(
            node_path=(1,),
            section="fields",
            value_path=("output", "items", 0, "value"),
            column="value",
        ),
    )

    rows = list(extract_fields(source, records, (selector,)))

    assert rows[0].values[0].value == 4
    assert rows[0].values[0].status is ExtractionStatus.AVAILABLE
    assert rows[1].values[0].status is ExtractionStatus.SKIPPED_COMPONENT
    assert selector.node_trail is not None
    assert selector.node_trail[-1].occurrence == 1


def test_explicit_null_missing_field_component_and_serialized_exception(tmp_path):
    paths = {
        "null.json": {"output": None},
        "missing.json": {"other": 1},
        "failure.json": {"output": "ValueError: invalid"},
    }
    for name, value in paths.items():
        (tmp_path / name).write_text(json.dumps(value), encoding="utf-8")
    source = JsonFileSource()
    records = source.discover([tmp_path])
    selector = FieldSelector((), "fields", ("output",), "result")

    statuses = {
        row.filename: row.values[0].status
        for row in extract_fields(source, records, (selector,))
    }

    assert statuses == {
        "failure.json": ExtractionStatus.SERIALIZED_EXCEPTION,
        "missing.json": ExtractionStatus.MISSING_FIELD,
        "null.json": ExtractionStatus.EXPLICIT_NULL,
    }


def test_component_extraction_csv_flatten_and_jsonl_preserve_structure(tmp_path):
    _write_semantic_collection(tmp_path)
    source = JsonFileSource()
    records = source.discover([tmp_path])
    document = source.load(records[0])
    component = selector_for_document(
        document,
        FieldSelector((1,), "component", column="component", flatten=True),
    )
    config = ExtractionConfig((component,))
    csv_path = tmp_path / "values.csv"
    jsonl_path = tmp_path / "values.jsonl"

    assert export_csv(csv_path, source, records, config) == 2
    assert export_jsonl(jsonl_path, source, records, config) == 2

    with csv_path.open(encoding="utf-8", newline="") as stream:
        csv_rows = list(csv.reader(stream))
    assert "component.fields/output/a~1b" in csv_rows[0]
    assert "component.fields/output/a~0b" in csv_rows[0]
    payload = json.loads(jsonl_path.read_text(encoding="utf-8").splitlines()[0])
    assert payload["values"]["component"]["fields"]["output"]["a/b"] == 1
    assert payload["status"]["component"] == "available"


def test_array_expansion_is_long_format_and_never_implicit_cartesian_product(tmp_path):
    (tmp_path / "one.json").write_text(
        json.dumps({"a": [1, 2], "b": [3, 4]}), encoding="utf-8"
    )
    source = JsonFileSource()
    records = source.discover([tmp_path])
    first = FieldSelector((), "fields", ("a",), "a", expand_array=True)
    second = FieldSelector((), "fields", ("b",), "b")
    output = tmp_path / "long.jsonl"

    assert export_jsonl(output, source, records, ExtractionConfig((first, second))) == 2
    assert [
        json.loads(line)["values"]["a"]
        for line in output.read_text(encoding="utf-8").splitlines()
    ] == [1, 2]

    ambiguous = ExtractionConfig(
        (first, FieldSelector((), "fields", ("b",), "b", expand_array=True))
    )
    with pytest.raises(ValueError, match="Cartesian"):
        list(extract_fields(source, records, ambiguous.selectors))


def test_configuration_round_trip_and_cancellation(tmp_path):
    selector = FieldSelector(
        (2, 1),
        "metadata",
        ("key", 0),
        "custom",
        flatten=True,
    )
    config = ExtractionConfig((selector,), include_status_columns=False)
    path = tmp_path / "config.json"
    save_config(path, config)

    assert load_config(path) == config

    source = JsonFileSource()
    record_path = tmp_path / "record.json"
    record_path.write_text("{}", encoding="utf-8")
    token = CancellationToken()
    token.cancel()
    with pytest.raises(OperationCancelled):
        list(
            extract_fields(
                source,
                source.discover([record_path]),
                (FieldSelector((), "fields", (), "root"),),
                cancellation=token,
            )
        )


def test_inspector_and_workspace_manage_arbitrary_field_selection(chemfit_document):
    inspector = Inspector()
    inspector.set_details(chemfit_document.details(()))
    requested = []
    components = []
    inspector.extraction_requested.connect(requested.append)
    inspector.component_extraction_requested.connect(components.append)
    inspector._value_selected("metadata", (("chemfit.structure",), {}))  # noqa: SLF001

    inspector.extract_button.click()
    inspector.extract_component_button.click()

    assert requested[0].section == "metadata"
    assert requested[0].value_path == ("chemfit.structure",)
    assert components[0].section == "component"

    panel = DataExtractionPanel()
    panel.add_selector(requested[0])
    panel.add_selector(components[0])
    panel.selections.setCurrentCell(1, 0)
    panel.up_button.click()
    panel.selections.item(0, 0).setText("whole_component")

    assert [selector.column for selector in panel.selectors] == [
        "whole_component",
        "chemfit.structure",
    ]
    panel.remove_button.click()
    assert len(panel.selectors) == 1
