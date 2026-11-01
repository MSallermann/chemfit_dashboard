"""
Semantic component identities shared by plotting and extraction.

Structural integer paths may differ between evaluations. ``NodeTrail`` records
declared names, occurrences, and indexes so a selection can resolve to the same
logical component in another document, including partial/missing paths.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from chemfit_dashboard.source import ContextDocument, ContextSourceError, NodePath


@dataclass(frozen=True, slots=True)
class NodeTrailSegment:
    """One child in a component's declared structural path."""

    name: str
    occurrence: int
    declared_index: int
    declared: bool


NodeTrail = tuple[NodeTrailSegment, ...]


@dataclass(frozen=True, slots=True)
class ComponentResolution:
    """The deepest resolved path and whether the full target was available."""

    path: NodePath
    complete: bool


def component_trail_for_path(document: ContextDocument, path: NodePath) -> NodeTrail:
    """Describe a component path using identities stable across evaluations."""
    document.details(path)
    segments: list[NodeTrailSegment] = []
    for depth in range(1, len(path) + 1):
        child_path = path[:depth]
        parent_path = child_path[:-1]
        child_segments = dict(_child_segments(document, parent_path))
        segments.append(child_segments[child_path])
    return tuple(segments)


def resolve_component_path(
    document: ContextDocument,
    preferred_path: NodePath,
    trail: NodeTrail,
) -> NodePath | None:
    """Resolve a full semantic component trail in another evaluation."""
    resolution = resolve_component_selection(document, preferred_path, trail)
    return resolution.path if resolution.complete else None


def resolve_component_selection(
    document: ContextDocument,
    preferred_path: NodePath,
    trail: NodeTrail,
) -> ComponentResolution:
    """Resolve a target, retaining the deepest declaration when it is truncated."""
    if not trail:
        return ComponentResolution(path=(), complete=True)
    try:
        if component_trail_for_path(document, preferred_path) == trail:
            return ComponentResolution(path=preferred_path, complete=True)
    except ContextSourceError:
        pass

    parent_path: NodePath = ()
    for expected in trail:
        candidates = _child_segments(document, parent_path)
        indexed = next(
            (
                (path, segment)
                for path, segment in candidates
                if segment.declared_index == expected.declared_index
                and _same_semantic_child(expected, segment)
            ),
            None,
        )
        if indexed is not None:
            parent_path = indexed[0]
            continue
        named = [
            path
            for path, segment in candidates
            if _same_semantic_child(expected, segment)
        ]
        if expected.occurrence >= len(named):
            return ComponentResolution(path=parent_path, complete=False)
        parent_path = named[expected.occurrence]
    return ComponentResolution(path=parent_path, complete=True)


def _child_segments(
    document: ContextDocument, parent_path: NodePath
) -> list[tuple[NodePath, NodeTrailSegment]]:
    occurrences: defaultdict[tuple[bool, str], int] = defaultdict(int)
    result: list[tuple[NodePath, NodeTrailSegment]] = []
    for child in document.children(parent_path):
        declared = (
            _declared_child_name(document, parent_path, child.path[-1]) is not None
        )
        name = document.component_label(child.path)
        key = (declared, name)
        occurrence = occurrences[key]
        occurrences[key] += 1
        result.append(
            (
                child.path,
                NodeTrailSegment(name, occurrence, child.path[-1], declared),
            )
        )
    return result


def _declared_child_name(
    document: ContextDocument, parent_path: NodePath, child_index: int
) -> str | None:
    structure = document.details(parent_path).structure
    children = structure.get("children") if structure is not None else None
    if not isinstance(children, list):
        return None
    for fallback_index, child in enumerate(children):
        if not isinstance(child, dict):
            continue
        index = child.get("index", fallback_index)
        name = child.get("name")
        if index == child_index and isinstance(name, str) and name:
            return name
    return None


def _same_semantic_child(
    expected: NodeTrailSegment, candidate: NodeTrailSegment
) -> bool:
    return expected.declared == candidate.declared and expected.name == candidate.name
