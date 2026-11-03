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
import threading
from abc import ABC, abstractmethod
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from chemfit_dashboard.context_document import (
    ContextDocument,
    RecordRef,
    alphanumeric_key,
)

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
            if cached is not None:
                self._cache.move_to_end(record.id)
                return cached[1]

        document = self._load(record)

        estimated_cost = max(1, round(record.size * self.object_overhead_factor))

        with self._lock:
            # Another thread may have loaded it in the meantime.
            cached = self._cache.get(record.id)
            if cached is not None:
                self._cache.move_to_end(record.id)
                return cached[1]

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


class JsonFileSource(CachedContextSource):
    """
    Discover and load evaluation contexts from JSON files.

    Each JSON file represents one evaluation. Documents are parsed
    and indexed on demand, with caching provided by the base class.
    """

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
                    position_in_file=1,
                    name=resolved.name,
                    path=resolved,
                    size=stat.st_size,
                    modified=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                )
            )
        return records

    def _load(self, record: RecordRef) -> ContextDocument:
        """Read and parse a JSON evaluation into a ContextDocument."""

        try:
            with record.path.open(encoding="utf-8") as stream:
                raw = json.load(stream)
        except (OSError, json.JSONDecodeError) as exc:
            message = f"Cannot load {record.path.name}: {exc}"
            raise ContextSourceError(message) from exc

        return ContextDocument(
            record,
            raw,
        )
