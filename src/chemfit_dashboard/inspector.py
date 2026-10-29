from __future__ import annotations

from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QModelIndex, Signal
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableView,
    QTabWidget,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from chemfit_dashboard.collection_plot import NumberSelector
from chemfit_dashboard.models import JsonValueModel
from chemfit_dashboard.source import NodeDetails

if TYPE_CHECKING:
    from collections.abc import Mapping


class JsonView(QTreeView):
    numeric_selected = Signal(object)

    def __init__(self, parent: QWidget | None = None):
        """Initialize a generic expandable value view."""
        super().__init__(parent)
        self._json_model = JsonValueModel(parent=self)
        self.setModel(self._json_model)
        self.setAlternatingRowColors(True)
        self.setUniformRowHeights(True)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.selectionModel().currentChanged.connect(self._current_changed)

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
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            self.numeric_selected.emit((self._json_model.value_path(current), value))
        else:
            self.numeric_selected.emit(None)


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

        self.raw = JsonView()
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

    def __init__(self, parent: QWidget | None = None):
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

        self.plot_selection = QLabel("Numeric selection: none")
        self.plot_selection.setObjectName("mutedLabel")
        self.plot_selection.setWordWrap(True)
        layout.addWidget(self.plot_selection)
        self.plot_button = QPushButton("Plot selected number across collection")
        self.plot_button.setEnabled(False)
        self.plot_button.clicked.connect(self._request_plot)
        layout.addWidget(self.plot_button)
        self.plot_add_button = QPushButton("Add selected number to current plot")
        self.plot_add_button.setEnabled(False)
        self.plot_add_button.clicked.connect(self._request_plot_add)
        layout.addWidget(self.plot_add_button)
        self.order_button = QPushButton("Order collection by selected number ↑")
        self.order_button.setEnabled(False)
        self.order_button.clicked.connect(self._request_order)
        layout.addWidget(self.order_button)

        self.tabs = QTabWidget()
        self.summary = JsonView()
        self.metadata = JsonView()
        self.structure = StructureView()
        self.raw = JsonView()
        self.tabs.addTab(self.summary, "Overview")
        self.tabs.addTab(self.metadata, "Metadata")
        self.tabs.addTab(self.structure, "Structure")
        self.tabs.addTab(self.raw, "Raw fields")
        layout.addWidget(self.tabs, 1)
        self._details: NodeDetails | None = None
        self._number_selector: NumberSelector | None = None
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

    def clear(self) -> None:
        self._details = None
        self._set_number_selector(None)
        self.eyebrow.setText("SELECT A CONTEXT")
        self.title.setText("Context details")
        self.subtitle.setText("Choose an evaluation and context to inspect its data.")
        self.summary.set_value({})
        self.metadata.set_value({})
        self.structure.set_structure(None)
        self.raw.set_value({})

    def set_details(self, details: NodeDetails) -> None:
        self._details = details
        self._set_number_selector(None)
        info = details.info
        self.eyebrow.setText(info.kind.upper())
        self.title.setText(info.label)
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
            self._details.info.label if self._details.info.path else "Evaluation root"
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
        self.plot_button.setToolTip(selector.label if selector is not None else "")
        self.plot_add_button.setToolTip(
            f"Add {selector.label} as another series" if selector is not None else ""
        )
        self.order_button.setToolTip(
            f"Order contexts ascending by {selector.label}; missing values go last"
            if selector is not None
            else ""
        )
        self.plot_selection.setText(
            f"Numeric selection: {selector.label}"
            if selector is not None
            else "Numeric selection: none"
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
