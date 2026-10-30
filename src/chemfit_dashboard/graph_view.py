from __future__ import annotations

import math
from typing import TYPE_CHECKING

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QCursor,
    QFont,
    QFontMetricsF,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsPathItem,
    QGraphicsPolygonItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsSceneMouseEvent,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import isValid

from chemfit_dashboard.appearance import ColorScheme, active_color_scheme
from chemfit_dashboard.data_flow import DataFlowGraph, DataFlowNode, build_data_flow
from chemfit_dashboard.graph_layout import GraphLayout, data_flow_layout
from chemfit_dashboard.source import ContextDocument, ContextSourceError, NodePath
from chemfit_dashboard.traversal import CancellationToken
from chemfit_dashboard.workers import TaskRunner

if TYPE_CHECKING:
    from collections.abc import Callable

NODE_WIDTH = 220.0
NODE_HEIGHT = 76.0
BACKGROUND_LAYOUT_THRESHOLD = 180


class _FlowNodeItem(QGraphicsPathItem):
    def __init__(
        self,
        shape: QPainterPath,
        node: DataFlowNode,
        open_nested: Callable[[NodePath], None],
    ) -> None:
        super().__init__(shape)
        self.node = node
        self._open_nested = open_nested
        self.normal_pen = QPen()

    def mouseDoubleClickEvent(self, event: QGraphicsSceneMouseEvent) -> None:  # noqa: N802
        if self.node.nested and self.node.path is not None:
            self._open_nested(self.node.path)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class DataFlowView(QGraphicsView):
    """Interactive canvas for one declared structural data-flow level."""

    node_selected = Signal(object)
    navigate_requested = Signal(object)
    level_changed = Signal(object)
    render_finished = Signal(int)

    def __init__(self, parent: QWidget | None = None):
        """Initialize an empty, deferred data-flow canvas."""
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._document: ContextDocument | None = None
        self._current_path: NodePath = ()
        self._selected_path: NodePath | None = None
        self._rendered = False
        self._nodes: dict[NodePath, _FlowNodeItem] = {}
        self._items_by_id: dict[str, _FlowNodeItem] = {}
        self._clearing_scene = False
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
        self._show_message("Select an evaluation to inspect its declared data flow.")

    @property
    def document(self) -> ContextDocument | None:
        return self._document

    @property
    def current_path(self) -> NodePath:
        return self._current_path

    @property
    def rendered_node_count(self) -> int:
        return len(self._items_by_id)

    def set_document(self, document: ContextDocument | None) -> None:
        self._cancel_layout()
        self._document = document
        self._current_path = ()
        self._selected_path = None
        self._rendered = False
        self._clear_scene()
        if document is None:
            self._show_message("Select an evaluation to inspect its declared data flow.")
        else:
            self._show_message("Open this tab to render the declared data flow.")
        self.level_changed.emit(self._current_path)

    def set_color_scheme(self, scheme: ColorScheme) -> None:
        was_rendered = self._rendered
        self._cancel_layout()
        self._scheme = scheme
        self.setBackgroundBrush(QColor(scheme.graph))
        self._rendered = False
        if was_rendered:
            self.ensure_rendered()

    def refresh_labels(self) -> None:
        """Rebuild visible node text while retaining navigation and selection."""
        was_rendered = self._rendered
        self._cancel_layout()
        self._layout_generation += 1
        self._rendered = False
        self._clear_scene()
        if was_rendered:
            self.ensure_rendered()

    def ensure_rendered(self) -> None:
        if self._rendered or self._document is None:
            return
        self._rendered = True
        self._render_current()

    def navigate_to(self, path: NodePath) -> None:
        if self._document is None:
            return
        try:
            self._document.details(path)
        except ContextSourceError:
            return
        self._current_path = path
        self._selected_path = path
        self._rendered = True
        self.level_changed.emit(path)
        self._render_current()

    def select_path(self, path: NodePath) -> None:
        self._selected_path = path
        if self._document is None:
            return
        if path == self._current_path:
            self._apply_selection()
            return
        if path[:-1] == self._current_path and path in self._nodes:
            self._apply_selection()
            return
        level = self._level_for_selection(path)
        if level != self._current_path:
            self._current_path = level
            self.level_changed.emit(level)
            if self._rendered:
                self._render_current()
        self._apply_selection()

    def _level_for_selection(self, path: NodePath) -> NodePath:
        if self._document is None or not path:
            return ()
        parent = path[:-1]
        try:
            if self._document.details(parent).structure is not None:
                return parent
        except ContextSourceError:
            pass
        candidate = path
        while candidate:
            try:
                if self._document.details(candidate).structure is not None:
                    return candidate
            except ContextSourceError:
                pass
            candidate = candidate[:-1]
        return ()

    def zoom_in(self) -> None:
        self.scale(1.2, 1.2)

    def zoom_out(self) -> None:
        self.scale(1 / 1.2, 1 / 1.2)

    def fit_graph(self) -> None:
        bounds = self._node_bounds()
        if bounds.isEmpty():
            return
        padded = bounds.adjusted(-40, -40, 40, 40)
        self.setSceneRect(padded)
        self.resetTransform()
        self.fitInView(padded, Qt.AspectRatioMode.KeepAspectRatio)
        if self.transform().m11() > 1.0:
            self.resetTransform()
            self.centerOn(padded.center())

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)
        event.accept()

    def cancel_pending_layout(self) -> None:
        self._cancel_layout()
        self._tasks.cancel_pending()

    def _render_current(self) -> None:
        document = self._document
        if document is None:
            return
        self._cancel_layout()
        self._layout_generation += 1
        generation = self._layout_generation
        graph = build_data_flow(document, self._current_path)
        self._clear_scene()
        if not graph.available:
            self._show_message(graph.message or "No declared data flow is available.")
            self.render_finished.emit(0)
            return
        edge_pairs = tuple((edge.source, edge.target) for edge in graph.edges)
        if len(graph.nodes) >= BACKGROUND_LAYOUT_THRESHOLD:
            cancellation = CancellationToken()
            self._layout_cancellation = cancellation
            self._show_message(f"Laying out {len(graph.nodes):,} flow nodes…")
            self._tasks.submit(
                lambda: data_flow_layout(
                    graph.node_ids,
                    edge_pairs,
                    node_width=NODE_WIDTH,
                    node_height=NODE_HEIGHT,
                    cancelled=lambda: cancellation.cancelled,
                ),
                lambda layout: self._layout_ready(
                    generation, document, graph, cancellation, layout
                ),
                lambda _details: self._layout_failed(generation, cancellation),
            )
            return
        layout = data_flow_layout(
            graph.node_ids,
            edge_pairs,
            node_width=NODE_WIDTH,
            node_height=NODE_HEIGHT,
        )
        self._finish_render(graph, layout)

    def _layout_ready(
        self,
        generation: int,
        document: ContextDocument,
        graph: DataFlowGraph,
        cancellation: CancellationToken,
        layout: GraphLayout,
    ) -> None:
        if (
            generation != self._layout_generation
            or document is not self._document
            or cancellation.cancelled
        ):
            return
        self._layout_cancellation = None
        self._finish_render(graph, layout)

    def _layout_failed(
        self, generation: int, cancellation: CancellationToken
    ) -> None:
        if generation != self._layout_generation or cancellation.cancelled:
            return
        self._layout_cancellation = None
        self._clear_scene()
        self._show_message("Could not calculate the data-flow layout.")

    def _cancel_layout(self) -> None:
        if self._layout_cancellation is not None:
            self._layout_cancellation.cancel()
            self._layout_cancellation = None

    def _finish_render(self, graph: DataFlowGraph, layout: GraphLayout) -> None:
        self._clear_scene()
        edge_color = QColor(self._scheme.accent)
        edge_pen = QPen(edge_color, 2.0)
        for edge in layout.edges:
            path = QPainterPath(QPointF(*edge.points[0]))
            path.cubicTo(*(QPointF(*point) for point in edge.points[1:]))
            self._scene.addPath(path, edge_pen)
            self._add_arrowhead(edge.points[-2], edge.points[-1], edge_color)
        for node in graph.nodes:
            position = layout.positions.get(node.id)
            if position is None:
                continue
            item = self._add_node(node, QPointF(*position))
            self._items_by_id[node.id] = item
            if node.path is not None:
                self._nodes[node.path] = item
        if graph.message:
            message = self._scene.addSimpleText(graph.message)
            message.setBrush(QColor(self._scheme.muted))
            message.setPos(0, -30)
        self._apply_selection(reveal=False)
        self.fit_graph()
        self.render_finished.emit(len(graph.nodes))

    def _add_node(self, node: DataFlowNode, position: QPointF) -> _FlowNodeItem:
        boundary = node.kind != "child"
        shape = QPainterPath()
        radius = NODE_HEIGHT / 2 if boundary else 10
        shape.addRoundedRect(QRectF(0, 0, NODE_WIDTH, NODE_HEIGHT), radius, radius)
        item = _FlowNodeItem(shape, node, self.navigate_requested.emit)
        item.setPos(position)
        status_color = QColor(_status_color(node, self._scheme))
        fill = QColor(self._scheme.accent_soft) if boundary else QColor(self._scheme.node)
        item.setBrush(QBrush(fill))
        normal_pen = QPen(
            QColor(self._scheme.accent if boundary or node.nested else self._scheme.node_border),
            2.0 if boundary or node.nested else 1.2,
        )
        item.normal_pen = normal_pen
        item.setPen(normal_pen)
        if node.path is not None:
            item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
            item.setData(0, node.path)
            item.setData(1, "structural" if node.nested else "atomic")
        if node.nested:
            item.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        details = [node.status]
        if node.component_type:
            details.insert(0, node.component_type)
        if node.nested:
            details.append("double-click to open")
        item.setToolTip(" · ".join(details))
        self._scene.addItem(item)

        status_bar = QGraphicsRectItem(0, 12, 5, NODE_HEIGHT - 24, item)
        status_bar.setBrush(QBrush(status_color))
        status_bar.setPen(QPen(Qt.PenStyle.NoPen))
        status_bar.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        title = QGraphicsSimpleTextItem(
            _elide(node.label, QFont(), NODE_WIDTH - 28),
            item,
        )
        title.setBrush(QColor(self._scheme.text_strong))
        title.setPos(14, 11 if node.component_type or node.kind == "child" else 25)
        title.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        if node.kind == "child":
            detail_text = " · ".join(
                part for part in (node.status, node.component_type) if part
            )
            if node.nested:
                detail_text = f"{detail_text}  >"
            detail = QGraphicsSimpleTextItem(
                _elide(detail_text, QFont(), NODE_WIDTH - 28), item
            )
            detail.setBrush(QColor(self._scheme.text))
            detail.setPos(14, 43)
            detail.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        return item

    def _apply_selection(self, *, reveal: bool = True) -> None:
        self._prune_deleted_items()
        signals_were_blocked = self._scene.blockSignals(True)  # noqa: FBT003
        try:
            self._scene.clearSelection()
            item = self._nodes.get(self._selected_path or ())
            if item is not None and isValid(item):
                item.setSelected(True)
        finally:
            self._scene.blockSignals(signals_were_blocked)
        self._update_node_pens()
        if reveal and item is not None and isValid(item):
            self.ensureVisible(item, 30, 30)

    def _selection_changed(self) -> None:
        if self._clearing_scene:
            return
        self._update_node_pens()
        selected = self._scene.selectedItems()
        if not selected:
            return
        path = selected[0].data(0)
        if isinstance(path, tuple):
            self._selected_path = path
            self.node_selected.emit(path)

    def _update_node_pens(self) -> None:
        for item in self._live_node_items():
            if item.isSelected():
                item.setPen(QPen(QColor(self._scheme.accent), 3.5))
            else:
                item.setPen(item.normal_pen)

    def _clear_scene(self) -> None:
        """Drop Python item references before Qt destroys their C++ objects."""
        self._clearing_scene = True
        signals_were_blocked = self._scene.blockSignals(True)  # noqa: FBT003
        try:
            self._nodes.clear()
            self._items_by_id.clear()
            self._scene.clear()
        finally:
            self._scene.blockSignals(signals_were_blocked)
            self._clearing_scene = False

    def _live_node_items(self) -> tuple[_FlowNodeItem, ...]:
        """Return live Qt items and discard wrappers whose C++ items are gone."""
        self._prune_deleted_items()
        return tuple(self._items_by_id.values())

    def _prune_deleted_items(self) -> None:
        """Forget graphics-item wrappers invalidated by Qt ownership teardown."""
        stale_ids = {
            node_id
            for node_id, item in self._items_by_id.items()
            if not isValid(item)
        }
        if not stale_ids:
            return
        stale_items = tuple(self._items_by_id[node_id] for node_id in stale_ids)
        for node_id in stale_ids:
            self._items_by_id.pop(node_id, None)
        for path, item in tuple(self._nodes.items()):
            if any(item is stale for stale in stale_items):
                self._nodes.pop(path, None)

    def _node_bounds(self) -> QRectF:
        bounds = QRectF()
        for item in self._live_node_items():
            bounds = bounds.united(item.sceneBoundingRect())
        return bounds

    def _add_arrowhead(
        self,
        previous: tuple[float, float],
        target: tuple[float, float],
        color: QColor,
    ) -> None:
        dx, dy = target[0] - previous[0], target[1] - previous[1]
        length = math.hypot(dx, dy)
        if not length:
            return
        ux, uy = dx / length, dy / length
        px, py = -uy, ux
        size = 8.0
        base_x, base_y = target[0] - ux * size, target[1] - uy * size
        polygon = QPolygonF(
            [
                QPointF(*target),
                QPointF(base_x + px * size * 0.55, base_y + py * size * 0.55),
                QPointF(base_x - px * size * 0.55, base_y - py * size * 0.55),
            ]
        )
        arrow = QGraphicsPolygonItem(polygon)
        arrow.setBrush(QBrush(color))
        arrow.setPen(QPen(color))
        self._scene.addItem(arrow)

    def _show_message(self, text: str) -> None:
        message = self._scene.addSimpleText(text)
        message.setBrush(QColor(self._scheme.muted))
        message.setPos(20, 20)


