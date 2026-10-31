from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QModelIndex, Qt, Signal
from PySide6.QtGui import QKeyEvent, QKeySequence, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFrame,
    QGridLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QPushButton,
    QTableView,
    QTabWidget,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from chemfit_dashboard.collection_plot import NumberSelector
from chemfit_dashboard.extraction import FieldSelector
from chemfit_dashboard.models import JsonValueModel
from chemfit_dashboard.source import NodeDetails

if TYPE_CHECKING:
    from collections.abc import Mapping


class JsonView(QTreeView):
    numeric_selected = Signal(object)
    value_selected = Signal(object)
    copy_completed = Signal(str)

    def __init__(self, section: str = "value", parent: QWidget | None = None):
        """Initialize a generic expandable value view."""
        super().__init__(parent)
        self._section = section
        self._json_model = JsonValueModel(parent=self)
        self.setModel(self._json_model)
        self.setAlternatingRowColors(True)
        self.setUniformRowHeights(True)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.selectionModel().currentChanged.connect(self._current_changed)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

    def set_value(self, value: Any) -> None:
        self._json_model.set_value(value)
        self.expandToDepth(0)

    def focus_path(self, path: tuple[str | int, ...]) -> bool:
        """Expand, select, and reveal one nested JSON value."""
        index = self._json_model.index_for_value_path(path)
        if not index.isValid():
            return False
        value_index = index.siblingAtColumn(1)
        self.setCurrentIndex(value_index)
        self.scrollTo(value_index, QAbstractItemView.ScrollHint.PositionAtCenter)
        return True

    def _current_changed(self, current: QModelIndex, _previous: QModelIndex) -> None:
        value = self._json_model.value(current)
        path = self._json_model.value_path(current)
        self.value_selected.emit((path, value) if current.isValid() else None)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            self.numeric_selected.emit((path, value))
        else:
            self.numeric_selected.emit(None)

    def copy_current_value(self, *, as_json: bool = False) -> None:
        index = self.currentIndex()
        if not index.isValid():
            return
        value = self._json_model.value(index)
        text = (
            json.dumps(value, ensure_ascii=False, indent=2)
            if as_json
            else _clipboard_value(value)
        )
        QApplication.clipboard().setText(text)
        self.copy_completed.emit(
            "Copied inspector value as JSON" if as_json else "Copied inspector value"
        )

    def copy_current_path(self) -> None:
        index = self.currentIndex()
        if not index.isValid():
            return
        path = self._json_model.value_path(index)
        suffix = _format_value_path(path)
        QApplication.clipboard().setText(f"{self._section}/{suffix}")
        self.copy_completed.emit("Copied inspector field path")

    def _show_context_menu(self, position) -> None:  # noqa: ANN001
        index = self.indexAt(position)
        if not index.isValid():
            return
        self.setCurrentIndex(index.siblingAtColumn(1))
        menu = QMenu(self)
        copy_value = menu.addAction("Copy value")
        copy_json = menu.addAction("Copy value as JSON")
        copy_path = menu.addAction("Copy field path")
        selected = menu.exec(self.viewport().mapToGlobal(position))
        if selected is copy_value:
            self.copy_current_value()
        elif selected is copy_json:
            self.copy_current_value(as_json=True)
        elif selected is copy_path:
            self.copy_current_path()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.matches(QKeySequence.StandardKey.Copy):
            self.copy_current_value()
            event.accept()
            return
        super().keyPressEvent(event)


