"""
Inspector widgets for one selected evaluation context.

``JsonView`` displays arbitrary JSON through a lazy Qt item model.
``StructureView`` adds a table for ChemFit's declared component structure.
``Inspector`` combines these views and emits requests to the main window;
plotting, sorting and extraction are handled elsewhere.
"""

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

from chemfit_dashboard.context_document import NodeDetails
from chemfit_dashboard.coordinator import DashboardCoordinator, NodePath
from chemfit_dashboard.extraction import (
    FieldSelector,
    JsonPath,
    OutputDescriptor,
)
from chemfit_dashboard.icons import make_icon_only
from chemfit_dashboard.models import JsonValueModel
from chemfit_dashboard.ui_components import muted_label
from chemfit_dashboard.util import (
    format_combined_path,
    format_json_path,
    format_json_value,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

MAX_STRUCTURE_TABLE_ROWS = 500


class JsonView(QTreeView):
    """
    Display arbitrary JSON through a lazy Qt item model.

    Emits the path of the selected value relative to the displayed JSON.
    Supports copying values and paths to the clipboard.
    """

    path_selected = Signal(object)  # JsonPath | None
    copy_completed = Signal(str)

    def __init__(self, prefix: JsonPath = (), parent: QWidget | None = None):
        """Initialize a generic expandable value view."""
        super().__init__(parent)
        self._prefix: JsonPath = prefix
        self._node_path: NodePath = ()
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
        """Replace the displayed value and expand its top-level fields."""
        self._json_model.set_value(value)
        self.expandToDepth(0)

    def set_node_path(self, path: NodePath) -> None:
        """Set the structural path containing the displayed JSON value."""
        self._node_path = path

    def focus_path(self, path: JsonPath) -> bool:
        """Expand, select, and reveal one nested JSON value."""
        index = self._json_model.index_for_value_path(path)
        if not index.isValid():
            return False
        value_index = index.siblingAtColumn(1)
        self.setCurrentIndex(value_index)
        self.scrollTo(value_index, QAbstractItemView.ScrollHint.PositionAtCenter)
        return True

    def focus_context_path(self, path: JsonPath) -> bool:
        """Focus a field using its context-relative path."""
        if path[: len(self._prefix)] != self._prefix:
            return False

        return self.focus_path(path[len(self._prefix) :])

    def _current_changed(self, current: QModelIndex, _previous: QModelIndex) -> None:
        """Report the selected path relative to the context root."""
        if not current.isValid():
            self.path_selected.emit(None)
            return

        path = self._json_model.value_path(current)
        self.path_selected.emit((*self._prefix, *path))

    def copy_current_value(self, *, as_json: bool = False) -> None:
        """Copy the selected value as text or pretty-printed JSON."""
        index = self.currentIndex()
        if not index.isValid():
            return
        value = self._json_model.value(index)
        text = (
            json.dumps(value, ensure_ascii=False, indent=2)
            if as_json
            else format_json_value(value)
        )
        QApplication.clipboard().setText(text)
        self.copy_completed.emit(
            "Copied inspector value as JSON" if as_json else "Copied inspector value"
        )

    def copy_current_path(self) -> None:
        """Copy the complete component and field path."""
        index = self.currentIndex()
        if not index.isValid():
            return
        path = self._json_model.value_path(index)
        text = format_combined_path(
            self._node_path,
            (*self._prefix, *path),
        )
        QApplication.clipboard().setText(text)
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
    """
    Show declared components in a table alongside the full raw structure.

    The table is deliberately capped at ``MAX_STRUCTURE_TABLE_ROWS``. The
    ``JsonView`` below it always exposes the complete declaration, including
    unfamiliar metadata fields.
    """

    def __init__(self, parent: QWidget | None = None):
        """Initialize the structured metadata and fallback views."""
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.note = muted_label(word_wrap=True)
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

        self.raw = JsonView(("meta", "chemfit.structure"))
        layout.addWidget(self.raw, 1)

    def set_structure(self, structure: Mapping[str, Any] | None) -> None:
        """Update the structure table and its generic JSON fallback."""
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

        limited = len(children) > MAX_STRUCTURE_TABLE_ROWS
        suffix = f" · showing first {MAX_STRUCTURE_TABLE_ROWS:,}" if limited else ""
        self.note.setText(
            f"{len(children)} declared components{suffix} · outputs: "
            f"{_format_sources(structure.get('outputs'))}"
        )
        for fallback_index, child_value in enumerate(
            children[:MAX_STRUCTURE_TABLE_ROWS]
        ):
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
    """
    Present all details and actions for the currently selected context node.

    The inspector owns only the selected ``NodeDetails`` and per-tab view state.
    Plot, order, and extraction actions are emitted as semantic selectors so
    MainWindow can apply them to the active collection.
    """

    plot_requested = Signal(object)
    plot_add_requested = Signal(object)
    order_ascending_requested = Signal(object)
    order_descending_requested = Signal(object)
    extraction_requested = Signal(object)
    component_extraction_requested = Signal(object)
    status_message = Signal(str)

    def __init__(
        self, *, coordinator: DashboardCoordinator, parent: QWidget | None = None
    ):
        """Build the inspector and connect its selection and action signals."""

        self.coordinator = coordinator

        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 18)
        layout.setSpacing(12)

        self._setup_header(layout)
        self._setup_actions(layout)
        self._setup_tabs(layout)

        # Only the active tab is populated when the selection changes. The
        # others remain untouched until the user opens them.
        self._details: NodeDetails | None = None
        self._populated_tabs: set[QWidget] = set()
        self._component_label = ""
        self._connect_view_signals()

        # Wire up signals
        self.coordinator.selected_context_changed.connect(
            self.on_selected_context_changed
        )
        self.coordinator.component_label_metadata_key_changed.connect(
            lambda _key: self.on_selected_context_changed(
                self.coordinator.selected_context
            )
        )
        self.coordinator.selected_field_changed.connect(self._on_selected_field_changed)

    def on_selected_context_changed(self, path: NodePath | None) -> None:
        document = self.coordinator.selected_document

        if document is None or path is None:
            self.clear()
            return

        self.set_details(
            document.details(path),
            document.component_label(path),
        )

    def _setup_header(self, layout: QVBoxLayout) -> None:
        """Create the component title and summary above the inspector tabs."""
        self.eyebrow = QLabel("SELECT A CONTEXT")
        self.eyebrow.setObjectName("eyebrow")
        layout.addWidget(self.eyebrow)

        self.title = QLabel("Context details")
        self.title.setObjectName("inspectorTitle")
        layout.addWidget(self.title)

        self.subtitle = muted_label(
            "Choose an evaluation and context to inspect its data.", word_wrap=True
        )
        layout.addWidget(self.subtitle)

        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(divider)

    def _setup_actions(self, layout: QVBoxLayout) -> None:  # noqa: PLR0915
        """Create the controls that act on a selected numeric value or field."""
        actions = QFrame()
        actions.setObjectName("inspectorActions")
        action_layout = QGridLayout(actions)
        action_layout.setContentsMargins(12, 10, 12, 10)
        action_layout.setHorizontalSpacing(8)
        action_layout.setVerticalSpacing(7)

        # Numeric selections can be plotted or used to order the collection.
        number_label = QLabel("NUMBER")
        number_label.setObjectName("actionGroupLabel")
        action_layout.addWidget(number_label, 0, 0)
        self.plot_selection = muted_label("No number selected")
        action_layout.addWidget(self.plot_selection, 0, 1, 1, 5)

        self.plot_button = QPushButton("Plot")
        self.plot_button.setProperty("primaryAction", "true")
        self.plot_button.setEnabled(False)
        self.plot_button.clicked.connect(self._request_plot)
        action_layout.addWidget(self.plot_button, 1, 1)

        self.plot_add_button = QPushButton("+ Series")
        self.plot_add_button.setEnabled(False)
        self.plot_add_button.clicked.connect(self._request_plot_add)
        action_layout.addWidget(self.plot_add_button, 1, 2)

        sort_label = QLabel("Sort:")
        action_layout.addWidget(sort_label, 1, 3)
        self.order_ascending_button = QPushButton("Sort ↑")
        make_icon_only(
            self.order_ascending_button,
            "sort_up",
            "Sort the collection by this value in ascending order",
        )
        self.order_ascending_button.setEnabled(False)
        self.order_ascending_button.clicked.connect(self._request_order_ascending)
        action_layout.addWidget(self.order_ascending_button, 1, 4)

        self.order_descending_button = QPushButton("Sort ↓")
        make_icon_only(
            self.order_descending_button,
            "sort_down",
            "Sort the collection by this value in descending order",
        )
        self.order_descending_button.setEnabled(False)
        self.order_descending_button.clicked.connect(self._request_order_descending)
        action_layout.addWidget(self.order_descending_button, 1, 5)

        # Field selections can be exported individually or by component.
        extraction_label = QLabel("EXTRACTION")
        extraction_label.setObjectName("actionGroupLabel")
        action_layout.addWidget(extraction_label, 2, 0)
        self.field_selection = muted_label("No field selected")
        action_layout.addWidget(self.field_selection, 2, 1, 1, 5)

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
        action_layout.setColumnStretch(6, 1)
        layout.addWidget(actions)

    def _setup_tabs(self, layout: QVBoxLayout) -> None:
        """Create the four views of a context node's serialized data."""
        self.tabs = QTabWidget()
        self.summary = JsonView(prefix=())
        self.metadata = JsonView(prefix=("meta",))
        self.structure = StructureView()
        self.raw = JsonView(prefix=())
        self.tabs.addTab(self.summary, "Overview")
        self.tabs.addTab(self.metadata, "Metadata")
        self.tabs.addTab(self.structure, "Structure")
        self.tabs.addTab(self.raw, "Raw fields")
        layout.addWidget(self.tabs, 1)

    def _connect_view_signals(self) -> None:
        views = (
            self.summary,
            self.metadata,
            self.structure.raw,
            self.raw,
        )

        for view in views:
            view.path_selected.connect(self._on_path_selected)
            view.copy_completed.connect(self.status_message.emit)

        self.tabs.currentChanged.connect(lambda _index: self._populate_current_tab())

    def _restore_visible_field(self) -> None:
        """Highlight the selected field in the currently visible JSON view."""
        path = self.coordinator.selected_field
        if path is None:
            return

        tab = self.tabs.currentWidget()
        view = self.structure.raw if tab is self.structure else tab

        if isinstance(view, JsonView):
            view.focus_context_path(path)

    def _on_path_selected(self, path: JsonPath | None) -> None:
        """Forward a selected JSON path to the coordinator."""
        if path is not None:
            self.coordinator.select_field(path)

    def _on_selected_field_changed(self, path: JsonPath | None) -> None:
        """Update field actions, labels, and tooltips from coordinator state."""

        selector = self.coordinator.current_field_selector
        extracted = self.coordinator.selected_field_value

        has_field = selector is not None
        is_numeric = extracted is not None and extracted.is_numeric

        label = format_json_path(path) if path is not None else ""
        selected = f"\nSelected: {label}" if has_field else ""

        # Extraction controls
        self.extract_button.setEnabled(has_field)
        self.field_selection.setText(
            f"Selected: {label}" if has_field else "No field selected"
        )
        self.field_selection.setToolTip(label if has_field else "")
        self.extract_button.setToolTip(f"Add this field to Data Extraction.{selected}")

        # Plotting and sorting controls
        self.plot_button.setEnabled(is_numeric)
        self.plot_add_button.setEnabled(is_numeric)
        self.order_ascending_button.setEnabled(is_numeric)
        self.order_descending_button.setEnabled(is_numeric)

        self.plot_selection.setText(
            f"Selected: {label}" if is_numeric else "No number selected"
        )
        self.plot_selection.setToolTip(label if is_numeric else "")

        numeric_selected = selected if is_numeric else ""

        self.plot_button.setToolTip(
            f"Create a plot of this number across the collection.{numeric_selected}"
        )
        self.plot_add_button.setToolTip(
            f"Add this number as another series in the current plot.{numeric_selected}"
        )
        self.order_ascending_button.setToolTip(
            f"Sort the collection by this value in ascending order; "
            f"missing values go last.{numeric_selected}"
        )
        self.order_descending_button.setToolTip(
            f"Sort the collection by this value in descending order; "
            f"missing values go last.{numeric_selected}"
        )

        self._restore_visible_field()

    def clear(self) -> None:
        """Reset the inspector when no context is selected."""
        self._details = None
        self._component_label = ""
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
        for view in (self.summary, self.metadata, self.structure.raw, self.raw):
            view.set_node_path(())
        self._populated_tabs = {
            self.summary,
            self.metadata,
            self.structure,
            self.raw,
        }

    def set_details(
        self, details: NodeDetails, component_label: str | None = None
    ) -> None:
        """Show a new node; defer inactive tabs until they are opened."""
        self._details = details
        self._component_label = component_label or details.info.label
        self.extract_component_button.setEnabled(True)
        self.extract_component_button.setToolTip(
            f"Add the entire {self._component_label} component to Data Extraction."
        )
        info = details.info
        for view in (self.summary, self.metadata, self.structure.raw, self.raw):
            view.set_node_path(info.path)
        self.eyebrow.setText(info.kind.upper())
        self.title.setText(self._component_label)
        recorded = "recorded" if info.recorded else "declaration only"
        self.subtitle.setText(f"Path {info.path_text} · {info.status} · {recorded}")
        self._populated_tabs.clear()
        self._populate_current_tab()

    def _populate_current_tab(self) -> None:
        """Populate the selected tab once for the current context node."""
        details = self._details
        current = self.tabs.currentWidget()
        if details is None or current is None or current in self._populated_tabs:
            return
        if current is self.summary:
            self.summary.set_value(details.fields)
        elif current is self.metadata:
            self.metadata.set_value(details.metadata)
        elif current is self.structure:
            self.structure.set_structure(details.structure)
        elif current is self.raw:
            self.raw.set_value(details.raw)
        self._populated_tabs.add(current)

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

    # The buttons only emit requests; MainWindow performs the operations.
    def _request_plot(self) -> None:
        selector = self.coordinator.current_field_selector
        if selector is not None:
            self.plot_requested.emit(selector)

    def _request_plot_add(self) -> None:
        selector = self.coordinator.current_field_selector
        if selector is not None:
            self.plot_add_requested.emit(selector)

    def _request_order_ascending(self) -> None:
        selector = self.coordinator.current_field_selector
        if selector is not None:
            self.coordinator.sort_records_by_field(
                field_selector=selector, ascending=True
            )

    def _request_order_descending(self) -> None:
        selector = self.coordinator.current_field_selector
        if selector is not None:
            self.coordinator.sort_records_by_field(
                field_selector=selector, ascending=False
            )

    def _request_extraction(self) -> None:
        selector = self.coordinator.current_field_selector
        if selector is None:
            return

        column = str(selector.value_path[-1]) if selector.value_path else "value"

        self.extraction_requested.emit((selector, OutputDescriptor(column=column)))

    def _request_component_extraction(self) -> None:
        context = self.coordinator.selected_context
        if context is None:
            return

        self.component_extraction_requested.emit(
            (
                FieldSelector(node_path=context),
                OutputDescriptor(column=self._component_label or "component"),
            )
        )


def _format_sources(value: Any) -> str:
    """Format structural input/output sources for the compact table."""
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
    """Format structural dependency indices for the compact table."""
    if not isinstance(value, list):
        return "—"
    return ", ".join(map(str, value)) if value else "none"
