"""
Qt editor and bounded preview for data-extraction configurations.

``DataExtractionPanel`` edits selectors and emits export or collection-sort
requests. ``ExtractionPreviewModel`` expands only enough data for interaction;
complete export remains the responsibility of :mod:`extraction`.
"""

from __future__ import annotations

# ruff: noqa: FBT003
import csv
import io
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

from PySide6.QtCore import (
    QAbstractTableModel,
    QItemSelectionModel,
    QModelIndex,
    QPersistentModelIndex,
    QPoint,
    QRect,
    QSettings,
    Qt,
    Signal,
)
from PySide6.QtGui import QKeySequence, QPainter, QPalette, QPen, QPolygon, QShortcut
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
    QSpinBox,
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
    ExtractionValue,
    FieldSelector,
    JsonPath,
    column_paths_for_value,
    flattened_column_name,
    load_config,
    save_config,
)
from chemfit_dashboard.ui_components import ActionRow, muted_label

PREVIEW_LIMIT = 200
_ACTIONS_COLUMN_MIN_WIDTH = 440
_PREVIEW_MISSING = object()
CONFIG_DIRECTORY_SETTING = "paths/extraction_config"
EXPORT_DIRECTORY_SETTING = "paths/extraction_export"


@dataclass(frozen=True, slots=True)
class _PreviewColumn:
    selector_index: int
    nested_path: JsonPath | None
    label: str


