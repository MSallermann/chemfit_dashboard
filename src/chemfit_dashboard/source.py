"""
Context discovery, JSON loading, and indexed document access.

``JsonFileSource`` discovers immediate JSON files, materializes GIO-backed
remote locations, and owns a bounded thread-safe document cache.
``ContextDocument`` interprets ChemFit's serialized context hierarchy and is
the data boundary consumed by every feature.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
import threading
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, unquote, urlparse

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

NodePath = tuple[int, ...]
DEFAULT_COMPONENT_LABEL_METADATA_KEY = "name"
COOPERATIVE_WORK_INTERVAL_SECONDS = 0.004
COOPERATIVE_PAUSE_SECONDS = 0.001


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
    """Stable identity and file metadata for one discoverable evaluation."""

    id: str
    name: str
    path: Path
    size: int
    modified: datetime

    @property
    def location(self) -> str:
        return str(self.path.parent)


@dataclass(slots=True)
class _InflightDocument:
    signature: tuple[int, int]
    ready: threading.Event
    document: ContextDocument | None = None
    error: str | None = None


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


class ContextDocument:
    """
    One parsed JSON document with an immutable context-node index.

    The document owns decoded JSON, structural node/child indexes, a small
    thread-safe LRU of computed ``NodeDetails``, and the mutable presentation
    label key. It does not own its source cache and remains valid after eviction.
    """

    def __init__(
        self,
        record: RecordRef,
        raw: Any,
        *,
        component_label_metadata_key: str = DEFAULT_COMPONENT_LABEL_METADATA_KEY,
    ):
        """Index the parsed context hierarchy without changing its values."""
        self.record = record
        self.raw = raw
        self._component_label_metadata_key = component_label_metadata_key.strip()
        self._nodes: dict[NodePath, _NodeRecord] = {}
        self._children: dict[NodePath, tuple[NodePath, ...]] = {}
        self._details_cache: OrderedDict[NodePath, NodeDetails] = OrderedDict()
        self._details_cache_capacity = 64
        self._details_lock = threading.RLock()
        self._component_label_metadata_keys: tuple[str, ...] | None = None
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

    def children_slice(
        self, path: NodePath, start: int, stop: int
    ) -> tuple[NodeInfo, ...]:
        """Return a bounded child window for incremental GUI models."""
        child_paths = self._children.get(path, ())[start:stop]
        return tuple(self._nodes[child].info for child in child_paths)

    def details(self, path: NodePath) -> NodeDetails:
        with self._details_lock:
            cached = self._details_cache.get(path)
            if cached is not None:
                self._details_cache.move_to_end(path)
                return cached
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

        details = NodeDetails(
            info=node.info,
            fields=fields,
            metadata=metadata,
            structure=structure,
            raw=raw,
        )
        with self._details_lock:
            self._details_cache[path] = details
            if len(self._details_cache) > self._details_cache_capacity:
                self._details_cache.popitem(last=False)
        return details

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
        next_yield = time.monotonic() + COOPERATIVE_WORK_INTERVAL_SECONDS
        for path in self._nodes:
            for key, value in self.details(path).metadata.items():
                if isinstance(key, str) and isinstance(value, str) and value.strip():
                    keys.add(key)
            if time.monotonic() >= next_yield:
                time.sleep(COOPERATIVE_PAUSE_SECONDS)
                next_yield = time.monotonic() + COOPERATIVE_WORK_INTERVAL_SECONDS
        self._component_label_metadata_keys = tuple(sorted(keys, key=str.casefold))
        return self._component_label_metadata_keys

    def _build_index(self) -> None:
        root_label = self._identity(self.raw) or self.record.path.stem
        pending: list[tuple[NodePath, Any, str, bool]] = [
            ((), self.raw, root_label, True)
        ]
        component_label_keys: set[str] = set()
        next_yield = time.monotonic() + COOPERATIVE_WORK_INTERVAL_SECONDS

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
            if time.monotonic() >= next_yield:
                time.sleep(COOPERATIVE_PAUSE_SECONDS)
                next_yield = time.monotonic() + COOPERATIVE_WORK_INTERVAL_SECONDS
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


class ContextSource(ABC):
    """Source-neutral interface consumed by the Qt application."""

    @abstractmethod
    def discover(self, locations: Iterable[str | Path]) -> list[RecordRef]:
        """Return context records available at the supplied locations."""

    @abstractmethod
    def load(self, record: RecordRef) -> ContextDocument:
        """Parse and index one context record."""

    @property
    def component_label_metadata_key(self) -> str:
        return getattr(
            self,
            "_component_label_metadata_key",
            DEFAULT_COMPONENT_LABEL_METADATA_KEY,
        )

    def set_component_label_metadata_key(self, key: str) -> None:
        """Configure the label metadata key for documents loaded by this source."""
        self._component_label_metadata_key = key.strip()

    def configure_document(self, document: ContextDocument) -> ContextDocument:
        """Apply source-level presentation configuration to a loaded document."""
        document.set_component_label_metadata_key(self.component_label_metadata_key)
        return document


class JsonFileSource(ContextSource):
    """
    JSON context source with a stat-aware, bounded LRU document cache.

    The source may be called concurrently by several Qt workers. In-flight load
    records ensure only one thread parses a given file signature; waiters reuse
    its document or receive the same source error. Cache capacity and estimated
    memory are independent limits, and callers may retain evicted documents.
    """

    def __init__(
        self,
        *,
        cache_capacity: int = 64,
        cache_memory_budget: int = 256 * 1024 * 1024,
        object_overhead_factor: float = 3.0,
    ) -> None:
        """Initialize a thread-safe cache bounded by entries and estimated bytes."""
        super().__init__()
        if cache_capacity < 0 or cache_memory_budget < 0:
            message = "Cache limits cannot be negative"
            raise ValueError(message)
        if object_overhead_factor < 1:
            message = "object_overhead_factor must be at least 1"
            raise ValueError(message)
        self.cache_capacity = cache_capacity
        self.cache_memory_budget = cache_memory_budget
        self.object_overhead_factor = object_overhead_factor
        self._cache: OrderedDict[str, tuple[tuple[int, int], int, ContextDocument]] = (
            OrderedDict()
        )
        self._cache_bytes = 0
        self._lock = threading.RLock()
        self._inflight: dict[str, _InflightDocument] = {}
        self._remote_cache = tempfile.TemporaryDirectory(
            prefix="chemfit-dashboard-remote-"
        )

    def set_component_label_metadata_key(self, key: str) -> None:
        super().set_component_label_metadata_key(key)
        with self._lock:
            for _signature, _cost, document in self._cache.values():
                self.configure_document(document)

    @property
    def cache_info(self) -> tuple[int, int]:
        """Return cached entry count and approximate retained bytes."""
        with self._lock:
            return len(self._cache), self._cache_bytes

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()
            self._cache_bytes = 0

    def discover(self, locations: Iterable[str | Path]) -> list[RecordRef]:
        paths: set[Path] = set()
        for location in locations:
            if isinstance(location, str) and "://" in location:
                paths.update(self._download_remote_location(location))
                continue
            path = Path(location).expanduser()
            if not path.exists():
                message = f"Path does not exist: {path}"
                raise ContextSourceError(message)
            if path.is_dir():
                paths.update(
                    candidate
                    for candidate in path.glob("*.json")
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

    def _download_remote_location(self, uri: str) -> set[Path]:
        """Materialize immediate JSON files from a GIO-backed remote location."""
        parsed = urlparse(uri)
        name = Path(unquote(parsed.path)).name
        if name.casefold().endswith(".json"):
            return {self._gio_copy(uri, name)}

        listing = subprocess.run(  # noqa: S603
            ["gio", "list", uri],  # noqa: S607
            check=False,
            capture_output=True,
            text=True,
        )
        if listing.returncode:
            message = listing.stderr.strip() or f"Cannot read remote location: {uri}"
            raise ContextSourceError(message)
        downloaded: set[Path] = set()
        base = uri if uri.endswith("/") else f"{uri}/"
        for listed_name in listing.stdout.splitlines():
            child_name = listed_name.strip()
            if child_name.casefold().endswith(".json"):
                downloaded.add(
                    self._gio_copy(f"{base}{quote(child_name)}", child_name)
                )
        return downloaded

    def _gio_copy(self, uri: str, name: str) -> Path:
        name = Path(name).name
        if not name or name in {".", ".."}:
            message = f"Invalid remote file name: {name!r}"
            raise ContextSourceError(message)
        digest = hashlib.blake2s(uri.encode(), digest_size=8).hexdigest()
        directory = Path(self._remote_cache.name) / digest
        directory.mkdir(exist_ok=True)
        destination = directory / name
        copied = subprocess.run(  # noqa: S603
            ["gio", "copy", "-T", uri, str(destination)],  # noqa: S607
            check=False,
            capture_output=True,
            text=True,
        )
        if copied.returncode:
            message = copied.stderr.strip() or f"Cannot copy remote file: {uri}"
            raise ContextSourceError(message)
        return destination

    def load(self, record: RecordRef) -> ContextDocument:  # noqa: PLR0912, PLR0915
        """Load one record, deduplicating concurrent work by file signature."""
        try:
            stat = record.path.stat()
        except OSError as exc:
            message = f"Cannot read {record.path}: {exc}"
            raise ContextSourceError(message) from exc
        signature = (stat.st_mtime_ns, stat.st_size)

        with self._lock:
            cached = self._cache.get(record.id)
            if cached is not None:
                if cached[0] == signature:
                    self._cache.move_to_end(record.id)
                    return self.configure_document(cached[2])
                self._cache_bytes -= cached[1]
                del self._cache[record.id]

            inflight = self._inflight.get(record.id)
            if inflight is not None and inflight.signature == signature:
                should_load = False
            else:
                inflight = _InflightDocument(signature, threading.Event())
                self._inflight[record.id] = inflight
                should_load = True

        if not should_load:
            inflight.ready.wait()
            if inflight.error is not None:
                raise ContextSourceError(inflight.error)
            if inflight.document is None:
                message = f"Concurrent load of {record.path.name} produced no document"
                raise ContextSourceError(message)
            return self.configure_document(inflight.document)

        try:
            try:
                with record.path.open(encoding="utf-8") as stream:
                    raw = json.load(stream)
            except (OSError, json.JSONDecodeError) as exc:
                message = f"Cannot load {record.path.name}: {exc}"
                raise ContextSourceError(message) from exc

            document = ContextDocument(
                record,
                raw,
                component_label_metadata_key=self.component_label_metadata_key,
            )
        except Exception as error:
            self._complete_inflight(record.id, inflight, error=str(error))
            raise
        estimated_cost = max(1, round(stat.st_size * self.object_overhead_factor))
        with self._lock:
            if (
                self.cache_capacity > 0
                and self.cache_memory_budget > 0
                and estimated_cost <= self.cache_memory_budget
            ):
                previous = self._cache.pop(record.id, None)
                if previous is not None:
                    self._cache_bytes -= previous[1]
                self._cache[record.id] = (signature, estimated_cost, document)
                self._cache_bytes += estimated_cost
                while self._cache and (
                    len(self._cache) > self.cache_capacity
                    or self._cache_bytes > self.cache_memory_budget
                ):
                    _key, (_signature, cost, _document) = self._cache.popitem(
                        last=False
                    )
                    self._cache_bytes -= cost
        self._complete_inflight(record.id, inflight, document=document)
        return document

    def _complete_inflight(
        self,
        record_id: str,
        inflight: _InflightDocument,
        *,
        document: ContextDocument | None = None,
        error: str | None = None,
    ) -> None:
        with self._lock:
            inflight.document = document
            inflight.error = error
            if self._inflight.get(record_id) is inflight:
                self._inflight.pop(record_id, None)
            inflight.ready.set()


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
