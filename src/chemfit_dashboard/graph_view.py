from __future__ import annotations

from collections import defaultdict

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
from chemfit_dashboard.source import ContextDocument, NodeInfo, NodePath

NODE_WIDTH = 196.0
NODE_HEIGHT = 76.0
HORIZONTAL_GAP = 34.0
VERTICAL_GAP = 92.0
MAX_GRAPH_NODES = 1000


class ContextGraphView(QGraphicsView):
    """Deferred graphics view of context hierarchy and dependency edges."""

    node_selected = Signal(object)

    def __init__(self, parent: QWidget | None = None):
        """Initialize an empty, pannable graph canvas."""
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._document: ContextDocument | None = None
        self._rendered = False
        self._nodes: dict[NodePath, QGraphicsRectItem] = {}
        self._selected_path: NodePath | None = None
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

    def set_document(self, document: ContextDocument | None) -> None:
        self._document = document
        self._rendered = False
        self._nodes = {}
        self._selected_path = None
        self._scene.clear()
        if document is None:
            self._show_message("Select an evaluation to build its context graph.")
        else:
            self._show_message("Open this tab to render the context graph.")

    def set_color_scheme(self, scheme: ColorScheme) -> None:
        """Repaint custom graph elements without losing its semantic selection."""
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
        self._apply_selected_path()

    def _apply_selected_path(self) -> None:
        if self._selected_path is None:
            return
        item = self._nodes.get(self._selected_path)
        if item is None:
            return
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

    def _render_graph(  # noqa: PLR0912
        self, document: ContextDocument
    ) -> None:
        self._scene.clear()
        self._nodes = {}
        nodes = document.nodes[:MAX_GRAPH_NODES]
        levels: dict[int, list[NodeInfo]] = defaultdict(list)
        for node in nodes:
            levels[len(node.path)].append(node)

        max_level_size = max((len(level) for level in levels.values()), default=1)
        canvas_width = max_level_size * (NODE_WIDTH + HORIZONTAL_GAP)
        positions: dict[NodePath, QPointF] = {}
        for depth, level in levels.items():
            level_width = len(level) * (NODE_WIDTH + HORIZONTAL_GAP)
            offset = (canvas_width - level_width) / 2
            for column, node in enumerate(level):
                positions[node.path] = QPointF(
                    offset + column * (NODE_WIDTH + HORIZONTAL_GAP),
                    depth * (NODE_HEIGHT + VERTICAL_GAP),
                )

        hierarchy_pen = QPen(QColor(self._scheme.hierarchy_edge), 1.6)
        for node in nodes:
            if not node.path or node.path[:-1] not in positions:
                continue
            self._scene.addPath(
                _edge_path(positions[node.path[:-1]], positions[node.path]),
                hierarchy_pen,
            )

        visible_paths = set(positions)
        dependency_pen = QPen(
            QColor(self._scheme.dependency_edge), 1.4, Qt.PenStyle.DashLine
        )
        for parent in nodes:
            structure = document.details(parent.path).structure
            if structure is None:
                continue
            dependencies = structure.get("dependencies")
            if not isinstance(dependencies, list):
                continue
            for target_index, sources in enumerate(dependencies):
                target_path = (*parent.path, target_index)
                if target_path not in visible_paths or not isinstance(sources, list):
                    continue
                for source_index in sources:
                    source_path = (*parent.path, source_index)
                    if source_path in visible_paths:
                        self._scene.addPath(
                            _dependency_path(
                                positions[source_path], positions[target_path]
                            ),
                            dependency_pen,
                        )

        for node in nodes:
            metadata_name = document.details(node.path).metadata.get("name")
            static_name = (
                metadata_name
                if isinstance(metadata_name, str) and metadata_name.strip()
                else None
            )
            self._nodes[node.path] = self._add_node(
                node, positions[node.path], static_name
            )

        if document.node_count > len(nodes):
            message = self._scene.addSimpleText(
                f"Graph limited to {len(nodes):,} of {document.node_count:,} contexts"
            )
            message.setBrush(QColor(self._scheme.warning))
            message.setPos(12, -32)
        self.fit_graph()

    def _add_node(
        self, info: NodeInfo, position: QPointF, static_name: str | None
    ) -> QGraphicsRectItem:
        item = QGraphicsRectItem(QRectF(0, 0, NODE_WIDTH, NODE_HEIGHT))
        item.setPos(position)
        item.setBrush(QBrush(QColor(_node_color(info, self._scheme))))
        item.setPen(QPen(QColor(self._scheme.node_border), 1.2))
        item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        item.setData(0, info.path)
        tooltip = f"{info.kind} · path {info.path_text} · {info.status}"
        if static_name is not None:
            tooltip += f"\nstatic name: {static_name}"
        item.setToolTip(tooltip)
        self._scene.addItem(item)

        title = QGraphicsSimpleTextItem(_elide(info.label, 24), item)
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
            self.node_selected.emit(selected[0].data(0))

    def _show_message(self, text: str) -> None:
        message = self._scene.addSimpleText(text)
        message.setBrush(QColor(self._scheme.muted))
        message.setPos(20, 20)


