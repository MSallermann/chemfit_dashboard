"""Context hierarchy panel synchronized with DashboardCoordinator."""

from PySide6.QtCore import (
    QItemSelection,
    QSignalBlocker,
    Qt,
)
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from chemfit_dashboard.context_document import ContextDocument, NodePath
from chemfit_dashboard.coordinator import DashboardCoordinator
from chemfit_dashboard.models import ContextTreeModel
from chemfit_dashboard.ui_components import muted_label


class ContextTreePanel(QWidget):
    """Display the hierarchy of the currently selected evaluation."""

    def __init__(
        self,
        coordinator: DashboardCoordinator,
        parent: QWidget | None = None,
    ) -> None:
        """Initialize context tree panel."""

        super().__init__(parent)
        self.coordinator = coordinator
        self.setObjectName("treePanel")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 16, 14, 14)
        layout.setSpacing(10)

        # Header
        header = QHBoxLayout()
        label = QLabel("CONTEXT HIERARCHY")
        label.setObjectName("sectionLabel")
        header.addWidget(label)
        header.addStretch()
        self.node_count = muted_label("—")
        header.addWidget(self.node_count)
        layout.addLayout(header)

        # Tree
        self.tree_model = ContextTreeModel(self)
        self.tree = QTreeView()
        self.tree.setModel(self.tree_model)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree.setAnimated(False)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

        header_view = self.tree.header()
        header_view.setStretchLastSection(False)
        header_view.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header_view.setMinimumSectionSize(72)
        header_view.resizeSection(0, 280)
        header_view.resizeSection(1, 110)
        header_view.resizeSection(2, 220)

        layout.addWidget(self.tree, 1)

        # View actions
        self.tree.customContextMenuRequested.connect(self._show_tree_context_menu)
        self.tree.selectionModel().selectionChanged.connect(self._context_selected)

        self._tree_copy_shortcut = QShortcut(QKeySequence.StandardKey.Copy, self.tree)
        self._tree_copy_shortcut.setContext(
            Qt.ShortcutContext.WidgetWithChildrenShortcut
        )
        self._tree_copy_shortcut.activated.connect(self._copy_current_tree_component)

        # Coordinator state
        self.coordinator.selected_record_changed.connect(
            self.on_selected_record_changed
        )
        self.coordinator.selected_context_changed.connect(
            self.on_selected_context_changed
        )
        self.coordinator.component_label_metadata_key_changed.connect(
            self._component_labels_changed
        )

        # Initialize from an existing selection, if any.
        self.on_selected_record_changed(self.coordinator.selected_document)
        self.on_selected_context_changed(self.coordinator.selected_context)

    def on_selected_record_changed(self, document: ContextDocument | None) -> None:
        """Show the committed document, or clear the hierarchy."""
        self.tree_model.set_document(document)
        self.node_count.setText(f"{document.node_count:,} nodes" if document else "—")

        if document is not None:
            # Reveal the first level without eagerly materializing the entire tree.
            self.tree.expand(self.tree_model.index_for_path(()))

    def _component_labels_changed(self, _key: str) -> None:
        """Refresh the hierarchy after the coordinator changes display labels."""
        self.tree_model.layoutChanged.emit()
        self.tree.viewport().update()

    def on_selected_context_changed(self, path: NodePath | None):
        if path is None:
            with QSignalBlocker(self.tree.selectionModel()):
                self.tree.clearSelection()
            return

        index = self.tree_model.index_for_path(path)
        if not index.isValid():
            return

        parent = index.parent()
        while parent.isValid():
            self.tree.expand(parent)
            parent = parent.parent()

        # Programmatic selection must not overwrite the preferred context path.
        # When a document lacks the preferred node, the coordinator selects the
        # nearest ancestor but remembers the original path for later restoration.
        # Block selectionChanged here so this fallback is not treated as a new
        # user selection.
        with QSignalBlocker(self.tree.selectionModel()):
            self.tree.setCurrentIndex(index)

        self.tree.scrollTo(index)

    def _context_selected(
        self, selected: QItemSelection, _deselected: QItemSelection
    ) -> None:
        """Forward the user's context selection to the coordinator."""
        indexes = selected.indexes()
        if not indexes:
            return

        info = self.tree_model.node_info(indexes[0])
        if info is not None:
            self.coordinator.select_context(info.path)

    def _show_tree_context_menu(self, position) -> None:  # noqa: ANN001
        info = self.tree_model.node_info(self.tree.indexAt(position))
        document = self.coordinator.selected_document
        if info is None or document is None:
            return

        menu = QMenu()
        copy_name = menu.addAction("Copy component name")
        copy_path = menu.addAction("Copy component path")
        copy_structural_path = menu.addAction("Copy structural path")
        selected = menu.exec(self.tree.viewport().mapToGlobal(position))

        if selected is copy_name:
            text = document.component_label(info.path)
        elif selected is copy_path:
            # The visible breadcrumb needs only the labels of its ancestors.
            text = (
                " / ".join(
                    document.component_label(info.path[:depth])
                    for depth in range(1, len(info.path) + 1)
                )
                or "Evaluation root"
            )
        elif selected is copy_structural_path:
            text = info.path_text
        else:
            return

        QApplication.clipboard().setText(text)

    def _copy_current_tree_component(self) -> None:
        info = self.tree_model.node_info(self.tree.currentIndex())
        document = self.coordinator.selected_document
        if info is not None and document is not None:
            QApplication.clipboard().setText(document.component_label(info.path))
