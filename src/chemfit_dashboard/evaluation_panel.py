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
from chemfit_dashboard.icons import (
    make_icon_only,
)
from chemfit_dashboard.models import EvaluationListModel
from chemfit_dashboard.ui_components import ActionRow, add_clear_action


class EvaluationPanel(QWidget):
    def __init__(
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
        self.coordinator.collection_order_changed.connect(self.file_model._rebuild_tree)

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

        self.file_model.modelReset.connect(self._update_evaluation_empty_state)
        self.file_model.rowsInserted.connect(self._update_evaluation_empty_state)
        self.file_model.rowsRemoved.connect(self._update_evaluation_empty_state)
        self._update_evaluation_empty_state()

        ##############################################
        # Row of action buttons under the record list
        ##############################################
        path_row = ActionRow()

        # Widget that contains the currently selected evaluation path
        self.evaluation_path = QLineEdit()
        self.evaluation_path.setObjectName("evaluationPath")
        self.evaluation_path.setReadOnly(True)
        self.evaluation_path.setPlaceholderText("No evaluation selected")
        self.evaluation_path.setToolTip("Full path of the selected evaluation")
        self.coordinator.selected_record_changed.connect(self._update_evaluation_path)

        path_row.add_widget(self.evaluation_path, 1)

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
        self.focus_selected_button = path_row.add_action(
            "focusSelectedEvaluationAction",
            "Focus selected",
            "Reveal and center the currently loaded evaluation",
            icon_name="target",
            icon_only=True,
        )
        self.focus_selected_button.clicked.connect(self._focus_selected_record)
        layout.addWidget(path_row)
        # self._set_evaluation_path(None)

        # self.file_count = muted_label("No files loaded")
        # layout.addWidget(self.file_count)
        # self.order_status = muted_label("Order: filename", word_wrap=True)
        # self.order_status.setToolTip("Natural alphanumeric filename order")
        # layout.addWidget(self.order_status)
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
            lambda: self.coordinator.collection.invert_order()
        )
        order_controls.addWidget(self.invert_order_button)

        self.reset_order_button = QPushButton("Filename order")
        make_icon_only(
            self.reset_order_button,
            "reset",
            "Restore natural filename ordering",
        )
        self.reset_order_button.setEnabled(False)
        self.reset_order_button.clicked.connect(
            lambda: self.coordinator.collection.reset_order()
        )
        order_controls.addWidget(self.reset_order_button)
        order_controls.addStretch()
        layout.addLayout(order_controls)

    def open_files(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open ChemFit context file",
            "",
            "JSON files (*.json);;All files (*)",
        )

        if path:
            self.coordinator.load_collection([path])

    def open_directory(self):
        path = QFileDialog.getExistingDirectory(self, "Open context directory")
        if path:
            self.coordinator.load_collection([path])

    def _update_evaluation_path(self, *args):
        document = self.coordinator.selected_document
        self.evaluation_path.setText(str(document.path) if document is not None else "")

    def _update_evaluation_empty_state(self, *_args: object) -> None:
        """Show opening instructions in the evaluation list when it has no records."""
        target = (
            self.file_list
            if len(self.coordinator.collection) > 0
            else self.evaluation_empty_state
        )
        self.evaluation_stack.setCurrentWidget(target)

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
        record = self.coordinator.selected_document
        if record is not None:
            QApplication.clipboard().setText(record.name)

    def _copy_current_evaluation_path(self) -> None:
        record = self.coordinator.selected_document
        if record is None:
            return
        QApplication.clipboard().setText(str(record.path))

    def _open_current_evaluation_folder(self) -> None:
        record = self.coordinator.selected_document
        if record is None:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(record.path.parent)))

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
