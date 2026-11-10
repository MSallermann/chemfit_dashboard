from __future__ import annotations

import csv
import json
import runpy
from pathlib import Path

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QFileDialog

from chemfit_dashboard.extraction import (
    ExtractionConfig,
    ExtractionStatus,
    FieldSelector,
    OutputDescriptor,
    export_csv,
    export_jsonl,
    extract_fields,
    load_config,
    resolve_fields,
    save_config,
)
from chemfit_dashboard.extraction_ui import (
    CONFIG_DIRECTORY_SETTING,
    EXPORT_DIRECTORY_SETTING,
    DataExtractionPanel,
)
from chemfit_dashboard.inspector import Inspector
from chemfit_dashboard.source import JsonFileSource
from chemfit_dashboard.traversal import CancellationToken, OperationCancelled


def _field(
    path=(),
    value_path=(),
    column=None,
    **options,
):
    return FieldSelector(path, value_path), OutputDescriptor(column, **options)


def test_readme_extraction_api_example_uses_current_public_api(tmp_path, monkeypatch):
    readme = Path(__file__).parents[1] / "README.md"
    text = readme.read_text(encoding="utf-8")
    marked = text.split("<!-- extraction-api-example:start -->", 1)[1].split(
        "<!-- extraction-api-example:end -->", 1
    )[0]
    code = marked.split("```python", 1)[1].split("```", 1)[0]

    collection = tmp_path / "examples" / "context_collection"
    collection.mkdir(parents=True)
    (collection / "step_0.json").write_text(
        json.dumps({"output": 1.25}), encoding="utf-8"
    )
    script = tmp_path / "readme_extraction_example.py"
    script.write_text(code, encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    runpy.run_path(str(script), run_name="__main__")

    rows = list(csv.reader((tmp_path / "losses.csv").open(encoding="utf-8")))
    assert rows[0][3:] == ["output@/"]
    assert rows[1][3:] == ["1.25"]


def test_extraction_reports_null_missing_failure_and_available_values(tmp_path):
    values = {
        "available.json": {"output": 3.5},
        "failure.json": {"output": "ValueError: invalid"},
        "missing.json": {"other": 1},
        "null.json": {"output": None},
    }
    for name, value in values.items():
        (tmp_path / name).write_text(json.dumps(value), encoding="utf-8")
    source = JsonFileSource()
    records = source.discover([tmp_path])

    statuses = {
        row.filename: row.values[0].status
        for row in extract_fields(source, records, (FieldSelector((), ("output",)),))
    }

    assert statuses == {
        "available.json": ExtractionStatus.AVAILABLE,
        "failure.json": ExtractionStatus.SERIALIZED_EXCEPTION,
        "missing.json": ExtractionStatus.MISSING_FIELD,
        "null.json": ExtractionStatus.EXPLICIT_NULL,
    }

    destination = tmp_path / "values.csv"
    config = ExtractionConfig(((FieldSelector((), ("output",)), OutputDescriptor()),))
    export_csv(destination, source, records, config)
    with destination.open(encoding="utf-8", newline="") as stream:
        csv_values = {
            row["filename"]: row["output@/"] for row in csv.DictReader(stream)
        }
    assert csv_values == {
        "available.json": "3.5",
        "failure.json": "",
        "missing.json": "",
        "null.json": "",
    }


def test_csv_omits_serialized_exceptions_inside_flattened_values(tmp_path):
    message = "RuntimeError: intentional failure in the structure loss term"
    (tmp_path / "step.json").write_text(
        json.dumps(
            {
                "output": {
                    "energy": 1.25,
                    "structure_loss": message,
                }
            }
        ),
        encoding="utf-8",
    )
    source = JsonFileSource()
    records = source.discover([tmp_path])
    destination = tmp_path / "values.csv"
    config = ExtractionConfig(
        (
            (
                FieldSelector((), ("output",)),
                OutputDescriptor(flatten=True),
            ),
        )
    )

    export_csv(destination, source, records, config)

    with destination.open(encoding="utf-8", newline="") as stream:
        row = next(csv.DictReader(stream))
    assert row["output.energy@/"] == "1.25"
    assert row["output.structure_loss@/"] == ""


def test_component_exports_preserve_json_and_flatten_csv(tmp_path):
    payload = {"children": [{"output": {"energy": 4, "force": [1, 2]}}]}
    (tmp_path / "step.json").write_text(json.dumps(payload), encoding="utf-8")
    source = JsonFileSource()
    records = source.discover([tmp_path])
    config = ExtractionConfig((_field((0,), column="component", flatten=True),))
    csv_path = tmp_path / "values.csv"
    jsonl_path = tmp_path / "values.jsonl"

    assert export_csv(csv_path, source, records, config) == 1
    assert export_jsonl(jsonl_path, source, records, config) == 1

    header = next(csv.reader(csv_path.open(encoding="utf-8", newline="")))
    result = json.loads(jsonl_path.read_text(encoding="utf-8"))
    assert "component.output.energy@/0" in header
    assert result["values"]["component@/0"]["output"]["energy"] == 4


@pytest.mark.parametrize(
    ("column", "include_node_paths", "expected"),
    [
        (None, True, "output.force@/2/0"),
        (None, False, "output.force"),
        ("force", True, "force@/2/0"),
        ("force", False, "force"),
    ],
)
def test_output_names_use_one_field_first_path_rule(
    column, include_node_paths, expected
):
    fields = resolve_fields(
        JsonFileSource(),
        (),
        (
            (
                FieldSelector((2, 0), ("output", "force")),
                OutputDescriptor(column=column),
            ),
        ),
        include_node_paths=include_node_paths,
    )

    assert fields[0].output.column == expected


def test_wildcard_selector_expands_union_of_sibling_paths(tmp_path):
    payloads = {
        "first.json": {
            "children": [
                {
                    "children": [
                        {"output": {"energy": 1}},
                        {"output": {"energy": 2}},
                    ]
                }
            ]
        },
        "second.json": {
            "children": [
                {
                    "children": [
                        {"output": {"energy": 3}},
                        {"output": {"energy": 4}},
                        {"output": {"energy": 5}},
                    ]
                }
            ]
        },
    }
    for name, payload in payloads.items():
        (tmp_path / name).write_text(json.dumps(payload), encoding="utf-8")
    source = JsonFileSource()
    records = source.discover([tmp_path])
    config = ExtractionConfig(
        (_field((0, "*"), ("output", "energy")),),
    )
    csv_path = tmp_path / "wildcard.csv"
    jsonl_path = tmp_path / "wildcard.jsonl"

    assert export_csv(csv_path, source, records, config) == 2
    assert export_jsonl(jsonl_path, source, records, config) == 2

    rows = list(csv.DictReader(csv_path.open(encoding="utf-8", newline="")))
    columns = [f"output.energy@/0/{index}" for index in range(3)]
    assert [rows[0][column] for column in columns] == ["1", "2", ""]
    assert [rows[1][column] for column in columns] == ["3", "4", "5"]

    payload = json.loads(jsonl_path.read_text(encoding="utf-8").splitlines()[0])
    assert payload["values"] == {
        "output.energy@/0/0": 1,
        "output.energy@/0/1": 2,
        "output.energy@/0/2": None,
    }
    assert payload["status"]["output.energy@/0/2"] == "missing_component"


def test_array_expansion_keeps_one_row_per_evaluation(tmp_path):
    (tmp_path / "step.json").write_text(
        json.dumps({"positions": [[1, 2], [3, 4]]}),
        encoding="utf-8",
    )
    source = JsonFileSource()
    records = source.discover([tmp_path])
    config = ExtractionConfig(
        (
            _field(
                value_path=("positions",), column="positions", array_expansion_depth=2
            ),
        )
    )
    destination = tmp_path / "positions.csv"

    assert export_csv(destination, source, records, config) == 1

    rows = list(csv.reader(destination.open(encoding="utf-8", newline="")))
    assert len(rows) == 2
    assert {"positions[0][0]@/", "positions[1][1]@/"}.issubset(rows[0])


def test_configuration_round_trip_uses_current_schema(tmp_path):
    config = ExtractionConfig(
        (
            _field(
                (1, "*"),
                ("output",),
                "energy",
                flatten=True,
                excluded_paths=(("debug",),),
                flattened_column_names=((("force",), "force_ev"),),
            ),
        ),
        include_identifiers=False,
        include_node_paths=False,
    )
    path = tmp_path / "extraction.json"

    save_config(path, config)

    assert load_config(path) == config
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 6


def test_extraction_honors_cancellation_and_progress(tmp_path):
    for index in range(3):
        (tmp_path / f"step_{index}.json").write_text(
            json.dumps({"value": index}), encoding="utf-8"
        )
    source = JsonFileSource()
    records = source.discover([tmp_path])
    cancellation = CancellationToken()
    progress = []
    rows = extract_fields(
        source,
        records,
        (FieldSelector((), ("value",)),),
        cancellation=cancellation,
        progress=lambda count, _record: progress.append(count),
    )

    assert next(rows).values[0].value == 0
    cancellation.cancel()
    with pytest.raises(OperationCancelled):
        next(rows)
    assert progress == [1]


def test_inspector_copies_full_component_and_json_path(loaded_coordinator, qapp):
    inspector = Inspector(coordinator=loaded_coordinator)
    loaded_coordinator.select_context((1,))
    inspector.on_selected_context_changed((1,))
    index = inspector.summary.model().index_for_value_path(("output",))
    inspector.summary.setCurrentIndex(index.siblingAtColumn(1))

    inspector.summary.copy_current_path()

    assert qapp.clipboard().text() == "output@/1"


def test_inspector_can_add_field_or_component_from_all_siblings(loaded_coordinator):
    inspector = Inspector(coordinator=loaded_coordinator)
    loaded_coordinator.select_context((1,))
    inspector.on_selected_context_changed((1,))
    output = inspector.summary.model().index_for_value_path(("output",))
    inspector.summary.setCurrentIndex(output.siblingAtColumn(1))
    requested = []
    inspector.extraction_requested.connect(requested.append)
    inspector.component_extraction_requested.connect(requested.append)

    inspector.extract_sibling_fields_button.click()
    inspector.extract_sibling_components_button.click()

    assert [selector for selector, _descriptor in requested] == [
        FieldSelector(("*",), ("output",)),
        FieldSelector(("*",)),
    ]


def test_extraction_panel_manages_unique_columns_and_clipboard(coordinator, qapp):
    panel = DataExtractionPanel(coordinator=coordinator)
    panel.add_selector(*_field(value_path=("energy",), column="energy"))
    panel.add_selector(*_field(value_path=("force",), column="energy"))
    panel.include_node_paths_checkbox.setChecked(False)

    assert [output.column for output in panel.outputs] == ["energy", "energy_2"]
    assert panel.selections.item(0, 1).text() == "energy@/"
    assert not panel.config.include_node_paths

    panel.copy_configuration()
    copied = ExtractionConfig.from_dict(json.loads(qapp.clipboard().text()))
    assert copied == panel.config

    panel.clear()
    panel.set_config(copied)
    assert panel.n_selectors == 2
    assert not panel.include_node_paths_checkbox.isChecked()


def test_extraction_dialog_directories_are_persisted(
    tmp_path, coordinator, monkeypatch
):
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    panel = DataExtractionPanel(coordinator=coordinator, settings=settings)
    panel.add_selector(*_field(value_path=("energy",), column="energy"))
    config_path = tmp_path / "configs" / "fields.json"
    export_path = tmp_path / "exports" / "values.csv"
    config_path.parent.mkdir()
    export_path.parent.mkdir()
    monkeypatch.setattr(
        QFileDialog,
        "getSaveFileName",
        lambda *_args, **_kwargs: (str(config_path), "JSON (*.json)"),
    )
    panel._save()  # noqa: SLF001
    monkeypatch.setattr(
        QFileDialog,
        "getSaveFileName",
        lambda *_args, **_kwargs: (str(export_path), "CSV (*.csv)"),
    )
    monkeypatch.setattr(panel, "export_extraction", lambda **_kwargs: None)

    panel._export("csv")  # noqa: SLF001

    assert settings.value(CONFIG_DIRECTORY_SETTING) == str(config_path.parent)
    assert settings.value(EXPORT_DIRECTORY_SETTING) == str(export_path.parent)