class StructureView(QWidget):
    def __init__(self, parent: QWidget | None = None):
        """Initialize the structured metadata and fallback views."""
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.note = QLabel()
        self.note.setWordWrap(True)
        self.note.setObjectName("mutedLabel")
        layout.addWidget(self.note)

        self.table = QTableView()
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.table_model = QStandardItemModel(self.table)
        self.table_model.setHorizontalHeaderLabels(
            ["Index", "Component", "Inputs", "Depends on"]
        )
        self.table.setModel(self.table_model)
        layout.addWidget(self.table, 2)

        self.raw = JsonView("structure")
        layout.addWidget(self.raw, 1)

    def set_structure(self, structure: Mapping[str, Any] | None) -> None:
        self.table_model.removeRows(0, self.table_model.rowCount())
        self.raw.set_value(structure or {})
        if not structure:
            self.note.setText(
                "No structured component metadata is recorded for this context."
            )
            self.table.hide()
            return

        children = structure.get("children")
        inputs = structure.get("inputs")
        dependencies = structure.get("dependencies")
        if not isinstance(children, list):
            self.note.setText(
                "Unfamiliar chemfit.structure metadata; the generic view is retained below."
            )
            self.table.hide()
            return

        self.note.setText(
            f"{len(children)} declared components · outputs: "
            f"{_format_sources(structure.get('outputs'))}"
        )
        for fallback_index, child_value in enumerate(children):
            child = child_value if isinstance(child_value, dict) else {}
            index = child.get("index", fallback_index)
            row_inputs = (
                inputs[index]
                if isinstance(inputs, list)
                and isinstance(index, int)
                and index < len(inputs)
                else None
            )
            row_dependencies = (
                dependencies[index]
                if isinstance(dependencies, list)
                and isinstance(index, int)
                and index < len(dependencies)
                else None
            )
            values = (
                str(index),
                str(child.get("name", "Unknown component")),
                _format_sources(row_inputs),
                _format_dependencies(row_dependencies),
            )
            self.table_model.appendRow([QStandardItem(value) for value in values])
        self.table.show()


