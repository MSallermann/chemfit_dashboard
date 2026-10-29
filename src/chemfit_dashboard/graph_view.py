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
    QCheckBox,
    QGraphicsEllipseItem,
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

from chemfit_dashboard.appearance import ColorScheme, active_color_scheme
from chemfit_dashboard.graph_layout import (
    GraphLayout,
    LayoutNode,
    dependency_layered_layout,
    hierarchy_outline_layout,
)
from chemfit_dashboard.source import (
    ContextDocument,
    ContextSourceError,
    NodeInfo,
    NodePath,
)
from chemfit_dashboard.traversal import CancellationToken
from chemfit_dashboard.workers import TaskRunner

if TYPE_CHECKING:
    from collections.abc import Callable

NODE_WIDTH = 300.0
NODE_HEIGHT = 82.0
MAX_VISIBLE_NODES = 2_000
BACKGROUND_LAYOUT_THRESHOLD = 300


class _NodeToggleItem(QGraphicsEllipseItem):
    """Small expand/collapse affordance attached to a structural node."""

    def __init__(
        self,
        path: NodePath,
        expanded: bool,
        scheme: ColorScheme,
        callback: Callable[[NodePath], None],
        parent: QGraphicsItem,
    ) -> None:
        super().__init__(0, 0, 22, 22, parent)
        self._path = path
        self._callback = callback
        self.setPos(NODE_WIDTH - 31, 8)
        self.setZValue(10)
        self.setBrush(QBrush(QColor(scheme.accent_soft)))
        self.setPen(QPen(QColor(scheme.accent), 1.5))
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setToolTip("Collapse component" if expanded else "Expand component")
        label = QGraphicsSimpleTextItem("-" if expanded else "+", self)
        label_font = label.font()
        label_font.setBold(True)
        label_font.setPointSizeF(label_font.pointSizeF() + 2)
        label.setFont(label_font)
        label.setBrush(QColor(scheme.text_strong))
        bounds = label.boundingRect()
        label.setPos(11 - bounds.width() / 2, 11 - bounds.height() / 2)
        label.setAcceptedMouseButtons(Qt.MouseButton.NoButton)

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._callback(self._path)
            event.accept()
            return
        super().mousePressEvent(event)


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
        self._nodes: dict[NodePath, QGraphicsPathItem] = {}
        self._toggles: dict[NodePath, _NodeToggleItem] = {}
        self._selected_path: NodePath | None = None
        self._expanded: set[NodePath] = {()}
        self._show_dependencies = True
        self._fit_next_render = True
        self._focus_after_render: NodePath | None = None
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

    @property
    def toggle_paths(self) -> frozenset[NodePath]:
        """Return structural paths with an inline expand/collapse control."""
        return frozenset(self._toggles)

    @property
    def dependencies_visible(self) -> bool:
        return self._show_dependencies

    def set_document(self, document: ContextDocument | None) -> None:
        self._cancel_layout()
        self._document = document
        self._rendered = False
        self._nodes = {}
        self._toggles = {}
        self._expanded = {()}
        self._fit_next_render = True
        self._focus_after_render = None
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
        self._toggles = {}
        self._fit_next_render = True
        self._focus_after_render = None
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
        self._fit_next_render = True
        self._render_graph(self._document)
        self._apply_selected_path(reveal=False)

    def select_path(self, path: NodePath) -> None:
        self._selected_path = path
        self._expanded.update(path[:depth] for depth in range(len(path)))
        if self._rendered and path not in self._nodes and self._document is not None:
            self._fit_next_render = False
            self._focus_after_render = path
            self._render_graph(self._document)
        self._apply_selected_path()

    def toggle_path(self, path: NodePath) -> None:
        """Expand or collapse a structural component from its inline control."""
        self._selected_path = path
        if path in self._expanded:
            self.collapse_selected()
        else:
            self.expand_selected()

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
        self._rebuild(focus=self._selected_path)

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
        self._rebuild(focus=selected)

    def expand_all(self) -> None:
        if self._document is not None:
            self._expanded = {
                node.path for node in self._document.nodes if node.child_count
            }
            self._rebuild(focus=self._selected_path or ())

    def overview(self) -> None:
        self._expanded = {()}
        self._rebuild(fit=True)

    def set_dependencies_visible(self, visible: bool) -> None:
        """Switch between a hierarchy-first view and dependency overlay."""
        if visible == self._show_dependencies:
            return
        self._show_dependencies = visible
        self._rebuild(focus=self._selected_path or ())

    def cancel_pending_layout(self) -> None:
        """Cancel queued or running layout work cooperatively."""
        self._cancel_layout()
        self._tasks.cancel_pending()

    def _rebuild(
        self,
        *,
        fit: bool = False,
        focus: NodePath | None = None,
    ) -> None:
        if self._document is not None:
            self._rendered = True
            self._fit_next_render = fit
            self._focus_after_render = focus
            self._render_graph(self._document)
            self._apply_selected_path(reveal=False)

    def _apply_selected_path(self, *, reveal: bool = True) -> None:
        if self._selected_path is None:
            return
        item = self._nodes.get(self._selected_path)
        if item is not None:
            self._scene.clearSelection()
            item.setSelected(True)
            if reveal:
                self.ensureVisible(item, 30, 30)

    def zoom_in(self) -> None:
        self.scale(1.2, 1.2)

    def zoom_out(self) -> None:
        self.scale(1 / 1.2, 1 / 1.2)

    def fit_graph(self) -> None:
        bounds = self._node_bounds()
        if bounds.isEmpty():
            return
        available_width = max(1, self.viewport().width() - 48)
        readable_scale = min(1.0, available_width / max(1.0, bounds.width()))
        readable_scale = max(0.78, readable_scale)
        self.resetTransform()
        self.scale(readable_scale, readable_scale)
        horizontal_margin = self.viewport().width() / (2 * readable_scale)
        vertical_margin = self.viewport().height() / (2 * readable_scale)
        self.setSceneRect(
            bounds.adjusted(
                -horizontal_margin,
                -vertical_margin,
                horizontal_margin,
                vertical_margin,
            )
        )
        top_center = QPointF(
            bounds.center().x(),
            bounds.top() + self.viewport().height() / (2 * readable_scale) - 24,
        )
        self.centerOn(top_center)

    def _node_bounds(self) -> QRectF:
        if not self._nodes:
            return self._scene.itemsBoundingRect()
        bounds = QRectF()
        for item in self._nodes.values():
            bounds = bounds.united(item.sceneBoundingRect())
        return bounds

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
                layout_function = (
                    dependency_layered_layout
                    if self._show_dependencies
                    else hierarchy_outline_layout
                )
                result = layout_function(
                    layout_nodes,
                    hierarchy,
                    dependencies if self._show_dependencies else (),
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
        layout_function = (
            dependency_layered_layout
            if self._show_dependencies
            else hierarchy_outline_layout
        )
        graph_layout = layout_function(
            layout_nodes,
            hierarchy,
            dependencies if self._show_dependencies else (),
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
        self._apply_selected_path(reveal=False)

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
        self._toggles = {}
        hierarchy_color = QColor(self._scheme.hierarchy_edge)
        dependency_color = QColor(self._scheme.dependency_edge)
        hierarchy_pen = QPen(
            hierarchy_color,
            1.2 if self._show_dependencies else 2.4,
        )
        dependency_pen = QPen(dependency_color, 2.0, Qt.PenStyle.DashLine)
        for edge in graph_layout.edges:
            edge_path = QPainterPath(QPointF(*edge.points[0]))
            if edge.kind == "dependency" and self._show_dependencies:
                edge_path.cubicTo(
                    QPointF(*edge.points[1]),
                    QPointF(*edge.points[2]),
                    QPointF(*edge.points[3]),
                )
            else:
                edge_path.lineTo(QPointF(*edge.points[1]))
                edge_path.lineTo(QPointF(*edge.points[2]))
                edge_path.lineTo(QPointF(*edge.points[3]))
            self._scene.addPath(
                edge_path,
                hierarchy_pen if edge.kind == "hierarchy" else dependency_pen,
            )
            self._add_arrowhead(
                edge.points[-2],
                edge.points[-1],
                hierarchy_color if edge.kind == "hierarchy" else dependency_color,
                6.0 if edge.kind == "hierarchy" else 7.0,
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
            parent_label = (
                info_by_path[path[:-1]].label
                if path and path[:-1] in info_by_path
                else None
            )
            self._nodes[path] = self._add_node(
                info,
                QPointF(*graph_layout.positions[path]),
                static_name,
                hidden_count,
                parent_label,
            )
        if document.node_count > len(visible_paths):
            message = self._scene.addSimpleText(
                f"Overview: {len(visible_paths):,} visible of {document.node_count:,} contexts"
            )
            message.setBrush(QColor(self._scheme.warning))
            message.setPos(12, -32)
        if self._fit_next_render:
            self.fit_graph()
        else:
            scale = max(self.transform().m11(), 0.01)
            horizontal_margin = self.viewport().width() / (2 * scale)
            vertical_margin = self.viewport().height() / (2 * scale)
            self.setSceneRect(
                self._node_bounds().adjusted(
                    -horizontal_margin,
                    -vertical_margin,
                    horizontal_margin,
                    vertical_margin,
                )
            )
            if self._focus_after_render in self._nodes:
                focused = self._nodes[self._focus_after_render].sceneBoundingRect()
                viewport_height = self.viewport().height() / scale
                self.centerOn(
                    focused.center().x(),
                    focused.center().y() + viewport_height / 2 - 90 / scale,
                )
        self._fit_next_render = False
        self._focus_after_render = None
        self.render_finished.emit(len(visible_paths), document.node_count)

    def _add_node(
        self,
        info: NodeInfo,
        position: QPointF,
        static_name: str | None,
        hidden_count: int,
        parent_label: str | None,
    ) -> QGraphicsPathItem:
        structural = info.child_count > 0
        item = QGraphicsPathItem(_node_shape(structural))
        item.setPos(position)
        status_color = QColor(_node_color(info, self._scheme))
        item.setBrush(
            QBrush(QColor(self._scheme.accent_soft) if structural else status_color)
        )
        item.setPen(
            QPen(
                QColor(self._scheme.accent if structural else self._scheme.node_border),
                2.3 if structural else 1.2,
            )
        )
        item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        item.setData(0, info.path)
        item.setData(1, "structural" if structural else "atomic")
        tooltip = f"{info.kind} · path {info.path_text} · {info.status}"
        if parent_label is not None:
            tooltip += f"\nchild of: {parent_label}"
        if hidden_count:
            tooltip += f"\n{hidden_count:,} descendants collapsed"
        if static_name is not None:
            tooltip += f"\nstatic name: {static_name}"
        item.setToolTip(tooltip)
        self._scene.addItem(item)

        status_bar = QGraphicsRectItem(0, 12, 5, NODE_HEIGHT - 24, item)
        status_bar.setBrush(QBrush(status_color))
        status_bar.setPen(QPen(Qt.PenStyle.NoPen))
        status_bar.setAcceptedMouseButtons(Qt.MouseButton.NoButton)

        title = QGraphicsSimpleTextItem("", item)
        title_width = NODE_WIDTH - (56 if structural else 24)
        title.setText(_elide_to_width(info.label, title.font(), title_width))
        title.setBrush(QColor(self._scheme.text_strong))
        title.setPos(12, 12)
        title.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        if static_name is not None:
            detail_text = f"name: {static_name}"
        elif parent_label is None:
            detail_text = f"{info.status} · evaluation root"
        else:
            detail_text = info.status
        detail = QGraphicsSimpleTextItem("", item)
        detail.setText(_elide_to_width(detail_text, detail.font(), NODE_WIDTH - 24))
        detail.setBrush(QColor(self._scheme.text))
        detail.setPos(12, 44)
        detail.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        if structural:
            toggle = _NodeToggleItem(
                info.path,
                info.path in self._expanded,
                self._scheme,
                self.toggle_path,
                item,
            )
            self._toggles[info.path] = toggle
        return item

    def _add_arrowhead(
        self,
        previous: tuple[float, float],
        target: tuple[float, float],
        color: QColor,
        size: float,
    ) -> None:
        dx = target[0] - previous[0]
        dy = target[1] - previous[1]
        length = math.hypot(dx, dy)
        if not length:
            return
        ux, uy = dx / length, dy / length
        px, py = -uy, ux
        base_x = target[0] - ux * size
        base_y = target[1] - uy * size
        polygon = QPolygonF(
            [
                QPointF(*target),
                QPointF(base_x + px * size * 0.55, base_y + py * size * 0.55),
                QPointF(base_x - px * size * 0.55, base_y - py * size * 0.55),
            ]
        )
        arrow = QGraphicsPolygonItem(polygon)
        arrow.setBrush(QBrush(color))
        arrow.setPen(QPen(color, 1.0))
        self._scene.addItem(arrow)

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
        self.status = QLabel("Graph builds on demand")
        self.status.setObjectName("mutedLabel")
        layout.addWidget(self.status)
        controls = QHBoxLayout()
        self.expand_all_button = QPushButton("Expand all")
        self.overview_button = QPushButton("Overview")
        self.zoom_out_button = QPushButton("-")
        self.zoom_in_button = QPushButton("+")
        self.fit_button = QPushButton("Fit width")
        for button in (
            self.overview_button,
            self.expand_all_button,
            self.fit_button,
            self.zoom_out_button,
            self.zoom_in_button,
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
        self.expand_all_button.clicked.connect(self.view.expand_all)
        self.overview_button.clicked.connect(self.view.overview)
        self.zoom_out_button.clicked.connect(self.view.zoom_out)
        self.zoom_in_button.clicked.connect(self.view.zoom_in)
        self.fit_button.clicked.connect(self.view.fit_graph)
        layout.addWidget(self.view, 1)
        legend_row = QHBoxLayout()
        self.dependencies = QCheckBox("Data-flow layout")
        self.dependencies.setChecked(True)
        self.dependencies.setToolTip(
            "Show the dependency DAG; turn off for a hierarchy outline"
        )
        self.dependencies.toggled.connect(self.view.set_dependencies_visible)
        legend_row.addWidget(self.dependencies)
        legend = QLabel("Dashed: data flow · Solid: hierarchy · +/- expands")
        legend.setObjectName("mutedLabel")
        legend.setWordWrap(True)
        legend_row.addWidget(legend, 1)
        layout.addLayout(legend_row)

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


def _node_shape(structural: bool) -> QPainterPath:
    shape = QPainterPath()
    shape.setFillRule(Qt.FillRule.WindingFill)
    corner_radius = 10 if structural else 18
    shape.addRoundedRect(
        QRectF(0, 0, NODE_WIDTH, NODE_HEIGHT), corner_radius, corner_radius
    )
    return shape


def _elide_to_width(value: str, font: QFont, maximum_width: float) -> str:
    return QFontMetricsF(font).elidedText(
        value,
        Qt.TextElideMode.ElideRight,
        maximum_width,
    )
