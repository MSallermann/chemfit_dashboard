from __future__ import annotations

# ruff: noqa: FBT003
import csv
import io
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Mapping

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QPersistentModelIndex,
    QSettings,
    Qt,
    Signal,
)
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMenu,
    QPushButton,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from chemfit_dashboard.extraction import (
    ExtractionConfig,
    ExtractionRow,
    ExtractionStatus,
    ExtractionValue,
    FieldSelector,
    JsonPath,
    flattened_column_name,
    load_config,
    save_config,
)

PREVIEW_LIMIT = 200
_PREVIEW_MISSING = object()
CONFIG_DIRECTORY_SETTING = "paths/extraction_config"
EXPORT_DIRECTORY_SETTING = "paths/extraction_export"


def _action_button(
    name: str,
    text: str,
    tooltip: str,
    *,
    checkable: bool = False,
) -> QToolButton:
    button = QToolButton()
    button.setObjectName(name)
    button.setText(text)
    button.setToolTip(tooltip)
    button.setAccessibleName(tooltip)
    button.setCheckable(checkable)
    button.setAutoRaise(True)
    return button


@dataclass(frozen=True, slots=True)
class _PreviewColumn:
    selector_index: int
    nested_path: JsonPath | None
    label: str


class ExtractionPreviewModel(QAbstractTableModel):
    """Compact preview model; export remains independent and complete."""

    def __init__(self, parent: QWidget | None = None):
        """Initialize an empty preview model."""
        super().__init__(parent)
        self._columns: tuple[_PreviewColumn, ...] = ()
        self._rows: tuple[ExtractionRow, ...] = ()

    def set_data(
        self, selectors: tuple[FieldSelector, ...], rows: tuple[ExtractionRow, ...]
    ) -> None:
        self.beginResetModel()
        expanded_rows = _expanded_preview_rows(rows, selectors)
        self._rows = expanded_rows[:PREVIEW_LIMIT]
        self._columns = _preview_columns(selectors, self._rows)
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
        return 0 if parent.isValid() else 3 + len(self._columns)

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
        return fixed[section] if section < 3 else self._columns[section - 3].label

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
        extracted = self._extracted_value(index)
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

    def headers(self) -> tuple[str, ...]:
        return tuple(
            str(self.headerData(column, Qt.Orientation.Horizontal))
            for column in range(self.columnCount())
        )

    def copy_value(self, index: QModelIndex) -> Any:
        """Return the complete, untruncated value represented by a preview cell."""
        if not index.isValid():
            return ""
        row = self._rows[index.row()]
        if index.column() == 0:
            return row.index
        if index.column() == 1:
            return row.filename
        if index.column() == 2:
            return row.record_id
        extracted = self._extracted_value(index)
        if extracted.status not in (
            ExtractionStatus.AVAILABLE,
            ExtractionStatus.EXPLICIT_NULL,
            ExtractionStatus.SERIALIZED_EXCEPTION,
        ):
            return f"<{extracted.status.value}>"
        return extracted.value

    def row_mapping(self, row: int) -> dict[str, Any]:
        if not 0 <= row < self.rowCount():
            return {}
        return {
            header: self.copy_value(self.index(row, column))
            for column, header in enumerate(self.headers())
        }

    def extraction_column(self, column: int) -> _PreviewColumn | None:
        """Return extraction metadata for a visible preview column."""
        index = column - 3
        return self._columns[index] if 0 <= index < len(self._columns) else None

    def _extracted_value(
        self, index: QModelIndex | QPersistentModelIndex
    ) -> ExtractionValue:
        row = self._rows[index.row()]
        column = self._columns[index.column() - 3]
        extracted = row.values[column.selector_index]
        if column.nested_path is None:
            return extracted
        nested = _nested_preview_value(extracted.value, column.nested_path)
        return ExtractionValue(
            None if nested is _PREVIEW_MISSING else nested,
            ExtractionStatus.MISSING_FIELD
            if nested is _PREVIEW_MISSING
            else extracted.status,
        )