class Inspector(QWidget):
    plot_requested = Signal(object)
    plot_add_requested = Signal(object)
    order_requested = Signal(object)
    extraction_requested = Signal(object)
    component_extraction_requested = Signal(object)
    status_message = Signal(str)

    def __init__(self, parent: QWidget | None = None):  # noqa: PLR0915
        """Initialize the tabbed context inspector."""
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 18)
        layout.setSpacing(12)

        self.eyebrow = QLabel("SELECT A CONTEXT")
        self.eyebrow.setObjectName("eyebrow")
        layout.addWidget(self.eyebrow)

        self.title = QLabel("Context details")
        self.title.setObjectName("inspectorTitle")
        layout.addWidget(self.title)

        self.subtitle = QLabel("Choose an evaluation and context to inspect its data.")
        self.subtitle.setObjectName("mutedLabel")
        self.subtitle.setWordWrap(True)
        layout.addWidget(self.subtitle)

        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(divider)

        actions = QFrame()
        actions.setObjectName("inspectorActions")
        action_layout = QGridLayout(actions)
        action_layout.setContentsMargins(12, 10, 12, 10)
        action_layout.setHorizontalSpacing(8)
        action_layout.setVerticalSpacing(7)

        number_label = QLabel("NUMBER")
        number_label.setObjectName("actionGroupLabel")
        action_layout.addWidget(number_label, 0, 0)
        self.plot_selection = QLabel("No number selected")
        self.plot_selection.setObjectName("mutedLabel")
        action_layout.addWidget(self.plot_selection, 0, 1, 1, 4)
        self.plot_button = QPushButton("Plot")
        self.plot_button.setProperty("primaryAction", "true")
        self.plot_button.setEnabled(False)
        self.plot_button.clicked.connect(self._request_plot)
        action_layout.addWidget(self.plot_button, 1, 1)
        self.plot_add_button = QPushButton("+ Series")
        self.plot_add_button.setEnabled(False)
        self.plot_add_button.clicked.connect(self._request_plot_add)
        action_layout.addWidget(self.plot_add_button, 1, 2)
        self.order_button = QPushButton("Sort ↑")
        self.order_button.setEnabled(False)
        self.order_button.clicked.connect(self._request_order)
        action_layout.addWidget(self.order_button, 1, 3)

        extraction_label = QLabel("EXTRACTION")
        extraction_label.setObjectName("actionGroupLabel")
        action_layout.addWidget(extraction_label, 2, 0)
        self.field_selection = QLabel("No field selected")
        self.field_selection.setObjectName("mutedLabel")
        action_layout.addWidget(self.field_selection, 2, 1, 1, 4)
        self.extract_button = QPushButton("+ Field")
        self.extract_button.setProperty("extractAction", "true")
        self.extract_button.setEnabled(False)
        self.extract_button.clicked.connect(self._request_extraction)
        action_layout.addWidget(self.extract_button, 3, 1)
        self.extract_component_button = QPushButton("+ Component")
        self.extract_component_button.setProperty("extractAction", "true")
        self.extract_component_button.setEnabled(False)
        self.extract_component_button.setToolTip(
            "Add the entire selected component to Data Extraction."
        )
        self.extract_component_button.clicked.connect(
            self._request_component_extraction
        )
        action_layout.addWidget(self.extract_component_button, 3, 2)
        action_layout.setColumnStretch(4, 1)
        layout.addWidget(actions)

        self.tabs = QTabWidget()
        self.summary = JsonView("fields")
        self.metadata = JsonView("metadata")
        self.structure = StructureView()
        self.raw = JsonView("raw")
        self.tabs.addTab(self.summary, "Overview")
        self.tabs.addTab(self.metadata, "Metadata")
        self.tabs.addTab(self.structure, "Structure")
        self.tabs.addTab(self.raw, "Raw fields")
        layout.addWidget(self.tabs, 1)
        self._details: NodeDetails | None = None
        self._component_label = ""
        self._number_selector: NumberSelector | None = None
        self._field_selector: FieldSelector | None = None
        self.summary.numeric_selected.connect(
            lambda selection: self._numeric_selected("fields", selection)
        )
        self.metadata.numeric_selected.connect(
            lambda selection: self._numeric_selected("metadata", selection)
        )
        self.structure.raw.numeric_selected.connect(
            lambda selection: self._numeric_selected("structure", selection)
        )
        self.raw.numeric_selected.connect(
            lambda selection: self._numeric_selected("raw", selection)
        )
        for view in (self.summary, self.metadata, self.structure.raw, self.raw):
            view.copy_completed.connect(self.status_message.emit)
        self.summary.value_selected.connect(
            lambda selection: self._value_selected("fields", selection)
        )
        self.metadata.value_selected.connect(
            lambda selection: self._value_selected("metadata", selection)
        )
        self.structure.raw.value_selected.connect(
            lambda selection: self._value_selected("structure", selection)
        )
        self.raw.value_selected.connect(
            lambda selection: self._value_selected("raw", selection)
        )

    def clear(self) -> None:
        self._details = None
        self._component_label = ""
        self._set_number_selector(None)
        self._set_field_selector(None)
        self.extract_component_button.setEnabled(False)
        self.extract_component_button.setToolTip(
            "Add the entire selected component to Data Extraction."
        )
        self.eyebrow.setText("SELECT A CONTEXT")
        self.title.setText("Context details")
        self.subtitle.setText("Choose an evaluation and context to inspect its data.")
        self.summary.set_value({})
        self.metadata.set_value({})
        self.structure.set_structure(None)
        self.raw.set_value({})

    def set_details(
        self, details: NodeDetails, component_label: str | None = None
    ) -> None:
        self._details = details
        self._component_label = component_label or details.info.label
        self._set_number_selector(None)
        self._set_field_selector(None)
        self.extract_component_button.setEnabled(True)
        self.extract_component_button.setToolTip(
            f"Add the entire {self._component_label} component to Data Extraction."
        )
        info = details.info
        self.eyebrow.setText(info.kind.upper())
        self.title.setText(self._component_label)
        recorded = "recorded" if info.recorded else "declaration only"
        self.subtitle.setText(f"Path {info.path_text} · {info.status} · {recorded}")
        self.summary.set_value(details.fields)
        self.metadata.set_value(details.metadata)
        self.structure.set_structure(details.structure)
        self.raw.set_value(details.raw)

    def focus_value(self, section: str, path: tuple[str | int, ...]) -> bool:
        """Open the appropriate inspector tab and reveal a searched value."""
        views = {
            "fields": (self.summary, self.summary),
            "metadata": (self.metadata, self.metadata),
            "structure": (self.structure, self.structure.raw),
            "raw": (self.raw, self.raw),
        }
        selected = views.get(section)
        if selected is None:
            return False
        tab, view = selected
        self.tabs.setCurrentWidget(tab)
        return view.focus_path(path)

    def _numeric_selected(self, section: str, selection: object) -> None:
        if self._details is None or not isinstance(selection, tuple):
            self._set_number_selector(None)
            return
        value_path, _value = selection
        if not isinstance(value_path, tuple):
            self._set_number_selector(None)
            return
        value_label = _format_value_path(value_path)
        component_label = (
            self._component_label if self._details.info.path else "Evaluation root"
        )
        label = f"{component_label} · {value_label}"
        self._set_number_selector(
            NumberSelector(
                node_path=self._details.info.path,
                section=section,
                value_path=value_path,
                label=label,
                value_label=value_label,
            )
        )

    def _set_number_selector(self, selector: NumberSelector | None) -> None:
        self._number_selector = selector
        self.plot_button.setEnabled(selector is not None)
        self.plot_add_button.setEnabled(selector is not None)
        self.order_button.setEnabled(selector is not None)
        selected = f"\nSelected: {selector.label}" if selector is not None else ""
        self.plot_button.setToolTip(
            f"Create a plot of this number across the collection.{selected}"
        )
        self.plot_add_button.setToolTip(
            f"Add this number as another series in the current plot.{selected}"
        )
        self.order_button.setToolTip(
            f"Sort contexts ascending by this number; missing values go last.{selected}"
        )
        self.plot_selection.setText(
            f"Selected: {selector.value_label}"
            if selector is not None
            else "No number selected"
        )
        self.plot_selection.setToolTip(selector.label if selector is not None else "")

    def _value_selected(self, section: str, selection: object) -> None:
        if self._details is None or not isinstance(selection, tuple):
            self._set_field_selector(None)
            return
        value_path, _value = selection
        if not isinstance(value_path, tuple):
            self._set_field_selector(None)
            return
        column = str(value_path[-1]) if value_path else section
        self._set_field_selector(
            FieldSelector(
                node_path=self._details.info.path,
                section=section,
                value_path=value_path,
                column=column,
            )
        )

    def _set_field_selector(self, selector: FieldSelector | None) -> None:
        self._field_selector = selector
        self.extract_button.setEnabled(selector is not None)
        self.extract_button.setToolTip(
            f"Add this field to Data Extraction.\nSelected: {selector.path_label}"
            if selector is not None
            else "Add a selected field to Data Extraction."
        )
        self.field_selection.setText(
            f"Selected: {selector.column}"
            if selector is not None
            else "No field selected"
        )
        self.field_selection.setToolTip(
            selector.path_label if selector is not None else ""
        )

    def _request_plot(self) -> None:
        if self._number_selector is not None:
            self.plot_requested.emit(self._number_selector)

    def _request_plot_add(self) -> None:
        if self._number_selector is not None:
            self.plot_add_requested.emit(self._number_selector)

    def _request_order(self) -> None:
        if self._number_selector is not None:
            self.order_requested.emit(self._number_selector)

    def _request_extraction(self) -> None:
        if self._field_selector is not None:
            self.extraction_requested.emit(self._field_selector)

    def _request_component_extraction(self) -> None:
        if self._details is not None:
            self.component_extraction_requested.emit(
                FieldSelector(
                    node_path=self._details.info.path,
                    section="component",
                    column=self._details.info.label or "component",
                )
            )


def _format_sources(value: Any) -> str:
    if not isinstance(value, list):
        return "—"
    formatted = []
    for source in value:
        if not isinstance(source, dict):
            formatted.append(str(source))
            continue
        kind = source.get("kind", "unknown")
        index = source.get("index")
        formatted.append(f"{kind} {index}" if index is not None else str(kind))
    return ", ".join(formatted) if formatted else "—"


def _format_dependencies(value: Any) -> str:
    if not isinstance(value, list):
        return "—"
    return ", ".join(map(str, value)) if value else "none"


def _format_value_path(path: tuple[str | int, ...]) -> str:
    result = ""
    for segment in path:
        if isinstance(segment, int):
            result += f"[{segment}]"
        else:
            result += f".{segment}" if result else segment
    return result or "value"


def _clipboard_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)
