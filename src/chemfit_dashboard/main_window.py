"""
Top-level Qt window and cross-feature application coordination.

``MainWindow`` assembles feature panels and translates user actions into
document loads, navigation, search, plotting, analysis, and extraction work.
Feature-specific data processing lives in the corresponding modules; this
module owns collection/session state and synchronization between views.
"""

from __future__ import annotations

import logging
import math
import re
import time
from contextlib import suppress
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import (
    QItemSelection,
    QPoint,
    QSettings,
    QSortFilterProxyModel,
    Qt,
    QThreadPool,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QCursor,
    QDesktopServices,
    QFont,
    QIcon,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QStyle,
    QTabWidget,
    QToolButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from chemfit_dashboard.activity import ActivityStatus
from chemfit_dashboard.appearance import (
    COLOR_SCHEME_SETTING,
    COLOR_SCHEMES,
    COMPONENT_LABEL_METADATA_KEY_SETTING,
    DEFAULT_COLOR_SCHEME,
    DENSITY_SETTING,
    FONT_SCALE_SETTING,
    STYLE_PROFILE_SETTING,
    ColorScheme,
    application_settings,
    color_scheme,
    set_active_color_scheme,
    stylesheet_for,
)
from chemfit_dashboard.collection_analysis import (
    CollectionAnalysisPanel,
    CollectionSummary,
    FailureOccurrence,
    analyze_collection,
)
from chemfit_dashboard.collection_plot import (
    CollectionPlotPanel,
    CollectionSeries,
    NumberSelector,
    build_collection_series,
    build_collection_series_multi,
    selector_for_document,
)
from chemfit_dashboard.context_search import (
    MAX_RESULTS,
    ContextSearchPanel,
    SearchHit,
    SearchMode,
    TopKSearchHits,
    clear_search_caches,
)
from chemfit_dashboard.extraction import (
    ExtractionConfig,
    ExtractionStatus,
    FieldSelector,
    export_csv,
    export_jsonl,
    extract_fields,
)
from chemfit_dashboard.extraction import (
    selector_for_document as extraction_selector_for_document,
)
from chemfit_dashboard.extraction_ui import PREVIEW_LIMIT, DataExtractionPanel
from chemfit_dashboard.graph_view import DataFlowPanel
from chemfit_dashboard.inspector import Inspector
from chemfit_dashboard.models import ContextTreeModel, EvaluationListModel
from chemfit_dashboard.process_search import (
    DEFAULT_SEARCH_WORKERS,
    CollectionSearchExecutor,
    SearchOptions,
)
from chemfit_dashboard.search_core import ContextSearchCache, ContextSearchIndex
from chemfit_dashboard.semantic import (
    NodeTrail,
    component_trail_for_path,
    resolve_component_selection,
)
from chemfit_dashboard.source import (
    DEFAULT_COMPONENT_LABEL_METADATA_KEY,
    ContextDocument,
    ContextSource,
    ContextSourceError,
    JsonFileSource,
    NodePath,
    RecordRef,
)
from chemfit_dashboard.traversal import (
    CancellationToken,
    iter_collection,
)
from chemfit_dashboard.ui_components import ActionRow, muted_label
from chemfit_dashboard.workers import TaskRunner

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

logger = logging.getLogger(__name__)

RECENT_LOCATIONS_SETTING = "session/recent_locations"
SESSION_LOCATIONS_SETTING = "session/locations"
SESSION_RECORD_SETTING = "session/selected_record"
SESSION_NODE_PATH_SETTING = "session/node_path"
SESSION_GEOMETRY_SETTING = "session/geometry"
SESSION_MAIN_SPLITTER_SETTING = "session/main_splitter"
SESSION_CONTEXT_SPLITTER_SETTING = "session/context_splitter"
SESSION_WORKSPACE_TAB_SETTING = "session/workspace_tab"
SESSION_WORKSPACE_KEY_SETTING = "session/workspace_key"
SESSION_HIERARCHY_TAB_SETTING = "session/hierarchy_tab"
MAX_RECENT_LOCATIONS = 10
PROGRESS_INTERVAL_SECONDS = 0.1
DOCUMENT_ACTIVITY_DELAY_MS = 200
AUTO_ANALYSIS_RECORD_LIMIT = 5_000
BACKGROUND_THREADS_SETTING = "performance/background_threads"
SEARCH_PROCESSES_SETTING = "performance/search_processes"
MAX_CONFIGURABLE_WORKERS = 64


@dataclass(frozen=True, slots=True)
class _DeterminateOperation:
    """Cancellation and progress channels for one background operation."""

    cancellation: CancellationToken
    progress: Callable[[int, object], None]


def _report_progress(
    emit: Callable[[str, int, int], None],
    name: str,
    completed: int,
    total: int,
    last_progress_at: float,
) -> float:
    """Emit bounded-rate progress so worker updates cannot flood Qt's event queue."""
    now = time.monotonic()
    if completed == total or now - last_progress_at >= PROGRESS_INTERVAL_SECONDS:
        emit(name, completed, total)
        return now
    return last_progress_at


def _worker_count(value: object, default: int) -> int:
    """Coerce a persisted worker count into the supported range."""
    if not isinstance(value, (int, str)):
        return min(MAX_CONFIGURABLE_WORKERS, max(1, default))
    try:
        count = int(value)
    except (TypeError, ValueError):
        count = default
    return min(MAX_CONFIGURABLE_WORKERS, max(1, count))


class MainWindow(QMainWindow):
    """
    Coordinate one open collection and the dashboard's feature panels.

    The window owns the current record/document, collection ordering, selection
    history, and generation counters for asynchronous requests. Individual
    widgets own presentation state; ``ActivityStatus`` owns operation tokens
    and progress; ``ContextSource`` owns document caching.

    Every background callback compares its captured generation with the current
    feature generation before changing widgets. This is the central invariant
    that prevents a slow result for an old selection from replacing newer UI.
    """

    operation_progress = Signal(str, int, int)

    def __init__(  # noqa: PLR0915
        self,
        source: ContextSource | None = None,
        initial_paths: Sequence[str | Path] | None = None,
        settings: QSettings | None = None,
        restore_session: bool = False,
    ):
        """Initialize the dashboard around a replaceable context source."""
        super().__init__()
        self.setAcceptDrops(True)
        self.source = source or JsonFileSource()
        self._settings = settings or application_settings()
        global_pool = QThreadPool.globalInstance()
        self._background_thread_count = _worker_count(
            self._settings.value(
                BACKGROUND_THREADS_SETTING, global_pool.maxThreadCount()
            ),
            global_pool.maxThreadCount(),
        )
        self._search_process_count = _worker_count(
            self._settings.value(SEARCH_PROCESSES_SETTING, DEFAULT_SEARCH_WORKERS),
            DEFAULT_SEARCH_WORKERS,
        )
        global_pool.setMaxThreadCount(self._background_thread_count)
        self.tasks = TaskRunner(self)
        self.collection_search = CollectionSearchExecutor(
            max_workers=self._search_process_count
        )
        # Generations invalidate results without unsafe thread termination.
        # Cancellation reduces wasted work; the generation check remains
        # necessary because already-running tasks can still complete.
        self._collection_generation = 0
        self._load_generation = 0
        self._plot_generation = 0
        self._order_generation = 0
        self._search_generation = 0
        self._analysis_generation = 0
        self._extraction_generation = 0
        self.search_indexes = ContextSearchCache(capacity=32)
        self._pending_search_hit: SearchHit | None = None
        self._pending_failure: FailureOccurrence | None = None
        self._pending_plot_selection: tuple[str, NumberSelector] | None = None
        self._navigation_history: list[tuple[str, NodePath]] = []
        self._navigation_index = -1
        self._navigation_replaying = False
        self._sticky_node_path: NodePath = ()
        self._sticky_node_trail: NodeTrail = ()
        self._suppress_context_memory = False
        self._numeric_order_present: tuple[str, ...] = ()
        self._numeric_order_missing: tuple[str, ...] = ()
        self._numeric_order_label: str | None = None
        self._order_descending = False
        self._filename_reversed = False
        self._active_plot_selector: NumberSelector | None = None
        self._active_plot_selectors: list[NumberSelector] = []
        self._current_document: ContextDocument | None = None
        self._tab_before_search: QWidget | None = None
        self._session_enabled = restore_session
        self._session_locations: list[str] = []
        self._restored_record_path: str | None = None
        self._font_scale = 1.0
        self._color_scheme_key = DEFAULT_COLOR_SCHEME
        self._style_profile = "classic"
        self._density = "comfortable"
        self._base_font: QFont | None = None
        self._base_stylesheet = ""
        self._expanded_data_flow_dialog: QDialog | None = None
        self._data_flow_placeholder: QWidget | None = None
        self._data_flow_tab_index = 1
        self._expanded_plot_dialog: QDialog | None = None
        self._plot_placeholder: QWidget | None = None
        self._plot_tab_index = 2
        saved_label_key = self._settings.value(
            COMPONENT_LABEL_METADATA_KEY_SETTING,
            DEFAULT_COMPONENT_LABEL_METADATA_KEY,
        )
        self._component_label_metadata_key = str(saved_label_key).strip()
        self.source.set_component_label_metadata_key(self._component_label_metadata_key)
        self._restore_appearance_settings()

        self.setWindowTitle("ChemFit Context Inspector")
        self.resize(1460, 860)
        self.setMinimumSize(980, 620)
        self._build_ui()
        self.operation_progress.connect(self._operation_progressed)
        self._build_menu()
        self._build_navigation_shortcuts()
        self._clipboard_toast = QLabel(self)
        self._clipboard_toast.setObjectName("clipboardToast")
        self._clipboard_toast.setTextFormat(Qt.TextFormat.PlainText)
        self._clipboard_toast.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents
        )
        self._clipboard_toast.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self._clipboard_toast.hide()
        self._pending_clipboard_message = ""
        self._clipboard_toast_show_timer = QTimer(self)
        self._clipboard_toast_show_timer.setSingleShot(True)
        self._clipboard_toast_show_timer.timeout.connect(self._show_clipboard_toast)
        self._clipboard_toast_follow_timer = QTimer(self)
        self._clipboard_toast_follow_timer.setInterval(16)
        self._clipboard_toast_follow_timer.timeout.connect(
            self._position_clipboard_toast
        )
        self._clipboard_toast_timer = QTimer(self)
        self._clipboard_toast_timer.setSingleShot(True)
        self._clipboard_toast_timer.timeout.connect(self._hide_clipboard_toast)
        if self._session_enabled:
            self._restore_session_state()

        startup_paths = list(initial_paths or ())
        if startup_paths and self._session_enabled:
            # Explicit command-line locations replace the saved collection while
            # retaining the restored window layout and appearance.
            self._session_locations = []
            self._restored_record_path = None
            self._sticky_node_path = ()
            self._sticky_node_trail = ()
        elif self._session_enabled:
            startup_paths = list(self._session_locations)
        if startup_paths:
            self.add_locations(startup_paths)

    def _restore_appearance_settings(self) -> None:
        saved_font_scale = self._settings.value(FONT_SCALE_SETTING, 1.0)
        try:
            font_scale = (
                float(saved_font_scale)
                if isinstance(saved_font_scale, (int, float, str))
                else 1.0
            )
        except (TypeError, ValueError):
            font_scale = 1.0
        self._font_scale = min(1.6, max(0.8, round(font_scale, 1)))
        self._style_profile = str(
            self._settings.value(STYLE_PROFILE_SETTING, "classic") or "classic"
        )
        self._density = str(
            self._settings.value(DENSITY_SETTING, "comfortable") or "comfortable"
        )
        saved_scheme = self._settings.value(COLOR_SCHEME_SETTING, DEFAULT_COLOR_SCHEME)
        self._color_scheme_key = color_scheme(str(saved_scheme)).key
        scheme = set_active_color_scheme(self._color_scheme_key)
        application = QApplication.instance()
        if isinstance(application, QApplication):
            stored_base_font = application.property("chemfitBaseFont")
            if isinstance(stored_base_font, QFont):
                self._base_font = QFont(stored_base_font)
            else:
                self._base_font = QFont(application.font())
                application.setProperty("chemfitBaseFont", self._base_font)
        self._base_stylesheet = stylesheet_for(
            scheme, style_profile=self._style_profile, density=self._density
        )
        self._apply_application_font()
        self._apply_application_stylesheet()

    def _build_ui(self) -> None:  # noqa: PLR0915
        container = QWidget()
        outer = QVBoxLayout(container)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

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
        self.back_button = QToolButton()
        self.back_button.setObjectName("historyBackButton")
        self.back_button.setAccessibleName("Back")
        self.back_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.back_button.setEnabled(False)
        self.forward_button = QToolButton()
        self.forward_button.setObjectName("historyForwardButton")
        self.forward_button.setAccessibleName("Forward")
        self.forward_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.forward_button.setEnabled(False)
        header_layout.addSpacing(10)
        header_layout.addWidget(self.back_button)
        header_layout.addWidget(self.forward_button)
        header_layout.addStretch()
        files_button = QPushButton("Open file")
        files_button.clicked.connect(self.open_files)
        directory_button = QPushButton("Open directory")
        directory_button.clicked.connect(self.open_directory)
        header_layout.addWidget(files_button)
        header_layout.addWidget(directory_button)
        self.font_smaller_button = QPushButton("A-")
        self.font_smaller_button.setToolTip("Decrease interface font sizes")
        self.font_smaller_button.clicked.connect(lambda: self._change_font_scale(-0.1))
        self.font_larger_button = QPushButton("A+")
        self.font_larger_button.setToolTip("Increase interface font sizes")
        self.font_larger_button.clicked.connect(lambda: self._change_font_scale(0.1))
        header_layout.addWidget(self.font_smaller_button)
        header_layout.addWidget(self.font_larger_button)
        outer.addWidget(header)

        self.context_workspace = QSplitter(Qt.Orientation.Horizontal)
        self.context_workspace.setObjectName("contextWorkspace")
        self.context_workspace.setChildrenCollapsible(False)
        self.context_workspace.addWidget(self._build_tree_panel())
        self.inspector = Inspector()
        self.inspector.status_message.connect(self._clipboard_copied)
        self.inspector.plot_requested.connect(self._plot_requested)
        self.inspector.plot_add_requested.connect(self._plot_add_requested)
        self.inspector.order_requested.connect(self._order_requested)
        self.inspector.order_descending_requested.connect(
            lambda selector: self._order_requested(selector, descending=True)
        )
        self.inspector.extraction_requested.connect(self._add_extraction_selector)
        self.inspector.component_extraction_requested.connect(
            self._add_extraction_selector
        )
        self.context_workspace.addWidget(self.inspector)
        self.context_workspace.setSizes([440, 745])

        self.collection_analysis = CollectionAnalysisPanel()
        self.collection_analysis.setObjectName("collectionOverviewWorkspace")
        self.collection_analysis.status_message.connect(self._clipboard_copied)
        self.collection_analysis.failure_selected.connect(
            self._failure_occurrence_selected
        )
        self.collection_analysis.refresh_requested.connect(
            lambda: self._refresh_collection_analysis(force=True)
        )
        self.workspace_tabs = QTabWidget()
        self.workspace_tabs.addTab(self.context_workspace, "Context inspector")
        self.workspace_tabs.addTab(self.collection_analysis, "Collection overview")
        self._search_tab_index = self.workspace_tabs.addTab(
            self.search_workspace, "Search"
        )
        self.extraction = DataExtractionPanel(settings=self._settings)
        self.extraction.setObjectName("dataExtractionWorkspace")
        self.extraction.status_message.connect(self._clipboard_copied)
        self.extraction.selectors_changed.connect(self._refresh_extraction_preview)
        self.extraction.selectors_changed.connect(self._update_extraction_tab_badge)
        self.extraction.export_requested.connect(self._export_extraction)
        self.extraction.sort_requested.connect(self._order_extraction_requested)
        self.workspace_tabs.setObjectName("workspaceTabs")
        self.workspace_tabs.tabBar().setObjectName("workspaceTabBar")
        self._extraction_tab_index = self.workspace_tabs.addTab(
            self.extraction, "Data Extraction"
        )
        self._update_extraction_tab_badge()

        self.evaluation_panel = self._build_evaluation_panel()
        self.evaluation_panel.setMinimumWidth(230)
        self.main_workspace = QSplitter(Qt.Orientation.Horizontal)
        self.main_workspace.setObjectName("mainWorkspace")
        self.main_workspace.setChildrenCollapsible(False)
        self.main_workspace.addWidget(self.evaluation_panel)
        self.main_workspace.addWidget(self.workspace_tabs)
        self.main_workspace.setStretchFactor(0, 0)
        self.main_workspace.setStretchFactor(1, 1)
        self.main_workspace.setSizes([275, 1185])
        outer.addWidget(self.main_workspace, 1)
        self.setCentralWidget(container)
        self.activity = ActivityStatus(self)
        # Compatibility handles for code that customizes the established
        # MainWindow widgets. Mutable state itself is owned by ActivityStatus.
        self.activity_toggle = self.activity.toggle
        self.activity_status = self.activity.status
        self.activity_progress = self.activity.progress
        self.activity_panel = self.activity.panel
        self.activity_panel_layout = self.activity.panel_layout
        self._activity_rows = self.activity.rows
        self._operation_tokens = self.activity.tokens
        self._operation_labels = self.activity.labels
        self._operation_progress = self.activity.progress_values
        self._show_status("Ready")

    def _build_evaluation_panel(self) -> QWidget:  # noqa: PLR0915
        panel = QFrame()
        panel.setObjectName("sidePanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 16, 14, 14)
        layout.setSpacing(10)
        label = QLabel("EVALUATIONS")
        label.setObjectName("sectionLabel")
        layout.addWidget(label)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter files…")
        self.search.setClearButtonEnabled(True)
        layout.addWidget(self.search)

        self.file_model = EvaluationListModel(self)
        self.file_proxy = QSortFilterProxyModel(self)
        self.file_proxy.setSourceModel(self.file_model)
        self.file_proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.file_proxy.setFilterRole(Qt.ItemDataRole.DisplayRole)
        self.file_proxy.setRecursiveFilteringEnabled(True)
        self.file_proxy.setAutoAcceptChildRows(True)
        self.search.textChanged.connect(self.file_proxy.setFilterFixedString)

        self.file_list = QTreeView()
        self.file_list.setModel(self.file_proxy)
        self.file_list.setHeaderHidden(True)
        self.file_list.setUniformRowHeights(True)
        self.file_list.setAnimated(False)
        self.file_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.file_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.file_list.customContextMenuRequested.connect(
            self._show_evaluation_context_menu
        )
        self.file_list.selectionModel().selectionChanged.connect(
            self._evaluation_selected
        )
        self._file_copy_shortcut = QShortcut(
            QKeySequence.StandardKey.Copy, self.file_list
        )
        self._file_copy_shortcut.setContext(
            Qt.ShortcutContext.WidgetWithChildrenShortcut
        )
        self._file_copy_shortcut.activated.connect(self._copy_current_evaluation)
        self._file_path_copy_shortcut = QShortcut(
            QKeySequence("Ctrl+Shift+C"), self.file_list
        )
        self._file_path_copy_shortcut.setContext(
            Qt.ShortcutContext.WidgetWithChildrenShortcut
        )
        self._file_path_copy_shortcut.activated.connect(
            self._copy_current_evaluation_path
        )
        self._file_explorer_shortcut = QShortcut(
            QKeySequence("Ctrl+Return"), self.file_list
        )
        self._file_explorer_shortcut.setContext(
            Qt.ShortcutContext.WidgetWithChildrenShortcut
        )
        self._file_explorer_shortcut.activated.connect(
            self._open_current_evaluation_folder
        )
        self.evaluation_empty_state = QLabel(
            "No collection loaded.\n\n"
            "Drag and drop a file or folder here, or use:\n"
            "Ctrl+O — open one file\n"
            "Ctrl+Shift+O — open one folder"
        )
        self.evaluation_empty_state.setObjectName("evaluationEmptyState")
        self.evaluation_empty_state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.evaluation_empty_state.setWordWrap(True)
        self.evaluation_empty_state.setTextFormat(Qt.TextFormat.PlainText)
        self.evaluation_stack = QStackedWidget()
        self.evaluation_stack.addWidget(self.evaluation_empty_state)
        self.evaluation_stack.addWidget(self.file_list)
        layout.addWidget(self.evaluation_stack, 1)
        self.file_model.modelReset.connect(self._update_evaluation_empty_state)
        self.file_model.rowsInserted.connect(self._update_evaluation_empty_state)
        self.file_model.rowsRemoved.connect(self._update_evaluation_empty_state)
        self._update_evaluation_empty_state()

        path_row = ActionRow()
        self.evaluation_path = QLineEdit()
        self.evaluation_path.setObjectName("evaluationPath")
        self.evaluation_path.setReadOnly(True)
        self.evaluation_path.setPlaceholderText("No evaluation selected")
        self.evaluation_path.setToolTip("Full path of the selected evaluation")
        path_row.add_widget(self.evaluation_path, 1)
        self.copy_evaluation_path_button = path_row.add_action(
            "copyEvaluationPathAction",
            "Copy path",
            "Copy the selected evaluation's full path",
        )
        self.copy_evaluation_path_button.clicked.connect(
            self._copy_current_evaluation_path
        )
        self.open_file_explorer_button = path_row.add_action(
            "openInFileExplorerAction",
            "Open folder",
            "Open the selected evaluation's folder in the file explorer",
        )
        self.open_file_explorer_button.clicked.connect(
            self._open_current_evaluation_folder
        )
        layout.addWidget(path_row)
        self._set_evaluation_path(None)

        self.focus_selected_button = QPushButton("Focus selected")
        self.focus_selected_button.setToolTip(
            "Reveal and center the currently loaded evaluation"
        )
        self.focus_selected_button.clicked.connect(self._focus_selected_record)
        layout.addWidget(self.focus_selected_button)

        self.file_count = muted_label("No files loaded")
        layout.addWidget(self.file_count)
        self.order_status = muted_label("Order: filename", word_wrap=True)
        self.order_status.setToolTip("Natural alphanumeric filename order")
        layout.addWidget(self.order_status)
        order_controls = QHBoxLayout()
        order_controls.setSpacing(6)
        self.ascending_order_button = QPushButton("Sort ↑")
        self.ascending_order_button.setToolTip("Sort the collection in ascending order")
        self.ascending_order_button.setEnabled(False)
        self.ascending_order_button.clicked.connect(
            lambda: self._set_collection_order(descending=False)
        )
        order_controls.addWidget(self.ascending_order_button)
        self.descending_order_button = QPushButton("Sort ↓")
        self.descending_order_button.setToolTip(
            "Sort the collection in descending order"
        )
        self.descending_order_button.setEnabled(False)
        self.descending_order_button.clicked.connect(
            lambda: self._set_collection_order(descending=True)
        )
        order_controls.addWidget(self.descending_order_button)
        # Compatibility handle for integrations that used the old toggle button.
        self.reverse_order_button = self.descending_order_button
        layout.addLayout(order_controls)
        self.reset_order_button = QPushButton("Filename order")
        self.reset_order_button.setToolTip("Restore natural filename ordering")
        self.reset_order_button.setEnabled(False)
        self.reset_order_button.clicked.connect(self._reset_collection_order)
        layout.addWidget(self.reset_order_button)
        return panel

    def _update_evaluation_empty_state(self, *_args: object) -> None:
        """Show opening instructions in the evaluation list when it has no records."""
        target = (
            self.file_list if self.file_model.records else self.evaluation_empty_state
        )
        self.evaluation_stack.setCurrentWidget(target)

    def _build_tree_panel(self) -> QWidget:  # noqa: PLR0915
        panel = QFrame()
        panel.setObjectName("treePanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 16, 14, 14)
        layout.setSpacing(10)
        top = QHBoxLayout()
        label = QLabel("CONTEXT HIERARCHY")
        label.setObjectName("sectionLabel")
        top.addWidget(label)
        top.addStretch()
        label_field_label = muted_label("Labels")
        label_field_label.setToolTip("Component label metadata key")
        top.addWidget(label_field_label)
        self.component_label_key = QComboBox()
        self.component_label_key.setEditable(True)
        self.component_label_key.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.component_label_key.setMaximumWidth(150)
        self.component_label_key.setToolTip(
            "Metadata key used for component labels in every view. "
            "Leave blank to use structural component names."
        )
        self.component_label_key.setEditText(self._component_label_metadata_key)
        editor = self.component_label_key.lineEdit()
        if editor is not None:
            editor.setPlaceholderText("name")
            editor.editingFinished.connect(self._component_label_key_changed)
        self.component_label_key.activated.connect(
            lambda _index: self._component_label_key_changed()
        )
        top.addWidget(self.component_label_key)
        self.node_count = muted_label("—")
        top.addWidget(self.node_count)
        layout.addLayout(top)

        search_options = QFrame()
        search_options.setObjectName("hierarchySearchControls")
        search_options_layout = QVBoxLayout(search_options)
        search_options_layout.setContentsMargins(8, 8, 8, 8)
        search_options_layout.setSpacing(7)

        self.field_search = QLineEdit()
        self.field_search.setPlaceholderText(
            "Search fields, values, and component hierarchy…"
        )
        self.field_search.setClearButtonEnabled(True)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(120)
        self._search_timer.timeout.connect(self._run_field_search)
        self.field_search.textChanged.connect(self._field_search_changed)
        search_options_layout.addWidget(self.field_search)
        search_controls = QHBoxLayout()
        search_controls.setSpacing(8)
        self.search_mode = QComboBox()
        self.search_mode.addItem("Fuzzy", "fuzzy")
        self.search_mode.addItem("Exact", "exact")
        self.search_mode.addItem("Regex", "regex")
        self.search_mode.setToolTip(
            "Choose fuzzy, literal substring, or regex matching"
        )
        self.search_mode.currentIndexChanged.connect(self._search_mode_changed)
        search_controls.addWidget(self.search_mode)
        self.search_scope = QComboBox()
        self.search_scope.addItem("Whole collection", "collection")
        self.search_scope.addItem("Current evaluation", "current")
        self.search_scope.setToolTip(
            "Search one evaluation or every loaded context file"
        )
        self.search_scope.currentIndexChanged.connect(self._search_scope_changed)
        search_controls.addWidget(self.search_scope)
        search_controls.addStretch()
        search_options_layout.addLayout(search_controls)
        search_dimensions = QGridLayout()
        search_dimensions.setSpacing(8)
        self.search_keys = QCheckBox("Include keys")
        self.search_keys.setChecked(True)
        self.search_keys.setToolTip("Match nested field paths and section names")
        self.search_keys.toggled.connect(self._search_options_toggled)
        search_dimensions.addWidget(self.search_keys, 0, 0)
        self.search_values = QCheckBox("Include values")
        self.search_values.setChecked(True)
        self.search_values.setToolTip("Match serialized field values")
        self.search_values.toggled.connect(self._search_options_toggled)
        search_dimensions.addWidget(self.search_values, 0, 1)
        self.search_metadata = QCheckBox("Include metadata")
        self.search_metadata.setChecked(True)
        self.search_metadata.setToolTip(
            "Include component metadata fields and values in search results"
        )
        self.search_metadata.toggled.connect(self._search_options_toggled)
        search_dimensions.addWidget(self.search_metadata, 1, 0)
        self.search_components = QCheckBox("Component names")
        self.search_components.setChecked(False)
        self.search_components.setToolTip(
            "Include component names and hierarchy breadcrumbs when matching"
        )
        self.search_components.toggled.connect(self._search_options_toggled)
        search_dimensions.addWidget(self.search_components, 1, 1)
        search_dimensions.setColumnStretch(0, 1)
        search_dimensions.setColumnStretch(1, 1)
        search_options_layout.addLayout(search_dimensions)

        self.tree_model = ContextTreeModel(self)
        self.tree = QTreeView()
        self.tree.setModel(self.tree_model)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree.setAnimated(False)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._show_tree_context_menu)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.tree.header().setMinimumSectionSize(72)
        self.tree.header().resizeSection(0, 280)
        self.tree.header().resizeSection(1, 110)
        self.tree.header().resizeSection(2, 220)
        self.tree.selectionModel().selectionChanged.connect(self._context_selected)
        self._tree_copy_shortcut = QShortcut(QKeySequence.StandardKey.Copy, self.tree)
        self._tree_copy_shortcut.setContext(
            Qt.ShortcutContext.WidgetWithChildrenShortcut
        )
        self._tree_copy_shortcut.activated.connect(self._copy_current_tree_component)

        self.data_flow = DataFlowPanel()
        # ``graph`` remains a private compatibility handle for integrations
        # built against early dashboard releases; both names reference the
        # single Data Flow implementation.
        self.graph = self.data_flow
        self.graph.node_selected.connect(self._graph_context_selected)
        self.graph.expand_requested.connect(self._toggle_data_flow_expansion)
        self.collection_plot = CollectionPlotPanel()
        self.collection_plot.status_message.connect(self._clipboard_copied)
        self.collection_plot.context_selected.connect(self._plot_context_selected)
        self.collection_plot.point_selected.connect(self._plot_point_selected)
        self.collection_plot.remove_series_requested.connect(self._remove_plot_series)
        self.collection_plot.clear_series_requested.connect(self._clear_plot_series)
        self.collection_plot.expand_requested.connect(self._toggle_plot_expansion)
        self.search_panel = ContextSearchPanel()
        self.search_panel.copy_completed.connect(self._clipboard_copied)
        self.search_panel.hit_selected.connect(self._search_hit_selected)
        self.search_workspace = QWidget()
        self.search_workspace.setObjectName("searchWorkspace")
        search_workspace_layout = QVBoxLayout(self.search_workspace)
        search_workspace_layout.setContentsMargins(8, 8, 8, 8)
        search_workspace_layout.setSpacing(8)
        search_workspace_layout.addWidget(search_options)
        search_workspace_layout.addWidget(self.search_panel, 1)
        self.hierarchy_tabs = QTabWidget()
        self.hierarchy_tabs.addTab(self.tree, "Context Explorer")
        self._data_flow_tab_index = self.hierarchy_tabs.addTab(self.graph, "Data Flow")
        self._plot_tab_index = self.hierarchy_tabs.addTab(self.collection_plot, "Plot")
        self.hierarchy_tabs.currentChanged.connect(self._hierarchy_tab_changed)
        layout.addWidget(self.hierarchy_tabs, 1)
        return panel

    def _build_menu(self) -> None:  # noqa: PLR0915
        file_menu = self.menuBar().addMenu("&File")
        open_files = QAction("Open &file…", self)
        open_files.setShortcut(QKeySequence.StandardKey.Open)
        open_files.triggered.connect(self.open_files)
        file_menu.addAction(open_files)
        open_directory = QAction("Open &directory…", self)
        open_directory.setShortcut("Ctrl+Shift+O")
        open_directory.triggered.connect(self.open_directory)
        file_menu.addAction(open_directory)
        file_menu.addSeparator()
        add_files = QAction("&Add files to collection…", self)
        add_files.triggered.connect(self.add_files_to_collection)
        file_menu.addAction(add_files)
        add_directory = QAction("Add &directory to collection…", self)
        add_directory.triggered.connect(self.add_directory_to_collection)
        file_menu.addAction(add_directory)
        self.recent_menu = QMenu("Open &recent", self)
        file_menu.addMenu(self.recent_menu)
        self._rebuild_recent_menu()
        file_menu.addSeparator()
        self.close_collection_action = QAction(
            self.style().standardIcon(QStyle.StandardPixmap.SP_DialogCloseButton),
            "&Close collection",
            self,
        )
        self.close_collection_action.setShortcut(QKeySequence.StandardKey.Close)
        self.close_collection_action.setToolTip(
            "Close the loaded collection and keep the application open"
        )
        self.close_collection_action.setEnabled(False)
        self.close_collection_action.triggered.connect(self.close_collection)
        file_menu.addAction(self.close_collection_action)
        file_menu.addSeparator()
        quit_action = QAction("&Quit", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        edit_menu = self.menuBar().addMenu("&Edit")
        self.find_action = QAction("&Find in contexts…", self)
        self.find_action.setShortcut(QKeySequence.StandardKey.Find)
        self.find_action.triggered.connect(self._focus_field_search)
        edit_menu.addAction(self.find_action)
        self.clear_search_action = QAction("Clear search", self)
        self.clear_search_action.setShortcut(QKeySequence(Qt.Key.Key_Escape))
        self.clear_search_action.triggered.connect(self._clear_field_search)
        edit_menu.addAction(self.clear_search_action)

        navigate_menu = self.menuBar().addMenu("&Navigate")
        self.back_action = QAction("&Back", self)
        self.back_action.setShortcut(QKeySequence.StandardKey.Back)
        self.back_action.setToolTip("Back to the previous context")
        self.back_action.setEnabled(False)
        self.back_action.triggered.connect(lambda: self._navigate_history(-1))
        navigate_menu.addAction(self.back_action)
        self.back_button.setDefaultAction(self.back_action)
        self.forward_action = QAction("&Forward", self)
        self.forward_action.setShortcut(QKeySequence.StandardKey.Forward)
        self.forward_action.setToolTip("Forward to the next context")
        self.forward_action.setEnabled(False)
        self.forward_action.triggered.connect(lambda: self._navigate_history(1))
        navigate_menu.addAction(self.forward_action)
        self.forward_button.setDefaultAction(self.forward_action)
        self._set_navigation_icons(color_scheme(self._color_scheme_key))

        settings_menu = self.menuBar().addMenu("&Settings")
        self.settings_menu = settings_menu
        self.color_scheme = QComboBox(self)
        self.color_scheme.hide()
        self.theme_menu = settings_menu.addMenu("Theme")
        self._theme_actions = QActionGroup(self)
        self._theme_actions.setExclusive(True)
        for scheme in COLOR_SCHEMES:
            self.color_scheme.addItem(scheme.label, scheme.key)
            action = self.theme_menu.addAction(scheme.label)
            action.setCheckable(True)
            action.setData(scheme.key)
            action.setChecked(scheme.key == self._color_scheme_key)
            self._theme_actions.addAction(action)
            action.triggered.connect(
                lambda _checked=False,
                key=scheme.key: self.color_scheme.setCurrentIndex(
                    self.color_scheme.findData(key)
                )
            )
        scheme_index = self.color_scheme.findData(self._color_scheme_key)
        self.color_scheme.setCurrentIndex(max(0, scheme_index))
        self.color_scheme.currentIndexChanged.connect(self._color_scheme_changed)

        style_menu = settings_menu.addMenu("Interface style")
        self._style_actions = QActionGroup(self)
        self._style_actions.setExclusive(True)
        for label, key in (("Classic", "classic"), ("Lightweight", "lightweight")):
            action = style_menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(key == self._style_profile)
            self._style_actions.addAction(action)
            action.triggered.connect(
                lambda _checked=False, value=key: self._set_style_profile(value)
            )

        density_menu = settings_menu.addMenu("Spacing")
        self._density_actions = QActionGroup(self)
        self._density_actions.setExclusive(True)
        for label, key in (
            ("Compact", "compact"),
            ("Comfortable", "comfortable"),
            ("Spacious", "spacious"),
        ):
            action = density_menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(key == self._density)
            self._density_actions.addAction(action)
            action.triggered.connect(
                lambda _checked=False, value=key: self._set_density(value)
            )

        workers_menu = settings_menu.addMenu("Workers")
        self.background_thread_workers = QSpinBox(workers_menu)
        self.background_thread_workers.setRange(1, MAX_CONFIGURABLE_WORKERS)
        self.background_thread_workers.setValue(self._background_thread_count)
        self.background_thread_workers.setPrefix("Background threads: ")
        self.background_thread_workers.setToolTip(
            "Shared Qt worker threads used by loading, analysis, plots, "
            "extraction, and graph layout"
        )
        thread_workers_action = QWidgetAction(workers_menu)
        thread_workers_action.setDefaultWidget(self.background_thread_workers)
        workers_menu.addAction(thread_workers_action)
        self.background_thread_workers.valueChanged.connect(
            self._set_background_thread_count
        )

        self.search_process_workers = QSpinBox(workers_menu)
        self.search_process_workers.setRange(1, MAX_CONFIGURABLE_WORKERS)
        self.search_process_workers.setValue(self._search_process_count)
        self.search_process_workers.setPrefix("Search processes: ")
        self.search_process_workers.setToolTip(
            "Persistent worker processes used only for whole-collection search"
        )
        search_workers_action = QWidgetAction(workers_menu)
        search_workers_action.setDefaultWidget(self.search_process_workers)
        workers_menu.addAction(search_workers_action)
        self.search_process_workers.valueChanged.connect(
            self._set_search_process_count
        )

        help_menu = self.menuBar().addMenu("&Help")
        shortcuts_action = QAction("Keyboard shortcuts…", self)
        shortcuts_action.setShortcut(QKeySequence(Qt.Key.Key_F1))
        shortcuts_action.triggered.connect(self._show_keyboard_shortcuts)
        help_menu.addAction(shortcuts_action)

    def _build_navigation_shortcuts(self) -> None:
        """Install direct, application-wide navigation to every major view."""
        destinations = (
            ("Ctrl+1", "context"),
            ("Ctrl+2", "flow"),
            ("Ctrl+3", "plot"),
            ("Ctrl+4", "overview"),
            ("Ctrl+5", "search"),
            ("Ctrl+6", "extraction"),
        )
        self._navigation_shortcuts: list[QShortcut] = []
        for sequence, destination in destinations:
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
            shortcut.activated.connect(
                lambda target=destination: self._navigate_to_view(target)
            )
            self._navigation_shortcuts.append(shortcut)

    def _navigate_to_view(self, destination: str) -> None:
        if destination in {"context", "flow", "plot"}:
            self.workspace_tabs.setCurrentWidget(self.context_workspace)
            widget = {
                "context": self.tree,
                "flow": self.graph,
                "plot": self.collection_plot,
            }[destination]
            index = self.hierarchy_tabs.indexOf(widget)
            if index >= 0:
                self.hierarchy_tabs.setCurrentIndex(index)
            elif destination == "flow" and self._expanded_data_flow_dialog is not None:
                self._expanded_data_flow_dialog.show()
                self._expanded_data_flow_dialog.raise_()
                self._expanded_data_flow_dialog.activateWindow()
            elif destination == "plot" and self._expanded_plot_dialog is not None:
                self._expanded_plot_dialog.show()
                self._expanded_plot_dialog.raise_()
                self._expanded_plot_dialog.activateWindow()
            return
        if destination == "overview":
            self.workspace_tabs.setCurrentWidget(self.collection_analysis)
        elif destination == "search":
            self.workspace_tabs.setCurrentWidget(self.search_workspace)
            self.field_search.setFocus()
        elif destination == "extraction":
            self.workspace_tabs.setCurrentWidget(self.extraction)

    def _set_navigation_icons(self, scheme: ColorScheme) -> None:
        self.back_action.setIcon(_chevron_icon(-1, scheme.text_strong))
        self.forward_action.setIcon(_chevron_icon(1, scheme.text_strong))

    def _setting_strings(self, key: str) -> list[str]:
        value = self._settings.value(key, [])
        if isinstance(value, (list, tuple)):
            return [str(item) for item in value if str(item)]
        return [str(value)] if value else []

    def _setting_ints(self, key: str) -> list[int]:
        values = self._setting_strings(key)
        try:
            return [int(value) for value in values]
        except ValueError:
            return []

    def _setting_int(self, key: str, default: int = 0) -> int:
        value = self._settings.value(key, default)
        if isinstance(value, (int, str)):
            try:
                return int(value)
            except ValueError:
                pass
        return default

    def _restore_session_state(self) -> None:
        geometry = self._settings.value(SESSION_GEOMETRY_SETTING)
        if geometry is not None:
            self.restoreGeometry(geometry)
        main_sizes = self._setting_ints(SESSION_MAIN_SPLITTER_SETTING)
        if len(main_sizes) == 2:
            self.main_workspace.setSizes(main_sizes)
        context_sizes = self._setting_ints(SESSION_CONTEXT_SPLITTER_SETTING)
        if len(context_sizes) == 2:
            self.context_workspace.setSizes(context_sizes)
        workspace_key = str(
            self._settings.value(SESSION_WORKSPACE_KEY_SETTING, "") or ""
        )
        workspace_tab = self._setting_int(SESSION_WORKSPACE_TAB_SETTING)
        hierarchy_tab = self._setting_int(SESSION_HIERARCHY_TAB_SETTING)
        restored_workspace = next(
            (
                self.workspace_tabs.widget(index)
                for index in range(self.workspace_tabs.count())
                if self.workspace_tabs.widget(index).objectName() == workspace_key
            ),
            None,
        )
        if restored_workspace is not None:
            self.workspace_tabs.setCurrentWidget(restored_workspace)
        elif workspace_tab == 2:
            # Before Search became a top-level workspace, index 2 was Extraction.
            self.workspace_tabs.setCurrentWidget(self.extraction)
        elif 0 <= workspace_tab < self.workspace_tabs.count():
            self.workspace_tabs.setCurrentIndex(workspace_tab)
        if 0 <= hierarchy_tab < self.hierarchy_tabs.count():
            self.hierarchy_tabs.setCurrentIndex(hierarchy_tab)
        self._session_locations = self._setting_strings(SESSION_LOCATIONS_SETTING)
        selected_record = self._settings.value(SESSION_RECORD_SETTING, "")
        self._restored_record_path = str(selected_record) or None
        node_path = self._setting_ints(SESSION_NODE_PATH_SETTING)
        self._sticky_node_path = tuple(node_path)

    def _save_session_state(self) -> None:
        if not self._session_enabled:
            return
        self._settings.setValue(SESSION_GEOMETRY_SETTING, self.saveGeometry())
        self._settings.setValue(
            SESSION_MAIN_SPLITTER_SETTING, self.main_workspace.sizes()
        )
        self._settings.setValue(
            SESSION_CONTEXT_SPLITTER_SETTING, self.context_workspace.sizes()
        )
        self._settings.setValue(
            SESSION_WORKSPACE_TAB_SETTING, self.workspace_tabs.currentIndex()
        )
        current_workspace = self.workspace_tabs.currentWidget()
        self._settings.setValue(
            SESSION_WORKSPACE_KEY_SETTING,
            current_workspace.objectName() if current_workspace is not None else "",
        )
        self._settings.setValue(
            SESSION_HIERARCHY_TAB_SETTING, self.hierarchy_tabs.currentIndex()
        )
        self._settings.setValue(SESSION_LOCATIONS_SETTING, self._session_locations)
        document = self._current_document
        if document is not None:
            self._settings.setValue(
                SESSION_RECORD_SETTING, str(document.record.path.resolve())
            )
            self._settings.setValue(
                SESSION_NODE_PATH_SETTING, list(self._sticky_node_path)
            )
        self._settings.sync()

    def _new_operation(self, name: str) -> CancellationToken:
        """Compatibility entry point for creating a named cancellation token."""
        return self.activity.new_token(name)

    def _begin_operation(
        self, name: str, label: str, *, display_delay_ms: int = 0
    ) -> CancellationToken:
        return self.activity.begin(
            name, label, display_delay_ms=display_delay_ms
        )

    def _show_delayed_operation(
        self, name: str, label: str, token: CancellationToken
    ) -> None:
        """Compatibility entry point for delayed activity callbacks."""
        self.activity.show_if_current(name, label, token)

    def _finish_operation(self, name: str, message: str | None = None) -> None:
        self.activity.finish(name, message)

    def _operation_progressed(self, name: str, value: int, total: int) -> None:
        self.activity.set_progress(name, value, total)

    def _progress_callback(
        self, name: str, total: int
    ) -> Callable[[int, object], None]:
        """Create a bounded-rate worker callback for one determinate operation."""
        self.operation_progress.emit(name, 0, total)
        last_progress_at = time.monotonic()

        def report(completed: int, _item: object = None) -> None:
            nonlocal last_progress_at
            last_progress_at = _report_progress(
                self.operation_progress.emit,
                name,
                completed,
                total,
                last_progress_at,
            )

        return report

    def _begin_determinate_operation(
        self, name: str, label: str, total: int
    ) -> _DeterminateOperation:
        """Start work through the single determinate-operation setup path."""
        return _DeterminateOperation(
            self._begin_operation(name, label),
            self._progress_callback(name, total),
        )

    def _collection_documents(
        self,
        records: Sequence[RecordRef],
        operation: _DeterminateOperation,
    ) -> Iterator[tuple[RecordRef, ContextDocument | None]]:
        """Traverse collection documents with the shared operation contract."""
        for item in iter_collection(
            self.source,
            records,
            cancellation=operation.cancellation,
            progress=operation.progress,
        ):
            yield item.record, item.document

    def _toggle_activity_panel(self, expanded: bool) -> None:
        self.activity.set_expanded(expanded)

    def _rebuild_activity_panel(self) -> None:
        self.activity.refresh_panel()

    def _position_activity_panel(self) -> None:
        self.activity.position_panel()

    def _show_status(self, message: str, timeout: int = 0) -> None:
        self.activity.show_message(message, timeout)

    def _cancel_operation(self, name: str) -> None:
        self.activity.cancel(name)

    def resizeEvent(self, event) -> None:  # noqa: N802, ANN001
        super().resizeEvent(event)
        if hasattr(self, "activity_panel") and self.activity_panel.isVisible():
            self._position_activity_panel()

    def closeEvent(self, event) -> None:  # noqa: N802, ANN001
        self._save_session_state()
        self._restore_data_flow_panel()
        self._restore_plot_panel()
        self.activity.cancel_all()
        self.graph.view.cancel_pending_layout()
        self.tasks.cancel_pending()
        self.collection_search.shutdown(wait_for_workers=False)
        super().closeEvent(event)

    def _change_font_scale(self, change: float) -> None:
        application = QApplication.instance()
        if not isinstance(application, QApplication) or self._base_font is None:
            return
        self._font_scale = min(1.6, max(0.8, round(self._font_scale + change, 1)))
        self._apply_application_font()
        self._apply_application_stylesheet()
        self._settings.setValue(FONT_SCALE_SETTING, self._font_scale)
        self._settings.sync()
        graph_document = self.graph.view.document
        graph_was_rendered = self.graph.view.rendered_node_count > 0
        if graph_document is not None:
            self.graph.set_document(graph_document)
            if graph_was_rendered:
                self.graph.ensure_rendered()
        self.collection_plot.refresh_fonts()
        percent = round(self._font_scale * 100)
        self.statusBar().showMessage(f"Interface font size: {percent}%", 2000)

    def _color_scheme_changed(self, _index: int) -> None:
        key = str(self.color_scheme.currentData())
        scheme = set_active_color_scheme(key)
        self._color_scheme_key = scheme.key
        self._base_stylesheet = stylesheet_for(
            scheme, style_profile=self._style_profile, density=self._density
        )
        self._apply_application_stylesheet()
        self._settings.setValue(COLOR_SCHEME_SETTING, scheme.key)
        self._settings.sync()
        self._set_navigation_icons(scheme)
        if hasattr(self, "_theme_actions"):
            for action in self._theme_actions.actions():
                action.setChecked(action.data() == scheme.key)
        self.graph.set_color_scheme(scheme)
        self.collection_plot.set_color_scheme(scheme)
        self.file_list.viewport().update()
        self.tree.viewport().update()
        self.statusBar().showMessage(
            f"Color scheme: {scheme.label} · saved in {self._settings.fileName()}",
            4000,
        )

    def _set_style_profile(self, profile: str) -> None:
        self._style_profile = profile
        self._settings.setValue(STYLE_PROFILE_SETTING, profile)
        self._settings.sync()
        self._base_stylesheet = stylesheet_for(
            color_scheme(self._color_scheme_key),
            style_profile=self._style_profile,
            density=self._density,
        )
        self._apply_application_stylesheet()
        self._show_status(
            "Interface style: Lightweight"
            if profile == "lightweight"
            else "Interface style: Classic",
            3000,
        )

    def _set_density(self, density: str) -> None:
        self._density = density
        self._settings.setValue(DENSITY_SETTING, density)
        self._settings.sync()
        self._base_stylesheet = stylesheet_for(
            color_scheme(self._color_scheme_key),
            style_profile=self._style_profile,
            density=self._density,
        )
        self._apply_application_stylesheet()
        self._show_status(f"Interface spacing: {density}", 3000)

    def _set_background_thread_count(self, count: int) -> None:
        """Resize the global Qt pool shared by GUI background operations."""
        count = _worker_count(count, self._background_thread_count)
        if count == self._background_thread_count:
            return
        self._background_thread_count = count
        QThreadPool.globalInstance().setMaxThreadCount(count)
        self._settings.setValue(BACKGROUND_THREADS_SETTING, count)
        self._settings.sync()
        self._show_status(f"Background worker threads: {count}", 3000)

    def _set_search_process_count(self, count: int) -> None:
        """Replace the persistent collection-search process pool."""
        count = _worker_count(count, self._search_process_count)
        if count == self._search_process_count:
            return
        self._cancel_operation("search")
        self._search_generation += 1
        previous = self.collection_search
        self.collection_search = CollectionSearchExecutor(max_workers=count)
        self._search_process_count = count
        previous.shutdown(wait_for_workers=False)
        self._settings.setValue(SEARCH_PROCESSES_SETTING, count)
        self._settings.sync()
        if self.field_search.text().strip():
            self._search_timer.start()
        self._show_status(f"Collection-search worker processes: {count}", 3000)

    def _show_keyboard_shortcuts(self) -> None:
        dialog = QMessageBox(self)
        dialog.setWindowTitle("Keyboard shortcuts")
        dialog.setIcon(QMessageBox.Icon.Information)
        dialog.setStyleSheet("QLabel#qt_msgbox_label { min-width: 760px; }")
        dialog.setTextFormat(Qt.TextFormat.RichText)
        dialog.setText(
            "<h3>Keyboard shortcuts</h3>"
            "<p>Shortcuts marked with a specific view apply when that view "
            "has focus.</p>"
            '<table cellpadding="4" cellspacing="0">'
            "<tr><th align='left'>Where</th><th align='left'>Shortcut</th>"
            "<th align='left'>Action</th></tr>"
            "<tr><td>Anywhere</td><td><code>Ctrl+1 … Ctrl+6</code></td>"
            "<td>Open Context Explorer, Data Flow, Plot, Collection Overview, "
            "Search, or Data Extraction</td></tr>"
            "<tr><td>Anywhere</td><td><code>Ctrl+F</code></td>"
            "<td>Search collection fields</td></tr>"
            "<tr><td>Anywhere</td><td><code>Ctrl+O</code></td>"
            "<td>Open one context file</td></tr>"
            "<tr><td>Anywhere</td><td><code>Ctrl+Shift+O</code></td>"
            "<td>Open one directory</td></tr>"
            "<tr><td>Focused view</td><td><code>Ctrl+C</code></td>"
            "<td>Copy the current selection</td></tr>"
            "<tr><td>Evaluations</td><td><code>Ctrl+Shift+C</code></td>"
            "<td>Copy the selected evaluation path</td></tr>"
            "<tr><td>Evaluations</td><td><code>Ctrl+Return</code></td>"
            "<td>Open the selected evaluation's folder</td></tr>"
            "<tr><td>Extraction fields</td><td><code>Delete</code></td>"
            "<td>Remove selected fields</td></tr>"
            "<tr><td>Extraction preview</td><td><code>Backspace</code></td>"
            "<td>Remove selected columns</td></tr>"
            "<tr><td>Extraction preview</td><td><code>S</code></td>"
            "<td>Sort or reverse the selected column</td></tr>"
            "<tr><td>Extraction preview</td><td><code>R</code></td>"
            "<td>Rename the selected column</td></tr>"
            "<tr><td>Search</td><td><code>Esc</code></td>"
            "<td>Clear search and return to the previous view</td></tr>"
            "<tr><td>Context history</td><td><code>Alt+Left / Alt+Right</code></td>"
            "<td>Move backward or forward</td></tr>"
            "<tr><td>Anywhere</td><td><code>Ctrl+W</code></td>"
            "<td>Close the current collection</td></tr>"
            "<tr><td>Anywhere</td><td><code>Ctrl+Q</code></td>"
            "<td>Quit</td></tr>"
            "<tr><td>Anywhere</td><td><code>F1</code></td>"
            "<td>Show this overview</td></tr>"
            "</table>"
        )
        dialog.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        dialog.setStandardButtons(QMessageBox.StandardButton.Ok)
        dialog.exec()

    def _component_label_key_changed(self) -> None:
        key = self.component_label_key.currentText().strip()
        if key == self._component_label_metadata_key:
            return
        self._component_label_metadata_key = key
        self.source.set_component_label_metadata_key(key)
        self._settings.setValue(COMPONENT_LABEL_METADATA_KEY_SETTING, key)
        self._settings.sync()
        self.search_indexes.clear()

        document = self._current_document
        if document is not None:
            document.set_component_label_metadata_key(key)
            self.tree_model.layoutChanged.emit()
            self.tree.viewport().update()
            self.graph.refresh_labels()
            current = self.tree_model.node_info(self.tree.currentIndex())
            if current is not None:
                self.inspector.set_details(
                    document.details(current.path),
                    document.component_label(current.path),
                )
            self._sticky_node_trail = component_trail_for_path(
                document, self._sticky_node_path
            )
            if self._active_plot_selectors:
                self._active_plot_selectors = [
                    selector_for_document(document, selector)
                    for selector in self._active_plot_selectors
                ]
                self._active_plot_selector = self._active_plot_selectors[0]
                self._refresh_active_plot()
            if self.extraction.selectors:
                config = self.extraction.config
                self.extraction.set_config(
                    ExtractionConfig(
                        tuple(
                            extraction_selector_for_document(document, selector)
                            for selector in config.selectors
                        ),
                        include_identifiers=config.include_identifiers,
                        include_status_columns=config.include_status_columns,
                        version=config.version,
                    )
                )
        self._refresh_collection_analysis()
        if self.field_search.text().strip():
            self._run_field_search()
        shown_key = key or "structural names"
        self.statusBar().showMessage(
            f"Component labels now use {shown_key} in every view", 4000
        )

    def _apply_application_stylesheet(self) -> None:
        application = QApplication.instance()
        if not isinstance(application, QApplication):
            return
        stylesheet = re.sub(
            r"(font-size:\s*)(\d+(?:\.\d+)?)px",
            lambda match: (
                f"{match.group(1)}{float(match.group(2)) * self._font_scale:.1f}px"
            ),
            self._base_stylesheet,
        )
        application.setStyleSheet(stylesheet)

    def _apply_application_font(self) -> None:
        application = QApplication.instance()
        if not isinstance(application, QApplication) or self._base_font is None:
            return
        font = QFont(self._base_font)
        point_size = font.pointSizeF()
        if point_size > 0:
            font.setPointSizeF(point_size * self._font_scale)
        application.setFont(font)

    def open_files(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open ChemFit context file",
            "",
            "JSON files (*.json);;All files (*)",
        )
        if path:
            if self.file_model.records:
                self.close_collection()
            self.add_locations([path])

    def open_directory(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Open context directory")
        if path:
            if self.file_model.records:
                self.close_collection()
            self.add_locations([path])

    def add_files_to_collection(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Add context files to collection",
            "",
            "JSON files (*.json);;All files (*)",
        )
        if paths:
            self.add_locations(paths, append=True)

    def add_directory_to_collection(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Add context directory to collection"
        )
        if path:
            self.add_locations([path], append=True)

    def dragEnterEvent(self, event) -> None:  # noqa: N802, ANN001
        if self._drop_locations(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:  # noqa: N802, ANN001
        locations = self._drop_locations(event.mimeData())
        if not locations:
            event.ignore()
            return
        if self.file_model.records:
            self.close_collection()
        self.add_locations(locations[:1])
        if len(locations) > 1:
            self._show_status(
                "Opened the first dropped item; use File > Add files to add more.",
                5000,
            )
        event.acceptProposedAction()

    @staticmethod
    def _drop_locations(mime_data) -> list[str]:  # noqa: ANN001
        if not mime_data.hasUrls():
            return []
        locations: list[str] = []
        for url in mime_data.urls():
            if not isinstance(url, QUrl):
                continue
            if url.isLocalFile():
                path = Path(url.toLocalFile()).expanduser()
                if path.is_dir() or (
                    path.is_file() and path.suffix.casefold() == ".json"
                ):
                    resolved = str(path.resolve())
                    if resolved not in locations:
                        locations.append(resolved)
                continue
            if url.isValid() and url.scheme() not in {"", "file"}:
                remote = url.toString()
                if remote not in locations:
                    locations.append(remote)
        return locations

    def _clipboard_copied(self, message: str) -> None:
        # Popup menus finish tearing down after their selected action returns.
        # Queue the toast so it is raised after that teardown instead of beneath it.
        self._pending_clipboard_message = message
        self._clipboard_toast_show_timer.start(0)
        self._show_status(message, 2500)

    def _copy_current_evaluation(self) -> None:
        record = self._selected_record()
        if record is not None:
            QApplication.clipboard().setText(record.name)
            self._clipboard_copied("Copied evaluation filename")

    def _copy_current_evaluation_path(self) -> None:
        record = self._selected_record()
        if record is None:
            return
        QApplication.clipboard().setText(str(record.path))
        self._clipboard_copied("Copied evaluation path")

    def _open_current_evaluation_folder(self) -> None:
        record = self._selected_record()
        if record is None:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(record.path.parent)))

    def _set_evaluation_path(self, record: RecordRef | None) -> None:
        path = str(record.path) if record is not None else ""
        self.evaluation_path.setText(path)
        enabled = record is not None
        self.copy_evaluation_path_button.setEnabled(enabled)
        self.open_file_explorer_button.setEnabled(enabled)

    def _copy_current_tree_component(self) -> None:
        info = self.tree_model.node_info(self.tree.currentIndex())
        if info is None or self._current_document is None:
            return
        QApplication.clipboard().setText(
            self._current_document.component_label(info.path)
        )
        self._clipboard_copied("Copied component name")

    def _focus_selected_record(self) -> None:
        record_id = self._selected_record_id()
        if record_id is None and self._current_document is not None:
            record_id = self._current_document.record.id
        if record_id is None:
            self._show_status("No evaluation is selected.", 3000)
            return
        if self.search.text():
            self.search.clear()
        source_index = self.file_model.index_for_record_id(record_id)
        proxy_index = self.file_proxy.mapFromSource(source_index)
        if not proxy_index.isValid():
            return
        parent = proxy_index.parent()
        while parent.isValid():
            self.file_list.expand(parent)
            parent = parent.parent()
        self.file_list.setCurrentIndex(proxy_index)
        self.file_list.scrollTo(
            proxy_index, QAbstractItemView.ScrollHint.PositionAtCenter
        )
        self.file_list.setFocus(Qt.FocusReason.ShortcutFocusReason)

    def _show_clipboard_toast(self) -> None:
        message = self._pending_clipboard_message
        if not message:
            return
        self._pending_clipboard_message = ""
        self._clipboard_toast.setText(message)
        self._clipboard_toast.adjustSize()
        self._position_clipboard_toast()
        self._clipboard_toast.show()
        self._clipboard_toast.raise_()
        self._clipboard_toast_follow_timer.start()
        self._clipboard_toast_timer.start(500)

    def _position_clipboard_toast(self) -> None:
        position = self.mapFromGlobal(QCursor.pos()) + QPoint(14, 18)
        margin = 8
        maximum_x = max(margin, self.width() - self._clipboard_toast.width() - margin)
        maximum_y = max(margin, self.height() - self._clipboard_toast.height() - margin)
        position.setX(min(max(position.x(), margin), maximum_x))
        position.setY(min(max(position.y(), margin), maximum_y))
        self._clipboard_toast.move(position)

    def _hide_clipboard_toast(self) -> None:
        self._clipboard_toast_follow_timer.stop()
        self._clipboard_toast.hide()

    def _rebuild_recent_menu(self) -> None:
        self.recent_menu.clear()
        locations = self._setting_strings(RECENT_LOCATIONS_SETTING)
        if not locations:
            empty = self.recent_menu.addAction("No recent locations")
            empty.setEnabled(False)
            return
        for location in locations:
            action = self.recent_menu.addAction(Path(location).name or location)
            action.setToolTip(location)
            action.triggered.connect(
                lambda _checked=False, target=location: self._open_recent(target)
            )
        self.recent_menu.addSeparator()
        clear_action = self.recent_menu.addAction("Clear recent locations")
        clear_action.triggered.connect(self._clear_recent_locations)

    def _open_recent(self, location: str) -> None:
        if self.file_model.records:
            self.close_collection()
        self.add_locations([location])

    def _clear_recent_locations(self) -> None:
        self._settings.remove(RECENT_LOCATIONS_SETTING)
        self._settings.sync()
        self._rebuild_recent_menu()

    def _show_evaluation_context_menu(self, position) -> None:  # noqa: ANN001
        proxy_index = self.file_list.indexAt(position)
        if not proxy_index.isValid():
            return
        record = self.file_model.record(self.file_proxy.mapToSource(proxy_index))
        if record is None:
            return
        menu = QMenu(self.file_list)
        copy_name = menu.addAction("Copy filename")
        copy_path = menu.addAction("Copy full path")
        open_folder = menu.addAction("Open folder in file explorer")
        selected = menu.exec(self.file_list.viewport().mapToGlobal(position))
        if selected is copy_name:
            QApplication.clipboard().setText(record.name)
            self._clipboard_copied("Copied evaluation filename")
        elif selected is copy_path:
            QApplication.clipboard().setText(str(record.path))
            self._clipboard_copied("Copied evaluation path")
        elif selected is open_folder:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(record.path.parent)))

    def _show_tree_context_menu(self, position) -> None:  # noqa: ANN001
        index = self.tree.indexAt(position)
        info = self.tree_model.node_info(index)
        document = self._current_document
        if info is None or document is None:
            return
        menu = QMenu(self.tree)
        copy_name = menu.addAction("Copy component name")
        copy_path = menu.addAction("Copy component path")
        copy_structural_path = menu.addAction("Copy structural path")
        selected = menu.exec(self.tree.viewport().mapToGlobal(position))
        if selected is copy_name:
            QApplication.clipboard().setText(document.component_label(info.path))
            self._clipboard_copied("Copied component name")
        elif selected is copy_path:
            trail = component_trail_for_path(document, info.path)
            path = " / ".join(segment.name for segment in trail) or "Evaluation root"
            QApplication.clipboard().setText(path)
            self._clipboard_copied("Copied component path")
        elif selected is copy_structural_path:
            QApplication.clipboard().setText(info.path_text)
            self._clipboard_copied("Copied structural path")

    def close_collection(self) -> None:  # noqa: PLR0915
        """Close all loaded contexts while leaving the application running."""
        if not self.file_model.records:
            return
        self._restore_data_flow_panel()
        self._restore_plot_panel()
        self.activity.clear()
        self.tasks.cancel_pending()
        self.graph.view.cancel_pending_layout()
        self._collection_generation += 1
        self._load_generation += 1
        self._plot_generation += 1
        self._order_generation += 1
        self._search_generation += 1
        self._analysis_generation += 1
        self._extraction_generation += 1
        self._pending_search_hit = None
        self._pending_failure = None
        self._pending_plot_selection = None
        self._navigation_history = []
        self._navigation_index = -1
        self._navigation_replaying = False
        self._update_navigation_actions()
        self._current_document = None
        self._sticky_node_path = ()
        self._sticky_node_trail = ()
        self._clear_numeric_order()
        self._filename_reversed = False
        self._active_plot_selector = None
        self._active_plot_selectors = []
        self.search_indexes.clear()
        clear_search_caches()
        clear_source_cache = getattr(self.source, "clear_cache", None)
        if callable(clear_source_cache):
            clear_source_cache()

        self.search.clear()
        self.field_search.clear()
        self.file_model.clear_records()
        self._set_evaluation_path(None)
        self.tree_model.set_document(None)
        self.graph.set_document(None)
        self.inspector.clear()
        self.search_panel.set_available(False)
        self.collection_plot.clear()
        self.collection_analysis.clear()
        self.extraction.clear()
        self.node_count.setText("—")
        self.file_count.setText("No files loaded")
        self.order_status.setText("Order: filename")
        self.order_status.setToolTip("Natural alphanumeric filename order")
        self.ascending_order_button.setEnabled(False)
        self.descending_order_button.setEnabled(False)
        self.reset_order_button.setEnabled(False)
        self.close_collection_action.setEnabled(False)
        if self._session_enabled:
            self._session_locations = []
            self._settings.remove(SESSION_LOCATIONS_SETTING)
            self._settings.remove(SESSION_RECORD_SETTING)
            self._settings.remove(SESSION_NODE_PATH_SETTING)
            self._settings.sync()
        self.statusBar().showMessage("Closed context collection", 3000)

    def add_locations(
        self, locations: Sequence[str | Path], *, append: bool = False
    ) -> None:
        """Discover locations off-thread and replace or extend the collection."""
        locations = [
            (
                str(location)
                if isinstance(location, str) and "://" in location
                else str(Path(location).expanduser().resolve())
            )
            for location in locations
        ]
        if self._session_enabled:
            for location in locations:
                if location not in self._session_locations:
                    self._session_locations.append(location)
            self._settings.setValue(SESSION_LOCATIONS_SETTING, self._session_locations)
            recent = self._setting_strings(RECENT_LOCATIONS_SETTING)
            for location in reversed(locations):
                if location in recent:
                    recent.remove(location)
                recent.insert(0, location)
            self._settings.setValue(
                RECENT_LOCATIONS_SETTING, recent[:MAX_RECENT_LOCATIONS]
            )
            self._settings.sync()
            self._rebuild_recent_menu()
        collection_generation = self._collection_generation
        self._begin_operation("discovery", "scanning context files")

        def discovery_failed(details: str) -> None:
            self._finish_operation("discovery")
            self._show_error("Could not scan locations", details)

        self.tasks.submit(
            lambda: self.source.discover(locations),
            lambda records: self._records_discovered(
                records, collection_generation, append=append
            ),
            discovery_failed,
        )

    def _records_discovered(
        self,
        records: list[RecordRef],
        collection_generation: int | None = None,
        *,
        append: bool = False,
    ) -> None:
        if (
            collection_generation is not None
            and collection_generation != self._collection_generation
        ):
            return
        selected_id = (
            self._current_document.record.id
            if self._current_document is not None
            else self._selected_record_id()
        )
        if append:
            merged = {record.id: record for record in self.file_model.records}
            merged.update({record.id: record for record in records})
            records = list(merged.values())
        self.file_model.set_records(records)
        self.extraction.clear_collection_sort()
        count = len(self.file_model.records)
        self.file_count.setText(f"{count} file{'s' if count != 1 else ''}")
        self.ascending_order_button.setEnabled(count > 1)
        self.descending_order_button.setEnabled(count > 1)
        self.close_collection_action.setEnabled(count > 0)
        self._finish_operation("discovery", f"Found {len(records)} context files")
        self._refresh_collection_analysis()
        self._refresh_extraction_preview(self.extraction.selectors)
        if not self.file_list.currentIndex().isValid() and count:
            root_index = self.file_proxy.index(0, 0)
            self.file_list.expand(root_index)
            source_index = self.file_model.first_record_index()
            if selected_id is not None:
                selected_index = self.file_model.index_for_record_id(selected_id)
                if selected_index.isValid():
                    source_index = selected_index
            if self._restored_record_path is not None:
                restored = next(
                    (
                        record
                        for record in self.file_model.records
                        if str(record.path.resolve()) == self._restored_record_path
                    ),
                    None,
                )
                if restored is not None:
                    restored_index = self.file_model.index_for_record_id(restored.id)
                    if restored_index.isValid():
                        source_index = restored_index
                self._restored_record_path = None
            file_index = self.file_proxy.mapFromSource(source_index)
            parent = file_index.parent()
            while parent.isValid():
                self.file_list.expand(parent)
                parent = parent.parent()
            self.file_list.setCurrentIndex(file_index)

    def _evaluation_selected(
        self, selected: QItemSelection, _deselected: QItemSelection
    ) -> None:
        indexes = selected.indexes()
        if not indexes:
            self._set_evaluation_path(None)
            return
        source_index = self.file_proxy.mapToSource(indexes[0])
        record = self.file_model.record(source_index)
        if record is None:
            self._set_evaluation_path(None)
            return
        self._set_evaluation_path(record)
        self.collection_plot.select_record(record.id)
        self._load_generation += 1
        generation = self._load_generation
        self._current_document = None
        self.tree_model.set_document(None)
        self.graph.set_document(None)
        if self.search_scope.currentData() == "current":
            self._search_generation += 1
            self.search_panel.set_available(False)
        self.inspector.clear()
        self.node_count.setText("loading…")
        self._begin_operation(
            "document",
            f"loading {record.name}",
            display_delay_ms=DOCUMENT_ACTIVITY_DELAY_MS,
        )
        self.tasks.submit(
            lambda: self._load_document(record),
            lambda document: self._document_loaded(generation, document),
            lambda details: self._load_failed(generation, record, details),
        )

    def _load_document(self, record: RecordRef) -> ContextDocument:
        document = self.source.configure_document(self.source.load(record))
        # Warm this collection-wide metadata scan off the GUI thread.
        _ = document.component_label_metadata_keys
        return document

    def _document_loaded(  # noqa: PLR0912, PLR0915
        self, generation: int, document: ContextDocument
    ) -> None:
        """Install a loaded document only if it is still the selected request."""
        if generation != self._load_generation:
            # A previous record load completed after another selection. The
            # source cache may keep its useful document, but the UI must not.
            return
        self._finish_operation("document")
        document.set_component_label_metadata_key(self._component_label_metadata_key)
        self._current_document = document
        current_key = self.component_label_key.currentText()
        existing_keys = {
            self.component_label_key.itemText(index)
            for index in range(self.component_label_key.count())
        }
        for key in document.component_label_metadata_keys:
            if key not in existing_keys:
                self.component_label_key.addItem(key)
        self.component_label_key.setEditText(current_key)
        self.tree_model.set_document(document)
        self.graph.set_document(document)
        if self.search_scope.currentData() == "current":
            self.search_panel.set_available(True)
        if (
            self.field_search.text().strip()
            and self.search_scope.currentData() == "current"
        ):
            self._run_field_search()
        if self.hierarchy_tabs.currentWidget() is self.graph:
            self.graph.ensure_rendered()
        self.node_count.setText(f"{document.node_count} nodes")
        self.statusBar().showMessage(f"Loaded {document.record.name}", 3000)
        if self._sticky_node_path and not self._sticky_node_trail:
            try:
                self._sticky_node_trail = component_trail_for_path(
                    document, self._sticky_node_path
                )
            except ContextSourceError:
                self._sticky_node_path = ()
        resolution = resolve_component_selection(
            document, self._sticky_node_path, self._sticky_node_trail
        )
        if resolution.complete:
            self._sticky_node_path = resolution.path
            self._select_context_path(resolution.path, remember=False)
            if resolution.path:
                selected_label = document.component_label(resolution.path)
                self.statusBar().showMessage(
                    f"Loaded {document.record.name}; restored {selected_label}",
                    3500,
                )
        else:
            self._select_context_path(resolution.path, remember=False)
            available_label = (
                document.component_label(resolution.path)
                if resolution.path
                else "evaluation root"
            )
            target_label = self._sticky_node_trail[-1].name
            self.statusBar().showMessage(
                f"Loaded {document.record.name}; {target_label} is unavailable "
                f"below {available_label} (selection retained)",
                5000,
            )
        # Search, analysis, and plot views select the target record first. Their
        # node/value focus is deliberately deferred until that record's document
        # is installed, otherwise it would address the previous document.
        has_pending_navigation = (
            (
                self._pending_search_hit is not None
                and self._pending_search_hit.record_id == document.record.id
            )
            or (
                self._pending_failure is not None
                and self._pending_failure.record_id == document.record.id
            )
            or (
                self._pending_plot_selection is not None
                and self._pending_plot_selection[0] == document.record.id
            )
        )
        if not has_pending_navigation:
            self._record_navigation(resolution.path)
        self._navigation_replaying = False
        pending_hit = self._pending_search_hit
        if pending_hit is not None and pending_hit.record_id == document.record.id:
            self._pending_search_hit = None
            self._focus_search_hit(pending_hit)
        pending_failure = self._pending_failure
        if (
            pending_failure is not None
            and pending_failure.record_id == document.record.id
        ):
            self._pending_failure = None
            self._focus_failure_occurrence(pending_failure)
        pending_plot = self._pending_plot_selection
        if pending_plot is not None and pending_plot[0] == document.record.id:
            self._pending_plot_selection = None
            self._focus_plot_selection(pending_plot[1])

    def _load_failed(self, generation: int, record: RecordRef, details: str) -> None:
        if generation != self._load_generation:
            return
        self._finish_operation("document")
        self._navigation_replaying = False
        if (
            self._pending_search_hit is not None
            and self._pending_search_hit.record_id == record.id
        ):
            self._pending_search_hit = None
        if (
            self._pending_failure is not None
            and self._pending_failure.record_id == record.id
        ):
            self._pending_failure = None
        if (
            self._pending_plot_selection is not None
            and self._pending_plot_selection[0] == record.id
        ):
            self._pending_plot_selection = None
        self.node_count.setText("error")
        self._show_error(f"Could not load {record.name}", details)

    def _context_selected(
        self, selected: QItemSelection, _deselected: QItemSelection
    ) -> None:
        if self._current_document is None:
            return
        indexes = selected.indexes()
        if not indexes:
            return
        info = self.tree_model.node_info(indexes[0])
        if info is None:
            return
        self.inspector.set_details(
            self._current_document.details(info.path),
            self._current_document.component_label(info.path),
        )
        self.graph.select_path(info.path)
        if not self._suppress_context_memory:
            self._remember_context_selection(info.path)

    def _hierarchy_tab_changed(self, index: int) -> None:
        if self.hierarchy_tabs.widget(index) is self.graph:
            self.graph.ensure_rendered()

    def _toggle_data_flow_expansion(self) -> None:
        if self._expanded_data_flow_dialog is not None:
            self._restore_data_flow_panel()
            return
        tab_index = self.hierarchy_tabs.indexOf(self.graph)
        if tab_index >= 0:
            self._data_flow_tab_index = tab_index
            self.hierarchy_tabs.removeTab(tab_index)
        placeholder = QWidget()
        placeholder.setObjectName("dataFlowDetachedPlaceholder")
        placeholder_layout = QVBoxLayout(placeholder)
        placeholder_layout.addStretch()
        message = QLabel("Data Flow is currently open in a detached window.")
        message.setObjectName("dataFlowDetachedMessage")
        message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        message.setWordWrap(True)
        placeholder_layout.addWidget(message)
        return_button = QPushButton("Return Data Flow to dashboard")
        return_button.setObjectName("returnDataFlowButton")
        return_button.clicked.connect(
            lambda _checked=False: self._restore_data_flow_panel()
        )
        placeholder_layout.addWidget(
            return_button, alignment=Qt.AlignmentFlag.AlignHCenter
        )
        placeholder_layout.addStretch()
        self._data_flow_placeholder = placeholder
        self.hierarchy_tabs.insertTab(
            self._data_flow_tab_index, placeholder, "Data Flow"
        )
        self.hierarchy_tabs.setCurrentWidget(placeholder)

        dialog = QDialog(self, Qt.WindowType.Window)
        dialog.setWindowTitle("ChemFit Data Flow")
        dialog.setModal(False)
        dialog_layout = QVBoxLayout(dialog)
        dialog_layout.setContentsMargins(12, 12, 12, 12)
        dialog_layout.addWidget(self.graph)
        # QTabWidget keeps a removed page explicitly hidden. Reparenting alone
        # therefore leaves an empty dialog unless the page is shown again.
        self.graph.show()
        dialog.finished.connect(
            lambda _result, target=dialog: self._restore_data_flow_panel(target)
        )
        self._expanded_data_flow_dialog = dialog
        self.graph.set_expanded(True)
        self.graph.ensure_rendered()
        dialog.resize(max(1100, self.width() - 80), max(720, self.height() - 80))
        dialog.showMaximized()

    def _restore_data_flow_panel(self, dialog: QDialog | None = None) -> None:
        expanded = self._expanded_data_flow_dialog
        if expanded is None or (dialog is not None and dialog is not expanded):
            return
        self._expanded_data_flow_dialog = None
        layout = expanded.layout()
        if layout is not None:
            layout.removeWidget(self.graph)
        placeholder = self._data_flow_placeholder
        placeholder_was_current = (
            placeholder is not None
            and self.hierarchy_tabs.currentWidget() is placeholder
        )
        if placeholder is not None:
            placeholder_index = self.hierarchy_tabs.indexOf(placeholder)
            if placeholder_index >= 0:
                self._data_flow_tab_index = placeholder_index
                self.hierarchy_tabs.removeTab(placeholder_index)
            placeholder.deleteLater()
        self._data_flow_placeholder = None
        insert_at = min(self._data_flow_tab_index, self.hierarchy_tabs.count())
        self._data_flow_tab_index = self.hierarchy_tabs.insertTab(
            insert_at, self.graph, "Data Flow"
        )
        if placeholder_was_current:
            self.hierarchy_tabs.setCurrentWidget(self.graph)
        self.graph.set_expanded(False)
        expanded.hide()
        expanded.deleteLater()

    def _toggle_plot_expansion(self) -> None:
        if self._expanded_plot_dialog is not None:
            self._restore_plot_panel()
            return
        tab_index = self.hierarchy_tabs.indexOf(self.collection_plot)
        if tab_index >= 0:
            self._plot_tab_index = tab_index
            self.hierarchy_tabs.removeTab(tab_index)
        placeholder = QWidget()
        placeholder.setObjectName("plotDetachedPlaceholder")
        placeholder_layout = QVBoxLayout(placeholder)
        placeholder_layout.addStretch()
        message = QLabel("Plot is currently open in a detached window.")
        message.setObjectName("plotDetachedMessage")
        message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        placeholder_layout.addWidget(message)
        return_button = QPushButton("Return Plot to dashboard")
        return_button.setObjectName("returnPlotButton")
        return_button.clicked.connect(lambda _checked=False: self._restore_plot_panel())
        placeholder_layout.addWidget(
            return_button, alignment=Qt.AlignmentFlag.AlignHCenter
        )
        placeholder_layout.addStretch()
        self._plot_placeholder = placeholder
        self.hierarchy_tabs.insertTab(self._plot_tab_index, placeholder, "Plot")
        self.hierarchy_tabs.setCurrentWidget(placeholder)

        dialog = QDialog(self, Qt.WindowType.Window)
        dialog.setWindowTitle("ChemFit Collection Plot")
        dialog.setModal(False)
        dialog_layout = QVBoxLayout(dialog)
        dialog_layout.setContentsMargins(12, 12, 12, 12)
        dialog_layout.addWidget(self.collection_plot)
        self.collection_plot.show()
        dialog.finished.connect(
            lambda _result, target=dialog: self._restore_plot_panel(target)
        )
        self._expanded_plot_dialog = dialog
        self.collection_plot.set_expanded(True)
        dialog.resize(max(1100, self.width() - 80), max(720, self.height() - 80))
        dialog.showMaximized()

    def _restore_plot_panel(self, dialog: QDialog | None = None) -> None:
        expanded = self._expanded_plot_dialog
        if expanded is None or (dialog is not None and dialog is not expanded):
            return
        self._expanded_plot_dialog = None
        layout = expanded.layout()
        if layout is not None:
            layout.removeWidget(self.collection_plot)
        placeholder = self._plot_placeholder
        placeholder_was_current = (
            placeholder is not None
            and self.hierarchy_tabs.currentWidget() is placeholder
        )
        if placeholder is not None:
            placeholder_index = self.hierarchy_tabs.indexOf(placeholder)
            if placeholder_index >= 0:
                self._plot_tab_index = placeholder_index
                self.hierarchy_tabs.removeTab(placeholder_index)
            placeholder.deleteLater()
        self._plot_placeholder = None
        insert_at = min(self._plot_tab_index, self.hierarchy_tabs.count())
        self._plot_tab_index = self.hierarchy_tabs.insertTab(
            insert_at, self.collection_plot, "Plot"
        )
        if placeholder_was_current:
            self.hierarchy_tabs.setCurrentWidget(self.collection_plot)
        self.collection_plot.set_expanded(False)
        expanded.hide()
        expanded.deleteLater()

    def _graph_context_selected(self, path: NodePath) -> None:
        if self._current_document is not None:
            self._select_context_path(path, remember=True)

    def _remember_context_selection(self, path: NodePath) -> None:
        if self._current_document is None:
            return
        try:
            trail = component_trail_for_path(self._current_document, path)
        except ContextSourceError:
            return
        self._sticky_node_path = path
        self._sticky_node_trail = trail
        self._record_navigation(path)

    def _record_navigation(self, path: NodePath) -> None:
        if self._navigation_replaying or self._current_document is None:
            return
        location = (self._current_document.record.id, path)
        if (
            0 <= self._navigation_index < len(self._navigation_history)
            and self._navigation_history[self._navigation_index] == location
        ):
            return
        del self._navigation_history[self._navigation_index + 1 :]
        self._navigation_history.append(location)
        if len(self._navigation_history) > 200:
            self._navigation_history.pop(0)
        self._navigation_index = len(self._navigation_history) - 1
        self._update_navigation_actions()

    def _update_navigation_actions(self) -> None:
        if not hasattr(self, "back_action"):
            return
        self.back_action.setEnabled(self._navigation_index > 0)
        self.forward_action.setEnabled(
            0 <= self._navigation_index < len(self._navigation_history) - 1
        )

    def _navigate_history(self, offset: int) -> None:
        target_index = self._navigation_index + offset
        if not 0 <= target_index < len(self._navigation_history):
            return
        previous_index = self._navigation_index
        record_id, path = self._navigation_history[target_index]
        self._navigation_index = target_index
        self._update_navigation_actions()
        self._navigation_replaying = True
        self._sticky_node_path = path
        self._sticky_node_trail = ()
        document = self._current_document
        if document is not None and document.record.id == record_id:
            if self._select_context_path(path, remember=False):
                with suppress(ContextSourceError):
                    self._sticky_node_trail = component_trail_for_path(document, path)
            self._navigation_replaying = False
            return
        if not self._select_record_by_id(record_id):
            self._navigation_index = previous_index
            self._navigation_replaying = False
            self._update_navigation_actions()

    def _select_context_path(self, path: NodePath, *, remember: bool) -> bool:
        """Synchronize tree, inspector, and Data Flow on one document path."""
        if self._current_document is None:
            return False
        tree_index = self.tree_model.index_for_path(path)
        if not tree_index.isValid():
            return False
        previous_suppression = self._suppress_context_memory
        self._suppress_context_memory = not remember
        try:
            parent = tree_index.parent()
            while parent.isValid():
                self.tree.expand(parent)
                parent = parent.parent()
            self.tree.setCurrentIndex(tree_index)
            self.tree.scrollTo(tree_index)
            self.inspector.set_details(
                self._current_document.details(path),
                self._current_document.component_label(path),
            )
            self.graph.select_path(path)
        finally:
            self._suppress_context_memory = previous_suppression
        if remember:
            self._remember_context_selection(path)
        return True

    def _refresh_collection_analysis(self, *, force: bool = False) -> None:
        records = list(self.file_model.records)
        self._cancel_operation("analysis")
        self._analysis_generation += 1
        generation = self._analysis_generation
        if not records:
            self.collection_analysis.clear()
            return
        if not force and len(records) > AUTO_ANALYSIS_RECORD_LIMIT:
            self.collection_analysis.show_deferred(len(records))
            return
        operation = self._begin_determinate_operation(
            "analysis", "analyzing collection", len(records)
        )
        self.collection_analysis.show_loading(len(records))

        def load_summary() -> CollectionSummary:
            return analyze_collection(self._collection_documents(records, operation))

        self.tasks.submit(
            load_summary,
            lambda summary: self._analysis_loaded(generation, summary),
            lambda details: self._analysis_failed(generation, details),
        )

    def _analysis_loaded(self, generation: int, summary: CollectionSummary) -> None:
        if generation == self._analysis_generation:
            self._finish_operation("analysis", "Finished analyzing collection")
            self.collection_analysis.set_summary(summary)

    def _analysis_failed(self, generation: int, details: str) -> None:
        if generation != self._analysis_generation:
            return
        self._finish_operation("analysis")
        logger.error("Could not analyze context collection\n%s", details)
        self.collection_analysis.show_error("Could not build the collection summary.")

    def _failure_occurrence_selected(self, occurrence: FailureOccurrence) -> None:
        if (
            self._current_document is not None
            and self._current_document.record.id == occurrence.record_id
        ):
            self._focus_failure_occurrence(occurrence)
            return
        self._pending_failure = occurrence
        if not self._select_record_by_id(occurrence.record_id):
            self._pending_failure = None

    def _focus_failure_occurrence(self, occurrence: FailureOccurrence) -> None:
        if self._current_document is None:
            return
        try:
            details = self._current_document.details(occurrence.node_path)
        except ContextSourceError:
            return
        self.inspector.set_details(
            details,
            self._current_document.component_label(occurrence.node_path),
        )
        tree_index = self.tree_model.index_for_path(occurrence.node_path)
        if tree_index.isValid():
            self.tree.setCurrentIndex(tree_index)
            self.tree.scrollTo(tree_index)
        self.graph.select_path(occurrence.node_path)
        self.hierarchy_tabs.setCurrentWidget(self.tree)
        self.workspace_tabs.setCurrentWidget(self.context_workspace)
        self._remember_context_selection(occurrence.node_path)
        self.statusBar().showMessage(
            f"{occurrence.record_name}: {occurrence.exception_type} in "
            f"{occurrence.component}",
            5000,
        )

    def _select_record_by_id(self, record_id: str) -> bool:
        source_index = self.file_model.index_for_record_id(record_id)
        if not source_index.isValid():
            return False
        proxy_index = self.file_proxy.mapFromSource(source_index)
        if not proxy_index.isValid() and self.search.text():
            self.search.clear()
            proxy_index = self.file_proxy.mapFromSource(source_index)
        if not proxy_index.isValid():
            return False
        self.file_list.setCurrentIndex(proxy_index)
        self.file_list.scrollTo(proxy_index)
        return True

    def _plot_context_selected(self, record_id: str) -> None:
        self._select_record_by_id(record_id)

    def _plot_point_selected(
        self, record_id: str, selector: NumberSelector | object
    ) -> None:
        if not isinstance(selector, NumberSelector):
            return
        if (
            self._current_document is not None
            and self._current_document.record.id == record_id
        ):
            self._focus_plot_selection(selector)
            return
        self._pending_plot_selection = (record_id, selector)
        if not self._select_record_by_id(record_id):
            self._pending_plot_selection = None

    def _focus_plot_selection(self, selector: NumberSelector) -> None:
        document = self._current_document
        if document is None:
            return
        if selector.node_trail is None:
            path = selector.node_path
            try:
                document.details(path)
            except ContextSourceError:
                return
        else:
            resolution = resolve_component_selection(
                document, selector.node_path, selector.node_trail
            )
            if not resolution.complete:
                self._select_context_path(resolution.path, remember=True)
                return
            path = resolution.path
        if self._select_context_path(path, remember=True):
            self.inspector.focus_value(selector.section, selector.value_path)

    def _field_search_changed(self, text: str) -> None:
        self._cancel_operation("search")
        if text.strip():
            self._search_generation += 1
            if self.workspace_tabs.currentWidget() is not self.search_workspace:
                self._tab_before_search = self.workspace_tabs.currentWidget()
            self.workspace_tabs.setCurrentWidget(self.search_workspace)
            self._search_timer.start()
        else:
            self._search_timer.stop()
            self._search_generation += 1
            self.search_panel.show_message(
                "Type above to search context fields and values."
            )
            # Stay in Search when the final character is deleted. The explicit
            # Escape action handles returning to the previous workspace.

    def _focus_field_search(self) -> None:
        current = self.workspace_tabs.currentWidget()
        if current is not self.search_workspace:
            self._tab_before_search = current
            self.workspace_tabs.setCurrentWidget(self.search_workspace)
        self.field_search.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.field_search.selectAll()

    def _clear_field_search(self) -> None:
        if self.field_search.text():
            self.field_search.clear()
            if (
                self.workspace_tabs.currentWidget() is self.search_workspace
                and self._tab_before_search is not None
            ):
                self.workspace_tabs.setCurrentWidget(self._tab_before_search)
                self._tab_before_search = None
            return
        if (
            self.workspace_tabs.currentWidget() is self.search_workspace
            and self._tab_before_search is not None
        ):
            self.workspace_tabs.setCurrentWidget(self._tab_before_search)
            self._tab_before_search = None

    def _run_field_search(self) -> None:
        query = self.field_search.text().strip()
        if not query:
            return
        mode: SearchMode = self.search_mode.currentData()
        if mode == "regex":
            try:
                re.compile(query, re.IGNORECASE)
            except re.error as error:
                self._search_generation += 1
                self.search_panel.show_message(f"Invalid regular expression: {error}")
                return
        include_keys = self.search_keys.isChecked()
        include_values = self.search_values.isChecked()
        include_metadata = self.search_metadata.isChecked()
        include_components = self.search_components.isChecked()
        scope = self.search_scope.currentData()
        if scope == "collection":
            records = list(self.file_model.records)
            current_document = self._current_document
            scope_label = "whole collection"
        else:
            if self._current_document is None:
                self.search_panel.show_message("Select an evaluation to search it.")
                return
            records = [self._current_document.record]
            current_document = self._current_document
            scope_label = "current evaluation"
        if not records:
            self.search_panel.show_message("No context collection is loaded.")
            return

        self._search_generation += 1
        generation = self._search_generation
        count = len(records)
        operation = self._begin_determinate_operation(
            "search", "searching collection", count
        )
        cancellation = operation.cancellation
        self.search_panel.show_loading(
            f"Searching {count:,} evaluation{'s' if count != 1 else ''} "
            "in the background…"
        )

        def search_records() -> tuple[list[SearchHit], int, bool]:
            if scope == "collection":
                result = self.collection_search.search(
                    records,
                    SearchOptions(
                        query=query,
                        include_keys=include_keys,
                        include_values=include_values,
                        include_metadata=include_metadata,
                        include_components=include_components,
                        mode=mode,
                    ),
                    component_label_key=self._component_label_metadata_key,
                    cancellation=cancellation,
                    progress=lambda value, _total: operation.progress(value, None),
                )
                return result.hits, result.failed_records, result.cancelled
            hits = TopKSearchHits(MAX_RESULTS)
            failed_records = 0
            for completed, record in enumerate(records, start=1):
                if cancellation.cancelled:
                    return [], failed_records, True
                try:
                    document = (
                        current_document
                        if current_document is not None
                        and current_document.record.id == record.id
                        else self.source.configure_document(self.source.load(record))
                    )
                    index = self._cached_search_index(
                        document,
                        cooperative=True,
                        cancelled=lambda: cancellation.cancelled,
                    )
                    if cancellation.cancelled:
                        return [], failed_records, True
                    hits.extend(
                        index.search(
                            query,
                            include_keys=include_keys,
                            include_values=include_values,
                            include_metadata=include_metadata,
                            include_components=include_components,
                            mode=mode,
                            cooperative=True,
                            cancelled=lambda: cancellation.cancelled,
                        )
                    )
                except ContextSourceError:
                    failed_records += 1
                finally:
                    if not cancellation.cancelled:
                        operation.progress(completed, None)
            return hits.results(), failed_records, False

        self.tasks.submit(
            search_records,
            lambda result: self._search_loaded(
                generation,
                result,
                query,
                include_keys,
                include_values,
                include_metadata,
                include_components,
                mode,
                scope_label,
                count,
            ),
            lambda details: self._search_failed(generation, details),
        )

    def _search_options_toggled(self, _checked: bool) -> None:
        if self.field_search.text().strip():
            self._cancel_operation("search")
            self._search_generation += 1
            self._search_timer.start()

    def _search_mode_changed(self, _index: int) -> None:
        if self.field_search.text().strip():
            self._cancel_operation("search")
            self._search_generation += 1
            self._search_timer.start()

    def _search_scope_changed(self, _index: int) -> None:
        if self.field_search.text().strip():
            self._cancel_operation("search")
            self._search_generation += 1
            self._search_timer.start()

    def _cached_search_index(
        self,
        document: ContextDocument,
        *,
        cooperative: bool = False,
        cancelled: Callable[[], bool] | None = None,
    ) -> ContextSearchIndex:
        """Return the cached local index used for current-evaluation search."""
        return self.search_indexes.index_for(
            document,
            cooperative=cooperative,
            cancelled=cancelled,
        )

    def _search_loaded(
        self,
        generation: int,
        result: tuple[list[SearchHit], int, bool],
        query: str,
        include_keys: bool,
        include_values: bool,
        include_metadata: bool,
        include_components: bool,
        mode: SearchMode,
        scope_label: str,
        searched_records: int,
    ) -> None:
        """Publish search results only while query and collection are current."""
        if generation != self._search_generation:
            return
        self._finish_operation("search", "Finished searching collection")
        hits, failed_records, cancelled = result
        if cancelled:
            return
        self.search_panel.show_results(
            hits,
            query,
            include_keys=include_keys,
            include_values=include_values,
            include_metadata=include_metadata,
            include_components=include_components,
            mode=mode,
            scope_label=scope_label,
            searched_records=searched_records,
            failed_records=failed_records,
        )

    def _search_failed(self, generation: int, details: str) -> None:
        if generation != self._search_generation:
            return
        self._finish_operation("search")
        logger.error("Could not search context collection\n%s", details)
        self.search_panel.show_message("Could not search the context collection.")

    def _search_hit_selected(self, hit: SearchHit) -> None:
        if hit.record_id and (
            self._current_document is None
            or hit.record_id != self._current_document.record.id
        ):
            source_index = self.file_model.index_for_record_id(hit.record_id)
            if not source_index.isValid():
                return
            proxy_index = self.file_proxy.mapFromSource(source_index)
            if not proxy_index.isValid() and self.search.text():
                self.search.clear()
                proxy_index = self.file_proxy.mapFromSource(source_index)
            if not proxy_index.isValid():
                return
            self._pending_search_hit = hit
            self.file_list.setCurrentIndex(proxy_index)
            self.file_list.scrollTo(proxy_index)
            return
        if self._current_document is None:
            return
        self._focus_search_hit(hit)

    def _focus_search_hit(self, hit: SearchHit) -> None:
        if self._current_document is None:
            return
        self.inspector.set_details(
            self._current_document.details(hit.node_path),
            self._current_document.component_label(hit.node_path),
        )
        tree_index = self.tree_model.index_for_path(hit.node_path)
        if tree_index.isValid():
            self.tree.setCurrentIndex(tree_index)
            self.tree.scrollTo(tree_index)
        self.graph.select_path(hit.node_path)
        self.hierarchy_tabs.setCurrentWidget(self.tree)
        self.workspace_tabs.setCurrentWidget(self.context_workspace)
        self._remember_context_selection(hit.node_path)
        if hit.section is not None:
            self.inspector.focus_value(hit.section, hit.value_path)

    def _plot_requested(self, selector: NumberSelector) -> None:
        if self._current_document is not None:
            selector = selector_for_document(self._current_document, selector)
        self._active_plot_selector = selector
        self._active_plot_selectors = [selector]
        self._load_collection_plot((selector,), activate=True)

    def _plot_add_requested(self, selector: NumberSelector) -> None:
        if self._current_document is not None:
            selector = selector_for_document(self._current_document, selector)
        if selector not in self._active_plot_selectors:
            self._active_plot_selectors.append(selector)
        self._active_plot_selector = self._active_plot_selectors[0]
        self._load_collection_plot(tuple(self._active_plot_selectors), activate=True)

    def _load_collection_plot(
        self,
        selectors: NumberSelector | tuple[NumberSelector, ...],
        *,
        activate: bool,
    ) -> None:
        selected = (selectors,) if isinstance(selectors, NumberSelector) else selectors
        records = list(self.file_model.records)
        self._cancel_operation("plot")
        self._plot_generation += 1
        generation = self._plot_generation
        if activate:
            self.hierarchy_tabs.setCurrentWidget(self.collection_plot)
        self.collection_plot.show_loading(selected, len(records))
        if not records:
            self.collection_plot.show_error("No context collection is loaded.")
            return
        operation = self._begin_determinate_operation(
            "plot", "loading plot values", len(records)
        )

        def load_series() -> tuple[CollectionSeries, ...]:
            return build_collection_series_multi(
                selected, self._collection_documents(records, operation)
            )

        self.tasks.submit(
            load_series,
            lambda data: self._plot_loaded(generation, data),
            lambda details: self._plot_failed(generation, details),
        )

    def _refresh_active_plot(self) -> None:
        if self._active_plot_selectors:
            self._load_collection_plot(
                tuple(self._active_plot_selectors), activate=False
            )
        elif self._active_plot_selector is not None:
            self._active_plot_selectors = [self._active_plot_selector]
            self._load_collection_plot((self._active_plot_selector,), activate=False)

    def _plot_loaded(
        self,
        generation: int,
        data: CollectionSeries | tuple[CollectionSeries, ...],
    ) -> None:
        if generation == self._plot_generation:
            self._finish_operation("plot", "Finished loading plot values")
            collection = (data,) if isinstance(data, CollectionSeries) else data
            self.collection_plot.set_series_collection(collection)
            self.collection_plot.select_record(self._selected_record_id())

    def _plot_failed(self, generation: int, details: str) -> None:
        if generation != self._plot_generation:
            return
        self._finish_operation("plot")
        logger.error("Could not build collection plot\n%s", details)
        self.collection_plot.show_error("Could not build the collection plot.")

    def _remove_plot_series(self, selector: NumberSelector) -> None:
        self._active_plot_selectors = [
            active for active in self._active_plot_selectors if active != selector
        ]
        self._active_plot_selector = (
            self._active_plot_selectors[0] if self._active_plot_selectors else None
        )
        if self._active_plot_selectors:
            self._load_collection_plot(
                tuple(self._active_plot_selectors), activate=False
            )
        else:
            self.collection_plot.clear()

    def _clear_plot_series(self) -> None:
        self._cancel_operation("plot")
        self._plot_generation += 1
        self._active_plot_selector = None
        self._active_plot_selectors = []
        self.collection_plot.clear()

    def _order_requested(
        self, selector: NumberSelector, *, descending: bool = False
    ) -> None:
        if self._current_document is not None:
            selector = selector_for_document(self._current_document, selector)
        records = list(self.file_model.filename_ordered_records)
        if not records:
            self.statusBar().showMessage("No context collection is loaded.", 3000)
            return
        self.extraction.clear_collection_sort()
        self._order_generation += 1
        generation = self._order_generation
        operation = self._begin_determinate_operation(
            "order", "loading sort values", len(records)
        )
        self.order_status.setText("Order: loading selected values…")
        self.statusBar().showMessage(
            f"Ordering {len(records):,} contexts by {selector.label}…"
        )

        def load_order_values() -> tuple[list[RecordRef], CollectionSeries]:
            return records, build_collection_series(
                selector, self._collection_documents(records, operation)
            )

        self.tasks.submit(
            load_order_values,
            lambda result: self._order_loaded(
                generation, *result, descending=descending
            ),
            lambda details: self._order_failed(generation, details),
        )

    def _order_loaded(
        self,
        generation: int,
        records: list[RecordRef],
        data: CollectionSeries,
        *,
        descending: bool = False,
    ) -> None:
        if generation != self._order_generation:
            return
        self._finish_operation("order")
        selected_id = self._selected_record_id()
        sorted_points = sorted(
            data.points, key=lambda point: (point.value, point.index)
        )
        present_indices = {point.index for point in sorted_points}
        self._numeric_order_present = tuple(
            records[point.index].id for point in sorted_points
        )
        self._numeric_order_missing = tuple(
            records[index].id
            for index in range(len(records))
            if index not in present_indices
        )
        self._numeric_order_label = data.selector.label
        self._order_descending = False
        self._filename_reversed = False
        self.file_model.set_record_order(
            (*self._numeric_order_present, *self._numeric_order_missing)
        )
        self._restore_record_selection(selected_id)
        self._refresh_active_plot()
        self._refresh_extraction_preview(self.extraction.selectors)
        self.order_status.setText(f"Order: {data.selector.label} ↑")
        self.order_status.setToolTip(
            f"Ascending numeric order by {data.selector.label}; missing values last"
        )
        self.reset_order_button.setEnabled(True)
        self.ascending_order_button.setEnabled(len(records) > 1)
        self.descending_order_button.setEnabled(len(records) > 1)
        missing = data.missing_count
        suffix = f"; {missing:,} missing placed last" if missing else ""
        self.statusBar().showMessage(
            f"Ordered {len(data.points):,} contexts by selected number{suffix}", 5000
        )
        if descending:
            self._set_collection_order(descending=True)

    def _order_failed(self, generation: int, details: str) -> None:
        if generation != self._order_generation:
            return
        self._finish_operation("order")
        self.extraction.clear_collection_sort()
        logger.error("Could not order context collection\n%s", details)
        self.order_status.setText("Order: filename")
        self.statusBar().showMessage("Could not order the context collection.", 5000)

    def _order_extraction_requested(self, request: object, descending: bool) -> None:
        if not (
            isinstance(request, tuple)
            and len(request) == 2
            and isinstance(request[0], FieldSelector)
        ):
            return
        selector, nested_path = request
        path = nested_path if isinstance(nested_path, tuple) else ()
        records = list(self.file_model.filename_ordered_records)
        if not records:
            self._show_status("No context collection is loaded.", 3000)
            return
        self._order_generation += 1
        generation = self._order_generation
        operation = self._begin_determinate_operation(
            "order",
            f"sorting by extraction column {selector.column}",
            len(records),
        )

        def load_values():
            present: list[tuple[tuple, int, str]] = []
            missing: list[str] = []
            for row in extract_fields(
                self.source,
                records,
                (selector,),
                cancellation=operation.cancellation,
                progress=operation.progress,
            ):
                extracted = row.values[0]
                value = extracted.value
                for part in path:
                    if isinstance(part, str) and isinstance(value, dict):
                        value = value.get(part)
                    elif (
                        isinstance(part, int)
                        and isinstance(value, list)
                        and 0 <= part < len(value)
                    ):
                        value = value[part]
                    else:
                        value = None
                        break
                if extracted.status != ExtractionStatus.AVAILABLE or value is None:
                    missing.append(row.record_id)
                    continue
                if isinstance(value, float) and math.isnan(value):
                    missing.append(row.record_id)
                    continue
                if not isinstance(value, (bool, int, float, str)):
                    message = (
                        f"Extraction column {selector.column!r} contains non-scalar "
                        "values; flatten it or select a scalar field before sorting"
                    )
                    raise ValueError(message)
                present.append((_sortable_value(value), row.index, row.record_id))
            present.sort(key=lambda item: (item[0], item[1]))
            return tuple(item[2] for item in present), tuple(missing)

        self.tasks.submit(
            load_values,
            lambda result: self._extraction_order_loaded(
                generation,
                selector.column,
                result[0],
                result[1],
                descending,
            ),
            lambda details: self._order_failed(generation, details),
        )

    def _extraction_order_loaded(
        self,
        generation: int,
        label: str,
        present: tuple[str, ...],
        missing: tuple[str, ...],
        descending: bool,
    ) -> None:
        if generation != self._order_generation:
            return
        self._finish_operation("order")
        selected_id = self._selected_record_id()
        self._numeric_order_present = present
        self._numeric_order_missing = missing
        self._numeric_order_label = label
        self._filename_reversed = False
        ordered_present = tuple(reversed(present)) if descending else present
        self.file_model.set_record_order((*ordered_present, *missing))
        self._order_descending = descending
        self._restore_record_selection(selected_id)
        self._refresh_active_plot()
        self._refresh_extraction_preview(self.extraction.selectors)
        arrow = "↓" if descending else "↑"
        self.order_status.setText(f"Order: {label} {arrow}")
        self.order_status.setToolTip(
            f"Sorted by extraction column {label}; missing values last"
        )
        self.reset_order_button.setEnabled(True)
        self._show_status(f"Sorted collection by extraction column {label}", 4000)

    def _reset_collection_order(self) -> None:
        self._cancel_operation("order")
        self._order_generation += 1
        selected_id = self._selected_record_id()
        self.extraction.clear_collection_sort()
        self._clear_numeric_order()
        self._filename_reversed = False
        self.file_model.set_record_order(None)
        self._restore_record_selection(selected_id)
        self._refresh_active_plot()
        self._refresh_extraction_preview(self.extraction.selectors)
        self.order_status.setText("Order: filename")
        self.order_status.setToolTip("Natural alphanumeric filename order")
        self.reset_order_button.setEnabled(False)
        self.statusBar().showMessage("Restored filename order", 3000)

    def _set_collection_order(self, *, descending: bool) -> None:
        if len(self.file_model.records) < 2:
            return
        # A sidebar order action supersedes a still-running Inspector or
        # extraction sort just as it supersedes the visible header marker.
        self._cancel_operation("order")
        self._order_generation += 1
        self.extraction.clear_collection_sort()
        selected_id = self._selected_record_id()
        self._order_descending = descending
        if self._numeric_order_label is not None:
            present = (
                tuple(reversed(self._numeric_order_present))
                if descending
                else self._numeric_order_present
            )
            known_ids = {*present, *self._numeric_order_missing}
            newly_added = tuple(
                record.id
                for record in self.file_model.filename_ordered_records
                if record.id not in known_ids
            )
            self.file_model.set_record_order(
                (*present, *self._numeric_order_missing, *newly_added)
            )
            arrow = "↓" if descending else "↑"
            direction = "Descending" if descending else "Ascending"
            self.order_status.setText(f"Order: {self._numeric_order_label} {arrow}")
            self.order_status.setToolTip(
                f"{direction} numeric order by {self._numeric_order_label}; "
                "missing values last"
            )
        else:
            self._filename_reversed = descending
            if descending:
                filename_records = self.file_model.filename_ordered_records
                self.file_model.set_record_order(
                    record.id for record in reversed(filename_records)
                )
                self.order_status.setText("Order: filename ↓")
                self.order_status.setToolTip(
                    "Reverse natural alphanumeric filename order"
                )
                self.reset_order_button.setEnabled(True)
            else:
                self.file_model.set_record_order(None)
                self.order_status.setText("Order: filename")
                self.order_status.setToolTip("Natural alphanumeric filename order")
                self.reset_order_button.setEnabled(False)
        self._restore_record_selection(selected_id)
        self._refresh_active_plot()
        self._refresh_extraction_preview(self.extraction.selectors)
        direction = "descending" if descending else "ascending"
        self.statusBar().showMessage(f"Sorted collection in {direction} order", 3000)

    def _reverse_collection_order(self) -> None:
        """Compatibility helper for callers of the former toggle action."""
        self._set_collection_order(descending=not self._order_descending)

    def _clear_numeric_order(self) -> None:
        self._numeric_order_present = ()
        self._numeric_order_missing = ()
        self._numeric_order_label = None
        self._order_descending = False

    def _add_extraction_selector(self, selector: FieldSelector) -> None:
        if self._current_document is not None:
            selector = extraction_selector_for_document(
                self._current_document, selector
            )
        self.extraction.add_selector(selector)

    def _update_extraction_tab_badge(self, _selectors: object = None) -> None:
        count = len(self.extraction.selectors)
        count_label = "20+" if count > 20 else str(count)
        label = f"Data Extraction ({count_label})" if count else "Data Extraction"
        self.workspace_tabs.setTabText(self._extraction_tab_index, label)
        tooltip = (
            f"{count:,} fields queued for extraction"
            if count
            else "No fields queued for extraction"
        )
        self.workspace_tabs.setTabToolTip(self._extraction_tab_index, tooltip)

    def _refresh_extraction_preview(
        self, selectors: tuple[FieldSelector, ...] | object
    ) -> None:
        selected = (
            selectors if isinstance(selectors, tuple) else self.extraction.selectors
        )
        if not all(isinstance(item, FieldSelector) for item in selected):
            selected = self.extraction.selectors
        records = list(self.file_model.records)
        self._cancel_operation("extraction_preview")
        self._extraction_generation += 1
        generation = self._extraction_generation
        if not selected:
            self.extraction.preview_model.set_data((), ())
            self.extraction.status.setText(
                "Add any inspector value or component to begin."
            )
            return
        if not records:
            self.extraction.show_error("No context collection is loaded.")
            return
        operation = self._begin_determinate_operation(
            "extraction_preview",
            "building extraction preview",
            min(len(records), PREVIEW_LIMIT),
        )
        self.extraction.show_loading(len(records))

        def preview():
            return tuple(
                islice(
                    extract_fields(
                        self.source,
                        records,
                        selected,
                        cancellation=operation.cancellation,
                        progress=operation.progress,
                    ),
                    PREVIEW_LIMIT,
                )
            )

        self.tasks.submit(
            preview,
            lambda rows: self._extraction_preview_loaded(
                generation, rows, len(records)
            ),
            lambda details: self._extraction_failed(generation, details),
        )

    def _extraction_preview_loaded(
        self, generation: int, rows: tuple, total: int
    ) -> None:
        if generation == self._extraction_generation:
            self._finish_operation(
                "extraction_preview", "Finished building extraction preview"
            )
            self.extraction.set_preview(rows, total)

    def _extraction_failed(self, generation: int, details: str) -> None:
        if generation != self._extraction_generation:
            return
        self._finish_operation("extraction_preview")
        logger.error("Could not build extraction preview\n%s", details)
        self.extraction.show_error("Could not build the extraction preview.")

    def _export_extraction(
        self, path: str, format_name: str, config: ExtractionConfig
    ) -> None:
        records = list(self.file_model.records)
        export_passes = (
            2
            if format_name == "csv"
            and any(
                selector.flatten or selector.column_expansion_depth
                for selector in config.selectors
            )
            else 1
        )
        operation = self._begin_determinate_operation(
            "extraction_export",
            "exporting extracted data",
            len(records) * export_passes,
        )
        self.extraction.status.setText(
            f"Exporting {len(records):,} evaluations to {format_name.upper()}…"
        )

        def write_export() -> int:
            if format_name == "csv":
                return export_csv(
                    path,
                    self.source,
                    records,
                    config,
                    cancellation=operation.cancellation,
                    progress=operation.progress,
                )
            return export_jsonl(
                path,
                self.source,
                records,
                config,
                cancellation=operation.cancellation,
                progress=operation.progress,
            )

        def export_finished(count: int) -> None:
            message = f"Exported {count:,} rows to {path}"
            self._finish_operation("extraction_export", message)
            self.extraction.status.setText(message)

        def export_failed(_details: str) -> None:
            self._finish_operation("extraction_export")
            self.extraction.show_error(
                "Could not export extraction; see the application log."
            )

        self.tasks.submit(
            write_export,
            export_finished,
            export_failed,
        )

    def _selected_record_id(self) -> str | None:
        record = self._selected_record()
        return record.id if record is not None else None

    def _selected_record(self) -> RecordRef | None:
        proxy_index = self.file_list.currentIndex()
        if not proxy_index.isValid():
            return None
        return self.file_model.record(self.file_proxy.mapToSource(proxy_index))

    def _restore_record_selection(self, record_id: str | None) -> None:
        if record_id is None:
            return
        source_index = self.file_model.index_for_record_id(record_id)
        if not source_index.isValid():
            return
        proxy_index = self.file_proxy.mapFromSource(source_index)
        parent = proxy_index.parent()
        while parent.isValid():
            self.file_list.expand(parent)
            parent = parent.parent()
        self.file_list.setCurrentIndex(proxy_index)
        self.file_list.scrollTo(proxy_index)

    def _show_error(self, title: str, details: str) -> None:
        logger.error("%s\n%s", title, details)
        self.statusBar().showMessage(title, 5000)
        message = QMessageBox(self)
        message.setIcon(QMessageBox.Icon.Critical)
        message.setWindowTitle("ChemFit Context Inspector")
        message.setText(title)
        message.setDetailedText(details)
        message.exec()


def _sortable_value(value: bool | float | str) -> tuple[int, object]:
    if isinstance(value, bool):
        return (0, int(value))
    if isinstance(value, (int, float)) and not (
        isinstance(value, float) and math.isnan(value)
    ):
        return (0, float(value))
    if isinstance(value, str):
        return (1, value.casefold())
    raise TypeError(type(value).__name__)


def _chevron_icon(direction: int, color: str) -> QIcon:
    """Create a small theme-aware navigation chevron."""
    pixmap = QPixmap(20, 20)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(color))
    pen.setWidthF(1.8)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    outer_x, tip_x = (13.0, 7.0) if direction < 0 else (7.0, 13.0)
    path = QPainterPath()
    path.moveTo(outer_x, 4.5)
    path.lineTo(tip_x, 10.0)
    path.lineTo(outer_x, 15.5)
    painter.drawPath(path)
    painter.end()
    return QIcon(pixmap)