class DataExtractionPanel(QWidget):
    """Selection editor, lazy preview, configuration, and export workspace."""

    selectors_changed = Signal(object)
    export_requested = Signal(str, str, object)
    status_message = Signal(str)
    sort_requested = Signal(object, bool)

    def __init__(  # noqa: PLR0915
        self,
        parent: QWidget | None = None,
        settings: QSettings | None = None,
    ):
        """Initialize extraction selection, preview, and export controls."""
        super().__init__(parent)
        self._selectors: list[FieldSelector] = []
        self._include_identifiers = True
        self._include_status_columns = True
        self._clean_config = ExtractionConfig(())
        self._settings = settings
        layout = QVBoxLayout(self)
        heading = QHBoxLayout()
        self.title = QLabel("DATA EXTRACTION")
        self.title.setObjectName("sectionLabel")
        heading.addWidget(self.title)
        self.dirty_indicator = QLabel("UNSAVED")
        self.dirty_indicator.setObjectName("dirtyIndicator")
        self.dirty_indicator.setToolTip(
            "The current extraction configuration has not been saved to a file"
        )
        self.dirty_indicator.hide()
        heading.addWidget(self.dirty_indicator)
        heading.addStretch()
        self.status = QLabel("Add any inspector value or component to begin.")
        self.status.setObjectName("mutedLabel")
        heading.addWidget(self.status)
        layout.addLayout(heading)
        order_hint = QLabel(
            "Rows follow the collection order shown in the Evaluations sidebar."
        )
        order_hint.setObjectName("mutedLabel")
        layout.addWidget(order_hint)

        self.selections = QTableWidget(0, 3)
        self.selections.setObjectName("extractionSelections")
        self.selections.setHorizontalHeaderLabels(("Column", "Field path", "Actions"))
        selection_header = self.selections.horizontalHeader()
        selection_header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        selection_header.setStretchLastSection(False)
        selection_header.resizeSection(0, 180)
        selection_header.resizeSection(1, 260)
        selection_header.resizeSection(2, 400)
        self.selections.setHorizontalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        self.selections.ensurePolished()
        self.selections.verticalHeader().setDefaultSectionSize(
            max(36, self.selections.fontMetrics().height() + 14)
        )
        self.selections.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.selections.setMaximumHeight(190)
        self.selections.itemChanged.connect(self._column_edited)
        self.selections.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.selections.customContextMenuRequested.connect(
            self._show_selection_context_menu
        )
        self._selector_copy_shortcut = QShortcut(
            QKeySequence.StandardKey.Copy, self.selections
        )
        self._selector_copy_shortcut.setContext(
            Qt.ShortcutContext.WidgetWithChildrenShortcut
        )
        self._selector_copy_shortcut.activated.connect(self._copy_current_selector)
        layout.addWidget(self.selections)

        controls = QHBoxLayout()
        self.clear_button = QPushButton("Clear all")
        self.clear_button.clicked.connect(self.clear)
        controls.addStretch()
        controls.addWidget(self.clear_button)
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
        self.preview.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.preview.customContextMenuRequested.connect(self._show_preview_context_menu)
        self._preview_copy_shortcut = QShortcut(
            QKeySequence.StandardKey.Copy, self.preview
        )
        self._preview_copy_shortcut.setContext(
            Qt.ShortcutContext.WidgetWithChildrenShortcut
        )
        self._preview_copy_shortcut.activated.connect(self._copy_preview_selection)
        layout.addWidget(self.preview, 1)

        actions = QHBoxLayout()
        self.save_button = QPushButton("Save config")
        self.load_button = QPushButton("Load config")
        self.copy_config_button = QPushButton("Copy config")
        self.copy_config_button.setToolTip(
            "Copy JSON compatible with chemfit-extract --config"
        )
        self.paste_config_button = QPushButton("Paste config")
        self.paste_config_button.setToolTip(
            "Load a chemfit-extract configuration from the clipboard"
        )
        self.csv_button = QPushButton("Export CSV")
        self.jsonl_button = QPushButton("Export JSONL")
        self.save_button.clicked.connect(self._save)
        self.load_button.clicked.connect(self._load)
        self.copy_config_button.clicked.connect(self.copy_configuration)
        self.paste_config_button.clicked.connect(self.paste_configuration)
        self.csv_button.clicked.connect(lambda: self._export("csv"))
        self.jsonl_button.clicked.connect(lambda: self._export("jsonl"))
        for widget in (
            self.save_button,
            self.load_button,
            self.copy_config_button,
            self.paste_config_button,
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
        return ExtractionConfig(
            self.selectors,
            include_identifiers=self._include_identifiers,
            include_status_columns=self._include_status_columns,
        )

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

    def set_config(self, config: ExtractionConfig, *, mark_clean: bool = False) -> None:
        self._selectors = list(config.selectors)
        self._include_identifiers = config.include_identifiers
        self._include_status_columns = config.include_status_columns
        if mark_clean:
            self._clean_config = config
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
        shown = self.preview_model.rowCount()
        suffix = " (limited)" if total > len(rows) or shown >= PREVIEW_LIMIT else ""
        self.status.setText(
            f"Preview: {shown:,} rows from {total:,} evaluations{suffix}"
        )

    def show_error(self, message: str) -> None:
        self.status.setText(message)

    def _rebuild_selection_table(self, selected_row: int) -> None:
        self.selections.blockSignals(True)
        self.selections.clearContents()
        self.selections.setRowCount(len(self._selectors))
        for row, selector in enumerate(self._selectors):
            self.selections.setItem(row, 0, QTableWidgetItem(selector.column))
            path_item = QTableWidgetItem(selector.path_label)
            path_item.setFlags(path_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.selections.setItem(row, 1, path_item)
            self.selections.setCellWidget(row, 2, self._field_actions(row, selector))
        self.selections.blockSignals(False)
        if 0 <= selected_row < len(self._selectors):
            self.selections.setCurrentCell(selected_row, 0)
        self._update_controls()

    def _field_actions(self, row: int, selector: FieldSelector) -> QWidget:
        actions = QWidget()
        layout = QHBoxLayout(actions)
        layout.setContentsMargins(2, 1, 2, 1)
        layout.setSpacing(3)

        move_up = _action_button("moveUpAction", "Up", f"Move {selector.column} up")
        move_up.setEnabled(row > 0)
        move_up.clicked.connect(
            lambda _checked=False, target=row: self._move_row(target, -1)
        )
        layout.addWidget(move_up)

        move_down = _action_button(
            "moveDownAction", "Down", f"Move {selector.column} down"
        )
        move_down.setEnabled(row < len(self._selectors) - 1)
        move_down.clicked.connect(
            lambda _checked=False, target=row: self._move_row(target, 1)
        )
        layout.addWidget(move_down)

        flatten = _action_button(
            "flattenAction",
            "Flatten all",
            f"Recursively flatten nested dictionaries in {selector.column}",
            checkable=True,
        )
        flatten.setChecked(selector.flatten)
        flatten.toggled.connect(
            lambda checked, target=row: self._set_option(target, "flatten", checked)
        )
        layout.addWidget(flatten)

        expand = _action_button(
            "expandAction",
            "Expand",
            f"Expand array values in {selector.column} into rows",
            checkable=True,
        )
        expand.setChecked(selector.expand_array)
        expand.toggled.connect(
            lambda checked, target=row: self._set_option(
                target, "expand_array", checked
            )
        )
        layout.addWidget(expand)

        remove = _action_button(
            "removeAction",
            "\N{MULTIPLICATION SIGN}",
            f"Remove {selector.column} from extraction",
        )
        remove.clicked.connect(
            lambda _checked=False, target=row: self._remove_row(target)
        )
        layout.addWidget(remove)
        return actions

    def _emit_changed(self) -> None:
        self._update_controls()
        self._update_dirty_indicator()
        self.selectors_changed.emit(self.selectors)

    def _update_dirty_indicator(self) -> None:
        self.dirty_indicator.setVisible(self.config != self._clean_config)

    def _mark_config_clean(self) -> None:
        self._clean_config = self.config
        self._update_dirty_indicator()

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

    def _set_option(self, row: int, option: str, checked: bool) -> None:
        if 0 <= row < len(self._selectors):
            selector = self._selectors[row]
            if option == "flatten":
                selector = replace(selector, flatten=checked)
            elif option == "expand_array":
                selector = replace(selector, expand_array=checked)
            self._selectors[row] = selector
            self._emit_changed()

    def _remove(self) -> None:
        self._remove_row(self.selections.currentRow())

    def _remove_row(self, row: int) -> None:
        if 0 <= row < len(self._selectors):
            del self._selectors[row]
            self._rebuild_selection_table(min(row, len(self._selectors) - 1))
            self._emit_changed()

    def _move(self, offset: int) -> None:
        self._move_row(self.selections.currentRow(), offset)

    def _move_row(self, row: int, offset: int) -> None:
        target = row + offset
        if 0 <= row < len(self._selectors) and 0 <= target < len(self._selectors):
            self._selectors[row], self._selectors[target] = (
                self._selectors[target],
                self._selectors[row],
            )
            self._rebuild_selection_table(target)
            self._emit_changed()

    def _update_controls(self) -> None:
        self.clear_button.setEnabled(bool(self._selectors))
        self.save_button.setEnabled(bool(self._selectors))
        self.copy_config_button.setEnabled(bool(self._selectors))
        self.csv_button.setEnabled(bool(self._selectors))
        self.jsonl_button.setEnabled(bool(self._selectors))

    def copy_configuration(self) -> None:
        """Copy the complete CLI-compatible extraction configuration."""
        self._copy_text(
            json.dumps(self.config.to_dict(), indent=2, ensure_ascii=False),
            "Copied extraction configuration as JSON",
        )

    def paste_configuration(self) -> None:
        """Load a CLI-compatible extraction configuration from the clipboard."""
        try:
            value = json.loads(QApplication.clipboard().text())
            if not isinstance(value, dict):
                message = "Configuration must be a JSON object"
                raise ValueError(message)
            config = ExtractionConfig.from_dict(value)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            self.show_error(f"Could not paste configuration: {error}")
            return
        self.set_config(config)
        self.status_message.emit("Pasted extraction configuration")

    def _show_selection_context_menu(self, position) -> None:  # noqa: ANN001
        item = self.selections.itemAt(position)
        if item is None or not 0 <= item.row() < len(self._selectors):
            return
        row = item.row()
        self.selections.setCurrentCell(row, item.column())
        menu = QMenu(self.selections)
        copy_column = menu.addAction("Copy column name")
        copy_path = menu.addAction("Copy field path")
        copy_selector = menu.addAction("Copy field configuration as JSON")
        selected = menu.exec(self.selections.viewport().mapToGlobal(position))
        if selected is copy_column:
            self._copy_text(
                self._selectors[row].column, "Copied extraction column name"
            )
        elif selected is copy_path:
            self._copy_text(
                self._selectors[row].path_label, "Copied extraction field path"
            )
        elif selected is copy_selector:
            self._copy_selector_json(row)

    def _copy_current_selector(self) -> None:
        self._copy_selector_json(self.selections.currentRow())

    def _copy_selector_json(self, row: int) -> None:
        if not 0 <= row < len(self._selectors):
            return
        selector = ExtractionConfig((self._selectors[row],)).to_dict()["selectors"][0]
        self._copy_text(
            json.dumps(selector, indent=2, ensure_ascii=False),
            "Copied extraction field configuration as JSON",
        )

    def _show_preview_context_menu(self, position) -> None:  # noqa: ANN001
        index = self.preview.indexAt(position)
        if not index.isValid():
            return
        self.preview.setCurrentIndex(index)
        menu = QMenu(self.preview)
        copy_cell = menu.addAction("Copy cell")
        copy_column = menu.addAction("Copy column as CSV")
        copy_row = menu.addAction("Copy row as CSV")
        copy_row_json = menu.addAction("Copy row as JSON")
        menu.addSeparator()
        copy_preview = menu.addAction("Copy preview as CSV")
        preview_column = self.preview_model.extraction_column(index.column())
        sort_ascending = sort_descending = remove_flattened = rename_flattened = None
        if preview_column is not None:
            menu.addSeparator()
            sort_menu = menu.addMenu("Sort collection by this column")
            sort_ascending = sort_menu.addAction("Sort ↑")
            sort_descending = sort_menu.addAction("Sort ↓")
            if preview_column.nested_path is not None:
                rename_flattened = menu.addAction("Rename flattened column…")
                remove_flattened = menu.addAction("Remove flattened column")
        selected = menu.exec(self.preview.viewport().mapToGlobal(position))
        if selected is copy_cell:
            self._copy_preview_cell(index)
        elif selected is copy_column:
            self._copy_preview_column(index.column())
        elif selected is copy_row:
            self._copy_preview_row(index.row())
        elif selected is copy_row_json:
            self._copy_preview_row(index.row(), as_json=True)
        elif selected is copy_preview:
            self._copy_preview_table()
        elif selected is sort_ascending and preview_column is not None:
            self.sort_requested.emit(
                (
                    self._selectors[preview_column.selector_index],
                    preview_column.nested_path,
                ),
                False,
            )
        elif selected is sort_descending and preview_column is not None:
            self.sort_requested.emit(
                (
                    self._selectors[preview_column.selector_index],
                    preview_column.nested_path,
                ),
                True,
            )
        elif selected is remove_flattened and preview_column is not None:
            self._exclude_flattened_column(preview_column)
        elif selected is rename_flattened and preview_column is not None:
            self._rename_flattened_column(preview_column)

    def _exclude_flattened_column(self, column: _PreviewColumn) -> None:
        path = column.nested_path
        if path is None:
            return
        selector = self._selectors[column.selector_index]
        names = tuple(
            (item_path, name)
            for item_path, name in selector.flattened_column_names
            if item_path != path
        )
        self._selectors[column.selector_index] = replace(
            selector,
            excluded_paths=(*selector.excluded_paths, path),
            flattened_column_names=names,
        )
        self._emit_changed()

    def _rename_flattened_column(self, column: _PreviewColumn) -> None:
        path = column.nested_path
        if path is None:
            return
        selector = self._selectors[column.selector_index]
        current = flattened_column_name(selector, path)
        name, accepted = QInputDialog.getText(
            self,
            "Rename flattened column",
            "Column name:",
            text=current,
        )
        name = name.strip()
        if not accepted or not name:
            return
        names = dict(selector.flattened_column_names)
        names[path] = name
        self._selectors[column.selector_index] = replace(
            selector,
            flattened_column_names=tuple(names.items()),
        )
        self._emit_changed()

    def _copy_preview_cell(self, index: QModelIndex) -> None:
        value = self.preview_model.copy_value(index)
        self._copy_text(_clipboard_text(value), "Copied extraction preview cell")

    def _copy_preview_selection(self) -> None:
        selection_model = self.preview.selectionModel()
        indexes = (
            selection_model.selectedIndexes() if selection_model is not None else []
        )
        if not indexes and self.preview.currentIndex().isValid():
            indexes = [self.preview.currentIndex()]
        if len(indexes) == 1:
            self._copy_preview_cell(indexes[0])
            return
        if not indexes:
            return
        selected = {(index.row(), index.column()) for index in indexes}
        rows = sorted({index.row() for index in indexes})
        columns = sorted({index.column() for index in indexes})
        csv_rows = [tuple(self.preview_model.headers()[column] for column in columns)]
        csv_rows.extend(
            tuple(
                _clipboard_text(
                    self.preview_model.copy_value(self.preview_model.index(row, column))
                )
                if (row, column) in selected
                else ""
                for column in columns
            )
            for row in rows
        )
        self._copy_text(_csv_text(csv_rows), "Copied preview selection as CSV")

    def _copy_preview_column(self, column: int) -> None:
        if not 0 <= column < self.preview_model.columnCount():
            return
        header = self.preview_model.headers()[column]
        rows = [(header,)]
        rows.extend(
            (
                _clipboard_text(
                    self.preview_model.copy_value(self.preview_model.index(row, column))
                ),
            )
            for row in range(self.preview_model.rowCount())
        )
        self._copy_text(_csv_text(rows), f"Copied {header} column as CSV")

    def _copy_preview_row(self, row: int, *, as_json: bool = False) -> None:
        values = self.preview_model.row_mapping(row)
        if not values:
            return
        text = (
            json.dumps(values, indent=2, ensure_ascii=False)
            if as_json
            else _csv_text(
                (tuple(values), tuple(map(_clipboard_text, values.values())))
            )
        )
        format_label = "JSON" if as_json else "CSV"
        self._copy_text(text, f"Copied extraction preview row as {format_label}")

    def _copy_preview_table(self) -> None:
        rows = [self.preview_model.headers()]
        rows.extend(
            tuple(map(_clipboard_text, self.preview_model.row_mapping(row).values()))
            for row in range(self.preview_model.rowCount())
        )
        self._copy_text(_csv_text(rows), "Copied extraction preview as CSV")

    def _copy_text(self, text: str, message: str) -> None:
        QApplication.clipboard().setText(text)
        self.status_message.emit(message)

    def _save(self) -> None:
        path, _filter = QFileDialog.getSaveFileName(
            self,
            "Save extraction configuration",
            self._dialog_path(CONFIG_DIRECTORY_SETTING, "extraction.json"),
            "JSON (*.json)",
        )
        if path:
            if not Path(path).suffix:
                path += ".json"
            try:
                save_config(path, self.config)
            except OSError as error:
                self.show_error(f"Could not save configuration: {error}")
                return
            self._remember_directory(CONFIG_DIRECTORY_SETTING, path)
            self._mark_config_clean()
            self.status_message.emit(f"Saved extraction configuration to {path}")

    def _load(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self,
            "Load extraction configuration",
            self._dialog_path(CONFIG_DIRECTORY_SETTING),
            "JSON (*.json)",
        )
        if path:
            try:
                self.set_config(load_config(path), mark_clean=True)
            except (OSError, ValueError, json.JSONDecodeError) as error:
                self.show_error(f"Could not load configuration: {error}")
                return
            self._remember_directory(CONFIG_DIRECTORY_SETTING, path)
            self.status_message.emit(f"Loaded extraction configuration from {path}")

    def _export(self, format_name: str) -> None:
        extension = format_name
        path, _filter = QFileDialog.getSaveFileName(
            self,
            f"Export {format_name.upper()}",
            self._dialog_path(EXPORT_DIRECTORY_SETTING, f"extraction.{extension}"),
            f"{format_name.upper()} (*.{extension})",
        )
        if path:
            if not Path(path).suffix:
                path += f".{extension}"
            self._remember_directory(EXPORT_DIRECTORY_SETTING, path)
            self.export_requested.emit(path, format_name, self.config)

    def _dialog_path(self, key: str, filename: str = "") -> str:
        directory = ""
        if self._settings is not None:
            directory = str(self._settings.value(key, "") or "")
        return (
            str(Path(directory) / filename)
            if directory and filename
            else (directory or filename)
        )

    def _remember_directory(self, key: str, path: str) -> None:
        if self._settings is None:
            return
        self._settings.setValue(key, str(Path(path).resolve().parent))
        self._settings.sync()


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


def _clipboard_text(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _csv_text(rows: tuple[tuple[str, ...], ...] | list[tuple[str, ...]]) -> str:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerows(rows)
    return stream.getvalue().rstrip("\n")


def _expanded_preview_rows(
    rows: tuple[ExtractionRow, ...], selectors: tuple[FieldSelector, ...]
) -> tuple[ExtractionRow, ...]:
    expanded_index = next(
        (index for index, selector in enumerate(selectors) if selector.expand_array),
        None,
    )
    if expanded_index is None:
        return rows
    result: list[ExtractionRow] = []
    for row in rows:
        selected = row.values[expanded_index]
        if not isinstance(selected.value, list):
            result.append(row)
            continue
        for item in selected.value:
            values = list(row.values)
            values[expanded_index] = ExtractionValue(item, selected.status)
            result.append(replace(row, values=tuple(values)))
            if len(result) >= PREVIEW_LIMIT:
                return tuple(result)
    return tuple(result)


def _preview_columns(
    selectors: tuple[FieldSelector, ...], rows: tuple[ExtractionRow, ...]
) -> tuple[_PreviewColumn, ...]:
    result: list[_PreviewColumn] = []
    for selector_index, selector in enumerate(selectors):
        paths: set[JsonPath] = set()
        if selector.flatten:
            for row in rows:
                value = row.values[selector_index].value
                if isinstance(value, dict):
                    paths.update(_mapping_leaf_paths(value))
        if selector.flatten:
            result.extend(
                _PreviewColumn(
                    selector_index,
                    path,
                    flattened_column_name(selector, path),
                )
                for path in sorted(paths, key=lambda item: tuple(map(str, item)))
                if path not in selector.excluded_paths
            )
        else:
            result.append(_PreviewColumn(selector_index, None, selector.column))
    return tuple(result)


def _mapping_leaf_paths(
    value: Mapping[str, Any], path: JsonPath = ()
) -> tuple[JsonPath, ...]:
    if not value:
        return (path,)
    result: list[JsonPath] = []
    for key in sorted(value, key=str):
        child_path = (*path, str(key))
        child = value[key]
        if isinstance(child, dict):
            result.extend(_mapping_leaf_paths(child, child_path))
        else:
            result.append(child_path)
    return tuple(result)


def _nested_preview_value(value: Any, path: JsonPath) -> Any:
    current = value
    for part in path:
        mapping_entry = (
            isinstance(part, str) and isinstance(current, dict) and part in current
        )
        sequence_entry = (
            isinstance(part, int)
            and isinstance(current, list)
            and 0 <= part < len(current)
        )
        if not (mapping_entry or sequence_entry):
            return _PREVIEW_MISSING
        current = _index_preview_value(current, part)
    return current


def _index_preview_value(value: Any, part: str | int) -> Any:
    if isinstance(part, str):
        return cast("dict[str, Any]", value)[part]
    return cast("list[Any]", value)[part]
