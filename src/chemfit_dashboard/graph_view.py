from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPainterPath, QPen, QWheelEvent
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from chemfit_dashboard.appearance import ColorScheme, active_color_scheme
from chemfit_dashboard.graph_layout import (
    GraphLayout,
    LayoutNode,
    dependency_layered_layout,
)
from chemfit_dashboard.source import (
    ContextDocument,
    ContextSourceError,
    NodeInfo,
    NodePath,
)
from chemfit_dashboard.traversal import CancellationToken
from chemfit_dashboard.workers import TaskRunner

NODE_WIDTH = 196.0
NODE_HEIGHT = 76.0
MAX_VISIBLE_NODES = 2_000
BACKGROUND_LAYOUT_THRESHOLD = 300


class ContextGraphView(QGraphicsView):
    """Collapsible dependency-aware view of a context hierarchy."""

    node_selected = Signal(object)
    render_finished = Signal(int, int)

    def __init__(self, parent: QWidget | None = None):
        """Initialize an empty deferred graph canvas."""
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._document: ContextDocument | None = None
        self._rendered = False
        self._nodes: dict[NodePath, QGraphicsRectItem] = {}
        self._selected_path: NodePath | None = None
        self._expanded: set[NodePath] = {()}
        self._tasks = TaskRunner(self)
        self._layout_generation = 0
        self._layout_cancellation: CancellationToken | None = None
        self._scheme = active_color_scheme()
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setBackgroundBrush(QColor(self._scheme.graph))
        self._scene.selectionChanged.connect(self._selection_changed)
        self._show_message("Select an evaluation to build its context graph.")

    @property
    def rendered_node_count(self) -> int:
        return len(self._nodes)

    @property
    def document(self) -> ContextDocument | None:
        return self._document

    @property
    def expanded_paths(self) -> frozenset[NodePath]:
        return frozenset(self._expanded)

    def set_document(self, document: ContextDocument | None) -> None:
        self._cancel_layout()
        self._document = document
        self._rendered = False
        self._nodes = {}
        self._expanded = {()}
        self._scene.clear()
        if document is None:
            self._selected_path = None
            self._show_message("Select an evaluation to build its context graph.")
        else:
            self._show_message("Open this tab to render the context graph.")

    def set_color_scheme(self, scheme: ColorScheme) -> None:
        self._cancel_layout()
        was_rendered = self._rendered
        self._scheme = scheme
        self.setBackgroundBrush(QColor(scheme.graph))
        self._rendered = False
        self._nodes = {}
        self._scene.clear()
        if self._document is None:
            self._show_message("Select an evaluation to build its context graph.")
        elif was_rendered:
            self.ensure_rendered()
        else:
            self._show_message("Open this tab to render the context graph.")

    def ensure_rendered(self) -> None:
        if self._rendered or self._document is None:
            return
        self._rendered = True
        self._render_graph(self._document)
        self._apply_selected_path()

    def select_path(self, path: NodePath) -> None:
        self._selected_path = path
        self._expanded.update(path[:depth] for depth in range(len(path)))
        if self._rendered and path not in self._nodes and self._document is not None:
            self._render_graph(self._document)
        self._apply_selected_path()

    def expand_selected(self) -> None:
        if self._selected_path is None or self._document is None:
            return
        try:
            selected = self._document.details(self._selected_path).info
        except ContextSourceError:
            return
        if not selected.child_count or self._selected_path in self._expanded:
            return
        self._expanded.add(self._selected_path)
        self._rebuild()

    def collapse_selected(self) -> None:
        if self._selected_path is None:
            return
        selected = self._selected_path
        descendants = {
            path
            for path in self._expanded
            if len(path) > len(selected) and path[: len(selected)] == selected
        }
        self._expanded.discard(selected)
        self._expanded.difference_update(descendants)
        self._rebuild()

    def expand_all(self) -> None:
        if self._document is not None:
            self._expanded = {
                node.path for node in self._document.nodes if node.child_count
            }
            self._rebuild()

    def overview(self) -> None:
        self._expanded = {()}
        self._rebuild()

    def cancel_pending_layout(self) -> None:
        """Cancel queued or running layout work cooperatively."""
        self._cancel_layout()
        self._tasks.cancel_pending()

    def _rebuild(self) -> None:
        if self._document is not None:
            self._rendered = True
            self._render_graph(self._document)
            self._apply_selected_path()

    def _apply_selected_path(self) -> None:
        if self._selected_path is None:
            return
        item = self._nodes.get(self._selected_path)
        if item is not None:
            self._scene.clearSelection()
            item.setSelected(True)
            self.ensureVisible(item, 30, 30)

    def zoom_in(self) -> None:
        self.scale(1.2, 1.2)

    def zoom_out(self) -> None:
        self.scale(1 / 1.2, 1 / 1.2)

    def fit_graph(self) -> None:
        bounds = self._scene.itemsBoundingRect()
        if not bounds.isEmpty():
            self.fitInView(
                bounds.adjusted(-30, -30, 30, 30), Qt.AspectRatioMode.KeepAspectRatio
            )

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)
        event.accept()

    def _visible_paths(self, document: ContextDocument) -> tuple[NodePath, ...]:
        visible = [
            node.path
            for node in document.nodes
            if not node.path
            or all(
                node.path[:depth] in self._expanded for depth in range(len(node.path))
            )
        ]
        visible.sort(key=lambda path: (len(path), path))
        if len(visible) <= MAX_VISIBLE_NODES:
            return tuple(visible)
        priority: list[NodePath] = []
        if self._selected_path is not None:
            priority = [
                self._selected_path[:depth]
                for depth in range(len(self._selected_path) + 1)
                if self._selected_path[:depth] in visible
            ]
        result = list(dict.fromkeys(priority))
        result.extend(path for path in visible if path not in result)
        return tuple(result[:MAX_VISIBLE_NODES])

    def _render_graph(self, document: ContextDocument) -> None:
        self._cancel_layout()
        self._layout_generation += 1
        generation = self._layout_generation
        self._scene.clear()
        self._nodes = {}
        visible_paths = self._visible_paths(document)
        visible_set = set(visible_paths)
        info_by_path = {node.path: node for node in document.nodes}
        hierarchy = [
            (path[:-1], path)
            for path in visible_paths
            if path and path[:-1] in visible_set
        ]
        dependencies: set[tuple[NodePath, NodePath]] = set()
        for parent in document.nodes:
            structure = document.details(parent.path).structure
            raw_dependencies = structure.get("dependencies") if structure else None
            if not isinstance(raw_dependencies, list):
                continue
            for target_index, sources in enumerate(raw_dependencies):
                if not isinstance(sources, list):
                    continue
                for source_index in sources:
                    if not isinstance(source_index, int):
                        continue
                    source = _visible_ancestor(
                        (*parent.path, source_index), visible_set
                    )
                    target = _visible_ancestor(
                        (*parent.path, target_index), visible_set
                    )
                    if source is not None and target is not None and source != target:
                        dependencies.add((source, target))

        layout_nodes = tuple(
            LayoutNode(path, path[:-1] if path else None) for path in visible_paths
        )
        if len(visible_paths) > BACKGROUND_LAYOUT_THRESHOLD:
            cancellation = CancellationToken()
            self._layout_cancellation = cancellation
            self._show_message(
                f"Calculating layout for {len(visible_paths):,} visible contexts…"
            )

            def calculate_layout() -> GraphLayout:
                cancellation.raise_if_cancelled()
                result = dependency_layered_layout(
                    layout_nodes,
                    hierarchy,
                    dependencies,
                    node_width=NODE_WIDTH,
                    node_height=NODE_HEIGHT,
                )
                cancellation.raise_if_cancelled()
                return result

            self._tasks.submit(
                calculate_layout,
                lambda result: self._layout_ready(
                    generation,
                    document,
                    visible_paths,
                    info_by_path,
                    result,
                ),
                lambda _details: self._layout_failed(generation, cancellation),
            )
            return
        graph_layout = dependency_layered_layout(
            layout_nodes,
            hierarchy,
            dependencies,
            node_width=NODE_WIDTH,
            node_height=NODE_HEIGHT,
        )
        self._finish_render(document, visible_paths, info_by_path, graph_layout)

    def _layout_ready(
        self,
        generation: int,
        document: ContextDocument,
        visible_paths: tuple[NodePath, ...],
        info_by_path: dict[NodePath, NodeInfo],
        graph_layout: GraphLayout,
    ) -> None:
        if generation != self._layout_generation or document is not self._document:
            return
        self._layout_cancellation = None
        self._finish_render(document, visible_paths, info_by_path, graph_layout)
        self._apply_selected_path()

    def _layout_failed(self, generation: int, cancellation: CancellationToken) -> None:
        if generation != self._layout_generation or cancellation.cancelled:
            return
        self._layout_cancellation = None
        self._scene.clear()
        self._show_message("Could not calculate the context graph layout.")

    def _cancel_layout(self) -> None:
        if self._layout_cancellation is not None:
            self._layout_cancellation.cancel()
            self._layout_cancellation = None

    def _finish_render(
        self,
        document: ContextDocument,
        visible_paths: tuple[NodePath, ...],
        info_by_path: dict[NodePath, NodeInfo],
        graph_layout: GraphLayout,
    ) -> None:
        self._scene.clear()
        self._nodes = {}
        hierarchy_pen = QPen(QColor(self._scheme.hierarchy_edge), 1.6)
        dependency_pen = QPen(
            QColor(self._scheme.dependency_edge), 1.4, Qt.PenStyle.DashLine
        )
        for edge in graph_layout.edges:
            edge_path = QPainterPath(QPointF(*edge.points[0]))
            edge_path.cubicTo(
                QPointF(*edge.points[1]),
                QPointF(*edge.points[2]),
                QPointF(*edge.points[3]),
            )
            self._scene.addPath(
                edge_path,
                hierarchy_pen if edge.kind == "hierarchy" else dependency_pen,
            )

        for path in visible_paths:
            info = info_by_path[path]
            metadata_name = document.details(path).metadata.get("name")
            static_name = (
                metadata_name
                if isinstance(metadata_name, str) and metadata_name.strip()
                else None
            )
            hidden_count = (
                _descendant_count(document, path) if path not in self._expanded else 0
            )
            self._nodes[path] = self._add_node(
                info, QPointF(*graph_layout.positions[path]), static_name, hidden_count
            )
        if document.node_count > len(visible_paths):
            message = self._scene.addSimpleText(
                f"Overview: {len(visible_paths):,} visible of {document.node_count:,} contexts"
            )
            message.setBrush(QColor(self._scheme.warning))
            message.setPos(12, -32)
        self.fit_graph()
        self.render_finished.emit(len(visible_paths), document.node_count)

    def _add_node(
        self,
        info: NodeInfo,
        position: QPointF,
        static_name: str | None,
        hidden_count: int,
    ) -> QGraphicsRectItem:
        item = QGraphicsRectItem(QRectF(0, 0, NODE_WIDTH, NODE_HEIGHT))
        item.setPos(position)
        item.setBrush(QBrush(QColor(_node_color(info, self._scheme))))
        item.setPen(QPen(QColor(self._scheme.node_border), 1.2))
        item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        item.setData(0, info.path)
        tooltip = f"{info.kind} · path {info.path_text} · {info.status}"
        if hidden_count:
            tooltip += f"\n{hidden_count:,} descendants collapsed"
        if static_name is not None:
            tooltip += f"\nstatic name: {static_name}"
        item.setToolTip(tooltip)
        self._scene.addItem(item)
        suffix = f"  +{hidden_count}" if hidden_count else ""
        title = QGraphicsSimpleTextItem(_elide(info.label, 22) + suffix, item)
        title.setBrush(QColor(self._scheme.text_strong))
        title.setPos(10, 8)
        title.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        if static_name is not None:
            name = QGraphicsSimpleTextItem(f"name: {_elide(static_name, 25)}", item)
            name.setBrush(QColor(self._scheme.accent))
            name.setPos(10, 29)
            name.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        detail = QGraphicsSimpleTextItem(f"{info.status}   ·   {info.path_text}", item)
        detail.setBrush(QColor(self._scheme.text))
        detail.setPos(10, 50)
        detail.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        return item

    def _selection_changed(self) -> None:
        selected = self._scene.selectedItems()
        if selected:
            self._selected_path = selected[0].data(0)
            self.node_selected.emit(self._selected_path)

    def _show_message(self, text: str) -> None:
        message = self._scene.addSimpleText(text)
        message.setBrush(QColor(self._scheme.muted))
        message.setPos(20, 20)


