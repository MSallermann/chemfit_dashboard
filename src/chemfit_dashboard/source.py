from __future__ import annotations

import hashlib
import json
import re
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

NodePath = tuple[int, ...]


def alphanumeric_key(value: str) -> tuple[tuple[int, int | str], ...]:
    """Return a case-insensitive natural-sort key for names containing numbers."""
    return tuple(
        (1, int(part)) if part.isdigit() else (0, part)
        for part in re.split(r"(\d+)", value.casefold())
    )


class ContextSourceError(RuntimeError):
    """Raised when a context source cannot discover or load a record."""


@dataclass(frozen=True, slots=True)
class RecordRef:
    id: str
    name: str
    path: Path
    size: int
    modified: datetime

    @property
    def location(self) -> str:
        return str(self.path.parent)


@dataclass(frozen=True, slots=True)
class NodeInfo:
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
    info: NodeInfo
    fields: Mapping[str, Any]
    metadata: Mapping[str, Any]
    structure: Mapping[str, Any] | None
    raw: Mapping[str, Any]


@dataclass(slots=True)
class _NodeRecord:
    value: Any
    info: NodeInfo


class ContextDocument:
    """One parsed JSON document with an immutable context-node index."""

    def __init__(self, record: RecordRef, raw: Any):
        """Index the parsed context hierarchy without changing its values."""
        self.record = record
        self.raw = raw
        self._nodes: dict[NodePath, _NodeRecord] = {}
        self._children: dict[NodePath, tuple[NodePath, ...]] = {}
        self._build_index()

    @property
    def root(self) -> NodeInfo:
        return self._nodes[()].info

    @property
    def node_count(self) -> int:
        return len(self._nodes)

    @property
    def nodes(self) -> tuple[NodeInfo, ...]:
        return tuple(node.info for node in self._nodes.values())

    def children(self, path: NodePath) -> tuple[NodeInfo, ...]:
        return tuple(self._nodes[child].info for child in self._children.get(path, ()))

    def details(self, path: NodePath) -> NodeDetails:
        try:
            node = self._nodes[path]
        except KeyError as exc:
            message = f"Unknown context path: {path!r}"
            raise ContextSourceError(message) from exc

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

    def _build_index(self) -> None:
        root_label = self._identity(self.raw) or self.record.path.stem
        pending: list[tuple[NodePath, Any, str, bool]] = [
            ((), self.raw, root_label, True)
        ]

        while pending:
            path, value, label, recorded = pending.pop()
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


class ContextSource(ABC):
    """Source-neutral interface consumed by the Qt application."""

    @abstractmethod
    def discover(self, locations: Iterable[str | Path]) -> list[RecordRef]:
        """Return context records available at the supplied locations."""

    @abstractmethod
    def load(self, record: RecordRef) -> ContextDocument:
        """Parse and index one context record."""


class JsonFileSource(ContextSource):
    """JSON context source with a stat-aware, per-session document cache."""

    def __init__(self) -> None:
        """Initialize an empty thread-safe session cache."""
        self._cache: dict[str, tuple[tuple[int, int], ContextDocument]] = {}
        self._lock = threading.Lock()

    def discover(self, locations: Iterable[str | Path]) -> list[RecordRef]:
        paths: set[Path] = set()
        for location in locations:
            path = Path(location).expanduser()
            if not path.exists():
                message = f"Path does not exist: {path}"
                raise ContextSourceError(message)
            if path.is_dir():
                paths.update(
                    candidate
                    for candidate in path.rglob("*.json")
                    if candidate.is_file()
                )
            elif path.is_file():
                paths.add(path)

        records: list[RecordRef] = []
        for path in sorted(
            paths,
            key=lambda item: (
                alphanumeric_key(item.name),
                str(item.parent).casefold(),
                str(item),
            ),
        ):
            resolved = path.resolve()
            stat = resolved.stat()
            digest = hashlib.blake2s(str(resolved).encode(), digest_size=8).hexdigest()
            records.append(
                RecordRef(
                    id=digest,
                    name=resolved.name,
                    path=resolved,
                    size=stat.st_size,
                    modified=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                )
            )
        return records

    def load(self, record: RecordRef) -> ContextDocument:
        try:
            stat = record.path.stat()
        except OSError as exc:
            message = f"Cannot read {record.path}: {exc}"
            raise ContextSourceError(message) from exc
        signature = (stat.st_mtime_ns, stat.st_size)

        with self._lock:
            cached = self._cache.get(record.id)
            if cached is not None and cached[0] == signature:
                return cached[1]

        try:
            with record.path.open(encoding="utf-8") as stream:
                raw = json.load(stream)
        except (OSError, json.JSONDecodeError) as exc:
            message = f"Cannot load {record.path.name}: {exc}"
            raise ContextSourceError(message) from exc

        document = ContextDocument(record, raw)
        with self._lock:
            self._cache[record.id] = (signature, document)
        return document


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
