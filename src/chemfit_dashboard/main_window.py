from __future__ import annotations

import logging
import re
import threading
from collections import OrderedDict
from itertools import islice
from typing import TYPE_CHECKING

from PySide6.QtCore import QItemSelection, QSettings, QSortFilterProxyModel, Qt, QTimer
from PySide6.QtGui import QAction, QFont, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from chemfit_dashboard.appearance import (
    COLOR_SCHEME_SETTING,
    COLOR_SCHEMES,
    DEFAULT_COLOR_SCHEME,
    FONT_SCALE_SETTING,
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
    ContextSearchIndex,
    ContextSearchPanel,
    SearchHit,
    SearchMode,
    TopKSearchHits,
)
from chemfit_dashboard.extraction import (
    ExtractionConfig,
    FieldSelector,
    export_csv,
    export_jsonl,
    extract_fields,
)
from chemfit_dashboard.extraction import (
    selector_for_document as extraction_selector_for_document,
)
from chemfit_dashboard.extraction_ui import PREVIEW_LIMIT, DataExtractionPanel
from chemfit_dashboard.graph_view import ContextGraphPanel
from chemfit_dashboard.inspector import Inspector
from chemfit_dashboard.models import ContextTreeModel, EvaluationListModel
from chemfit_dashboard.semantic import (
    NodeTrail,
    component_trail_for_path,
    resolve_component_selection,
)
from chemfit_dashboard.source import (
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
from chemfit_dashboard.workers import TaskRunner

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

logger = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    def __init__(
        self,
        source: ContextSource | None = None,
        initial_paths: Sequence[str | Path] | None = None,
        settings: QSettings | None = None,
    ):
        """Initialize the dashboard around a replaceable context source."""
        super().__init__()
        self.source = source or JsonFileSource()
        self.tasks = TaskRunner(self)
        self._load_generation = 0
        self._plot_generation = 0
        self._order_generation = 0
        self._search_generation = 0
        self._analysis_generation = 0
        self._extraction_generation = 0
        self._search_index_cache: OrderedDict[
            str, tuple[tuple[int, int], ContextSearchIndex]
        ] = OrderedDict()
        self._search_index_capacity = 32
        self._search_index_lock = threading.Lock()
        self._operation_tokens: dict[str, CancellationToken] = {}
        self._pending_search_hit: SearchHit | None = None
        self._pending_failure: FailureOccurrence | None = None
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
        self._settings = settings or application_settings()
        self._font_scale = 1.0
        self._color_scheme_key = DEFAULT_COLOR_SCHEME
        self._base_font: QFont | None = None
        self._base_stylesheet = ""
        self._restore_appearance_settings()

        self.setWindowTitle("ChemFit Context Inspector")
        self.resize(1460, 860)
        self.setMinimumSize(980, 620)
        self._build_ui()
        self._build_menu()

        if initial_paths:
            self.add_locations(initial_paths)

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
        self._base_stylesheet = stylesheet_for(scheme)
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
        subtitle = QLabel("Evaluation diagnostics · local read-only session")
        subtitle.setObjectName("mutedLabel")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header_layout.addLayout(title_box)
        header_layout.addStretch()
        scheme_label = QLabel("COLOR")
        scheme_label.setObjectName("sectionLabel")
        header_layout.addWidget(scheme_label)
        self.color_scheme = QComboBox()
        self.color_scheme.setToolTip("Choose and persist the interface color scheme")
        for scheme in COLOR_SCHEMES:
            self.color_scheme.addItem(scheme.label, scheme.key)
        scheme_index = self.color_scheme.findData(self._color_scheme_key)
        self.color_scheme.setCurrentIndex(max(0, scheme_index))
        self.color_scheme.currentIndexChanged.connect(self._color_scheme_changed)
        header_layout.addWidget(self.color_scheme)
        files_button = QPushButton("Open files")
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
        self.context_workspace.setChildrenCollapsible(False)
        self.context_workspace.addWidget(self._build_evaluation_panel())
        self.context_workspace.addWidget(self._build_tree_panel())
        self.inspector = Inspector()
        self.inspector.plot_requested.connect(self._plot_requested)
        self.inspector.plot_add_requested.connect(self._plot_add_requested)
        self.inspector.order_requested.connect(self._order_requested)
        self.inspector.extraction_requested.connect(self._add_extraction_selector)
        self.inspector.component_extraction_requested.connect(
            self._add_extraction_selector
        )
        self.context_workspace.addWidget(self.inspector)
        self.context_workspace.setSizes([275, 440, 745])

        self.collection_analysis = CollectionAnalysisPanel()
        self.collection_analysis.failure_selected.connect(
            self._failure_occurrence_selected
        )
        self.collection_analysis.refresh_requested.connect(
            self._refresh_collection_analysis
        )
        self.workspace_tabs = QTabWidget()
        self.workspace_tabs.addTab(self.context_workspace, "Context inspector")
        self.workspace_tabs.addTab(self.collection_analysis, "Collection overview")
        self.extraction = DataExtractionPanel()
        self.extraction.selectors_changed.connect(self._refresh_extraction_preview)
        self.extraction.export_requested.connect(self._export_extraction)
        self.workspace_tabs.addTab(self.extraction, "Data Extraction")
        outer.addWidget(self.workspace_tabs, 1)
        self.setCentralWidget(container)
        self.statusBar().showMessage("Ready")

    def _build_evaluation_panel(self) -> QWidget:
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
        self.file_list.selectionModel().selectionChanged.connect(
            self._evaluation_selected
        )
        layout.addWidget(self.file_list, 1)

        self.file_count = QLabel("No files loaded")
        self.file_count.setObjectName("mutedLabel")
        layout.addWidget(self.file_count)
        self.order_status = QLabel("Order: filename")
        self.order_status.setObjectName("mutedLabel")
        self.order_status.setToolTip("Natural alphanumeric filename order")
        self.order_status.setWordWrap(True)
        layout.addWidget(self.order_status)
        order_controls = QHBoxLayout()
        self.reverse_order_button = QPushButton("Reverse order")
        self.reverse_order_button.setToolTip("Reverse the current collection order")
        self.reverse_order_button.setEnabled(False)
        self.reverse_order_button.clicked.connect(self._reverse_collection_order)
        order_controls.addWidget(self.reverse_order_button)
        self.reset_order_button = QPushButton("Filename order")
        self.reset_order_button.setToolTip("Restore natural filename ordering")
        self.reset_order_button.setEnabled(False)
        self.reset_order_button.clicked.connect(self._reset_collection_order)
        order_controls.addWidget(self.reset_order_button)
        layout.addLayout(order_controls)
        return panel

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
        self.node_count = QLabel("—")
        self.node_count.setObjectName("mutedLabel")
        top.addWidget(self.node_count)
        layout.addLayout(top)

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
        layout.addWidget(self.field_search)
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
        self.search_scope.addItem("Current evaluation", "current")
        self.search_scope.addItem("Whole collection", "collection")
        self.search_scope.setToolTip(
            "Search one evaluation or every loaded context file"
        )
        self.search_scope.currentIndexChanged.connect(self._search_scope_changed)
        search_controls.addWidget(self.search_scope)
        search_controls.addStretch()
        layout.addLayout(search_controls)
        search_dimensions = QHBoxLayout()
        search_dimensions.setSpacing(8)
        search_dimensions.addStretch()
        self.search_keys = QCheckBox("Include keys")
        self.search_keys.setChecked(True)
        self.search_keys.setToolTip("Match nested field paths and section names")
        self.search_keys.toggled.connect(self._search_options_toggled)
        search_dimensions.addWidget(self.search_keys)
        self.search_values = QCheckBox("Include values")
        self.search_values.setChecked(True)
        self.search_values.setToolTip(
            "Match serialized field values as well as keys and hierarchy names"
        )
        self.search_values.toggled.connect(self._search_options_toggled)
        search_dimensions.addWidget(self.search_values)
        layout.addLayout(search_dimensions)

        self.tree_model = ContextTreeModel(self)
        self.tree = QTreeView()
        self.tree.setModel(self.tree_model)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree.setAnimated(False)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().resizeSection(0, 225)
        self.tree.header().resizeSection(1, 95)
        self.tree.header().resizeSection(2, 120)
        self.tree.selectionModel().selectionChanged.connect(self._context_selected)

        self.graph = ContextGraphPanel()
        self.graph.node_selected.connect(self._graph_context_selected)
        self.collection_plot = CollectionPlotPanel()
        self.collection_plot.context_selected.connect(self._plot_context_selected)
        self.collection_plot.remove_series_requested.connect(self._remove_plot_series)
        self.collection_plot.clear_series_requested.connect(self._clear_plot_series)
        self.search_panel = ContextSearchPanel()
        self.search_panel.hit_selected.connect(self._search_hit_selected)
        self.hierarchy_tabs = QTabWidget()
        self.hierarchy_tabs.addTab(self.tree, "Tree")
        self.hierarchy_tabs.addTab(self.graph, "Graph")
        self.hierarchy_tabs.addTab(self.collection_plot, "Plot")
        self.hierarchy_tabs.addTab(self.search_panel, "Search")
        self.hierarchy_tabs.currentChanged.connect(self._hierarchy_tab_changed)
        layout.addWidget(self.hierarchy_tabs, 1)
        return panel

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        open_files = QAction("Open &files…", self)
        open_files.setShortcut(QKeySequence.StandardKey.Open)
        open_files.triggered.connect(self.open_files)
        file_menu.addAction(open_files)
        open_directory = QAction("Open &directory…", self)
        open_directory.setShortcut("Ctrl+Shift+O")
        open_directory.triggered.connect(self.open_directory)
        file_menu.addAction(open_directory)
        file_menu.addSeparator()
        close = QAction("&Quit", self)
        close.setShortcut(QKeySequence.StandardKey.Quit)
        close.triggered.connect(self.close)
        file_menu.addAction(close)

    def _new_operation(self, name: str) -> CancellationToken:
        previous = self._operation_tokens.get(name)
        if previous is not None:
            previous.cancel()
        token = CancellationToken()
        self._operation_tokens[name] = token
        return token

    def _cancel_operation(self, name: str) -> None:
        token = self._operation_tokens.get(name)
        if token is not None:
            token.cancel()

    def closeEvent(self, event) -> None:  # noqa: N802, ANN001
        for token in self._operation_tokens.values():
            token.cancel()
        self.graph.view.cancel_pending_layout()
        self.tasks.cancel_pending()
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
        self._base_stylesheet = stylesheet_for(scheme)
        self._apply_application_stylesheet()
        self._settings.setValue(COLOR_SCHEME_SETTING, scheme.key)
        self._settings.sync()
        self.graph.set_color_scheme(scheme)
        self.collection_plot.set_color_scheme(scheme)
        self.file_list.viewport().update()
        self.tree.viewport().update()
        self.statusBar().showMessage(
            f"Color scheme: {scheme.label} · saved in {self._settings.fileName()}",
            4000,
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
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Open ChemFit context files",
            "",
            "JSON files (*.json);;All files (*)",
        )
        if paths:
            self.add_locations(paths)

    def open_directory(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Open context directory")
        if path:
            self.add_locations([path])

    def add_locations(self, locations: Sequence[str | Path]) -> None:
        locations = list(locations)
        self.statusBar().showMessage("Scanning for context files…")
        self.tasks.submit(
            lambda: self.source.discover(locations),
            self._records_discovered,
            lambda details: self._show_error("Could not scan locations", details),
        )

    def _records_discovered(self, records: list[RecordRef]) -> None:
        self.file_model.set_records(records)
        count = len(self.file_model.records)
        self.file_count.setText(f"{count} file{'s' if count != 1 else ''}")
        self.reverse_order_button.setEnabled(count > 1)
        self.statusBar().showMessage(f"Found {len(records)} context files", 3500)
        self._refresh_collection_analysis()
        self._refresh_extraction_preview(self.extraction.selectors)
        if not self.file_list.currentIndex().isValid() and count:
            root_index = self.file_proxy.index(0, 0)
            self.file_list.expand(root_index)
            source_index = self.file_model.first_record_index()
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
            return
        source_index = self.file_proxy.mapToSource(indexes[0])
        record = self.file_model.record(source_index)
        if record is None:
            return
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
        self.statusBar().showMessage(f"Loading {record.name}…")
        self.tasks.submit(
            lambda: self.source.load(record),
            lambda document: self._document_loaded(generation, document),
            lambda details: self._load_failed(generation, record, details),
        )

    def _document_loaded(self, generation: int, document: ContextDocument) -> None:
        if generation != self._load_generation:
            return
        self._current_document = document
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
        resolution = resolve_component_selection(
            document, self._sticky_node_path, self._sticky_node_trail
        )
        if resolution.complete:
            self._sticky_node_path = resolution.path
            self._select_context_path(resolution.path, remember=False)
            if resolution.path:
                selected_label = document.details(resolution.path).info.label
                self.statusBar().showMessage(
                    f"Loaded {document.record.name}; restored {selected_label}",
                    3500,
                )
        else:
            self._select_context_path(resolution.path, remember=False)
            available_label = (
                document.details(resolution.path).info.label
                if resolution.path
                else "evaluation root"
            )
            target_label = self._sticky_node_trail[-1].name
            self.statusBar().showMessage(
                f"Loaded {document.record.name}; {target_label} is unavailable "
                f"below {available_label} (selection retained)",
                5000,
            )
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

    def _load_failed(self, generation: int, record: RecordRef, details: str) -> None:
        if generation != self._load_generation:
            return
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
        self.inspector.set_details(self._current_document.details(info.path))
        self.graph.select_path(info.path)
        if not self._suppress_context_memory:
            self._remember_context_selection(info.path)

    def _hierarchy_tab_changed(self, index: int) -> None:
        if self.hierarchy_tabs.widget(index) is self.graph:
            self.graph.ensure_rendered()

    def _graph_context_selected(self, path: NodePath) -> None:
        if self._current_document is not None:
            self.inspector.set_details(self._current_document.details(path))
            self._remember_context_selection(path)

    def _remember_context_selection(self, path: NodePath) -> None:
        if self._current_document is None:
            return
        try:
            trail = component_trail_for_path(self._current_document, path)
        except ContextSourceError:
            return
        self._sticky_node_path = path
        self._sticky_node_trail = trail

    def _select_context_path(self, path: NodePath, *, remember: bool) -> bool:
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
            self.inspector.set_details(self._current_document.details(path))
            self.graph.select_path(path)
        finally:
            self._suppress_context_memory = previous_suppression
        if remember:
            self._remember_context_selection(path)
        return True

    def _refresh_collection_analysis(self) -> None:
        records = list(self.file_model.records)
        self._analysis_generation += 1
        generation = self._analysis_generation
        cancellation = self._new_operation("analysis")
        if not records:
            self.collection_analysis.show_error("No context collection is loaded.")
            return
        self.collection_analysis.show_loading(len(records))

        def load_summary() -> CollectionSummary:
            pairs = (
                (item.record, item.document)
                for item in iter_collection(
                    self.source, records, cancellation=cancellation
                )
            )
            return analyze_collection(pairs)

        self.tasks.submit(
            load_summary,
            lambda summary: self._analysis_loaded(generation, summary),
            lambda details: self._analysis_failed(generation, details),
        )

    def _analysis_loaded(self, generation: int, summary: CollectionSummary) -> None:
        if generation == self._analysis_generation:
            self.collection_analysis.set_summary(summary)

    def _analysis_failed(self, generation: int, details: str) -> None:
        if generation != self._analysis_generation:
            return
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
        self.inspector.set_details(details)
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

    def _field_search_changed(self, text: str) -> None:
        self._cancel_operation("search")
        if text.strip():
            self._search_generation += 1
            if self.hierarchy_tabs.currentWidget() is not self.search_panel:
                self._tab_before_search = self.hierarchy_tabs.currentWidget()
            self.hierarchy_tabs.setCurrentWidget(self.search_panel)
            self._search_timer.start()
        else:
            self._search_timer.stop()
            self._search_generation += 1
            self.search_panel.show_message(
                "Type above to search context fields and values."
            )
            if (
                self.hierarchy_tabs.currentWidget() is self.search_panel
                and self._tab_before_search is not None
            ):
                self.hierarchy_tabs.setCurrentWidget(self._tab_before_search)
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
        cancellation = self._new_operation("search")
        count = len(records)
        self.search_panel.show_loading(
            f"Searching {count:,} evaluation{'s' if count != 1 else ''} "
            "in the background…"
        )

        def search_records() -> tuple[list[SearchHit], int, bool]:
            hits = TopKSearchHits(MAX_RESULTS)
            failed_records = 0
            for record in records:
                if cancellation.cancelled:
                    return [], failed_records, True
                try:
                    document = (
                        current_document
                        if current_document is not None
                        and current_document.record.id == record.id
                        else self.source.load(record)
                    )
                    index = self._cached_search_index(document)
                    hits.extend(
                        index.search(
                            query,
                            include_keys=include_keys,
                            include_values=include_values,
                            mode=mode,
                        )
                    )
                except ContextSourceError:
                    failed_records += 1
            return hits.results(), failed_records, False

        self.tasks.submit(
            search_records,
            lambda result: self._search_loaded(
                generation,
                result,
                query,
                include_keys,
                include_values,
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

    def _cached_search_index(self, document: ContextDocument) -> ContextSearchIndex:
        try:
            stat = document.record.path.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            signature = (0, document.record.size)
        with self._search_index_lock:
            cached = self._search_index_cache.get(document.record.id)
            if cached is not None and cached[0] == signature:
                self._search_index_cache.move_to_end(document.record.id)
                return cached[1]
        index = ContextSearchIndex(document)
        with self._search_index_lock:
            self._search_index_cache[document.record.id] = (signature, index)
            self._search_index_cache.move_to_end(document.record.id)
            while len(self._search_index_cache) > self._search_index_capacity:
                self._search_index_cache.popitem(last=False)
        return index

    def _search_loaded(
        self,
        generation: int,
        result: tuple[list[SearchHit], int, bool],
        query: str,
        include_keys: bool,
        include_values: bool,
        mode: SearchMode,
        scope_label: str,
        searched_records: int,
    ) -> None:
        if generation != self._search_generation:
            return
        hits, failed_records, cancelled = result
        if cancelled:
            return
        self.search_panel.show_results(
            hits,
            query,
            include_keys=include_keys,
            include_values=include_values,
            mode=mode,
            scope_label=scope_label,
            searched_records=searched_records,
            failed_records=failed_records,
        )

    def _search_failed(self, generation: int, details: str) -> None:
        if generation != self._search_generation:
            return
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
        self.inspector.set_details(self._current_document.details(hit.node_path))
        tree_index = self.tree_model.index_for_path(hit.node_path)
        if tree_index.isValid():
            self.tree.setCurrentIndex(tree_index)
            self.tree.scrollTo(tree_index)
        self.graph.select_path(hit.node_path)
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
        self._plot_generation += 1
        generation = self._plot_generation
        cancellation = self._new_operation("plot")
        if activate:
            self.hierarchy_tabs.setCurrentWidget(self.collection_plot)
        self.collection_plot.show_loading(selected, len(records))
        if not records:
            self.collection_plot.show_error("No context collection is loaded.")
            return

        def load_series() -> tuple[CollectionSeries, ...]:
            pairs = (
                (item.record, item.document)
                for item in iter_collection(
                    self.source, records, cancellation=cancellation
                )
            )
            return build_collection_series_multi(selected, pairs)

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
            collection = (data,) if isinstance(data, CollectionSeries) else data
            self.collection_plot.set_series_collection(collection)
            self.collection_plot.select_record(self._selected_record_id())

    def _plot_failed(self, generation: int, details: str) -> None:
        if generation != self._plot_generation:
            return
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

    def _order_requested(self, selector: NumberSelector) -> None:
        if self._current_document is not None:
            selector = selector_for_document(self._current_document, selector)
        records = list(self.file_model.filename_ordered_records)
        if not records:
            self.statusBar().showMessage("No context collection is loaded.", 3000)
            return
        self._order_generation += 1
        generation = self._order_generation
        cancellation = self._new_operation("order")
        self.order_status.setText("Order: loading selected values…")
        self.statusBar().showMessage(
            f"Ordering {len(records):,} contexts by {selector.label}…"
        )

        def load_order_values() -> tuple[list[RecordRef], CollectionSeries]:
            pairs = (
                (item.record, item.document)
                for item in iter_collection(
                    self.source, records, cancellation=cancellation
                )
            )
            return records, build_collection_series(selector, pairs)

        self.tasks.submit(
            load_order_values,
            lambda result: self._order_loaded(generation, *result),
            lambda details: self._order_failed(generation, details),
        )

    def _order_loaded(
        self,
        generation: int,
        records: list[RecordRef],
        data: CollectionSeries,
    ) -> None:
        if generation != self._order_generation:
            return
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
        self.order_status.setText(f"Order: {data.selector.label} ↑")
        self.order_status.setToolTip(
            f"Ascending numeric order by {data.selector.label}; missing values last"
        )
        self.reset_order_button.setEnabled(True)
        self.reverse_order_button.setEnabled(len(records) > 1)
        missing = data.missing_count
        suffix = f"; {missing:,} missing placed last" if missing else ""
        self.statusBar().showMessage(
            f"Ordered {len(data.points):,} contexts by selected number{suffix}", 5000
        )

    def _order_failed(self, generation: int, details: str) -> None:
        if generation != self._order_generation:
            return
        logger.error("Could not order context collection\n%s", details)
        self.order_status.setText("Order: filename")
        self.statusBar().showMessage("Could not order the context collection.", 5000)

    def _reset_collection_order(self) -> None:
        self._cancel_operation("order")
        self._order_generation += 1
        selected_id = self._selected_record_id()
        self._clear_numeric_order()
        self._filename_reversed = False
        self.file_model.set_record_order(None)
        self._restore_record_selection(selected_id)
        self._refresh_active_plot()
        self.order_status.setText("Order: filename")
        self.order_status.setToolTip("Natural alphanumeric filename order")
        self.reset_order_button.setEnabled(False)
        self.statusBar().showMessage("Restored filename order", 3000)

    def _reverse_collection_order(self) -> None:
        if len(self.file_model.records) < 2:
            return
        selected_id = self._selected_record_id()
        if self._numeric_order_label is not None:
            self._order_descending = not self._order_descending
            present = (
                tuple(reversed(self._numeric_order_present))
                if self._order_descending
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
            arrow = "↓" if self._order_descending else "↑"
            direction = "Descending" if self._order_descending else "Ascending"
            self.order_status.setText(f"Order: {self._numeric_order_label} {arrow}")
            self.order_status.setToolTip(
                f"{direction} numeric order by {self._numeric_order_label}; "
                "missing values last"
            )
        else:
            self._filename_reversed = not self._filename_reversed
            if self._filename_reversed:
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
        self.statusBar().showMessage("Reversed collection order", 3000)

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
        self.workspace_tabs.setCurrentWidget(self.extraction)

    def _refresh_extraction_preview(
        self, selectors: tuple[FieldSelector, ...] | object
    ) -> None:
        selected = (
            selectors if isinstance(selectors, tuple) else self.extraction.selectors
        )
        if not all(isinstance(item, FieldSelector) for item in selected):
            selected = self.extraction.selectors
        records = list(self.file_model.records)
        self._extraction_generation += 1
        generation = self._extraction_generation
        cancellation = self._new_operation("extraction_preview")
        if not selected:
            self.extraction.preview_model.set_data((), ())
            self.extraction.status.setText(
                "Add any inspector value or component to begin."
            )
            return
        if not records:
            self.extraction.show_error("No context collection is loaded.")
            return
        self.extraction.show_loading(len(records))

        def preview():
            return tuple(
                islice(
                    extract_fields(
                        self.source,
                        records,
                        selected,
                        cancellation=cancellation,
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
            self.extraction.set_preview(rows, total)

    def _extraction_failed(self, generation: int, details: str) -> None:
        if generation != self._extraction_generation:
            return
        logger.error("Could not build extraction preview\n%s", details)
        self.extraction.show_error("Could not build the extraction preview.")

    def _export_extraction(
        self, path: str, format_name: str, config: ExtractionConfig
    ) -> None:
        records = list(self.file_model.records)
        cancellation = self._new_operation("extraction_export")
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
                    cancellation=cancellation,
                )
            return export_jsonl(
                path,
                self.source,
                records,
                config,
                cancellation=cancellation,
            )

        self.tasks.submit(
            write_export,
            lambda count: self.extraction.status.setText(
                f"Exported {count:,} rows to {path}"
            ),
            lambda _details: self.extraction.show_error(
                "Could not export extraction; see the application log."
            ),
        )

    def _selected_record_id(self) -> str | None:
        proxy_index = self.file_list.currentIndex()
        if not proxy_index.isValid():
            return None
        record = self.file_model.record(self.file_proxy.mapToSource(proxy_index))
        return record.id if record is not None else None

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
