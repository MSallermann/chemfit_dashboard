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
from copy import deepcopy
from dataclasses import dataclass
from itertools import islice
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
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QSpinBox,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from chemfit_dashboard.coordinator import DashboardCoordinator
from chemfit_dashboard.extraction import (
    ExtractionConfig,
    ExtractionRow,
    ExtractionStatus,
    ExtractionValue,
    FieldSelector,
    JsonPath,
    OutputDescriptor,
    ResolvedField,
    column_paths_for_value,
    export_file_atomically,
    export_json_file_process,
    extract_fields,
    field_to_dict,
    flattened_column_name,
    load_config,
    resolve_fields,
    resolved_flattened_column_name,
    save_config,
)
from chemfit_dashboard.icons import make_icon_only, set_themed_icon
from chemfit_dashboard.source import JsonFileSource
from chemfit_dashboard.ui_components import ActionRow, muted_label
from chemfit_dashboard.util import format_combined_path
from chemfit_dashboard.workers import TaskContext

PREVIEW_LIMIT = 200
PROCESS_EXPORT_THRESHOLD = 200
_ACTIONS_COLUMN_MIN_WIDTH = 320
_PREVIEW_MISSING = object()
CONFIG_DIRECTORY_SETTING = "paths/extraction_config"
EXPORT_DIRECTORY_SETTING = "paths/extraction_export"


@dataclass(frozen=True, slots=True)
class _PreviewColumn:
    selector_index: int
    source_index: int
    selector: FieldSelector
    nested_path: JsonPath | None
    label: str


@dataclass(slots=True)
class _PreviewInteractionState:
    """Selection restoration and collection sort owned by the preview."""

    pending_selection_column: int | None = None
    sort_column: int | None = None
    sort_descending: bool = False

    def clear_sort(self) -> None:
        """Forget the sort marker when another action changes collection order."""
        self.sort_column = None
        self.sort_descending = False


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
            selection and selection.isColumnSelected(logical_index, QModelIndex())
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
        self, fields: tuple[ResolvedField, ...], rows: tuple[ExtractionRow, ...]
    ) -> None:
        """Replace preview rows and derive visible flattened columns."""
        self.beginResetModel()
        self._rows = rows[:PREVIEW_LIMIT]
        self._columns = _preview_columns(fields, self._rows)
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
            if (
                extracted.status != ExtractionStatus.AVAILABLE
                or extracted.value is None
            ):
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

        if extracted.status not in (
            ExtractionStatus.AVAILABLE,
            ExtractionStatus.SERIALIZED_EXCEPTION,
        ):
            return extracted

        nested = _nested_preview_value(extracted.value, column.nested_path)
        return ExtractionValue(
            None if nested is _PREVIEW_MISSING else nested,
            (
                ExtractionStatus.MISSING_FIELD
                if nested is _PREVIEW_MISSING
                else extracted.status
            ),
        )


