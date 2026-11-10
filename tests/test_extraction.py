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
    column="value",
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
    assert rows[0][3:5] == ["loss", "loss__status"]
    assert rows[1][3:5] == ["1.25", "available"]


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
    assert "component.output.energy" in header
    assert result["values"]["component"]["output"]["energy"] == 4


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
    assert {"positions.[0][0]", "positions.[1][1]"}.issubset(rows[0])


def test_configuration_round_trip_uses_current_schema(tmp_path):
    config = ExtractionConfig(
        (
            _field(
                (1,),
                ("output",),
                "energy",
                flatten=True,
                excluded_paths=(("debug",),),
                flattened_column_names=((("force",), "force_ev"),),
            ),
        ),
        include_identifiers=False,
    )
    path = tmp_path / "extraction.json"

    save_config(path, config)

    assert load_config(path) == config
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 3


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

    assert qapp.clipboard().text() == "/1 : output"


def test_extraction_panel_manages_unique_columns_and_clipboard(coordinator, qapp):
    panel = DataExtractionPanel(coordinator=coordinator)
    panel.add_selector(*_field(value_path=("energy",), column="energy"))
    panel.add_selector(*_field(value_path=("force",), column="energy"))

    assert [output.column for output in panel.outputs] == ["energy", "energy_2"]
    assert panel.selections.item(0, 1).text() == "/ : energy"

    panel.copy_configuration()
    copied = ExtractionConfig.from_dict(json.loads(qapp.clipboard().text()))
    assert copied == panel.config

    panel.clear()
    panel.set_config(copied)
    assert panel.n_selectors == 2


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