class ContextGraphPanel(QWidget):
    """Graph view with compact controls and a visual-edge legend."""

    node_selected = Signal(object)

    def __init__(self, parent: QWidget | None = None):
        """Initialize the graph controls and canvas."""
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        controls = QHBoxLayout()
        self.status = QLabel("Graph builds on demand")
        self.status.setObjectName("mutedLabel")
        controls.addWidget(self.status)
        controls.addStretch()
        self.zoom_out_button = QPushButton("-")
        self.zoom_out_button.setToolTip("Zoom out")
        self.zoom_in_button = QPushButton("+")
        self.zoom_in_button.setToolTip("Zoom in")
        self.fit_button = QPushButton("Fit")
        self.fit_button.setToolTip("Fit the complete graph")
        controls.addWidget(self.zoom_out_button)
        controls.addWidget(self.zoom_in_button)
        controls.addWidget(self.fit_button)
        layout.addLayout(controls)

        self.view = ContextGraphView()
        self.view.node_selected.connect(self.node_selected.emit)
        self.zoom_out_button.clicked.connect(self.view.zoom_out)
        self.zoom_in_button.clicked.connect(self.view.zoom_in)
        self.fit_button.clicked.connect(self.view.fit_graph)
        layout.addWidget(self.view, 1)

        legend = QLabel(
            "Solid lines: context hierarchy   ·   Dashed lines: declared dependencies"
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
            shown = self.view.rendered_node_count
            self.status.setText(f"{shown:,} of {document.node_count:,} contexts")

    def select_path(self, path: NodePath) -> None:
        self.view.select_path(path)

    def set_color_scheme(self, scheme: ColorScheme) -> None:
        self.view.set_color_scheme(scheme)


def _edge_path(parent: QPointF, child: QPointF) -> QPainterPath:
    start = QPointF(parent.x() + NODE_WIDTH / 2, parent.y() + NODE_HEIGHT)
    end = QPointF(child.x() + NODE_WIDTH / 2, child.y())
    midpoint = (start.y() + end.y()) / 2
    path = QPainterPath(start)
    path.cubicTo(start.x(), midpoint, end.x(), midpoint, end.x(), end.y())
    return path


def _dependency_path(source: QPointF, target: QPointF) -> QPainterPath:
    start = QPointF(source.x() + NODE_WIDTH / 2, source.y() + NODE_HEIGHT)
    end = QPointF(target.x() + NODE_WIDTH / 2, target.y() + NODE_HEIGHT)
    curve_y = max(start.y(), end.y()) + 28
    path = QPainterPath(start)
    path.cubicTo(start.x(), curve_y, end.x(), curve_y, end.x(), end.y())
    return path


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
