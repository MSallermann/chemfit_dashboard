"""
Top-level Qt window and cross-feature application coordination.

``MainWindow`` assembles feature panels and translates user actions into
document loads, navigation, search, plotting, analysis, and extraction work.
Feature-specific data processing lives in the corresponding modules; this
module owns collection/session state and synchronization between views.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from chemfit_dashboard.activity import ActivityStatus
from chemfit_dashboard.collection_plot import CollectionPlotPanel
from chemfit_dashboard.context_hierarchy_panel import ContextTreePanel
from chemfit_dashboard.context_search import ContextSearchPanel
from chemfit_dashboard.coordinator import DashboardCoordinator
from chemfit_dashboard.evaluation_panel import EvaluationPanel
from chemfit_dashboard.extraction import (
    FieldSelector,
    OutputDescriptor,
)
from chemfit_dashboard.extraction_ui import DataExtractionPanel
from chemfit_dashboard.graph_view import DataFlowPanel
from chemfit_dashboard.inspector import Inspector
from chemfit_dashboard.search_core import SearchHit
from chemfit_dashboard.source import ContextSource, JsonFileSource
from chemfit_dashboard.ui_components import DetachablePanelController


class MainWindow(QMainWindow):
    """todo."""

    def __init__(
        self,
        source: ContextSource | None = None,
    ):
        """Initialize the dashboard around a replaceable context source."""
        super().__init__()

        # set up the coordinator
        if source is None:
            source = JsonFileSource()

        self.coordinator = DashboardCoordinator(source=source)
        self._tab_before_search: QWidget | None = None

        # some basic settings
        self.setAcceptDrops(True)
        self.setWindowTitle("ChemFit Context Inspector")
        self.resize(1460, 860)
        self.setMinimumSize(980, 620)

        self.build_ui()
        self.activity = ActivityStatus(self)
        self.build_menu()
        self.build_navigation_shortcuts()
        self.connect_signals()

    def build_ui(self):
        """Build the main UI."""

        # OVerall container
        container = QWidget()
        outer = QVBoxLayout(container)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        #########################
        # Header
        #########################

        header = QFrame()
        header.setObjectName("appHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(20, 14, 20, 14)
        title_box = QVBoxLayout()
        title_box.setSpacing(1)
        title = QLabel("CHEMFIT / CONTEXT INSPECTOR")
        title.setObjectName("brand")
        title_box.addWidget(title)
        header_layout.addLayout(title_box)
        # self.back_button = QToolButton()
        # self.back_button.setObjectName("historyBackButton")
        # self.back_button.setAccessibleName("Back")
        # self.back_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        # self.back_button.setEnabled(False)
        # self.forward_button = QToolButton()
        # self.forward_button.setObjectName("historyForwardButton")
        # self.forward_button.setAccessibleName("Forward")
        # self.forward_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        # self.forward_button.setEnabled(False)
        header_layout.addSpacing(10)
        # header_layout.addWidget(self.back_button)
        # header_layout.addWidget(self.forward_button)
        header_layout.addStretch()

        outer.addWidget(header)

        #########################
        # Evaluation panel
        #########################

        self.evaluation_panel = EvaluationPanel(
            coordinator=self.coordinator, parent=self
        )

        #########################
        # Context hierarchy
        #########################

        self.context_tree_panel = ContextTreePanel(
            coordinator=self.coordinator, parent=self
        )

        #########################
        # Data flow
        #########################

        self.data_flow_panel = DataFlowPanel(coordinator=self.coordinator, parent=self)

        #########################
        # Context search
        #########################

        self.search_panel = ContextSearchPanel(
            coordinator=self.coordinator, parent=self
        )

        #########################
        # Collection plot
        #########################

        self.collection_plot = CollectionPlotPanel(
            coordinator=self.coordinator, parent=self
        )

        #########################
        # Context inspector
        #########################

        self.inspector = Inspector(
            coordinator=self.coordinator,
            parent=self,
        )

        #########################
        # Extraction ui
        #########################

        self.extraction_panel = DataExtractionPanel(
            parent=self, coordinator=self.coordinator
        )

        #########################
        # Center workspace
        #########################

        self.center_tabs = QTabWidget()
        self.center_tabs.addTab(self.context_tree_panel, "Context Hierarchy")
        data_flow_tab_index = self.center_tabs.addTab(self.data_flow_panel, "Data Flow")
        self.center_tabs.addTab(self.search_panel, "Context Search")
        self.center_tabs.addTab(self.extraction_panel, "Data Extraction")
        self.center_tabs.addTab(self.collection_plot, "Collection Plot")
        self._data_flow_detachment = DetachablePanelController(
            self,
            self.center_tabs,
            self.data_flow_panel,
            tab_label="Data Flow",
            window_title="ChemFit Data Flow",
            object_prefix="dataFlow",
            tab_index=data_flow_tab_index,
            expanded_changed=self.data_flow_panel.set_expanded,
            detached=self.data_flow_panel.ensure_rendered,
        )

        #########################
        # Main workspace
        #########################

        self.main_workspace = QSplitter(Qt.Orientation.Horizontal)
        self.main_workspace.setObjectName("mainWorkspace")
        self.main_workspace.setChildrenCollapsible(False)

        self.main_workspace.addWidget(self.evaluation_panel)
        self.main_workspace.addWidget(self.center_tabs)
        self.main_workspace.addWidget(self.inspector)

        self.main_workspace.setStretchFactor(0, 0)
        self.main_workspace.setStretchFactor(1, 1)
        self.main_workspace.setStretchFactor(2, 1)

        self.main_workspace.setSizes([275, 700, 485])

        outer.addWidget(self.main_workspace, 1)

        # set central widget
        self.setCentralWidget(container)

    def build_menu(self): ...

    def build_navigation_shortcuts(self) -> None:
        """Install application-level search navigation shortcuts."""
        self.find_shortcut = QShortcut(QKeySequence.StandardKey.Find, self)
        self.find_shortcut.activated.connect(self.focus_context_search)

        self.clear_search_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        self.clear_search_shortcut.setEnabled(False)
        self.clear_search_shortcut.activated.connect(self.clear_context_search)

    def connect_signals(self) -> None:
        """Connect requests between the application's feature panels."""

        self.inspector.extraction_requested.connect(self.add_extraction_field)
        self.inspector.component_extraction_requested.connect(self.add_extraction_field)

        self.extraction_panel.record_selected.connect(self.open_extraction_record)
        self.extraction_panel.status_message.connect(self.statusBar().showMessage)

        self.inspector.plot_requested.connect(self.plot_field)
        self.inspector.plot_add_requested.connect(self.add_plot_series)

        self.center_tabs.currentChanged.connect(self._center_tab_changed)
        self.data_flow_panel.expand_requested.connect(self._data_flow_detachment.toggle)
        self.search_panel.copy_completed.connect(self.statusBar().showMessage)
        self.search_panel.hit_previewed.connect(self._preview_search_hit)
        self.search_panel.hit_selected.connect(self._show_search_hit)
        self.coordinator.task_runner.lifecycle_changed.connect(
            self.activity.task_changed
        )

    def _center_tab_changed(self, index: int) -> None:
        """Update behavior associated with the active workspace tab."""
        current = self.center_tabs.widget(index)
        self.clear_search_shortcut.setEnabled(current is self.search_panel)
        if current is self.data_flow_panel:
            self.data_flow_panel.ensure_rendered()

    def focus_context_search(self) -> None:
        """Open Context Search and focus its query editor."""
        current = self.center_tabs.currentWidget()
        if current is not self.search_panel:
            self._tab_before_search = current
            self.center_tabs.setCurrentWidget(self.search_panel)
        self.search_panel.focus_query()

    def clear_context_search(self) -> None:
        """Clear Search and return to the workspace active before Find."""
        self.search_panel.clear_query()
        previous = self._tab_before_search
        self._tab_before_search = None
        if previous is not None and self.center_tabs.indexOf(previous) >= 0:
            self.center_tabs.setCurrentWidget(previous)

    def _show_search_hit(self, hit: SearchHit) -> None:
        """Reveal a coordinator-selected hit in the hierarchy and Inspector."""
        self._tab_before_search = None
        self.center_tabs.setCurrentWidget(self.context_tree_panel)
        self._preview_search_hit(hit)

    def _preview_search_hit(self, hit: SearchHit) -> None:
        """Focus a highlighted result in the Inspector without leaving Search."""
        if hit.section is not None:
            self.inspector.focus_value(hit.section, hit.value_path)

    def closeEvent(self, event) -> None:  # noqa: N802, ANN001
        """Release persistent background workers before window destruction."""
        self.search_panel.shutdown()
        self.coordinator.task_runner.shutdown()
        super().closeEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802, ANN001
        """Keep the expanded activity panel anchored above the status bar."""
        super().resizeEvent(event)
        if hasattr(self, "activity") and self.activity.panel.isVisible():
            self.activity.position_panel()

    def plot_field(self, selector: FieldSelector) -> None:
        """Plot the selected field and switch to the plot tab."""
        self.collection_plot.build_series((selector,))
        self.center_tabs.setCurrentWidget(self.collection_plot)

    def add_plot_series(self, selector: FieldSelector) -> None:
        """Plot the selected field and switch to the plot tab."""
        self.collection_plot.add_series(selector)
        self.center_tabs.setCurrentWidget(self.collection_plot)

    def open_extraction_record(self, record_id: str) -> None:
        """Open a previewed evaluation in the Inspector."""
        record = next(
            (ref for ref in self.coordinator.collection if ref.id == record_id),
            None,
        )
        if record is not None:
            self.coordinator.select_record(record)
            self.center_tabs.setCurrentWidget(self.inspector)

    def add_extraction_field(
        self, field: tuple[FieldSelector, OutputDescriptor]
    ) -> None:
        """Add an Inspector selection to the extraction configuration."""
        selector, output = field
        self.extraction_panel.add_selector(selector, output)
        self.center_tabs.setCurrentWidget(self.extraction_panel)
