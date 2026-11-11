"""
Context discovery, JSON/JSONL loading, and indexed document access.

``JsonFileSource`` discovers immediate local JSON and JSONL files and owns a
bounded, thread-safe document cache.
``ContextDocument`` interprets ChemFit's serialized context hierarchy and is
the data boundary consumed by every feature.
"""

from __future__ import annotations

import hashlib
import json
import threading
from abc import ABC, abstractmethod
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from chemfit_dashboard.context_document import (
    ContextDocument,
    RecordRef,
    alphanumeric_key,
)
from chemfit_dashboard.record_format import RecordFormatError, normalize_record

if TYPE_CHECKING:
    from collections.abc import Iterable


class ContextSourceError(Exception):
    """Error class for raised by context sources."""


class ContextSource(ABC):
    """Source-neutral interface consumed by the Qt application."""

    @abstractmethod
    def discover(self, locations: Iterable[str | Path]) -> list[RecordRef]:
        """Return context records available at the supplied locations."""

    @abstractmethod
    def load(self, record: RecordRef) -> ContextDocument:
        """Parse and index one context record."""


class CachedContextSource(ContextSource):
    def __init__(
        self,
        *,
        cache_capacity: int = 64,
        cache_memory_budget: int = 256 * 1024 * 1024,
        object_overhead_factor: float = 3.0,
    ) -> None:
        """Initialize a thread-safe cache bounded by entries and estimated bytes."""

        if cache_capacity < 0 or cache_memory_budget < 0:
            message = "Cache limits cannot be negative"
            raise ValueError(message)
        if object_overhead_factor < 1:
            message = "object_overhead_factor must be at least 1"
            raise ValueError(message)
        self.cache_capacity = cache_capacity
        self.cache_memory_budget = cache_memory_budget
        self.object_overhead_factor = object_overhead_factor

        # self._cache maps: record.id -> (estimated_cost, document)
        self._cache: OrderedDict[str, tuple[int, ContextDocument]] = OrderedDict()
        self._cache_bytes = 0
        self._lock = threading.RLock()

    @property
    def cache_info(self) -> tuple[int, int]:
        """Return cached entry count and approximate retained bytes."""
        with self._lock:
            return len(self._cache), self._cache_bytes

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()
            self._cache_bytes = 0

    @abstractmethod
    def _load(self, record: RecordRef) -> ContextDocument:
        """Load a ContextDocument from a RecordRef."""

    def load(self, record: RecordRef) -> ContextDocument:
        with self._lock:
            cached = self._cache.get(record.id)
            if cached is not None and _same_record_version(record, cached[1].record):
                self._cache.move_to_end(record.id)
                return cached[1]

        document = self._load(record)

        estimated_cost = max(1, round(record.size * self.object_overhead_factor))

        with self._lock:
            # Another thread may have loaded it in the meantime.
            cached = self._cache.get(record.id)
            if cached is not None and _same_record_version(record, cached[1].record):
                self._cache.move_to_end(record.id)
                return cached[1]

            if cached is not None:
                cached_cost, _ = self._cache.pop(record.id)
                self._cache_bytes -= cached_cost

            if (
                self.cache_capacity > 0
                and self.cache_memory_budget > 0
                and estimated_cost <= self.cache_memory_budget
            ):
                self._cache[record.id] = (estimated_cost, document)
                self._cache_bytes += estimated_cost

                while self._cache and (
                    len(self._cache) > self.cache_capacity
                    or self._cache_bytes > self.cache_memory_budget
                ):
                    _, (cost, _) = self._cache.popitem(last=False)
                    self._cache_bytes -= cost

        return document


def _same_record_version(first: RecordRef, second: RecordRef) -> bool:
    return first.modified == second.modified and first.size == second.size


class JsonFileSource(CachedContextSource):
    """
    Discover and load evaluation contexts from JSON files or JSONL trajectories.

    Each JSON file represents one evaluation. Every nonblank JSONL line
    represents one evaluation. Records are validated and indexed on demand,
    with caching provided by the base class.
    """

    def discover(self, locations: Iterable[str | Path]) -> list[RecordRef]:
        """Discover JSON and JSONL evaluations from local files and directories."""
        paths: set[Path] = set()
        supported_suffixes = {".json", ".jsonl"}

        for location in locations:
            path = Path(location).expanduser()

            if path.is_dir():
                # Scan only the immediate directory, not its subdirectories.
                paths.update(
                    candidate
                    for candidate in path.iterdir()
                    if candidate.is_file()
                    and candidate.suffix.casefold() in supported_suffixes
                )
            elif path.is_file():
                if path.suffix.casefold() not in supported_suffixes:
                    message = (
                        f"Unsupported file type: {path}. "
                        "Expected a .json or .jsonl file."
                    )
                    raise ContextSourceError(message)
                paths.add(path)
            else:
                msg = f"Invalid or missing path: {path}"
                raise ContextSourceError(msg)

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
            modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            if resolved.suffix.casefold() == ".jsonl":
                records.extend(_jsonl_records(resolved, modified))
            else:
                digest = _record_id(resolved)
                record = RecordRef(
                    id=digest,
                    position_in_file=1,
                    name=resolved.name,
                    path=resolved,
                    size=stat.st_size,
                    modified=modified,
                )
                records.append(record)

        return records

    def _load(self, record: RecordRef) -> ContextDocument:
        """Read and parse a JSON or JSONL evaluation into a ContextDocument."""
        return ContextDocument(record, _read_normalized_record(record))


def _record_id(path: Path, position: int | None = None) -> str:
    identity = str(path) if position is None else f"{path}\0{position}"
    return hashlib.blake2s(identity.encode(), digest_size=8).hexdigest()


def _jsonl_records(path: Path, modified: datetime) -> list[RecordRef]:
    records = []
    with path.open("rb") as stream:
        line_number = 0
        while True:
            byte_offset = stream.tell()
            line = stream.readline()
            if not line:
                break
            line_number += 1
            if not line.strip():
                continue
            records.append(
                RecordRef(
                    id=_record_id(path, line_number),
                    position_in_file=line_number,
                    name=f"{path.name}:{line_number}",
                    path=path,
                    size=len(line),
                    modified=modified,
                    byte_offset=byte_offset,
                )
            )
    return records


def _read_normalized_record(record: RecordRef) -> Any:
    """Read and validate one record when its contents are requested."""
    try:
        raw = (
            _load_jsonl_record(record)
            if record.path.suffix.casefold() == ".jsonl"
            else _load_json_record(record)
        )
        return normalize_record(
            raw,
            require_versioned=record.path.suffix.casefold() == ".jsonl",
        )
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecordFormatError,
    ) as exc:
        location = record.path.name
        if record.path.suffix.casefold() == ".jsonl":
            location = f"{location} line {record.position_in_file}"
        message = f"Cannot load {location}: {exc}"
        raise ContextSourceError(message) from exc


def _load_json_record(record: RecordRef) -> Any:
    with record.path.open(encoding="utf-8") as stream:
        return json.load(stream)


def _load_jsonl_record(record: RecordRef) -> Any:
    with record.path.open("rb") as stream:
        if record.byte_offset:
            stream.seek(record.byte_offset)
            line = stream.readline()
        else:
            line = b""
            for _line_number in range(record.position_in_file):
                line = stream.readline()
        if not line:
            message = "record line no longer exists"
            raise OSError(message)
        return json.loads(line)
