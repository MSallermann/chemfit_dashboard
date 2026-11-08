"""
Top-level Qt window and cross-feature application coordination.

``MainWindow`` assembles feature panels and translates user actions into
document loads, navigation, search, plotting, analysis, and extraction work.
Feature-specific data processing lives in the corresponding modules; this
module owns collection/session state and synchronization between views.
"""

from __future__ import annotations

from itertools import islice

from PySide6.QtCore import (
    Qt,
    Signal,
)
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

from chemfit_dashboard.context_hierarchy_panel import ContextTreePanel
from chemfit_dashboard.coordinator import DashboardCoordinator
from chemfit_dashboard.evaluation_panel import EvaluationPanel
from chemfit_dashboard.extraction import (
    ExtractionConfig,
    FieldSelector,
    OutputDescriptor,
    export_csv,
    export_jsonl,
    extract_fields,
)
from chemfit_dashboard.extraction_ui import PREVIEW_LIMIT, DataExtractionPanel
from chemfit_dashboard.inspector import Inspector
from chemfit_dashboard.source import ContextSource, JsonFileSource


class MainWindow(QMainWindow):
    """todo."""

    operation_progress = Signal(str, int, int)

    def __init__(
        self,
        source: ContextSource | None = None,
    ):
        """Initialize the dashboard around a replaceable context source."""
        super().__init__()

        # set up the coordinator
        if source is None:
            source = JsonFileSource()

        self.source = source
        self.coordinator = DashboardCoordinator(source=self.source)

        self.coordinator = DashboardCoordinator(source=source)

        # some basic settings
        self.setAcceptDrops(True)
        self.setWindowTitle("ChemFit Context Inspector")
        self.resize(1460, 860)
        self.setMinimumSize(980, 620)

        self.build_ui()
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
        # Context inspector
        #########################
        self.inspector = Inspector(
            coordinator=self.coordinator,
            parent=self,
        )

        #########################
        # Extraction ui
        #########################
        self.extraction_panel = DataExtractionPanel(parent=self)

        #########################
        # Center workspace
        #########################

        self.center_tabs = QTabWidget()
        self.center_tabs.addTab(self.context_tree_panel, "Context Hierarchy")
        self.center_tabs.addTab(self.extraction_panel, "Data Extraction")

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

    def build_navigation_shortcuts(self): ...

    def connect_signals(self) -> None:
        """Connect requests between the application's feature panels."""

        self.inspector.extraction_requested.connect(self.add_extraction_field)
        self.inspector.component_extraction_requested.connect(self.add_extraction_field)

        self.extraction_panel.selectors_changed.connect(self.refresh_extraction_preview)
        self.coordinator.collection_changed.connect(self.refresh_extraction_preview)
        self.coordinator.collection_order_changed.connect(
            self.refresh_extraction_preview
        )

        self.extraction_panel.export_requested.connect(self.export_extraction)
        self.extraction_panel.record_selected.connect(self.open_extraction_record)
        self.extraction_panel.status_message.connect(self.statusBar().showMessage)

    def refresh_extraction_preview(self) -> None:
        """Build a bounded extraction preview in the background."""
        records = tuple(self.coordinator.collection)
        config = self.extraction_panel.config
        selectors = tuple(selector for selector, _ in config.fields)

        if not records or not selectors:
            self.coordinator.task_runner.invalidate("extraction_preview")
            self.extraction_panel.set_preview((), len(records))
            return

        self.extraction_panel.show_loading(len(records))

        def task():
            return tuple(
                islice(
                    extract_fields(self.source, records, selectors),
                    PREVIEW_LIMIT,
                )
            )

        self.coordinator.task_runner.submit_latest(
            "extraction_preview",
            task,
            lambda rows: self.extraction_panel.set_preview(rows, len(records)),
            self.extraction_panel.show_error,
        )

    def export_extraction(
        self, path: str, format_name: str, config: ExtractionConfig
    ) -> None:
        """Export the current collection using the supplied configuration."""
        records = tuple(self.coordinator.collection)

        def task() -> int:
            if format_name == "csv":
                return export_csv(path, self.source, records, config)
            return export_jsonl(path, self.source, records, config)

        self.coordinator.task_runner.submit(
            task,
            lambda count: self.statusBar().showMessage(
                f"Exported {count:,} evaluations to {path}"
            ),
            self.extraction_panel.show_error,
        )

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
