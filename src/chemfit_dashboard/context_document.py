from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime


def alphanumeric_key(value: str) -> tuple[tuple[int, int | str], ...]:
    """Return a case-insensitive natural-sort key for names containing numbers."""
    return tuple(
        (1, int(part)) if part.isdigit() else (0, part)
        for part in re.split(r"(\d+)", value.casefold())
    )


def _preview(value: Any, limit: int = 80) -> str:
    if isinstance(value, dict):
        text = f"{{{len(value)} fields}}"
    elif isinstance(value, list):
        text = f"[{len(value)} items]"
    elif value is None:
        text = "null"
    elif isinstance(value, str):
        text = value
    else:
        text = str(value)
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def _is_serialized_exception(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    exception_name, separator, _ = value.partition(": ")
    return bool(separator) and exception_name.endswith(("Error", "Exception"))


class ContextDocumentError(Exception):
    """Error class for raised by context documents."""


NodePath = tuple[int, ...]
DEFAULT_COMPONENT_LABEL_METADATA_KEY = "name"


@dataclass(frozen=True, slots=True)
class NodeInfo:
    """Small immutable summary used by lazy hierarchy models."""

    path: NodePath
    label: str
    kind: str
    status: str
    output_preview: str
    child_count: int
    recorded: bool

    @property
    def path_text(self) -> str:
        return "root" if not self.path else ".".join(map(str, self.path))


@dataclass(frozen=True, slots=True)
class NodeDetails:
    """Full, non-child data exposed for one indexed context node."""

    info: NodeInfo
    fields: Mapping[str, Any]
    metadata: Mapping[str, Any]
    structure: Mapping[str, Any] | None
    raw: Mapping[str, Any]


@dataclass(slots=True)
class _NodeRecord:
    value: Any
    info: NodeInfo


@dataclass(frozen=True, slots=True)
class RecordRef:
    """Stable identity and file metadata for one discoverable evaluation."""

    id: str  # unique id
    name: str  # name of the evaluation
    path: Path  # path to the document .json or .jsonl
    size: int  # size (from os.stat)
    modified: datetime  # modification time
    position_in_file: int  # position of this record in it's file (for .json this is always 1, for .jsonl it is the line number)

    @property
    def location(self) -> str:
        return str(self.path.parent)


class ContextDocument:
    """
    Represents one parsed JSON record.

    The document owns decoded JSON, structural node/child indexes and the mutable presentation
    label key. It does not own its source cache and remains valid after eviction.
    """

    def __init__(
        self,
        record: RecordRef,
        raw: Any,
        *,
        component_label_metadata_key: str = DEFAULT_COMPONENT_LABEL_METADATA_KEY,
        name: str | None = None,
        path: Path | None = None,
    ):
        """Index the parsed context hierarchy without changing its values."""
        self.record = record
        self.raw = raw
        self._component_label_metadata_key = component_label_metadata_key.strip()
        self._nodes: dict[NodePath, _NodeRecord] = {}
        self._children: dict[NodePath, tuple[NodePath, ...]] = {}
        self._component_label_metadata_keys: tuple[str, ...] | None = None
        self._path = path
        self._name = name

        self._build_index()

    @property
    def root(self) -> NodeInfo:
        return self._nodes[()].info

    @property
    def node_count(self) -> int:
        return len(self._nodes)

    @property
    def name(self) -> str:
        return self._name if self._name is not None else ""

    @property
    def path(self) -> Path | None:
        return self._path

    @property
    def nodes(self) -> tuple[NodeInfo, ...]:
        return tuple(node.info for node in self._nodes.values())

    def has_path(self, path: NodePath) -> bool:
        """Return whether a context path exists in this document."""
        return path in self._nodes

    def children(self, path: NodePath) -> tuple[NodeInfo, ...]:
        return tuple(self._nodes[child].info for child in self._children.get(path, ()))

    def children_slice(
        self, path: NodePath, start: int, stop: int
    ) -> tuple[NodeInfo, ...]:
        """Return a bounded child window for incremental GUI models."""
        child_paths = self._children.get(path, ())[start:stop]
        return tuple(self._nodes[child].info for child in child_paths)

    def details(self, path: NodePath) -> NodeDetails:
        """Retrieve node details."""

        try:
            node = self._nodes[path]
        except KeyError as exc:
            message = f"Unknown context path: {path!r}"
            raise ContextDocumentError(message) from exc

        value = node.value
        if isinstance(value, dict):
            metadata_value = value.get("meta")
            metadata = metadata_value if isinstance(metadata_value, dict) else {}
            structure_value = metadata.get("chemfit.structure")
            structure = structure_value if isinstance(structure_value, dict) else None
            fields = {
                key: item
                for key, item in value.items()
                if key not in {"children", "meta"}
            }
            raw = {key: item for key, item in value.items() if key != "children"}
        else:
            metadata = {}
            structure = None
            fields = {"value": value}
            raw = fields

        return NodeDetails(
            info=node.info,
            fields=fields,
            metadata=metadata,
            structure=structure,
            raw=raw,
        )

    @property
    def component_label_metadata_key(self) -> str:
        """Return the metadata key used for user-facing component labels."""
        return self._component_label_metadata_key

    def set_component_label_metadata_key(self, key: str) -> None:
        """Select the metadata field used for labels without rebuilding the index."""
        self._component_label_metadata_key = key.strip()

    def component_label(self, path: NodePath) -> str:
        """Return the configured metadata label, falling back to structural identity."""
        details = self.details(path)
        key = self._component_label_metadata_key
        candidate = details.metadata.get(key) if key else None
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
        return details.info.label

    @property
    def component_label_metadata_keys(self) -> tuple[str, ...]:
        """Return string-valued metadata keys available as component labels."""
        if self._component_label_metadata_keys is not None:
            return self._component_label_metadata_keys
        keys: set[str] = set()

        for path in self._nodes:
            for key, value in self.details(path).metadata.items():
                if isinstance(key, str) and isinstance(value, str) and value.strip():
                    keys.add(key)

        self._component_label_metadata_keys = tuple(sorted(keys, key=str.casefold))
        return self._component_label_metadata_keys

    def _build_index(self) -> None:
        """
        Build lookup indexes for the complete context hierarchy.

        Traverse the context tree iteratively in depth-first order, assigning
        each node a positional path. Populate ``_nodes`` with the original
        context data and its ``NodeInfo``, and ``_children`` with the paths
        of each node's immediate children.

        Include components declared in structural metadata even when no
        corresponding runtime context was recorded. Such nodes are represented
        by synthetic entries marked as unrecorded.

        Also collect string-valued metadata keys that can be used as
        component labels.

        The traversal does not modify the original context data.
        """

        root_label = self._identity(self.raw) or self.record.path.stem
        pending: list[tuple[NodePath, Any, str, bool]] = [
            ((), self.raw, root_label, True)
        ]
        component_label_keys: set[str] = set()

        while pending:
            path, value, label, recorded = pending.pop()
            if isinstance(value, dict):
                metadata = value.get("meta")
                if isinstance(metadata, dict):
                    component_label_keys.update(
                        key
                        for key, candidate in metadata.items()
                        if isinstance(key, str)
                        and isinstance(candidate, str)
                        and candidate.strip()
                    )
            declaration_map = self._declarations(value)
            actual_children = self._actual_children(value)
            child_indices = sorted(
                set(range(len(actual_children))) | set(declaration_map)
            )
            child_paths = tuple((*path, index) for index in child_indices)
            self._children[path] = child_paths

            info = NodeInfo(
                path=path,
                label=label,
                kind=self._kind(value, recorded),
                status=self._status(value, recorded),
                output_preview=self._output_preview(value, recorded),
                child_count=len(child_paths),
                recorded=recorded,
            )
            self._nodes[path] = _NodeRecord(value=value, info=info)

            for index in reversed(child_indices):
                child_recorded = index < len(actual_children)
                declaration = declaration_map.get(index, {})
                child_value = (
                    actual_children[index]
                    if child_recorded
                    else {"declaration": declaration}
                )
                declared_name = declaration.get("name")
                child_label = (
                    declared_name
                    if isinstance(declared_name, str) and declared_name
                    else self._identity(child_value) or f"Context {index + 1}"
                )
                pending.append(
                    ((*path, index), child_value, child_label, child_recorded)
                )

        self._component_label_metadata_keys = tuple(
            sorted(component_label_keys, key=str.casefold)
        )

    @staticmethod
    def _actual_children(value: Any) -> list[Any]:
        if not isinstance(value, dict):
            return []
        children = value.get("children")
        return children if isinstance(children, list) else []

    @staticmethod
    def _declarations(value: Any) -> dict[int, dict[str, Any]]:
        if not isinstance(value, dict):
            return {}
        metadata = value.get("meta")
        if not isinstance(metadata, dict):
            return {}
        structure = metadata.get("chemfit.structure")
        if not isinstance(structure, dict):
            return {}
        declared_children = structure.get("children")
        if not isinstance(declared_children, list):
            return {}

        result: dict[int, dict[str, Any]] = {}
        for fallback_index, child in enumerate(declared_children):
            if not isinstance(child, dict):
                continue
            index = child.get("index", fallback_index)
            if isinstance(index, int) and index >= 0:
                result[index] = child
        return result

    @staticmethod
    def _identity(value: Any) -> str | None:
        if not isinstance(value, dict):
            return None
        metadata = value.get("meta")
        if not isinstance(metadata, dict):
            return None
        for key in ("component_name", "component", "label"):
            candidate = metadata.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
        return None

    @classmethod
    def _kind(cls, value: Any, recorded: bool) -> str:
        if not recorded:
            return "declared component"
        if isinstance(value, dict):
            if any(key in value for key in ("loss", "parameters", "quantities")):
                return "evaluation checkpoint"
            if cls._declarations(value):
                return "structural context"
        return "context"

    @staticmethod
    def _status(value: Any, recorded: bool) -> str:  # noqa: PLR0911
        if not recorded:
            return "not recorded"
        if not isinstance(value, dict):
            return "value"
        if (
            isinstance(value.get("output"), dict)
            and value["output"].get("skipped") is True
        ):
            return "skipped"
        if "output" in value:
            if _is_serialized_exception(value["output"]):
                return "failed"
            return "no output" if value["output"] is None else "complete"
        if any(key in value for key in ("loss", "quantities")):
            return "complete"
        return "partial"

    @staticmethod
    def _output_preview(value: Any, recorded: bool) -> str:
        if not recorded:
            return "—"
        if not isinstance(value, dict):
            return _preview(value)
        for key in ("loss", "output", "quantities"):
            if key in value:
                return _preview(value[key])
        return "—"