class _PreviewHeaderView(QHeaderView):
    """Header with a passive sort marker that does not alter section behavior."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(Qt.Orientation.Horizontal, parent)
        self._sort_marker_section = -1
        self._sort_marker_order = Qt.SortOrder.AscendingOrder
        self.setSortIndicatorShown(False)

    @property
    def sort_marker_section(self) -> int:
        return self._sort_marker_section

    @property
    def sort_marker_order(self) -> Qt.SortOrder:
        return self._sort_marker_order

    def set_sort_marker(self, section: int, order: Qt.SortOrder) -> None:
        previous = self._sort_marker_section
        self._sort_marker_section = section
        self._sort_marker_order = order
        if previous >= 0:
            self.updateSection(previous)
        self.updateSection(section)

    def clear_sort_marker(self) -> None:
        previous = self._sort_marker_section
        self._sort_marker_section = -1
        if previous >= 0:
            self.updateSection(previous)

    def paintSection(  # noqa: N802
        self, painter: QPainter, rect: QRect, logical_index: int
    ) -> None:
        super().paintSection(painter, rect, logical_index)
        if logical_index != self._sort_marker_section:
            return
        selection = self.selectionModel()
        selected = bool(
            selection
            and selection.isColumnSelected(logical_index, QModelIndex())
        )
        color_role = (
            QPalette.ColorRole.HighlightedText
            if selected
            else QPalette.ColorRole.ButtonText
        )
        center_x = rect.right() - 9
        center_y = rect.center().y()
        if self._sort_marker_order == Qt.SortOrder.DescendingOrder:
            points = QPolygon(
                (
                    QPoint(center_x - 4, center_y - 2),
                    QPoint(center_x, center_y + 2),
                    QPoint(center_x + 4, center_y - 2),
                )
            )
        else:
            points = QPolygon(
                (
                    QPoint(center_x - 4, center_y + 2),
                    QPoint(center_x, center_y - 2),
                    QPoint(center_x + 4, center_y + 2),
                )
            )
        painter.save()
        painter.setPen(QPen(self.palette().color(color_role), 1.5))
        painter.drawPolyline(points)
        painter.restore()


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
        """Replace preview rows and derive visible flattened columns."""
        self.beginResetModel()
        self._rows = rows[:PREVIEW_LIMIT]
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
        if orientation == Qt.Orientation.Vertical:
            return section + 1 if role == Qt.ItemDataRole.DisplayRole else None
        if role == Qt.ItemDataRole.ToolTipRole and section >= 3:
            label = self._columns[section - 3].label
            reason = self.sort_unavailable_reason(section)
            if reason is not None:
                return f"Click to select {label}. {reason}"
            return (
                f"Click to select {label}; press S to sort or reverse sorting, "
                "or right-click for explicit sort directions"
            )
        if role != Qt.ItemDataRole.DisplayRole:
            return None
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

    def sort_unavailable_reason(self, column: int) -> str | None:
        """Explain why a preview column cannot define collection order."""
        preview_column = self.extraction_column(column)
        if preview_column is None:
            return "Only extracted value columns can sort the collection."
        found_value = False
        for row in range(self.rowCount()):
            extracted = self._extracted_value(self.index(row, column))
            if extracted.status != ExtractionStatus.AVAILABLE or extracted.value is None:
                continue
            found_value = True
            if not isinstance(extracted.value, (bool, int, float, str)):
                return (
                    f"{preview_column.label} contains component or array values. "
                    "Flatten it or select a scalar field before sorting."
                )
        if not found_value:
            return f"{preview_column.label} has no available scalar values to sort."
        return None

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
    """
    Selection editor, lazy preview, configuration, and export workspace.

    The panel owns editable selector/options state and a bounded preview model.
    It emits immutable configurations and sort/export requests; MainWindow owns
    collection traversal and background-task lifetimes.
    """

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
        self.status = muted_label("Add any inspector value or component to begin.")
        heading.addWidget(self.status)
        layout.addLayout(heading)
        order_hint = muted_label(
            "Rows follow the collection order shown in the Evaluations sidebar."
        )
        layout.addWidget(order_hint)

        self.selections = QTableWidget(0, 3)
        self.selections.setObjectName("extractionSelections")
        self.selections.setHorizontalHeaderLabels(("Column", "Field path", "Actions"))
        selection_header = self.selections.horizontalHeader()
        selection_header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        selection_header.setStretchLastSection(False)
        selection_header.resizeSection(0, 180)
        selection_header.resizeSection(1, 260)
        selection_header.resizeSection(2, _ACTIONS_COLUMN_MIN_WIDTH)
        self._automatic_actions_column_width = _ACTIONS_COLUMN_MIN_WIDTH
        self.selections.setHorizontalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        self.selections.ensurePolished()
        self.selections.verticalHeader().setDefaultSectionSize(
            max(40, self.selections.fontMetrics().height() + 18)
        )
        self.selections.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.selections.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
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
        self._selector_delete_shortcut = QShortcut(
            QKeySequence.StandardKey.Delete, self.selections
        )
        self._selector_delete_shortcut.setContext(
            Qt.ShortcutContext.WidgetWithChildrenShortcut
        )
        self._selector_delete_shortcut.activated.connect(self._remove_selected_rows)
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
        self.preview_header = _PreviewHeaderView(self.preview)
        self.preview.setHorizontalHeader(self.preview_header)
        self.preview.setAlternatingRowColors(True)
        self.preview.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.preview.setHorizontalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        self.preview_header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.preview_header.setSectionsClickable(True)
        self.preview_header.setHighlightSections(True)
        self.preview_header.sectionClicked.connect(self._select_preview_column)
        self.preview_header.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.preview_header.customContextMenuRequested.connect(
            self._show_preview_header_context_menu
        )
        self._pending_preview_selection_column: int | None = None
        self._preview_sort_column: int | None = None
        self._preview_sort_descending = False
        self.preview.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.preview.customContextMenuRequested.connect(self._show_preview_context_menu)
        self._preview_copy_shortcut = QShortcut(
            QKeySequence.StandardKey.Copy, self.preview
        )
        self._preview_copy_shortcut.setContext(
            Qt.ShortcutContext.WidgetWithChildrenShortcut
        )
        self._preview_copy_shortcut.activated.connect(self._copy_preview_selection)
        self._preview_delete_shortcut = QShortcut(
            QKeySequence("Backspace"), self.preview
        )
        self._preview_delete_shortcut.setContext(
            Qt.ShortcutContext.WidgetWithChildrenShortcut
        )
        self._preview_delete_shortcut.activated.connect(
            self._remove_selected_preview_columns
        )
        self._preview_sort_shortcut = QShortcut(QKeySequence("S"), self.preview)
        self._preview_sort_shortcut.setContext(
            Qt.ShortcutContext.WidgetWithChildrenShortcut
        )
        self._preview_sort_shortcut.activated.connect(
            self._sort_selected_preview_column
        )
        self._preview_rename_shortcut = QShortcut(QKeySequence("R"), self.preview)
        self._preview_rename_shortcut.setContext(
            Qt.ShortcutContext.WidgetWithChildrenShortcut
        )
        self._preview_rename_shortcut.activated.connect(
            self._rename_selected_preview_column
        )
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
        """Return selectors in their visible configuration order."""
        return tuple(self._selectors)

    @property
    def config(self) -> ExtractionConfig:
        """Build the complete configuration represented by the editor."""
        return ExtractionConfig(
            self.selectors,
            include_identifiers=self._include_identifiers,
            include_status_columns=self._include_status_columns,
        )

    def add_selector(self, selector: FieldSelector) -> None:
        """Append a selector with a unique export-column name."""
        self._pending_preview_selection_column = None
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
        """Replace editor state from a validated saved configuration."""
        self._pending_preview_selection_column = None
        self._selectors = list(config.selectors)
        self._include_identifiers = config.include_identifiers
        self._include_status_columns = config.include_status_columns
        if mark_clean:
            self._clean_config = config
        self._rebuild_selection_table(0 if self._selectors else -1)
        self._emit_changed()

    def clear(self) -> None:
        """Remove all configured selectors and preview rows."""
        self._pending_preview_selection_column = None
        self._selectors.clear()
        self._rebuild_selection_table(-1)
        self.preview_model.set_data((), ())
        self._emit_changed()

    def show_loading(self, count: int) -> None:
        """Show that a bounded preview is being built for ``count`` records."""
        self.status.setText(f"Building preview from {count:,} evaluations…")

    def set_preview(self, rows: tuple[ExtractionRow, ...], total: int) -> None:
        """Install preview rows while retaining the collection's full count."""
        self.preview_model.set_data(self.selectors, rows)
        pending_column = self._pending_preview_selection_column
        self._pending_preview_selection_column = None
        if pending_column is not None and self.preview_model.columnCount() > 3:
            self._select_preview_column(
                min(pending_column, self.preview_model.columnCount() - 1),
                use_keyboard_modifiers=False,
            )
        shown = self.preview_model.rowCount()
        suffix = " (limited)" if total > len(rows) or shown >= PREVIEW_LIMIT else ""
        self.status.setText(
            f"Preview: {shown:,} rows from {total:,} evaluations{suffix}"
        )

    def show_error(self, message: str) -> None:
        """Replace preview status with a user-facing extraction error."""
        self.status.setText(message)

    def clear_collection_sort(self) -> None:
        """Remove the header marker after collection order changes elsewhere."""
        self._preview_sort_column = None
        self._preview_sort_descending = False
        self.preview_header.clear_sort_marker()

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
        self._resize_field_actions_column()
        self._update_controls()

    def _resize_field_actions_column(self) -> None:
        required = _ACTIONS_COLUMN_MIN_WIDTH
        for row in range(self.selections.rowCount()):
            actions = self.selections.cellWidget(row, 2)
            if actions is None:
                continue
            actions.layout().invalidate()
            actions.layout().activate()
            required = max(required, actions.minimumSizeHint().width() + 8)
        header = self.selections.horizontalHeader()
        current = header.sectionSize(2)
        if current == self._automatic_actions_column_width or required > current:
            header.resizeSection(2, required)
        self._automatic_actions_column_width = required

    def _field_actions(self, row: int, selector: FieldSelector) -> ActionRow:
        actions = ActionRow()

        move_up = actions.add_action(
            "moveUpAction",
            "↑",
            f"Move {selector.column} up",
            compact=True,
            enabled=row > 0,
        )
        move_up.clicked.connect(
            lambda _checked=False, target=row: self._move_row(target, -1)
        )

        move_down = actions.add_action(
            "moveDownAction",
            "↓",
            f"Move {selector.column} down",
            compact=True,
            enabled=row < len(self._selectors) - 1,
        )
        move_down.clicked.connect(
            lambda _checked=False, target=row: self._move_row(target, 1)
        )

        rename = actions.add_action(
            "renameAction", "Rename", f"Rename extraction column {selector.column}"
        )
        rename.clicked.connect(
            lambda _checked=False, target=row: self._edit_column_name(target)
        )

        flatten = actions.add_action(
            "flattenAction",
            "Flatten objects",
            f"Recursively flatten nested dictionaries in {selector.column}",
            checkable=True,
        )
        flatten.setChecked(selector.flatten)
        flatten.toggled.connect(
            lambda checked, target=row: self._set_option(target, "flatten", checked)
        )

        expand_arrays = actions.add_action(
            "expandArraysAction",
            "Expand arrays",
            f"Expand array values in {selector.column} into columns",
            checkable=True,
        )
        expand_arrays.setChecked(selector.column_expansion_depth > 0)

        array_depth = QSpinBox()
        array_depth.setObjectName("arrayExpansionDepth")
        array_depth.setRange(1, 8)
        array_depth.setValue(max(1, selector.column_expansion_depth))
        array_depth.setPrefix("Levels: ")
        depth_metrics = array_depth.fontMetrics()
        array_depth.setMinimumWidth(
            depth_metrics.horizontalAdvance("Levels: 8") + 40
        )
        array_depth.setToolTip(
            f"Expand this many array levels in {selector.column} into columns; "
            "for shape [N, 3], choose 2 for scalar coordinate columns"
        )
        array_depth.setHidden(not expand_arrays.isChecked())
        expand_arrays.toggled.connect(
            lambda checked, target=row, control=array_depth: (
                self._set_array_expansion_enabled(target, checked, control)
            )
        )
        array_depth.valueChanged.connect(
            lambda depth, target=row, toggle=expand_arrays: (
                self._set_array_expansion_depth(target, depth)
                if toggle.isChecked()
                else None
            )
        )
        actions.add_widget(array_depth)

        remove = actions.add_action(
            "removeAction",
            "\u00d7",
            f"Remove {selector.column} from extraction",
            compact=True,
        )
        remove.clicked.connect(
            lambda _checked=False, target=row: self._remove_row(target)
        )
        return actions

    def _emit_changed(self) -> None:
        # Moving, replacing, or reshaping selectors can make a numeric preview
        # column refer to different data, so its collection-sort marker is stale.
        self.clear_collection_sort()
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

    def _edit_column_name(self, row: int) -> None:
        item = self.selections.item(row, 0)
        if item is None:
            return
        self.selections.setCurrentCell(row, 0)
        self.selections.editItem(item)

    def _set_option(self, row: int, option: str, checked: bool) -> None:
        if 0 <= row < len(self._selectors):
            selector = self._selectors[row]
            if option == "flatten":
                selector = replace(selector, flatten=checked)
            self._selectors[row] = selector
            self._emit_changed()

    def _set_array_expansion_depth(self, row: int, depth: int) -> None:
        if not 0 <= row < len(self._selectors):
            return
        self._selectors[row] = replace(
            self._selectors[row],
            array_expansion_depth=depth,
            expand_array=False,
        )
        self._emit_changed()

    def _set_array_expansion_enabled(
        self, row: int, enabled: bool, control: QSpinBox
    ) -> None:
        control.setVisible(enabled)
        self._resize_field_actions_column()
        self._set_array_expansion_depth(row, control.value() if enabled else 0)

    def _remove(self) -> None:
        self._remove_row(self.selections.currentRow())

    def _remove_row(self, row: int) -> None:
        if 0 <= row < len(self._selectors):
            del self._selectors[row]
            self._rebuild_selection_table(min(row, len(self._selectors) - 1))
            self._emit_changed()

    def _remove_selected_rows(self) -> None:
        selection = self.selections.selectionModel()
        rows = sorted(
            {index.row() for index in selection.selectedRows()} if selection else (),
            reverse=True,
        )
        if not rows and self.selections.currentRow() >= 0:
            rows = [self.selections.currentRow()]
        if not rows:
            return
        for row in rows:
            if 0 <= row < len(self._selectors):
                del self._selectors[row]
        self._rebuild_selection_table(min(rows[-1], len(self._selectors) - 1))
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

    def _show_preview_context_menu(self, position: QPoint) -> None:
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
        toggle_sort = sort_ascending = sort_descending = None
        if preview_column is not None:
            menu.addSeparator()
            sort_menu = menu.addMenu("Sort collection by this column")
            toggle_sort = sort_menu.addAction("Sort / reverse sort")
            toggle_sort.setShortcut(QKeySequence("S"))
            sort_menu.addSeparator()
            sort_ascending = sort_menu.addAction("Sort ↑")
            sort_descending = sort_menu.addAction("Sort ↓")
            sort_reason = self.preview_model.sort_unavailable_reason(index.column())
            if sort_reason is not None:
                sort_menu.setTitle("Sorting unavailable (non-scalar column)")
                sort_menu.setToolTip(sort_reason)
                sort_menu.setEnabled(False)
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
        elif selected is toggle_sort:
            self._toggle_preview_sort(index.column())
        elif selected is sort_ascending and preview_column is not None:
            self._request_preview_sort(index.column(), preview_column, False)
        elif selected is sort_descending and preview_column is not None:
            self._request_preview_sort(index.column(), preview_column, True)

    def _show_preview_header_context_menu(self, position: QPoint) -> None:
        header = self.preview_header
        column = header.logicalIndexAt(position)
        if column < 0:
            return
        preview_column = self.preview_model.extraction_column(column)
        menu = QMenu(header)
        copy_column = menu.addAction("Copy column as CSV")
        toggle_sort = sort_ascending = sort_descending = None
        rename_column = remove_column = None
        selected_columns = self._selected_preview_columns(column)
        remove_selected = None
        if preview_column is not None:
            menu.addSeparator()
            sort_menu = menu.addMenu("Sort collection by this column")
            toggle_sort = sort_menu.addAction("Sort / reverse sort")
            toggle_sort.setShortcut(QKeySequence("S"))
            sort_menu.addSeparator()
            sort_ascending = sort_menu.addAction("Sort ↑")
            sort_descending = sort_menu.addAction("Sort ↓")
            reason = self.preview_model.sort_unavailable_reason(column)
            if reason is not None:
                sort_menu.setTitle("Sorting unavailable (non-scalar column)")
                sort_menu.setToolTip(reason)
                sort_menu.setEnabled(False)
            rename_column = menu.addAction("Rename column…")
            rename_column.setShortcut(QKeySequence("R"))
            remove_column = menu.addAction("Remove column")
            if len(selected_columns) > 1:
                remove_selected = menu.addAction(
                    f"Remove {len(selected_columns)} selected columns"
                )
                remove_selected.setShortcut(QKeySequence("Backspace"))
        selected = menu.exec(header.mapToGlobal(position))
        if selected is copy_column:
            self._copy_preview_column(column)
        elif selected is toggle_sort:
            self._toggle_preview_sort(column)
        elif selected is sort_ascending and preview_column is not None:
            self._request_preview_sort(column, preview_column, False)
        elif selected is sort_descending and preview_column is not None:
            self._request_preview_sort(column, preview_column, True)
        elif selected is rename_column and preview_column is not None:
            self._rename_preview_column(preview_column)
        elif selected is remove_column and preview_column is not None:
            self._remove_preview_columns((preview_column,))
        elif selected is remove_selected:
            self._remove_preview_columns(selected_columns)

    def _selected_preview_columns(
        self, clicked_column: int | None = None
    ) -> tuple[_PreviewColumn, ...]:
        selection = self.preview.selectionModel()
        indexes = selection.selectedIndexes() if selection is not None else []
        columns = {index.column() for index in indexes}
        if clicked_column is not None:
            columns.add(clicked_column)
        return tuple(
            preview_column
            for column in sorted(columns)
            if (preview_column := self.preview_model.extraction_column(column))
            is not None
        )

    def _remove_selected_preview_columns(self) -> None:
        self._remove_preview_columns(self._selected_preview_columns())

    def _remove_preview_columns(
        self, columns: tuple[_PreviewColumn, ...]
    ) -> None:
        if not columns:
            return
        self._pending_preview_selection_column = (
            self._next_preview_column_after_removal(columns)
        )
        remove_selectors = {
            column.selector_index for column in columns if column.nested_path is None
        }
        excluded: dict[int, set[JsonPath]] = {}
        for column in columns:
            if column.nested_path is not None:
                excluded.setdefault(column.selector_index, set()).add(
                    column.nested_path
                )
        updated: list[FieldSelector] = []
        for index, selector in enumerate(self._selectors):
            if index in remove_selectors:
                continue
            paths = excluded.get(index)
            if paths:
                names = tuple(
                    (path, name)
                    for path, name in selector.flattened_column_names
                    if path not in paths
                )
                updated_selector = replace(
                    selector,
                    excluded_paths=tuple(
                        dict.fromkeys(
                            (
                                *selector.excluded_paths,
                                *sorted(paths, key=lambda path: tuple(map(str, path))),
                            )
                        )
                    ),
                    flattened_column_names=names,
                )
            else:
                updated_selector = selector
            updated.append(updated_selector)
        self._selectors = updated
        self._rebuild_selection_table(
            min(self.selections.currentRow(), len(self._selectors) - 1)
        )
        self._emit_changed()

    def _next_preview_column_after_removal(
        self, columns: tuple[_PreviewColumn, ...]
    ) -> int | None:
        visible = tuple(
            preview_column
            for logical_column in range(3, self.preview_model.columnCount())
            if (
                preview_column := self.preview_model.extraction_column(logical_column)
            )
            is not None
        )
        removed = set(columns)
        removed_positions = [
            index for index, column in enumerate(visible) if column in removed
        ]
        survivors = [
            (index, column)
            for index, column in enumerate(visible)
            if column not in removed
        ]
        if not removed_positions or not survivors:
            return None
        next_survivor = next(
            (
                survivor_index
                for survivor_index, (original_index, _column) in enumerate(survivors)
                if original_index > max(removed_positions)
            ),
            len(survivors) - 1,
        )
        return 3 + next_survivor

    def _select_preview_column(
        self, column: int, *, use_keyboard_modifiers: bool = True
    ) -> None:
        if not 0 <= column < self.preview_model.columnCount():
            return
        selection = self.preview.selectionModel()
        if selection is None or self.preview_model.rowCount() == 0:
            return
        modifiers = (
            QApplication.keyboardModifiers()
            if use_keyboard_modifiers
            else Qt.KeyboardModifier.NoModifier
        )
        flags = QItemSelectionModel.SelectionFlag.Columns
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            flags |= QItemSelectionModel.SelectionFlag.Toggle
        else:
            flags |= QItemSelectionModel.SelectionFlag.ClearAndSelect
        index = self.preview_model.index(0, column)
        selection.select(index, flags)
        selection.setCurrentIndex(index, QItemSelectionModel.SelectionFlag.NoUpdate)
        self.preview.setFocus()

    def _sort_selected_preview_column(self) -> None:
        index = self.preview.currentIndex()
        if not index.isValid():
            return
        self._toggle_preview_sort(index.column())

    def _rename_selected_preview_column(self) -> None:
        index = self.preview.currentIndex()
        if not index.isValid():
            return
        column = self.preview_model.extraction_column(index.column())
        if column is not None:
            self._rename_preview_column(column)

    def _rename_preview_column(self, column: _PreviewColumn) -> None:
        if column.nested_path is None:
            self._edit_column_name(column.selector_index)
        else:
            self._rename_flattened_column(column)

    def _toggle_preview_sort(self, column: int) -> None:
        preview_column = self.preview_model.extraction_column(column)
        if preview_column is None:
            return
        descending = (
            not self._preview_sort_descending
            if self._preview_sort_column == column
            else False
        )
        self._request_preview_sort(column, preview_column, descending)

    def _request_preview_sort(
        self,
        column: int,
        preview_column: _PreviewColumn,
        descending: bool,
    ) -> None:
        reason = self.preview_model.sort_unavailable_reason(column)
        if reason is not None:
            self.status_message.emit(reason)
            return
        self._preview_sort_column = column
        self._preview_sort_descending = descending
        order = (
            Qt.SortOrder.DescendingOrder
            if descending
            else Qt.SortOrder.AscendingOrder
        )
        header = self.preview_header
        header.set_sort_marker(column, order)
        header.resizeSection(column, max(header.sectionSize(column), 150))
        self.sort_requested.emit(
            (
                self._selectors[preview_column.selector_index],
                preview_column.nested_path,
            ),
            descending,
        )

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


def _preview_columns(
    selectors: tuple[FieldSelector, ...], rows: tuple[ExtractionRow, ...]
) -> tuple[_PreviewColumn, ...]:
    result: list[_PreviewColumn] = []
    for selector_index, selector in enumerate(selectors):
        paths: set[JsonPath] = set()
        if selector.flatten or selector.column_expansion_depth:
            for row in rows:
                value = row.values[selector_index].value
                paths.update(
                    column_paths_for_value(
                        value,
                        flatten_mappings=selector.flatten,
                        array_depth=selector.column_expansion_depth,
                    )
                )
        if selector.flatten or selector.column_expansion_depth:
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
