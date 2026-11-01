from __future__ import annotations

import csv
import json

import pytest
from PySide6.QtCore import QItemSelectionModel, QSettings, Qt
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHeaderView,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QToolButton,
)

from chemfit_dashboard.extraction import (
    ExtractionConfig,
    ExtractionRow,
    ExtractionStatus,
    ExtractionValue,
    FieldSelector,
    export_csv,
    export_jsonl,
    extract_fields,
    load_config,
    save_config,
    selector_for_document,
)
from chemfit_dashboard.extraction_ui import (
    CONFIG_DIRECTORY_SETTING,
    EXPORT_DIRECTORY_SETTING,
    DataExtractionPanel,
)
from chemfit_dashboard.inspector import Inspector
from chemfit_dashboard.source import JsonFileSource
from chemfit_dashboard.traversal import CancellationToken, OperationCancelled


def _inline_action(panel: DataExtractionPanel, row: int, name: str) -> QToolButton:
    actions = panel.selections.cellWidget(row, 2)
    assert actions is not None
    button = actions.findChild(QToolButton, name)
    assert button is not None
    return button


def _array_depth(panel: DataExtractionPanel, row: int) -> QSpinBox:
    actions = panel.selections.cellWidget(row, 2)
    assert actions is not None
    control = actions.findChild(QSpinBox, "arrayExpansionDepth")
    assert control is not None
    return control


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


def test_array_expansion_creates_columns_and_preserves_one_row_per_evaluation(tmp_path):
    (tmp_path / "one.json").write_text(
        json.dumps({"positions": [[1, 2, 3], [4, 5, 6]], "b": [7, 8]}),
        encoding="utf-8",
    )
    source = JsonFileSource()
    records = source.discover([tmp_path])
    positions = FieldSelector(
        (),
        "fields",
        ("positions",),
        "positions",
        array_expansion_depth=2,
    )
    second = FieldSelector(
        (), "fields", ("b",), "b", array_expansion_depth=1
    )
    csv_output = tmp_path / "wide.csv"
    jsonl_output = tmp_path / "structured.jsonl"

    assert export_csv(
        csv_output, source, records, ExtractionConfig((positions, second))
    ) == 1
    with csv_output.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.reader(stream))
    assert rows[0][3:9] == [
        "positions.0/0",
        "positions.0/1",
        "positions.0/2",
        "positions.1/0",
        "positions.1/1",
        "positions.1/2",
    ]
    assert rows[1][3:9] == ["1", "2", "3", "4", "5", "6"]
    assert export_jsonl(
        jsonl_output, source, records, ExtractionConfig((positions, second))
    ) == 1
    payload = json.loads(jsonl_output.read_text(encoding="utf-8"))
    assert payload["values"]["positions"] == [[1, 2, 3], [4, 5, 6]]


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


def test_version_one_array_expansion_migrates_to_one_column_level():
    legacy = {
        "version": 1,
        "include_identifiers": True,
        "include_status_columns": False,
        "selectors": [
            {
                "component": {"preferred_path": [], "trail": None},
                "section": "fields",
                "path": ["positions"],
                "column": "positions",
                "flatten": False,
                "expand_array": True,
                "excluded_paths": [],
                "flattened_column_names": [],
            }
        ],
    }

    migrated = ExtractionConfig.from_dict(legacy)

    assert migrated.version == 2
    assert migrated.selectors[0].array_expansion_depth == 1
    assert migrated.selectors[0].column_expansion_depth == 1


def test_extraction_and_exports_report_completed_records(tmp_path):
    _write_semantic_collection(tmp_path)
    source = JsonFileSource()
    records = source.discover([tmp_path])
    selector = FieldSelector((), "fields", (), "root", flatten=True)
    extraction_progress = []

    list(
        extract_fields(
            source,
            records,
            (selector,),
            progress=lambda completed, record: extraction_progress.append(
                (completed, record.name)
            ),
        )
    )

    assert extraction_progress == [
        (1, records[0].name),
        (2, records[1].name),
    ]

    export_progress = []
    export_csv(
        tmp_path / "progress.csv",
        source,
        records,
        ExtractionConfig((selector,)),
        progress=lambda completed, _record: export_progress.append(completed),
    )

    assert export_progress == [1, 2, 3, 4]


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
    _inline_action(panel, 1, "moveUpAction").click()
    panel.selections.item(0, 0).setText("whole_component")

    assert [selector.column for selector in panel.selectors] == [
        "whole_component",
        "chemfit.structure",
    ]
    _inline_action(panel, 0, "removeAction").click()
    assert len(panel.selectors) == 1


