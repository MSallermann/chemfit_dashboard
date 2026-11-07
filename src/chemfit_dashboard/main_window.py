"""
Top-level Qt window and cross-feature application coordination.

``MainWindow`` assembles feature panels and translates user actions into
document loads, navigation, search, plotting, analysis, and extraction work.
Feature-specific data processing lives in the corresponding modules; this
module owns collection/session state and synchronization between views.
"""

from __future__ import annotations

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
    QVBoxLayout,
    QWidget,
)

from chemfit_dashboard.context_hierarchy_panel import ContextTreePanel
from chemfit_dashboard.coordinator import DashboardCoordinator
from chemfit_dashboard.evaluation_panel import EvaluationPanel
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
        self.coordinator = DashboardCoordinator(source=source)

        # some basic settings
        self.setAcceptDrops(True)
        self.setWindowTitle("ChemFit Context Inspector")
        self.resize(1460, 860)
        self.setMinimumSize(980, 620)

        self.build_ui()
        self.build_menu()
        self.build_navigation_shortcuts()

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
        # Main workspace
        #########################

        self.main_workspace = QSplitter(Qt.Orientation.Horizontal)
        self.main_workspace.setObjectName("mainWorkspace")
        self.main_workspace.setChildrenCollapsible(False)

        self.main_workspace.addWidget(self.evaluation_panel)
        self.main_workspace.addWidget(self.context_tree_panel)
        self.main_workspace.addWidget(self.inspector)

        self.main_workspace.setStretchFactor(0, 0)
        self.main_workspace.setStretchFactor(1, 1)
        self.main_workspace.setSizes([275, 1185])

        outer.addWidget(self.main_workspace, 1)

        # set central widget
        self.setCentralWidget(container)

    def build_menu(self): ...

    def build_navigation_shortcuts(self): ...

    def connect_signals(self): ...
