from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Hashable
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeAlias

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

LayoutId: TypeAlias = Hashable


@dataclass(frozen=True, slots=True)
class RoutedEdge:
    source: LayoutId
    target: LayoutId
    kind: str
    points: tuple[tuple[float, float], ...]


@dataclass(frozen=True, slots=True)
class GraphLayout:
    positions: Mapping[LayoutId, tuple[float, float]]
    edges: tuple[RoutedEdge, ...]


def data_flow_layout(  # noqa: PLR0912, PLR0915
    nodes: Iterable[LayoutId],
    edges: Iterable[tuple[LayoutId, LayoutId]],
    *,
    input_node: LayoutId = "input",
    output_node: LayoutId = "output",
    node_width: float = 220.0,
    node_height: float = 76.0,
    horizontal_gap: float = 92.0,
    vertical_gap: float = 42.0,
    cancelled: Callable[[], bool] | None = None,
) -> GraphLayout:
    """Return a deterministic, Qt-independent left-to-right flow layout."""
    node_list = sorted(set(nodes), key=_sort_key)
    node_set = set(node_list)
    edge_list = tuple(
        sorted(
            (
                (source, target)
                for source, target in edges
                if source in node_set and target in node_set and source != target
            ),
            key=lambda edge: (_sort_key(edge[0]), _sort_key(edge[1])),
        )
    )

    outgoing: defaultdict[LayoutId, set[LayoutId]] = defaultdict(set)
    incoming: defaultdict[LayoutId, set[LayoutId]] = defaultdict(set)
    indegree = dict.fromkeys(node_list, 0)
    for source, target in edge_list:
        if target not in outgoing[source]:
            outgoing[source].add(target)
            incoming[target].add(source)
            indegree[target] += 1

    ranks = {node: (0 if node == input_node else 1) for node in node_list}
    queue = deque(
        sorted((node for node in node_list if indegree[node] == 0), key=_sort_key)
    )
    processed: set[LayoutId] = set()
    while queue:
        if cancelled is not None and cancelled():
            return GraphLayout({}, ())
        source = queue.popleft()
        processed.add(source)
        ready: list[LayoutId] = []
        for target in sorted(outgoing[source], key=_sort_key):
            ranks[target] = max(ranks[target], ranks[source] + 1)
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
        if ready:
            queue.extend(ready)
            queue = deque(sorted(queue, key=_sort_key))

    # Cyclic declarations remain visible. Their acyclic predecessors determine
    # the group's rank; members of the cycle stay together.
    residual = [node for node in node_list if node not in processed]
    for node in residual:
        known = [ranks[parent] for parent in incoming[node] if parent in processed]
        ranks[node] = max(known, default=0) + 1
    if input_node in ranks:
        ranks[input_node] = 0
    if output_node in ranks:
        ranks[output_node] = (
            max(
                (rank for node, rank in ranks.items() if node != output_node), default=0
            )
            + 1
        )

    levels: defaultdict[int, list[LayoutId]] = defaultdict(list)
    for node in node_list:
        levels[ranks[node]].append(node)
    ordered_levels: dict[int, list[LayoutId]] = {}
    prior_order: dict[LayoutId, float] = {}
    for rank in sorted(levels):
        level = levels[rank]
        level.sort(
            key=lambda node: (
                _barycenter(incoming[node], prior_order),
                _sort_key(node),
            )
        )
        ordered_levels[rank] = level
        prior_order.update({node: float(index) for index, node in enumerate(level)})

    largest_level = max((len(level) for level in ordered_levels.values()), default=1)
    canvas_height = largest_level * node_height + (largest_level - 1) * vertical_gap
    positions: dict[LayoutId, tuple[float, float]] = {}
    for rank, level in ordered_levels.items():
        height = len(level) * node_height + (len(level) - 1) * vertical_gap
        offset = (canvas_height - height) / 2
        for row, node in enumerate(level):
            positions[node] = (
                rank * (node_width + horizontal_gap),
                offset + row * (node_height + vertical_gap),
            )

    routed: list[RoutedEdge] = []
    occurrence: defaultdict[tuple[LayoutId, LayoutId], int] = defaultdict(int)
    for source, target in edge_list:
        sx, sy = positions[source]
        tx, ty = positions[target]
        duplicate = occurrence[(source, target)]
        occurrence[(source, target)] += 1
        offset = duplicate * 5.0
        start = (sx + node_width, sy + node_height / 2 + offset)
        end = (tx, ty + node_height / 2 + offset)
        if end[0] > start[0]:
            middle = (end[0] - start[0]) / 2
            controls = (
                (start[0] + middle, start[1]),
                (end[0] - middle, end[1]),
            )
        else:
            detour = max(start[1], end[1]) + node_height + vertical_gap
            controls = (
                (start[0] + horizontal_gap / 2, detour),
                (end[0] - 24, detour),
            )
        routed.append(RoutedEdge(source, target, "data", (start, *controls, end)))
    return GraphLayout(positions, tuple(routed))


def _barycenter(
    neighbors: Iterable[LayoutId], positions: Mapping[LayoutId, float]
) -> float:
    known = [positions[node] for node in neighbors if node in positions]
    return sum(known) / len(known) if known else float("inf")


def _sort_key(value: LayoutId) -> tuple[str, str]:
    if value == "input":
        return ("0", "")
    if isinstance(value, str) and value.startswith("child:"):
        suffix = value.partition(":")[2]
        return ("1", suffix.zfill(12) if suffix.isdigit() else suffix)
    if value == "output":
        return ("2", "")
    return ("1", repr(value))