def test_inspector_values_and_paths_can_be_copied(chemfit_document, qapp):
    inspector = Inspector()
    inspector.set_details(chemfit_document.details((1,)))
    output_index = inspector.summary.model().index(1, 1)
    inspector.summary.setCurrentIndex(output_index)

    inspector.summary.copy_current_value()
    assert qapp.clipboard().text() == str(output_index.data())

    inspector.summary.copy_current_path()
    assert qapp.clipboard().text() == "fields/output"

    inspector.summary.copy_current_value(as_json=True)
    assert json.loads(qapp.clipboard().text()) == 3.0


def test_extraction_configuration_and_preview_can_be_copied(qapp):
    panel = DataExtractionPanel()
    assert (
        panel.selections.contextMenuPolicy() == Qt.ContextMenuPolicy.CustomContextMenu
    )
    assert panel.preview.contextMenuPolicy() == Qt.ContextMenuPolicy.CustomContextMenu
    selector = FieldSelector((), "fields", ("energy",), "energy")
    panel.add_selector(selector)
    assert not panel.dirty_indicator.isHidden()
    panel.copy_config_button.click()

    copied_config = json.loads(qapp.clipboard().text())
    assert copied_config == panel.config.to_dict()
    panel.clear()
    qapp.clipboard().setText(json.dumps(copied_config))
    panel.paste_config_button.click()
    assert panel.config.to_dict() == copied_config

    panel.selections.setCurrentCell(0, 0)
    panel._selector_copy_shortcut.activated.emit()  # noqa: SLF001
    copied_selector = json.loads(qapp.clipboard().text())
    assert copied_selector["column"] == "energy"
    assert copied_selector["path"] == ["energy"]

    panel.set_preview(
        (
            ExtractionRow(
                0,
                "record-1",
                "step_1.json",
                (ExtractionValue(3.5, ExtractionStatus.AVAILABLE),),
            ),
        ),
        1,
    )
    value_index = panel.preview_model.index(0, 3)
    panel.preview.setCurrentIndex(value_index)
    panel._preview_copy_shortcut.activated.emit()  # noqa: SLF001
    assert qapp.clipboard().text() == "3.5"

    panel._copy_preview_column(3)  # noqa: SLF001
    assert qapp.clipboard().text().splitlines() == ["energy", "3.5"]

    panel._copy_preview_row(0, as_json=True)  # noqa: SLF001
    copied_row = json.loads(qapp.clipboard().text())
    assert copied_row == {
        "Index": 0,
        "Filename": "step_1.json",
        "Record ID": "record-1",
        "energy": 3.5,
    }

    panel._copy_preview_table()  # noqa: SLF001
    assert qapp.clipboard().text().splitlines() == [
        "Index,Filename,Record ID,energy",
        "0,step_1.json,record-1,3.5",
    ]


def test_extraction_remembers_dialog_directories_and_saved_state(tmp_path, monkeypatch):
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    panel = DataExtractionPanel(settings=settings)
    panel.add_selector(FieldSelector((), "fields", ("energy",), "energy"))
    config_path = tmp_path / "configs" / "extract.json"
    config_path.parent.mkdir()
    monkeypatch.setattr(
        QFileDialog,
        "getSaveFileName",
        lambda *_args, **_kwargs: (str(config_path), "JSON (*.json)"),
    )

    panel._save()  # noqa: SLF001

    assert panel.dirty_indicator.isHidden()
    assert settings.value(CONFIG_DIRECTORY_SETTING) == str(config_path.parent)

    export_path = tmp_path / "exports" / "values.csv"
    export_path.parent.mkdir()
    monkeypatch.setattr(
        QFileDialog,
        "getSaveFileName",
        lambda *_args, **_kwargs: (str(export_path), "CSV (*.csv)"),
    )
    requested = []
    panel.export_requested.connect(lambda *args: requested.append(args))

    panel._export("csv")  # noqa: SLF001

    assert requested
    assert requested[0][0] == str(export_path)
    assert settings.value(EXPORT_DIRECTORY_SETTING) == str(export_path.parent)


