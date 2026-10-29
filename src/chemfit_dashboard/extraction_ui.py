from __future__ import annotations

# ruff: noqa: FBT003
import json
from dataclasses import replace
from typing import Any

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QPersistentModelIndex,
    Qt,
    Signal,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from chemfit_dashboard.extraction import (
    ExtractionConfig,
    ExtractionRow,
    ExtractionStatus,
    FieldSelector,
    load_config,
    save_config,
)

PREVIEW_LIMIT = 200


class ExtractionPreviewModel(QAbstractTableModel):
    """Compact preview model; export remains independent and complete."""

    def __init__(self, parent: QWidget | None = None):
        """Initialize an empty preview model."""
        super().__init__(parent)
        self._selectors: tuple[FieldSelector, ...] = ()
        self._rows: tuple[ExtractionRow, ...] = ()

    def set_data(
        self, selectors: tuple[FieldSelector, ...], rows: tuple[ExtractionRow, ...]
    ) -> None:
        self.beginResetModel()
        self._selectors = selectors
        self._rows = rows[:PREVIEW_LIMIT]
        self.endResetModel()

    def rowCount(  # noqa: N802
        self,
        parent: QModelIndex | QPersistentModelIndex = QModelIndex(),  # noqa: B008
    ) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(  # noqa: N802
        self,
        parent: QModelIndex | QPersistentModelIndex = QModelIndex(),  # noqa: B008
    ) -> int:
        return 0 if parent.isValid() else 3 + len(self._selectors)

    def headerData(  # noqa: N802
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Vertical:
            return section + 1
        fixed = ("Index", "Filename", "Record ID")
        return fixed[section] if section < 3 else self._selectors[section - 3].column

    def data(  # noqa: PLR0911
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if not index.isValid() or role not in (
            Qt.ItemDataRole.DisplayRole,
            Qt.ItemDataRole.ToolTipRole,
        ):
            return None
        row = self._rows[index.row()]
        if index.column() == 0:
            return row.index
        if index.column() == 1:
            return row.filename
        if index.column() == 2:
            return row.record_id
        extracted = row.values[index.column() - 3]
        if role == Qt.ItemDataRole.ToolTipRole:
            return (
                f"Status: {extracted.status.value}\n{_compact(extracted.value, 1000)}"
            )
        if extracted.status not in (
            ExtractionStatus.AVAILABLE,
            ExtractionStatus.EXPLICIT_NULL,
            ExtractionStatus.SERIALIZED_EXCEPTION,
        ):
            return f"<{extracted.status.value}>"
        return _compact(extracted.value)


class DataExtractionPanel(QWidget):
    """Selection editor, lazy preview, configuration, and export workspace."""

    selectors_changed = Signal(object)
    export_requested = Signal(str, str, object)

    def __init__(self, parent: QWidget | None = None):  # noqa: PLR0915
        """Initialize extraction selection, preview, and export controls."""
        super().__init__(parent)
        self._selectors: list[FieldSelector] = []
        layout = QVBoxLayout(self)
        heading = QHBoxLayout()
        title = QLabel("DATA EXTRACTION")
        title.setObjectName("sectionLabel")
        heading.addWidget(title)
        heading.addStretch()
        self.status = QLabel("Add any inspector value or component to begin.")
        self.status.setObjectName("mutedLabel")
        heading.addWidget(self.status)
        layout.addLayout(heading)

        self.selections = QTableWidget(0, 2)
        self.selections.setHorizontalHeaderLabels(("Column", "Field path"))
        self.selections.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Interactive
        )
        self.selections.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.selections.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.selections.setMaximumHeight(190)
        self.selections.itemChanged.connect(self._column_edited)
        self.selections.currentCellChanged.connect(self._selection_changed)
        layout.addWidget(self.selections)

        controls = QHBoxLayout()
        self.remove_button = QPushButton("Remove")
        self.up_button = QPushButton("Move up")
        self.down_button = QPushButton("Move down")
        self.clear_button = QPushButton("Clear")
        self.flatten = QCheckBox("Flatten dictionary")
        self.expand = QCheckBox("Expand array into rows")
        self.remove_button.clicked.connect(self._remove)
        self.up_button.clicked.connect(lambda: self._move(-1))
        self.down_button.clicked.connect(lambda: self._move(1))
        self.clear_button.clicked.connect(self.clear)
        self.flatten.toggled.connect(self._options_changed)
        self.expand.toggled.connect(self._options_changed)
        for widget in (
            self.remove_button,
            self.up_button,
            self.down_button,
            self.clear_button,
            self.flatten,
            self.expand,
        ):
            controls.addWidget(widget)
        controls.addStretch()
        layout.addLayout(controls)

        self.preview_model = ExtractionPreviewModel(self)
        self.preview = QTableView()
        self.preview.setModel(self.preview_model)
        self.preview.setAlternatingRowColors(True)
        self.preview.setHorizontalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        self.preview.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive
        )
        layout.addWidget(self.preview, 1)

        actions = QHBoxLayout()
        self.save_button = QPushButton("Save configuration")
        self.load_button = QPushButton("Load configuration")
        self.csv_button = QPushButton("Export CSV")
        self.jsonl_button = QPushButton("Export JSONL")
        self.save_button.clicked.connect(self._save)
        self.load_button.clicked.connect(self._load)
        self.csv_button.clicked.connect(lambda: self._export("csv"))
        self.jsonl_button.clicked.connect(lambda: self._export("jsonl"))
        for widget in (
            self.save_button,
            self.load_button,
            self.csv_button,
            self.jsonl_button,
        ):
            actions.addWidget(widget)
        actions.addStretch()
        layout.addLayout(actions)
        self._update_controls()

    @property
    def selectors(self) -> tuple[FieldSelector, ...]:
        return tuple(self._selectors)

    @property
    def config(self) -> ExtractionConfig:
        return ExtractionConfig(self.selectors)

    def add_selector(self, selector: FieldSelector) -> None:
        existing = {item.column.casefold() for item in self._selectors}
        base = selector.column.strip() or "value"
        column = base
        suffix = 2
        while column.casefold() in existing:
            column = f"{base}_{suffix}"
            suffix += 1
        self._selectors.append(replace(selector, column=column))
        self._rebuild_selection_table(len(self._selectors) - 1)
        self._emit_changed()

    def set_config(self, config: ExtractionConfig) -> None:
        self._selectors = list(config.selectors)
        self._rebuild_selection_table(0 if self._selectors else -1)
        self._emit_changed()

    def clear(self) -> None:
        self._selectors.clear()
        self._rebuild_selection_table(-1)
        self.preview_model.set_data((), ())
        self._emit_changed()

    def show_loading(self, count: int) -> None:
        self.status.setText(f"Building preview from {count:,} evaluations…")

    def set_preview(self, rows: tuple[ExtractionRow, ...], total: int) -> None:
        self.preview_model.set_data(self.selectors, rows)
        suffix = f" (first {len(rows):,} shown)" if total > len(rows) else ""
        self.status.setText(f"Preview: {total:,} evaluations{suffix}")

    def show_error(self, message: str) -> None:
        self.status.setText(message)

    def _rebuild_selection_table(self, selected_row: int) -> None:
        self.selections.blockSignals(True)
        self.selections.setRowCount(len(self._selectors))
        for row, selector in enumerate(self._selectors):
            self.selections.setItem(row, 0, QTableWidgetItem(selector.column))
            path_item = QTableWidgetItem(selector.path_label)
            path_item.setFlags(path_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.selections.setItem(row, 1, path_item)
        self.selections.blockSignals(False)
        if 0 <= selected_row < len(self._selectors):
            self.selections.setCurrentCell(selected_row, 0)
        self._update_controls()

    def _emit_changed(self) -> None:
        self._update_controls()
        self.selectors_changed.emit(self.selectors)

    def _column_edited(self, item: QTableWidgetItem) -> None:
        if item.column() != 0 or not (0 <= item.row() < len(self._selectors)):
            return
        column = item.text().strip()
        if not column:
            self._rebuild_selection_table(item.row())
            return
        self._selectors[item.row()] = replace(
            self._selectors[item.row()], column=column
        )
        self._emit_changed()

    def _selection_changed(
        self, row: int, _column: int, _old_row: int, _old_column: int
    ) -> None:
        self.flatten.blockSignals(True)
        self.expand.blockSignals(True)
        if 0 <= row < len(self._selectors):
            self.flatten.setChecked(self._selectors[row].flatten)
            self.expand.setChecked(self._selectors[row].expand_array)
        self.flatten.blockSignals(False)
        self.expand.blockSignals(False)
        self._update_controls()

    def _options_changed(self, _checked: bool) -> None:
        row = self.selections.currentRow()
        if 0 <= row < len(self._selectors):
            self._selectors[row] = replace(
                self._selectors[row],
                flatten=self.flatten.isChecked(),
                expand_array=self.expand.isChecked(),
            )
            self._emit_changed()

    def _remove(self) -> None:
        row = self.selections.currentRow()
        if 0 <= row < len(self._selectors):
            del self._selectors[row]
            self._rebuild_selection_table(min(row, len(self._selectors) - 1))
            self._emit_changed()

    def _move(self, offset: int) -> None:
        row = self.selections.currentRow()
        target = row + offset
        if 0 <= row < len(self._selectors) and 0 <= target < len(self._selectors):
            self._selectors[row], self._selectors[target] = (
                self._selectors[target],
                self._selectors[row],
            )
            self._rebuild_selection_table(target)
            self._emit_changed()

    def _update_controls(self) -> None:
        row = self.selections.currentRow()
        selected = 0 <= row < len(self._selectors)
        self.remove_button.setEnabled(selected)
        self.up_button.setEnabled(selected and row > 0)
        self.down_button.setEnabled(selected and row < len(self._selectors) - 1)
        self.clear_button.setEnabled(bool(self._selectors))
        self.flatten.setEnabled(selected)
        self.expand.setEnabled(selected)
        self.save_button.setEnabled(bool(self._selectors))
        self.csv_button.setEnabled(bool(self._selectors))
        self.jsonl_button.setEnabled(bool(self._selectors))

    def _save(self) -> None:
        path, _filter = QFileDialog.getSaveFileName(
            self, "Save extraction configuration", "extraction.json", "JSON (*.json)"
        )
        if path:
            save_config(path, self.config)

    def _load(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self, "Load extraction configuration", "", "JSON (*.json)"
        )
        if path:
            try:
                self.set_config(load_config(path))
            except (OSError, ValueError, json.JSONDecodeError) as error:
                self.show_error(f"Could not load configuration: {error}")

    def _export(self, format_name: str) -> None:
        extension = format_name
        path, _filter = QFileDialog.getSaveFileName(
            self,
            f"Export {format_name.upper()}",
            f"extraction.{extension}",
            f"{format_name.upper()} (*.{extension})",
        )
        if path:
            self.export_requested.emit(path, format_name, self.config)


def _compact(value: Any, limit: int = 160) -> str:
    if isinstance(value, (dict, list)):
        text = json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
    elif value is None:
        text = "null"
    elif isinstance(value, bool):
        text = "true" if value else "false"
    else:
        text = str(value)
    return text if len(text) <= limit else f"{text[: limit - 1]}…"
