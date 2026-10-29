from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from chemfit_dashboard.source import NodePath


@dataclass(frozen=True, slots=True)
class LayoutNode:
    path: NodePath
    parent: NodePath | None


@dataclass(frozen=True, slots=True)
class RoutedEdge:
    source: NodePath
    target: NodePath
    kind: str
    points: tuple[tuple[float, float], ...]


@dataclass(frozen=True, slots=True)
class GraphLayout:
    positions: Mapping[NodePath, tuple[float, float]]
    edges: tuple[RoutedEdge, ...]


def dependency_layered_layout(  # noqa: PLR0912
    nodes: Iterable[LayoutNode],
    hierarchy_edges: Iterable[tuple[NodePath, NodePath]],
    dependency_edges: Iterable[tuple[NodePath, NodePath]],
    *,
    node_width: float = 196.0,
    node_height: float = 76.0,
    horizontal_gap: float = 42.0,
    vertical_gap: float = 92.0,
) -> GraphLayout:
    """Return a deterministic layered layout without depending on Qt."""
    node_list = sorted(nodes, key=lambda node: (len(node.path), node.path))
    paths = {node.path for node in node_list}
    hierarchy = tuple(
        sorted(
            ((source, target) for source, target in hierarchy_edges if target in paths),
            key=lambda edge: (edge[0], edge[1]),
        )
    )
    dependencies = tuple(
        sorted(
            (
                (source, target)
                for source, target in dependency_edges
                if source in paths and target in paths and source != target
            ),
            key=lambda edge: (edge[0], edge[1]),
        )
    )

    ranks = {node.path: len(node.path) for node in node_list}
    outgoing: defaultdict[NodePath, list[NodePath]] = defaultdict(list)
    indegree = {node.path: 0 for node in node_list}
    for source, target in dependencies:
        outgoing[source].append(target)
        indegree[target] += 1
    queue = deque(sorted(path for path, degree in indegree.items() if degree == 0))
    visited: set[NodePath] = set()
    while queue:
        source = queue.popleft()
        visited.add(source)
        for target in sorted(outgoing[source]):
            ranks[target] = max(ranks[target], ranks[source] + 1)
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    # Cycles are legal input. Their hierarchy depth remains a stable fallback.

    levels: defaultdict[int, list[NodePath]] = defaultdict(list)
    for node in node_list:
        levels[ranks[node.path]].append(node.path)
    dependency_neighbors: defaultdict[NodePath, list[NodePath]] = defaultdict(list)
    for source, target in dependencies:
        dependency_neighbors[source].append(target)
        dependency_neighbors[target].append(source)
    for rank in sorted(levels):
        levels[rank].sort(
            key=lambda path: (
                path[:-1],
                min(dependency_neighbors[path], default=path),
                path,
            )
        )

    maximum = max((len(level) for level in levels.values()), default=1)
    canvas_width = maximum * (node_width + horizontal_gap)
    positions: dict[NodePath, tuple[float, float]] = {}
    for rank, level in sorted(levels.items()):
        width = len(level) * (node_width + horizontal_gap)
        offset = (canvas_width - width) / 2
        for column, path in enumerate(level):
            positions[path] = (
                offset + column * (node_width + horizontal_gap),
                rank * (node_height + vertical_gap),
            )

    routed: list[RoutedEdge] = []
    for kind, edges in (("hierarchy", hierarchy), ("dependency", dependencies)):
        for source, target in edges:
            if source not in positions or target not in positions:
                continue
            sx, sy = positions[source]
            tx, ty = positions[target]
            start = (sx + node_width / 2, sy + node_height)
            end = (tx + node_width / 2, ty)
            middle_y = (start[1] + end[1]) / 2
            if kind == "dependency" and end[1] <= start[1]:
                middle_y = max(start[1], end[1]) + vertical_gap / 2
            routed.append(
                RoutedEdge(
                    source,
                    target,
                    kind,
                    (start, (start[0], middle_y), (end[0], middle_y), end),
                )
            )
    return GraphLayout(positions, tuple(routed))
