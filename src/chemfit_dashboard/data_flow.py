from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from chemfit_dashboard.source import ContextDocument, ContextSourceError, NodePath

if TYPE_CHECKING:
    from collections.abc import Mapping

NodeKind = Literal["input", "child", "output"]
NodeId = str

INPUT_NODE: NodeId = "input"
OUTPUT_NODE: NodeId = "output"


def child_node(index: int) -> NodeId:
    return f"child:{index}"


@dataclass(frozen=True, slots=True)
class DataFlowNode:
    """One boundary or immediate child in a structural component."""

    id: NodeId
    kind: NodeKind
    label: str
    path: NodePath | None = None
    component_type: str | None = None
    status: str = "unavailable"
    recorded: bool = False
    nested: bool = False


@dataclass(frozen=True, slots=True)
class DataFlowEdge:
    """One explicitly declared transfer of data between flow nodes."""

    source: NodeId
    target: NodeId


@dataclass(frozen=True, slots=True)
class DataFlowGraph:
    """The declared data flow for exactly one structural context level."""

    structural_path: NodePath
    nodes: tuple[DataFlowNode, ...]
    edges: tuple[DataFlowEdge, ...]
    available: bool
    message: str | None = None

    @property
    def node_ids(self) -> tuple[NodeId, ...]:
        return tuple(node.id for node in self.nodes)


def build_data_flow(  # noqa: PLR0912
    document: ContextDocument,
    structural_path: NodePath = (),
) -> DataFlowGraph:
    """Build one flow level solely from ``chemfit.structure`` declarations."""
    try:
        details = document.details(structural_path)
    except ContextSourceError:
        return _unavailable(structural_path, "This component is not available.")
    structure = details.structure
    if structure is None:
        return _unavailable(
            structural_path,
            "No declared data flow is available for this component.",
        )

    declarations = _child_declarations(structure)
    inputs = structure.get("inputs")
    outputs = structure.get("outputs")
    referenced = set(declarations)
    if isinstance(inputs, list):
        referenced.update(range(len(inputs)))
        for sources in inputs:
            referenced.update(_referenced_children(sources))
    referenced.update(_referenced_children(outputs))

    runtime = {child.path[-1]: child for child in document.children(structural_path)}
    nodes: list[DataFlowNode] = [
        DataFlowNode(INPUT_NODE, "input", "Input", status="boundary")
    ]
    for index in sorted(referenced):
        info = runtime.get(index)
        declaration = declarations.get(index, {})
        declared_type = _nonempty_string(declaration.get("name"))
        child_path = (*structural_path, index)
        metadata_name: str | None = None
        nested = False
        if info is not None:
            try:
                child_details = document.details(child_path)
            except ContextSourceError:
                child_details = None
            if child_details is not None:
                metadata_name = _nonempty_string(child_details.metadata.get("name"))
                nested = child_details.structure is not None
        label = metadata_name or (info.label if info is not None else None)
        label = label or declared_type or f"Component {index + 1}"
        component_type = declared_type if declared_type != label else None
        nodes.append(
            DataFlowNode(
                id=child_node(index),
                kind="child",
                label=label,
                path=child_path,
                component_type=component_type,
                status=info.status if info is not None else "not recorded",
                recorded=bool(info and info.recorded),
                nested=nested,
            )
        )
    nodes.append(DataFlowNode(OUTPUT_NODE, "output", "Output", status="boundary"))

    known_ids = {node.id for node in nodes}
    edges: list[DataFlowEdge] = []
    if isinstance(inputs, list):
        for target_index, sources in enumerate(inputs):
            target = child_node(target_index)
            if target not in known_ids:
                continue
            for source in _sources(sources):
                source_id = _source_node(source)
                if source_id is not None and source_id in known_ids:
                    edges.append(DataFlowEdge(source_id, target))
    for source in _sources(outputs):
        source_id = _source_node(source)
        if source_id is not None and source_id in known_ids:
            edges.append(DataFlowEdge(source_id, OUTPUT_NODE))

    message = None
    if not declarations and not edges:
        message = "The structural declaration contains no data connections."
    return DataFlowGraph(
        structural_path,
        tuple(nodes),
        tuple(edges),
        available=True,
        message=message,
    )


def _unavailable(path: NodePath, message: str) -> DataFlowGraph:
    return DataFlowGraph(path, (), (), available=False, message=message)


def _child_declarations(structure: Mapping[str, Any]) -> dict[int, Mapping[str, Any]]:
    children = structure.get("children")
    if not isinstance(children, list):
        return {}
    result: dict[int, Mapping[str, Any]] = {}
    for fallback, declaration in enumerate(children):
        if not isinstance(declaration, dict):
            continue
        index = declaration.get("index", fallback)
        if isinstance(index, int) and not isinstance(index, bool) and index >= 0:
            result[index] = declaration
    return result


def _sources(value: Any) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list):
        return ()
    return tuple(source for source in value if isinstance(source, dict))


def _referenced_children(value: Any) -> set[int]:
    result: set[int] = set()
    for source in _sources(value):
        index = source.get("index")
        if (
            source.get("kind") == "child"
            and isinstance(index, int)
            and not isinstance(index, bool)
            and index >= 0
        ):
            result.add(index)
    return result


def _source_node(source: Mapping[str, Any]) -> NodeId | None:
    kind = source.get("kind")
    if kind == "parent_input":
        return INPUT_NODE
    index = source.get("index")
    if (
        kind == "child"
        and isinstance(index, int)
        and not isinstance(index, bool)
        and index >= 0
    ):
        return child_node(index)
    return None


def _nonempty_string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