class DataFlowPanel(QWidget):
    """Data-flow canvas with breadcrumbs and level navigation."""

    node_selected = Signal(object)

    def __init__(self, parent: QWidget | None = None):
        """Initialize flow controls, navigation, and the graphics view."""
        super().__init__(parent)
        self._document: ContextDocument | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        controls = QHBoxLayout()
        self.back_button = QPushButton("Back")
        self.back_button.setEnabled(False)
        self.breadcrumbs = QHBoxLayout()
        self.breadcrumbs.setSpacing(3)
        controls.addWidget(self.back_button)
        controls.addLayout(self.breadcrumbs, 1)
        self.fit_button = QPushButton("Fit View")
        self.zoom_out_button = QPushButton("-")
        self.zoom_in_button = QPushButton("+")
        controls.addWidget(self.fit_button)
        controls.addWidget(self.zoom_out_button)
        controls.addWidget(self.zoom_in_button)
        layout.addLayout(controls)
        self.status = QLabel("Data Flow builds on demand")
        self.status.setObjectName("mutedLabel")
        layout.addWidget(self.status)
        self.view = DataFlowView()
        layout.addWidget(self.view, 1)
        hint = QLabel(
            "Arrows show declared data connections. Double-click a nested component to open it."
        )
        hint.setObjectName("mutedLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.view.node_selected.connect(self.node_selected.emit)
        self.view.navigate_requested.connect(self._enter)
        self.view.level_changed.connect(self._update_breadcrumbs)
        self.view.render_finished.connect(
            lambda count: self.status.setText(
                f"{count:,} declared flow node{'s' if count != 1 else ''}"
            )
        )
        self.back_button.clicked.connect(self._back)
        self.fit_button.clicked.connect(self.view.fit_graph)
        self.zoom_out_button.clicked.connect(self.view.zoom_out)
        self.zoom_in_button.clicked.connect(self.view.zoom_in)
        self._update_breadcrumbs(())

    def set_document(self, document: ContextDocument | None) -> None:
        self._document = document
        self.view.set_document(document)
        self.status.setText(
            "Data Flow builds on demand" if document is not None else "No evaluation selected"
        )

    def ensure_rendered(self) -> None:
        self.view.ensure_rendered()

    def select_path(self, path: NodePath) -> None:
        self.view.select_path(path)

    def set_color_scheme(self, scheme: ColorScheme) -> None:
        self.view.set_color_scheme(scheme)

    def refresh_labels(self) -> None:
        self.view.refresh_labels()
        self._update_breadcrumbs(self.view.current_path)

    def _enter(self, path: NodePath) -> None:
        self.view.navigate_to(path)
        self.node_selected.emit(path)

    def _back(self) -> None:
        path = self.view.current_path
        if not path:
            return
        parent = path[:-1]
        self.view.navigate_to(parent)
        self.node_selected.emit(parent)

    def _navigate_breadcrumb(self, path: NodePath) -> None:
        self.view.navigate_to(path)
        self.node_selected.emit(path)

    def _update_breadcrumbs(self, path: NodePath) -> None:
        while self.breadcrumbs.count():
            item = self.breadcrumbs.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        paths = [path[:depth] for depth in range(len(path) + 1)]
        for index, level in enumerate(paths):
            if index:
                separator = QLabel(">")
                separator.setObjectName("mutedLabel")
                self.breadcrumbs.addWidget(separator)
            button = QPushButton(self._breadcrumb_label(level))
            button.setFlat(True)
            button.setEnabled(level != path)
            button.clicked.connect(lambda _checked=False, target=level: self._navigate_breadcrumb(target))
            self.breadcrumbs.addWidget(button)
        self.breadcrumbs.addStretch()
        self.back_button.setEnabled(bool(path))

    def _breadcrumb_label(self, path: NodePath) -> str:
        if not path:
            return "Root"
        if self._document is None:
            return ".".join(map(str, path))
        try:
            self._document.details(path)
        except ContextSourceError:
            return ".".join(map(str, path))
        return self._document.component_label(path)


# Kept as an import compatibility alias; there is only one renderer.
ContextGraphPanel = DataFlowPanel
ContextGraphView = DataFlowView


def _status_color(node: DataFlowNode, scheme: ColorScheme) -> str:
    if node.kind != "child":
        return scheme.accent
    if not node.recorded:
        return scheme.status_missing
    return {
        "complete": scheme.status_complete,
        "failed": scheme.danger,
        "skipped": scheme.status_skipped,
    }.get(node.status, scheme.status_missing)


def _elide(text: str, font: QFont, width: float) -> str:
    return QFontMetricsF(font).elidedText(
        text, Qt.TextElideMode.ElideRight, int(width)
    )
