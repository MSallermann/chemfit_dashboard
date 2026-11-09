from PySide6.QtCore import (
    QItemSelection,
    QSortFilterProxyModel,
    Qt,
    QUrl,
)
from PySide6.QtGui import (
    QDesktopServices,
    QKeySequence,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QStackedWidget,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from chemfit_dashboard.coordinator import DashboardCoordinator
from chemfit_dashboard.icons import make_icon_only
from chemfit_dashboard.models import EvaluationListModel
from chemfit_dashboard.ui_components import ActionRow, add_clear_action, muted_label


class EvaluationPanel(QWidget):
    def __init__(  # noqa: PLR0915
        self,
        coordinator: DashboardCoordinator,
        parent: QWidget | None = None,
    ):
        """Build the left hand panel showing the records in the current collection."""

        super().__init__(parent=parent)
        self.coordinator = coordinator
        self.setMinimumWidth(230)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 16, 14, 14)
        layout.setSpacing(10)

        ##############################################
        # Headers and stuff
        ##############################################
        header_layout = QHBoxLayout()
        layout.addLayout(header_layout)

        label = QLabel("RECORDS")
        label.setObjectName("sectionLabel")
        header_layout.addWidget(label)
        header_layout.addStretch()

        files_button = QPushButton("Open file")
        make_icon_only(files_button, "open_file", "Open context file")
        files_button.clicked.connect(self.open_files)
        directory_button = QPushButton("Open directory")
        make_icon_only(directory_button, "open_folder", "Open context directory")
        directory_button.clicked.connect(self.open_directory)
        header_layout.addWidget(files_button)
        header_layout.addWidget(directory_button)

        ##############################################
        # Search bar
        ##############################################
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter files…")
        add_clear_action(self.search)
        layout.addWidget(self.search)

        ##############################################
        # The files widget
        ##############################################
        # Data model
        self.file_model = EvaluationListModel(self, coordinator=self.coordinator)

        # Proxy for filtering
        self.file_proxy = QSortFilterProxyModel(self)
        self.file_proxy.setSourceModel(self.file_model)
        self.file_proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.file_proxy.setFilterRole(Qt.ItemDataRole.DisplayRole)
        self.file_proxy.setRecursiveFilteringEnabled(True)
        self.file_proxy.setAutoAcceptChildRows(True)
        self.search.textChanged.connect(self.file_proxy.setFilterFixedString)

        # The tree view
        self.file_list = QTreeView()
        self.file_list.setModel(self.file_proxy)
        self.file_list.setHeaderHidden(True)
        self.file_list.setUniformRowHeights(True)
        self.file_list.setAnimated(False)
        self.file_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.file_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

        # context menus
        self.file_list.customContextMenuRequested.connect(
            self._show_evaluation_context_menu
        )
        self.file_list.selectionModel().selectionChanged.connect(
            self._evaluation_selected
        )

        # shortcuts
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

        # This is displayed when no collection is loaded
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

        # Stack the two widgets over each other
        self.evaluation_stack = QStackedWidget()
        self.evaluation_stack.addWidget(self.evaluation_empty_state)
        self.evaluation_stack.addWidget(self.file_list)

        layout.addWidget(self.evaluation_stack, 1)

        ##############################################
        # Row of action buttons under the record list
        ##############################################
        path_row = ActionRow()

        # The currently selected evaluation path
        self.evaluation_path = QLineEdit()
        self.evaluation_path.setObjectName("evaluationPath")
        self.evaluation_path.setReadOnly(True)
        self.evaluation_path.setPlaceholderText("No evaluation selected")
        self.evaluation_path.setToolTip("Full path of the selected evaluation")

        path_row.add_widget(self.evaluation_path, 1)

        # Copy the path
        self.copy_evaluation_path_button = path_row.add_action(
            "copyEvaluationPathAction",
            "Copy path",
            "Copy the selected evaluation's full path",
            icon_name="copy",
            icon_only=True,
        )
        self.copy_evaluation_path_button.clicked.connect(
            self._copy_current_evaluation_path
        )

        # Open in file explorer
        self.open_file_explorer_button = path_row.add_action(
            "openInFileExplorerAction",
            "Open folder",
            "Open the selected evaluation's folder in the file explorer",
            icon_name="open_folder",
            icon_only=True,
        )
        self.open_file_explorer_button.clicked.connect(
            self._open_current_evaluation_folder
        )

        # Focus selected record
        self.focus_selected_button = path_row.add_action(
            "focusSelectedEvaluationAction",
            "Focus selected",
            "Reveal and center the currently loaded evaluation",
            icon_name="target",
            icon_only=True,
        )
        self.focus_selected_button.clicked.connect(self._focus_selected_record)
        layout.addWidget(path_row)

        # Label for record count
        self.record_count = muted_label("No records loaded")
        layout.addWidget(self.record_count)

        # Label for ordering
        self.order_status = muted_label(
            "", word_wrap=True
        )  # text gets set in `on_order_changed`
        layout.addWidget(self.order_status)

        # Controls for orderings
        order_controls = QHBoxLayout()
        order_controls.setSpacing(6)
        self.invert_order_button = QPushButton("Invert order")
        make_icon_only(
            self.invert_order_button,
            "sort_up",
            "Sort the collection in reverse order",
        )
        self.invert_order_button.setEnabled(False)
        self.invert_order_button.clicked.connect(
            lambda: self.coordinator.invert_order_records()
        )
        self.invert_order_button.setToolTip("Invert the order of records")
        order_controls.addWidget(self.invert_order_button)

        self.reset_order_button = QPushButton("Filename order")
        make_icon_only(
            self.reset_order_button,
            "reset",
            "Restore natural filename ordering",
        )
        self.reset_order_button.setEnabled(False)
        self.reset_order_button.clicked.connect(
            lambda: self.coordinator.reset_order_records()
        )
        order_controls.addWidget(self.reset_order_button)
        order_controls.addStretch()
        layout.addLayout(order_controls)

        ##############################################
        # Connect signals
        ##############################################

        self.coordinator.collection_changed.connect(self.on_collection_changed)
        self.coordinator.collection_order_changed.connect(
            self.on_collection_order_changed
        )
        self.coordinator.selected_record_changed.connect(
            self.on_selected_record_changed
        )

        ##############################################
        # Initialize GUI
        ##############################################
        self.on_collection_changed()

    def open_files(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open ChemFit context file",
            "",
            "JSON files (*.json);;All files (*)",
        )

        if path:
            self.coordinator.load_collection([path])

    def on_collection_changed(self):
        n_records = len(self.coordinator.collection)

        self.record_count.setText(
            f"{n_records} records loaded" if n_records else "No records loaded"
        )

        self.evaluation_stack.setCurrentWidget(
            self.file_list if n_records else self.evaluation_empty_state
        )

        self.on_collection_order_changed()

    def on_collection_order_changed(self):
        ascending = self.coordinator.collection.ascending

        label = f"Order: {self.coordinator.collection.order_label}"
        if ascending is not None:
            if ascending:
                label += " ↑"
            else:
                label += " ↓"

        self.order_status.setText(label)
        self.invert_order_button.setEnabled(len(self.coordinator.collection) > 1)
        self.reset_order_button.setEnabled(len(self.coordinator.collection) > 1)

        self._focus_selected_record()

    def on_selected_record_changed(self) -> None:
        selected_document = self.coordinator.selected_document

        if selected_document is not None:
            self.evaluation_path.setText(str(selected_document.path))
            self._focus_selected_record()
        else:
            self.evaluation_path.clear()

    def open_directory(self):
        path = QFileDialog.getExistingDirectory(self, "Open context directory")
        if path:
            self.coordinator.load_collection([path])

    def add_files(self) -> None:
        """Append selected context files to the current collection."""
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Add context files to collection",
            "",
            "JSON files (*.json);;All files (*)",
        )
        if paths:
            self.coordinator.load_collection(paths, append=True)

    def add_directory(self) -> None:
        """Append one context directory to the current collection."""
        path = QFileDialog.getExistingDirectory(
            self, "Add context directory to collection"
        )
        if path:
            self.coordinator.load_collection([path], append=True)

    def _show_evaluation_context_menu(self, position) -> None:  # noqa: ANN001
        proxy_index = self.file_list.indexAt(position)
        if not proxy_index.isValid():
            return
        source_index = self.file_proxy.mapToSource(proxy_index)

        record = source_index.data(EvaluationListModel.RecordRole)

        if record is None:
            return

        menu = QMenu(self.file_list)
        copy_name = menu.addAction("Copy filename")
        copy_path = menu.addAction("Copy full path")
        open_folder = menu.addAction("Open folder in file explorer")
        selected = menu.exec(self.file_list.viewport().mapToGlobal(position))
        if selected is copy_name:
            QApplication.clipboard().setText(record.name)
            # self._clipboard_copied("Copied evaluation filename")
        elif selected is copy_path:
            QApplication.clipboard().setText(str(record.path))
            # self._clipboard_copied("Copied evaluation path")
        elif selected is open_folder:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(record.path.parent)))

    def _evaluation_selected(
        self, selected: QItemSelection, _deselected: QItemSelection
    ) -> None:
        indexes = selected.indexes()
        if not indexes:
            # self._set_evaluation_path(None)
            return
        source_index = self.file_proxy.mapToSource(indexes[0])
        record = source_index.data(EvaluationListModel.RecordRole)

        if record is None:
            return  # Folder selected, not a record

        self.coordinator.select_record(record)

    def _copy_current_evaluation(self) -> None:
        document = self.coordinator.selected_document
        if document is not None:
            QApplication.clipboard().setText(document.record.name)

    def _copy_current_evaluation_path(self) -> None:
        document = self.coordinator.selected_document
        if document is None:
            return
        QApplication.clipboard().setText(str(document.record.path))

    def _open_current_evaluation_folder(self) -> None:
        document = self.coordinator.selected_document
        if document is None:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(document.record.path.parent)))

    def _focus_selected_record(self) -> None:
        """Reveal and focus the currently selected record."""

        document = self.coordinator.selected_document
        if document is None:
            return

        record_id = document.record.id
        source_index = self.file_model.index_for_record_id(record_id)
        if not source_index.isValid():
            return

        self.search.clear()

        proxy_index = self.file_proxy.mapFromSource(source_index)
        if not proxy_index.isValid():
            return

        parent = proxy_index.parent()
        while parent.isValid():
            self.file_list.expand(parent)
            parent = parent.parent()

        self.file_list.setCurrentIndex(proxy_index)
        self.file_list.scrollTo(
            proxy_index,
            QAbstractItemView.ScrollHint.PositionAtCenter,
        )
        self.file_list.setFocus(Qt.FocusReason.ShortcutFocusReason)