def test_extraction_options_immediately_rebuild_preview_shape():
    panel = DataExtractionPanel()
    panel.add_selector(FieldSelector((), "fields", column="payload"))
    rows = (
        ExtractionRow(
            0,
            "record",
            "record.json",
            (
                ExtractionValue(
                    [
                        {"metrics": {"energy": 1}},
                        {"metrics": {"energy": 2}},
                    ],
                    ExtractionStatus.AVAILABLE,
                ),
            ),
        ),
    )
    panel.set_preview(rows, 1)
    panel.selectors_changed.connect(lambda _selectors: panel.set_preview(rows, 1))

    _inline_action(panel, 0, "expandArraysAction").click()

    assert panel.preview_model.rowCount() == 1
    assert panel.preview_model.columnCount() == 5
    assert panel.preview_model.data(panel.preview_model.index(0, 3)) == (
        '{"metrics":{"energy":1}}'
    )
    assert panel.preview_model.data(panel.preview_model.index(0, 4)) == (
        '{"metrics":{"energy":2}}'
    )

    flatten = _inline_action(panel, 0, "flattenAction")
    assert flatten.text() == "Flatten objects"
    assert "Recursively" in flatten.toolTip()
    flatten.click()

    assert panel.preview_model.columnCount() == 5
    assert panel.preview_model.headerData(3, Qt.Orientation.Horizontal) == (
        "payload.0/metrics/energy"
    )
    assert panel.preview_model.data(panel.preview_model.index(0, 3)) == "1"
    assert panel.preview_model.data(panel.preview_model.index(0, 4)) == "2"
    preview_column = panel.preview_model.extraction_column(3)
    assert preview_column is not None
    panel._exclude_flattened_column(preview_column)  # noqa: SLF001

    assert panel.selectors[0].excluded_paths == ((0, "metrics", "energy"),)


def test_flattened_columns_can_be_filtered_renamed_and_round_trip(tmp_path):
    selector = FieldSelector(
        (),
        "fields",
        ("payload",),
        "payload",
        flatten=True,
        excluded_paths=(("energy",),),
        flattened_column_names=((("force",), "force_ev"),),
    )
    config = ExtractionConfig((selector,))
    config_path = tmp_path / "extraction.json"
    save_config(config_path, config)

    restored = load_config(config_path)

    assert restored.version == 2
    assert restored.selectors[0].excluded_paths == (("energy",),)
    assert restored.selectors[0].flattened_column_names == (
        (("force",), "force_ev"),
    )

    context = tmp_path / "step_1.json"
    context.write_text(
        json.dumps({"payload": {"energy": 2.0, "force": 3.0}}),
        encoding="utf-8",
    )
    source = JsonFileSource()
    output = tmp_path / "values.csv"
    export_csv(output, source, source.discover([context]), restored)

    with output.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.reader(stream))
    assert "payload.energy" not in rows[0]
    assert "force_ev" in rows[0]


def test_each_extraction_field_has_an_inline_remove_control():
    panel = DataExtractionPanel()
    panel.add_selector(FieldSelector((), "fields", ("energy",), "energy"))
    panel.add_selector(FieldSelector((), "fields", ("force",), "force"))
    remove_force = _inline_action(panel, 1, "removeAction")

    remove_force.click()

    assert [selector.column for selector in panel.selectors] == ["energy"]
    assert panel.selections.rowCount() == 1