class ContextGraphPanel(QWidget):
    """Graph view with navigation and collapse controls."""

    node_selected = Signal(object)

    def __init__(self, parent: QWidget | None = None):
        """Initialize graph controls and the graphics view."""
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        controls = QHBoxLayout()
        self.status = QLabel("Graph builds on demand")
        self.status.setObjectName("mutedLabel")
        controls.addWidget(self.status)
        controls.addStretch()
        self.expand_button = QPushButton("Expand")
        self.collapse_button = QPushButton("Collapse")
        self.expand_all_button = QPushButton("Expand all")
        self.overview_button = QPushButton("Overview")
        self.zoom_out_button = QPushButton("-")
        self.zoom_in_button = QPushButton("+")
        self.fit_button = QPushButton("Fit")
        for button in (
            self.expand_button,
            self.collapse_button,
            self.expand_all_button,
            self.overview_button,
            self.zoom_out_button,
            self.zoom_in_button,
            self.fit_button,
        ):
            controls.addWidget(button)
        layout.addLayout(controls)
        self.view = ContextGraphView()
        self.view.node_selected.connect(self.node_selected.emit)
        self.view.render_finished.connect(
            lambda shown, total: self.status.setText(
                f"{shown:,} of {total:,} contexts visible"
            )
        )
        self.expand_button.clicked.connect(self.view.expand_selected)
        self.collapse_button.clicked.connect(self.view.collapse_selected)
        self.expand_all_button.clicked.connect(self.view.expand_all)
        self.overview_button.clicked.connect(self.view.overview)
        self.zoom_out_button.clicked.connect(self.view.zoom_out)
        self.zoom_in_button.clicked.connect(self.view.zoom_in)
        self.fit_button.clicked.connect(self.view.fit_graph)
        layout.addWidget(self.view, 1)
        legend = QLabel(
            "Solid: hierarchy · Dashed: dependencies · +N: collapsed descendants"
        )
        legend.setObjectName("mutedLabel")
        layout.addWidget(legend)

    def set_document(self, document: ContextDocument | None) -> None:
        self.view.set_document(document)
        self.status.setText(
            "Graph builds on demand"
            if document is not None
            else "No evaluation selected"
        )

    def ensure_rendered(self) -> None:
        self.view.ensure_rendered()
        document = self.view.document
        if document is not None:
            self.status.setText(
                f"{self.view.rendered_node_count:,} of {document.node_count:,} contexts visible"
            )

    def select_path(self, path: NodePath) -> None:
        self.view.select_path(path)

    def set_color_scheme(self, scheme: ColorScheme) -> None:
        self.view.set_color_scheme(scheme)


def _visible_ancestor(path: NodePath, visible: set[NodePath]) -> NodePath | None:
    candidate = path
    while candidate not in visible:
        if not candidate:
            return None
        candidate = candidate[:-1]
    return candidate


def _descendant_count(document: ContextDocument, path: NodePath) -> int:
    return sum(
        1
        for node in document.nodes
        if len(node.path) > len(path) and node.path[: len(path)] == path
    )


def _node_color(info: NodeInfo, scheme: ColorScheme) -> str:
    if not info.recorded:
        return scheme.node_missing
    return {
        "complete": scheme.node_complete,
        "skipped": scheme.node_skipped,
        "partial": scheme.node_partial,
        "no output": scheme.node_missing,
    }.get(info.status, scheme.node)


def _elide(value: str, limit: int) -> str:
    return value if len(value) <= limit else f"{value[: limit - 1]}…"