class DataExtractionPanel(QWidget):
    """
    Widget for configuring and previewing data extraction.

    Manages field selections and their output settings, displays a bounded
    preview of extracted values, and provides configuration and export actions.

    Emits signals for extraction, sorting, and navigation requests. The parent
    application handles evaluation traversal and background processing.
    """

    selectors_changed = Signal(object)
    export_requested = Signal(str, str, object)
    status_message = Signal(str)
    sort_requested = Signal(object, bool)
    record_selected = Signal(str)

    def __init__(  # noqa: PLR0915
        self,
        *,
        coordinator: DashboardCoordinator,
        parent: QWidget | None = None,
        settings: QSettings | None = None,
    ):
        """Initialize extraction selection, preview, and export controls."""
        super().__init__(parent)
        self._fields: list[tuple[FieldSelector, OutputDescriptor]] = []
        self._include_identifiers = True
        self._include_node_paths = True
        self._settings = settings
        self.coordinator = coordinator
        layout = QVBoxLayout(self)
        heading = QHBoxLayout()

        self.title = QLabel("DATA EXTRACTION")
        self.title.setObjectName("sectionLabel")
        heading.addWidget(self.title)
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
        self.include_node_paths_checkbox = QCheckBox("Include node paths")
        self.include_node_paths_checkbox.setChecked(True)
        self.include_node_paths_checkbox.setToolTip(
            "Prefix every output name with its concrete context path"
        )
        self.include_node_paths_checkbox.toggled.connect(self._set_include_node_paths)
        controls.addWidget(self.include_node_paths_checkbox)
        self.clear_button = QPushButton("Clear all")
        set_themed_icon(self.clear_button, "trash", role="danger")
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
        self.preview.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.preview.setHorizontalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        self.preview_header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.preview_header.setSectionsClickable(True)
        self.preview_header.setHighlightSections(True)
        self.preview_header.sectionClicked.connect(self._select_preview_column)
        self.preview_header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.preview_header.customContextMenuRequested.connect(
            self._show_preview_header_context_menu
        )
        preview_row_header = self.preview.verticalHeader()
        preview_row_header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        preview_row_header.customContextMenuRequested.connect(
            self._show_preview_row_context_menu
        )
        # Preview columns rebuild asynchronously after selector edits. This
        # state carries only interaction continuity across that rebuild.
        self._preview_state = _PreviewInteractionState()
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
        make_icon_only(self.save_button, "save", "Save extraction configuration")
        self.load_button = QPushButton("Load config")
        make_icon_only(self.load_button, "load", "Load extraction configuration")
        self.copy_config_button = QPushButton("Copy config")
        make_icon_only(
            self.copy_config_button,
            "copy",
            "Copy JSON compatible with chemfit-extract --config",
        )
        self.paste_config_button = QPushButton("Paste config")
        make_icon_only(
            self.paste_config_button,
            "paste",
            "Load an extraction configuration from the clipboard",
        )
        self.csv_button = QPushButton("CSV")
        self.csv_button.setToolTip("Export extracted values as CSV")
        self.jsonl_button = QPushButton("JSONL")
        self.jsonl_button.setToolTip("Export extracted values as JSON Lines")
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
        ):
            actions.addWidget(widget)
        actions.addSpacing(8)
        actions.addWidget(QLabel("Export:"))
        actions.addWidget(self.csv_button)
        actions.addWidget(self.jsonl_button)
        actions.addStretch()
        layout.addLayout(actions)
        self._update_controls()

        self.selectors_changed.connect(self.refresh_preview)
        self.coordinator.collection_changed.connect(self.refresh_preview)
        self.coordinator.collection_order_changed.connect(self.refresh_preview)

    @property
    def fields(self) -> tuple[tuple[FieldSelector, OutputDescriptor], ...]:
        """Return selectors in their visible configuration order."""
        return tuple(self._fields)

    @property
    def selectors(self) -> tuple[FieldSelector, ...]:
        """Return selectors in their visible configuration order."""
        return tuple(selector for (selector, _output) in self._fields)

    @property
    def n_selectors(self) -> int:
        return len(self._fields)

    @property
    def outputs(self) -> tuple[OutputDescriptor, ...]:
        """Return output descriptors in their visible configuration order."""
        return tuple(output for (_selector, output) in self._fields)

    @property
    def config(self) -> ExtractionConfig:
        """Build the complete configuration represented by the editor."""
        return ExtractionConfig(
            fields=deepcopy(tuple(self._fields)),
            include_identifiers=self._include_identifiers,
            include_node_paths=self._include_node_paths,
        )

    def add_selector(self, selector: FieldSelector, output: OutputDescriptor) -> None:
        """Append a selector with a unique export-column name."""

        # so we dont modify the callers object
        output = deepcopy(output)

        self._preview_state.pending_selection_column = None
        if output.column is not None:
            output.column = output.column.strip() or None
        if output.column is not None:
            existing = {
                candidate.column.casefold()
                for candidate in self.outputs
                if candidate.column is not None
            }
            base = output.column

            # if there is a naming conflict in explicit overrides, append a suffix
            column = base
            suffix = 2
            while column.casefold() in existing:
                column = f"{base}_{suffix}"
                suffix += 1
            output.column = column
        self._fields.append((selector, output))

        self._rebuild_selection_table(self.n_selectors - 1)
        self._emit_changed()

    def export_extraction(
        self, path: str, format_name: str, config: ExtractionConfig
    ) -> None:
        """Export the current collection using the supplied configuration."""
        records = tuple(self.coordinator.collection)
        source = self.coordinator.source

        def thread_task(*, task_context: TaskContext) -> int:
            return export_file_atomically(
                path,
                format_name,
                source,
                records,
                config,
                task_context=task_context,
            )

        process_safe = (
            source.__class__ is JsonFileSource
            and len(records) >= PROCESS_EXPORT_THRESHOLD
        )
        function = export_json_file_process if process_safe else thread_task
        args = (path, format_name, records, config) if process_safe else ()

        self.coordinator.task_runner.submit(
            function=function,
            on_success=lambda count: self.status_message.emit(
                f"Exported {count:,} evaluations to {path}"
            ),
            on_error=self.show_error,
            backend="process" if process_safe else "thread",
            label=f"Exporting {format_name.upper()}",
            args=args,
            with_context=True,
        )

    def set_config(self, config: ExtractionConfig) -> None:
        """Replace editor state from a validated saved configuration."""
        self._preview_state.pending_selection_column = None
        self._fields = deepcopy(list(config.fields))
        self._include_identifiers = config.include_identifiers
        self._include_node_paths = config.include_node_paths
        self.include_node_paths_checkbox.blockSignals(True)
        self.include_node_paths_checkbox.setChecked(self._include_node_paths)
        self.include_node_paths_checkbox.blockSignals(False)
        self._rebuild_selection_table(0 if self._fields else -1)
        self._emit_changed()

    def clear(self) -> None:
        """Remove all configured selectors and preview rows."""
        self._preview_state.pending_selection_column = None
        self._fields.clear()
        self._rebuild_selection_table(-1)
        self.preview_model.set_data((), ())
        self._emit_changed()

    def show_loading(self, count: int) -> None:
        """Show that a bounded preview is being built for ``count`` records."""
        self.status.setText(f"Building preview from {count:,} evaluations…")

    def set_preview(
        self,
        fields: tuple[ResolvedField, ...],
        rows: tuple[ExtractionRow, ...],
        total: int,
    ) -> None:
        """Install preview rows while retaining the collection's full count."""
        self.preview_model.set_data(fields, rows)
        pending_column = self._preview_state.pending_selection_column
        self._preview_state.pending_selection_column = None
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

    def refresh_preview(self) -> None:
        """Build a bounded extraction preview in the background."""
        records = tuple(self.coordinator.collection)
        config = self.config
        selectors = tuple(selector for selector, _ in config.fields)

        if not records or not selectors:
            self.coordinator.task_runner.invalidate("extraction_preview")
            self.set_preview((), (), len(records))
            return

        self.show_loading(len(records))

        def task():
            preview_records = records[:PREVIEW_LIMIT]
            fields = resolve_fields(
                self.coordinator.source,
                preview_records,
                config.fields,
                include_node_paths=config.include_node_paths,
            )
            rows = tuple(
                islice(
                    extract_fields(
                        self.coordinator.source,
                        preview_records,
                        tuple(field.selector for field in fields),
                    ),
                    PREVIEW_LIMIT,
                )
            )
            return fields, rows

        def install_preview(
            result: tuple[tuple[ResolvedField, ...], tuple[ExtractionRow, ...]],
        ) -> None:
            fields, rows = result
            self.set_preview(fields, rows, len(records))

        self.coordinator.task_runner.submit_latest(
            key="extraction_preview",
            function=task,
            on_success=install_preview,
            on_error=self.show_error,
            label="Building extraction preview",
        )

    def show_error(self, message: str) -> None:
        """Replace preview status with a user-facing extraction error."""
        self.status.setText(message)

    def clear_collection_sort(self) -> None:
        """Remove the header marker after collection order changes elsewhere."""
        self._preview_state.clear_sort()
        self.preview_header.clear_sort_marker()

    def set_collection_sort_direction(self, descending: bool) -> None:
        """Change sort direction without losing the selected column."""
        column = self._preview_state.sort_column
        if column is None:
            return

        self._preview_state.sort_descending = descending

        order = (
            Qt.SortOrder.DescendingOrder if descending else Qt.SortOrder.AscendingOrder
        )
        self.preview_header.set_sort_marker(column, order)

    def _rebuild_selection_table(self, selected_row: int) -> None:
        self.selections.blockSignals(True)
        self.selections.clearContents()
        self.selections.setRowCount(len(self._fields))
        for row, (selector, output) in enumerate(self.fields):
            self.selections.setItem(
                row,
                0,
                QTableWidgetItem(_configured_column_label(selector, output)),
            )
            path_item = QTableWidgetItem(
                format_combined_path(selector.node_path, selector.value_path)
            )
            path_item.setFlags(path_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.selections.setItem(row, 1, path_item)
            self.selections.setCellWidget(
                row,
                2,
                self._field_actions(row, selector, output),
            )
        self.selections.blockSignals(False)
        if 0 <= selected_row < self.n_selectors:
            self.selections.setCurrentCell(selected_row, 0)
        self._resize_field_actions_column()
        self._update_controls()

    def _resize_field_actions_column(self) -> None:
        required = _ACTIONS_COLUMN_MIN_WIDTH
        for row in range(self.selections.rowCount()):
            actions = self.selections.cellWidget(row, 2)
            if actions is None:
                continue
            actions_layout = actions.layout()
            if actions_layout is not None:
                actions_layout.invalidate()
                actions_layout.activate()
            required = max(required, actions.minimumSizeHint().width() + 8)
        header = self.selections.horizontalHeader()
        current = header.sectionSize(2)
        if current == self._automatic_actions_column_width or required > current:
            header.resizeSection(2, required)
        self._automatic_actions_column_width = required

    def _field_actions(
        self,
        row: int,
        selector: FieldSelector,
        output: OutputDescriptor,
    ) -> ActionRow:
        actions = ActionRow()
        column_label = _configured_column_label(selector, output)

        move_up = actions.add_action(
            "moveUpAction",
            "↑",
            f"Move {column_label} up",
            compact=True,
            enabled=row > 0,
            icon_name="arrow_up",
            icon_only=True,
        )
        move_up.clicked.connect(
            lambda _checked=False, target=row: self._move_row(target, -1)
        )

        move_down = actions.add_action(
            "moveDownAction",
            "↓",
            f"Move {column_label} down",
            compact=True,
            enabled=row < self.n_selectors - 1,
            icon_name="arrow_down",
            icon_only=True,
        )
        move_down.clicked.connect(
            lambda _checked=False, target=row: self._move_row(target, 1)
        )

        rename = actions.add_action(
            "renameAction",
            "Rename",
            f"Rename extraction column {column_label}",
            icon_name="pencil",
            icon_only=True,
        )
        rename.clicked.connect(
            lambda _checked=False, target=row: self._edit_column_name(target)
        )

        flatten = actions.add_action(
            "flattenAction",
            "Flatten objects",
            f"Recursively flatten nested dictionaries in {column_label}",
            checkable=True,
        )
        flatten.setChecked(output.flatten)
        flatten.toggled.connect(
            lambda checked, target=row: self._set_flatten(target, checked)
        )

        expand_arrays = actions.add_action(
            "expandArraysAction",
            "Expand arrays",
            f"Expand array values in {column_label} into columns",
            checkable=True,
        )
        expand_arrays.setChecked(output.array_expansion_depth > 0)

        array_depth = QSpinBox()
        array_depth.setObjectName("arrayExpansionDepth")
        array_depth.setRange(1, 8)
        array_depth.setValue(max(1, output.array_expansion_depth))
        array_depth.setPrefix("Levels: ")
        depth_metrics = array_depth.fontMetrics()
        array_depth.setMinimumWidth(depth_metrics.horizontalAdvance("Levels: 8") + 40)
        array_depth.setToolTip(
            f"Expand this many array levels in {column_label} into columns; "
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
            f"Remove {column_label} from extraction",
            compact=True,
            icon_name="trash",
            icon_role="danger",
            icon_only=True,
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
        self.selectors_changed.emit(self.config.fields)

    def _set_include_node_paths(self, checked: bool) -> None:
        self._include_node_paths = checked
        self._emit_changed()

    def _column_edited(self, item: QTableWidgetItem) -> None:
        if item.column() != 0 or not (0 <= item.row() < self.n_selectors):
            return
        column = item.text().strip()
        selector, output = self._fields[item.row()]
        if output.column is None and column == selector.format():
            return
        if not column:
            output.column = None
            self._rebuild_selection_table(item.row())
            self._emit_changed()
            return

        output.column = column
        self._emit_changed()

    def _edit_column_name(self, row: int) -> None:
        item = self.selections.item(row, 0)
        if item is None:
            return
        self.selections.setCurrentCell(row, 0)
        self.selections.editItem(item)
        # Some Qt styles size the transient item editor one pixel below its
        # font's readable height after other top-level windows have existed.
        # Keep the editor and its row explicitly compatible across styles.
        editor = self.selections.findChild(QLineEdit)
        if editor is not None:
            minimum_height = editor.fontMetrics().height() + 4
            editor.setMinimumHeight(minimum_height)
            self.selections.setRowHeight(
                row, max(self.selections.rowHeight(row), minimum_height)
            )

    def _set_flatten(self, row: int, checked: bool) -> None:
        if 0 <= row < self.n_selectors:
            output = self.outputs[row]
            output.flatten = checked
            self._emit_changed()

    def _set_array_expansion_depth(self, row: int, depth: int) -> None:
        if not 0 <= row < self.n_selectors:
            return
        self.outputs[row].array_expansion_depth = depth
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
        if 0 <= row < self.n_selectors:
            del self._fields[row]
            self._rebuild_selection_table(min(row, self.n_selectors - 1))
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
            if 0 <= row < self.n_selectors:
                del self._fields[row]
        self._rebuild_selection_table(min(rows[-1], self.n_selectors - 1))
        self._emit_changed()

    def _move(self, offset: int) -> None:
        self._move_row(self.selections.currentRow(), offset)

    def _move_row(self, row: int, offset: int) -> None:
        target = row + offset
        if 0 <= row < self.n_selectors and 0 <= target < self.n_selectors:
            self._fields[row], self._fields[target] = (
                self._fields[target],
                self._fields[row],
            )
            self._rebuild_selection_table(target)
            self._emit_changed()

    def _update_controls(self) -> None:
        self.clear_button.setEnabled(bool(self._fields))
        self.save_button.setEnabled(bool(self._fields))
        self.copy_config_button.setEnabled(bool(self._fields))
        self.csv_button.setEnabled(bool(self._fields))
        self.jsonl_button.setEnabled(bool(self._fields))

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
        if item is None or not 0 <= item.row() < self.n_selectors:
            return
        row = item.row()
        self.selections.setCurrentCell(row, item.column())
        menu = QMenu(self.selections)
        copy_column = menu.addAction("Copy column name")
        copy_path = menu.addAction("Copy field path")
        copy_selector = menu.addAction("Copy field configuration as JSON")
        selected = menu.exec(self.selections.viewport().mapToGlobal(position))
        if selected is None:
            return
        if selected is copy_column:
            self._copy_text(
                _configured_column_label(self.selectors[row], self.outputs[row]),
                "Copied extraction column name",
            )
        elif selected is copy_path:
            self._copy_text(
                format_combined_path(
                    self.selectors[row].node_path, self.selectors[row].value_path
                ),
                "Copied extraction field path",
            )
        elif selected is copy_selector:
            self._copy_selector_json(row)

    def _copy_current_selector(self) -> None:
        self._copy_selector_json(self.selections.currentRow())

    def _copy_selector_json(self, row: int) -> None:
        # This is a context-menu action that copies the configuration of one extraction field to the clipboard as JSON.

        if not 0 <= row < len(self._fields):
            return

        selector, output = self._fields[row]

        self._copy_text(
            json.dumps(
                field_to_dict(selector, output),
                indent=2,
                ensure_ascii=False,
            ),
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
        if selected is None:
            return
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
        if selected is None:
            return
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

    def _show_preview_row_context_menu(self, position: QPoint) -> None:
        header = self.preview.verticalHeader()
        row = header.logicalIndexAt(position)
        values = self.preview_model.row_mapping(row)
        if not values:
            return

        index = self.preview_model.index(row, 0)
        selection = self.preview.selectionModel()
        if selection is not None:
            selection.select(
                index,
                QItemSelectionModel.SelectionFlag.ClearAndSelect
                | QItemSelectionModel.SelectionFlag.Rows,
            )
            selection.setCurrentIndex(index, QItemSelectionModel.SelectionFlag.NoUpdate)

        menu = QMenu(header)
        open_evaluation = menu.addAction("Open evaluation in Context Inspector")
        menu.addSeparator()
        copy_row = menu.addAction("Copy row as CSV")
        copy_row_json = menu.addAction("Copy row as JSON")
        menu.addSeparator()
        copy_filename = menu.addAction("Copy filename")
        copy_record_id = menu.addAction("Copy record ID")
        selected = menu.exec(header.mapToGlobal(position))
        if selected is None:
            return
        if selected is open_evaluation:
            self.record_selected.emit(str(values["Record ID"]))
        elif selected is copy_row:
            self._copy_preview_row(row)
        elif selected is copy_row_json:
            self._copy_preview_row(row, as_json=True)
        elif selected is copy_filename:
            self._copy_text(str(values["Filename"]), "Copied evaluation filename")
        elif selected is copy_record_id:
            self._copy_text(str(values["Record ID"]), "Copied evaluation record ID")

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

    def _remove_preview_columns(self, columns: tuple[_PreviewColumn, ...]) -> None:
        if not columns:
            return
        self._preview_state.pending_selection_column = (
            self._next_preview_column_after_removal(columns)
        )
        remove_selectors = {
            column.source_index for column in columns if column.nested_path is None
        }
        excluded: dict[int, set[JsonPath]] = {}
        for column in columns:
            if column.nested_path is not None:
                excluded.setdefault(column.source_index, set()).add(column.nested_path)

        updated_fields: list[tuple[FieldSelector, OutputDescriptor]] = []

        for index, (selector, output) in enumerate(self._fields):
            if index in remove_selectors:
                continue

            paths = excluded.get(index)
            if paths:
                output.excluded_paths = tuple(
                    dict.fromkeys(
                        (
                            *output.excluded_paths,
                            *sorted(paths, key=lambda path: tuple(map(str, path))),
                        )
                    )
                )
                output.flattened_column_names = tuple(
                    (path, name)
                    for path, name in output.flattened_column_names
                    if path not in paths
                )

            updated_fields.append((selector, output))

        self._fields = updated_fields

        self._rebuild_selection_table(
            min(self.selections.currentRow(), len(self._fields) - 1)
        )
        self._emit_changed()

    def _next_preview_column_after_removal(
        self, columns: tuple[_PreviewColumn, ...]
    ) -> int | None:
        visible = tuple(
            preview_column
            for logical_column in range(3, self.preview_model.columnCount())
            if (preview_column := self.preview_model.extraction_column(logical_column))
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
            self._edit_column_name(column.source_index)
        else:
            self._rename_flattened_column(column)

    def _toggle_preview_sort(self, column: int) -> None:
        preview_column = self.preview_model.extraction_column(column)
        if preview_column is None:
            return
        descending = (
            not self._preview_state.sort_descending
            if self._preview_state.sort_column == column
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
        # Rebuilding the preview after collection reordering resets Qt's item
        # selection. Carry the sorted column through that reset so its header
        # remains highlighted and subsequent S/R/Backspace shortcuts still
        # target the same column.
        self._preview_state.pending_selection_column = column
        self._preview_state.sort_column = column
        self._preview_state.sort_descending = descending
        order = (
            Qt.SortOrder.DescendingOrder if descending else Qt.SortOrder.AscendingOrder
        )
        header = self.preview_header
        header.set_sort_marker(column, order)
        header.resizeSection(column, max(header.sectionSize(column), 150))

        field_selector = FieldSelector(
            node_path=preview_column.selector.node_path,
            value_path=(
                *preview_column.selector.value_path,
                *(preview_column.nested_path or ()),
            ),
        )

        self.coordinator.sort_records_by_field(
            field_selector=field_selector,
            ascending=not descending,
        )

    def _exclude_flattened_column(self, column: _PreviewColumn) -> None:
        path = column.nested_path
        if path is None:
            return
        selector = self.outputs[column.source_index]
        names = tuple(
            (item_path, name)
            for item_path, name in selector.flattened_column_names
            if item_path != path
        )
        self.outputs[column.source_index].excluded_paths = (
            *selector.excluded_paths,
            path,
        )
        self.outputs[column.source_index].flattened_column_names = names

        self._emit_changed()

    def _rename_flattened_column(self, column: _PreviewColumn) -> None:
        path = column.nested_path
        if path is None:
            return
        output = self.outputs[column.source_index]
        current = flattened_column_name(output, path)
        name, accepted = QInputDialog.getText(
            self,
            "Rename flattened column",
            "Column name:",
            text=current,
        )
        name = name.strip()
        if not accepted or not name:
            return
        names = dict(output.flattened_column_names)
        names[path] = name

        output.flattened_column_names = tuple(names.items())
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
                (
                    _clipboard_text(
                        self.preview_model.copy_value(
                            self.preview_model.index(row, column)
                        )
                    )
                    if (row, column) in selected
                    else ""
                )
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
                self.set_config(load_config(path))
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
            self.export_extraction(
                path=path, format_name=format_name, config=self.config
            )

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


def _configured_column_label(
    selector: FieldSelector,
    output: OutputDescriptor,
) -> str:
    return output.column or selector.format()


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
    fields: tuple[ResolvedField, ...], rows: tuple[ExtractionRow, ...]
) -> tuple[_PreviewColumn, ...]:
    result: list[_PreviewColumn] = []
    for selector_index, field in enumerate(fields):
        output = field.output
        paths: set[JsonPath] = set()
        if output.flatten or output.array_expansion_depth:
            for row in rows:
                value = row.values[selector_index].value
                paths.update(
                    column_paths_for_value(
                        value,
                        flatten_mappings=output.flatten,
                        array_depth=output.array_expansion_depth,
                    )
                )
        if output.flatten or output.array_expansion_depth:
            result.extend(
                _PreviewColumn(
                    selector_index,
                    field.source_index,
                    field.selector,
                    path,
                    resolved_flattened_column_name(field, path),
                )
                for path in sorted(paths, key=lambda item: tuple(map(str, item)))
                if path not in output.excluded_paths
            )
        else:
            result.append(
                _PreviewColumn(
                    selector_index,
                    field.source_index,
                    field.selector,
                    None,
                    cast("str", output.column),
                )
            )
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