def test_extraction_fields_and_preview_columns_support_bulk_removal():
    panel = DataExtractionPanel()
    for column in ("energy", "force", "positions"):
        panel.add_selector(FieldSelector((), "fields", (column,), column))
    selection = panel.selections.selectionModel()
    assert selection is not None
    selection.clearSelection()
    for row in (0, 1):
        selection.select(
            panel.selections.model().index(row, 0),
            QItemSelectionModel.SelectionFlag.Select
            | QItemSelectionModel.SelectionFlag.Rows,
        )

    panel._remove_selected_rows()  # noqa: SLF001

    assert [selector.column for selector in panel.selectors] == ["positions"]
    assert (
        panel.preview.horizontalHeader().contextMenuPolicy()
        == Qt.ContextMenuPolicy.CustomContextMenu
    )

    panel.set_config(
        ExtractionConfig(
            (
                FieldSelector(
                    (),
                    "fields",
                    ("positions",),
                    "positions",
                    array_expansion_depth=2,
                ),
            )
        )
    )
    preview_rows = (
        ExtractionRow(
            0,
            "record",
            "step.json",
            (
                ExtractionValue(
                    [[1, 2, 3], [4, 5, 6]], ExtractionStatus.AVAILABLE
                ),
            ),
        ),
    )
    panel.set_preview(preview_rows, 1)
    preview_selection = panel.preview.selectionModel()
    assert preview_selection is not None
    for column in (3, 4, 5):
        preview_selection.select(
            panel.preview_model.index(0, column),
            QItemSelectionModel.SelectionFlag.Select
            | QItemSelectionModel.SelectionFlag.Columns,
        )

    panel._preview_delete_shortcut.activated.emit()  # noqa: SLF001

    assert panel._preview_delete_shortcut.key().toString() == "Backspace"  # noqa: SLF001
    assert panel.selectors[0].excluded_paths == ((0, 0), (0, 1), (0, 2))
    panel.set_preview(preview_rows, 1)
    assert panel.preview.currentIndex().column() == 3
    assert panel.preview_model.headerData(3, Qt.Orientation.Horizontal) == (
        "positions.1/0"
    )
    assert {
        index.column() for index in panel.preview.selectionModel().selectedIndexes()
    } == {3}


def test_field_specific_extraction_controls_are_inline():
    panel = DataExtractionPanel()
    panel.add_selector(FieldSelector((), "fields", ("energy",), "energy"))
    panel.add_selector(FieldSelector((), "fields", ("force",), "force"))

    assert not any(
        button.text() == "Remove selected"
        for button in panel.findChildren(QPushButton)
    )
    assert not _inline_action(panel, 0, "moveUpAction").isEnabled()
    assert _inline_action(panel, 0, "moveDownAction").isEnabled()
    assert _inline_action(panel, 1, "moveUpAction").isEnabled()
    assert not _inline_action(panel, 1, "moveDownAction").isEnabled()
    assert _inline_action(panel, 0, "flattenAction").isCheckable()
    expand_arrays = _inline_action(panel, 0, "expandArraysAction")
    assert expand_arrays.isCheckable()
    assert not expand_arrays.isChecked()
    assert _inline_action(panel, 0, "renameAction").text() == "Rename"
    assert _inline_action(panel, 0, "moveUpAction").text() == "↑"
    assert _inline_action(panel, 0, "moveDownAction").text() == "↓"
    assert _inline_action(panel, 0, "removeAction").text() == "\u00d7"
    flatten = _inline_action(panel, 0, "flattenAction")
    array_depth = _array_depth(panel, 0)
    assert flatten.text() == "Flatten objects"
    assert expand_arrays.text() == "Expand arrays"
    assert array_depth.isHidden()
    packed_actions_width = panel.selections.columnWidth(2)
    expand_arrays.click()
    assert not array_depth.isHidden()
    assert array_depth.value() == 1
    assert panel.selectors[0].array_expansion_depth == 1
    assert panel.selections.columnWidth(2) > packed_actions_width
    assert "shape [N, 3]" in array_depth.toolTip()
    assert array_depth.minimumWidth() >= (
        array_depth.fontMetrics().horizontalAdvance("Levels: 8") + 40
    )
    assert flatten.minimumWidth() >= flatten.fontMetrics().horizontalAdvance(
        flatten.text()
    )
    assert panel.selections.verticalHeader().defaultSectionSize() >= 40
    assert panel.selections.columnWidth(2) >= 440
    assert (
        panel.selections.horizontalHeader().sectionResizeMode(2)
        == QHeaderView.ResizeMode.Interactive
    )
    assert all(
        panel.selections.horizontalHeader().sectionResizeMode(column)
        == QHeaderView.ResizeMode.Interactive
        for column in range(3)
    )
    assert not panel.selections.horizontalHeader().stretchLastSection()
    assert panel.selections.columnWidth(2) >= 400

    expand_arrays.click()
    assert array_depth.isHidden()
    assert panel.selections.columnWidth(2) == packed_actions_width

    panel.selections.horizontalHeader().resizeSection(2, 480)

    assert panel.selections.columnWidth(2) == 480


def test_extraction_preview_header_selects_column_and_sort_shortcut_toggles():
    panel = DataExtractionPanel()
    energy = FieldSelector((), "fields", ("energy",), "energy")
    panel.add_selector(energy)
    panel.set_preview(
        (
            ExtractionRow(
                0,
                "record-1",
                "step_1.json",
                (ExtractionValue(3.5, ExtractionStatus.AVAILABLE),),
            ),
            ExtractionRow(
                1,
                "record-2",
                "step_2.json",
                (ExtractionValue(2.5, ExtractionStatus.AVAILABLE),),
            ),
        ),
        2,
    )
    requested_sorts = []
    panel.sort_requested.connect(
        lambda request, descending: requested_sorts.append((request, descending))
    )
    header = panel.preview.horizontalHeader()

    header.sectionClicked.emit(3)

    selected = panel.preview.selectionModel().selectedIndexes()
    assert selected
    assert {index.column() for index in selected} == {3}
    assert {index.row() for index in selected} == {0, 1}
    assert not requested_sorts

    panel._preview_sort_shortcut.activated.emit()  # noqa: SLF001
    panel._preview_sort_shortcut.activated.emit()  # noqa: SLF001

    assert requested_sorts == [((energy, None), False), ((energy, None), True)]
    assert not header.isSortIndicatorShown()
    assert panel.preview_header.sort_marker_section == 3
    assert panel.preview_header.sort_marker_order == Qt.SortOrder.DescendingOrder
    assert panel.preview_model.headerData(3, Qt.Orientation.Horizontal) == "energy"
    assert "press s to sort or reverse" in str(
        panel.preview_model.headerData(
            3, Qt.Orientation.Horizontal, Qt.ItemDataRole.ToolTipRole
        )
    ).lower()

    panel.clear_collection_sort()

    assert not header.isSortIndicatorShown()
    assert panel.preview_header.sort_marker_section == -1
    assert panel.preview_model.headerData(3, Qt.Orientation.Horizontal) == "energy"


def test_extraction_preview_rename_shortcut_targets_selected_column(monkeypatch):
    panel = DataExtractionPanel()
    panel.add_selector(FieldSelector((), "fields", ("energy",), "energy"))
    panel.set_preview(
        (
            ExtractionRow(
                0,
                "record-1",
                "step_1.json",
                (ExtractionValue(3.5, ExtractionStatus.AVAILABLE),),
            ),
        ),
        1,
    )
    renamed = []
    monkeypatch.setattr(panel, "_edit_column_name", renamed.append)

    panel.preview.horizontalHeader().sectionClicked.emit(3)
    panel._preview_rename_shortcut.activated.emit()  # noqa: SLF001

    assert panel._preview_rename_shortcut.key().toString() == "R"  # noqa: SLF001
    assert renamed == [0]


def test_extraction_preview_rejects_opaque_component_sorting():
    panel = DataExtractionPanel()
    panel.add_selector(FieldSelector((), "component", column="whole_component"))
    panel.set_preview(
        (
            ExtractionRow(
                0,
                "record-1",
                "step_1.json",
                (
                    ExtractionValue(
                        {"energy": 3.5}, ExtractionStatus.AVAILABLE
                    ),
                ),
            ),
        ),
        1,
    )
    requested_sorts = []
    messages = []
    panel.sort_requested.connect(lambda *args: requested_sorts.append(args))
    panel.status_message.connect(messages.append)

    panel.preview.horizontalHeader().sectionClicked.emit(3)
    panel._preview_sort_shortcut.activated.emit()  # noqa: SLF001

    assert not requested_sorts
    assert "flatten it or select a scalar field" in messages[-1].lower()
    assert "component or array values" in str(
        panel.preview_model.headerData(
            3, Qt.Orientation.Horizontal, Qt.ItemDataRole.ToolTipRole
        )
    ).lower()


def test_extraction_column_editor_fits_inside_table_row():
    panel = DataExtractionPanel()
    panel.add_selector(FieldSelector((), "fields", ("energy",), "energy"))
    panel.show()

    _inline_action(panel, 0, "renameAction").click()
    QApplication.processEvents()

    editor = panel.selections.findChild(QLineEdit)
    assert editor is not None
    assert editor.height() >= editor.fontMetrics().height() + 4
    assert editor.height() <= panel.selections.rowHeight(0)
