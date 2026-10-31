from __future__ import annotations

import math
import threading
from collections import OrderedDict
from concurrent.futures import (
    FIRST_COMPLETED,
    Executor,
    Future,
    ProcessPoolExecutor,
    wait,
)
from dataclasses import dataclass, field
from multiprocessing import get_context
from typing import TYPE_CHECKING

from chemfit_dashboard.search_core import (
    MAX_RESULTS,
    ContextSearchIndex,
    SearchHit,
    SearchMode,
    TopKSearchHits,
)
from chemfit_dashboard.source import ContextSourceError, JsonFileSource, RecordRef

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from chemfit_dashboard.traversal import CancellationToken

# On the 1,000-evaluation standard benchmark, two workers cut warm latency from
# 0.313 s to 0.168 s. Four reached 0.107 s but raised process-tree RSS from
# 205 MiB to 328 MiB, so two is the better default for an interactive desktop.
DEFAULT_SEARCH_WORKERS = 2
# With two workers and 1,000 lightweight evaluations, 16/64/256-record chunks
# measured 0.141/0.120/0.099 s warm. The adaptive target chooses about 64 there:
# close to the larger chunk's throughput while limiting obsolete work after a
# query switch to a quarter of the 256-record cancellation window.
MIN_CHUNK_RECORDS = 16
MAX_CHUNK_RECORDS = 256
TARGET_CHUNKS_PER_WORKER = 8
WORKER_DOCUMENT_CACHE_ENTRIES = 8
WORKER_DOCUMENT_CACHE_BYTES = 64 * 1024 * 1024
WORKER_INDEX_CACHE_ENTRIES = 32
WORKER_INDEX_ENTRY_BUDGET = 40_000


@dataclass(frozen=True, slots=True)
class SearchOptions:
    """Serializable options shared by serial and process-based search."""

    query: str
    include_keys: bool = True
    include_values: bool = True
    include_metadata: bool = True
    include_components: bool = True
    mode: SearchMode = "fuzzy"


@dataclass(frozen=True, slots=True)
class SearchBatchResult:
    """Best hits and failures produced by one contiguous record chunk."""

    hits: tuple[SearchHit, ...]
    failed_records: int


@dataclass(frozen=True, slots=True)
class CollectionSearchResult:
    """Globally merged result returned to the GUI coordinator thread."""

    hits: list[SearchHit]
    failed_records: int
    cancelled: bool = False


@dataclass(slots=True)
class _WorkerState:
    source: JsonFileSource | None = None
    indexes: OrderedDict[
        tuple[str, int, int, str], ContextSearchIndex
    ] = field(default_factory=OrderedDict)
    index_entries: int = 0


_worker_state = _WorkerState()


def _worker_source_instance(component_label_key: str) -> JsonFileSource:
    """Return the bounded source cache owned by one persistent worker process."""
    if _worker_state.source is None:
        _worker_state.source = JsonFileSource(
            cache_capacity=WORKER_DOCUMENT_CACHE_ENTRIES,
            cache_memory_budget=WORKER_DOCUMENT_CACHE_BYTES,
        )
    _worker_state.source.set_component_label_metadata_key(component_label_key)
    return _worker_state.source


def _worker_index(
    source: JsonFileSource,
    record: RecordRef,
    component_label_key: str,
) -> ContextSearchIndex:
    """Load or build one stat-aware index in the worker-local bounded cache."""
    try:
        stat = record.path.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        signature = (0, record.size)
    key = (record.id, signature[0], signature[1], component_label_key)
    cached = _worker_state.indexes.get(key)
    if cached is not None:
        _worker_state.indexes.move_to_end(key)
        return cached

    index = ContextSearchIndex(source.configure_document(source.load(record)))
    _worker_state.indexes[key] = index
    _worker_state.indexes.move_to_end(key)
    _worker_state.index_entries += len(index.entries)
    while _worker_state.indexes and (
        len(_worker_state.indexes) > WORKER_INDEX_CACHE_ENTRIES
        or _worker_state.index_entries > WORKER_INDEX_ENTRY_BUDGET
    ):
        _old_key, old_index = _worker_state.indexes.popitem(last=False)
        _worker_state.index_entries -= len(old_index.entries)
    return index


def _search_record(
    source: JsonFileSource,
    record: RecordRef,
    options: SearchOptions,
    component_label_key: str,
) -> tuple[list[SearchHit], bool]:
    try:
        index = _worker_index(source, record, component_label_key)
    except ContextSourceError:
        return [], True
    return (
        index.search(
            options.query,
            include_keys=options.include_keys,
            include_values=options.include_values,
            include_metadata=options.include_metadata,
            include_components=options.include_components,
            mode=options.mode,
        ),
        False,
    )


def search_record_batch(
    records: tuple[RecordRef, ...],
    options: SearchOptions,
    component_label_key: str,
) -> SearchBatchResult:
    """Search one chunk in a process without importing or touching Qt."""
    source = _worker_source_instance(component_label_key)
    hits = TopKSearchHits(MAX_RESULTS)
    failed_records = 0
    for record in records:
        record_hits, failed = _search_record(
            source, record, options, component_label_key
        )
        hits.extend(record_hits)
        failed_records += failed
    return SearchBatchResult(tuple(hits.results()), failed_records)


def search_chunk_size(record_count: int, worker_count: int) -> int:
    """Balance useful work per process against prompt query supersession."""
    if record_count <= 0:
        return MIN_CHUNK_RECORDS
    target_chunks = max(1, worker_count * TARGET_CHUNKS_PER_WORKER)
    return min(
        MAX_CHUNK_RECORDS,
        max(MIN_CHUNK_RECORDS, math.ceil(record_count / target_chunks)),
    )


def _record_chunks(
    records: Sequence[RecordRef], worker_count: int
) -> tuple[tuple[RecordRef, ...], ...]:
    size = search_chunk_size(len(records), worker_count)
    return tuple(
        tuple(records[start : start + size])
        for start in range(0, len(records), size)
    )


def search_collection(
    executor: Executor,
    records: Sequence[RecordRef],
    options: SearchOptions,
    *,
    component_label_key: str,
    worker_count: int,
    cancellation: CancellationToken | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> CollectionSearchResult:
    """
    Search chunks with bounded submission and merge them in record order.

    At most ``worker_count`` futures are submitted at once. Cancellation therefore
    stops after currently running chunks finish; Python futures cannot interrupt a
    running process task. Results are retained by chunk index and merged in original
    record order so equal-ranked hits remain identical to serial traversal regardless
    of worker completion order.
    """
    if not records:
        return CollectionSearchResult([], 0)
    if cancellation is not None and cancellation.cancelled:
        return CollectionSearchResult([], 0, cancelled=True)

    chunks = _record_chunks(records, worker_count)
    next_chunk = 0
    futures: dict[Future[SearchBatchResult], int] = {}
    results: dict[int, SearchBatchResult] = {}
    completed_records = 0

    def submit_next() -> bool:
        nonlocal next_chunk
        if next_chunk >= len(chunks) or (
            cancellation is not None and cancellation.cancelled
        ):
            return False
        try:
            future = executor.submit(
                search_record_batch,
                chunks[next_chunk],
                options,
                component_label_key,
            )
        except RuntimeError:
            if cancellation is not None and cancellation.cancelled:
                return False
            raise
        futures[future] = next_chunk
        next_chunk += 1
        return True

    for _ in range(min(worker_count, len(chunks))):
        submit_next()

    while futures:
        if cancellation is not None and cancellation.cancelled:
            for future in futures:
                future.cancel()
            return CollectionSearchResult([], 0, cancelled=True)
        done, _pending = wait(
            tuple(futures), timeout=0.05, return_when=FIRST_COMPLETED
        )
        for future in done:
            chunk_index = futures.pop(future)
            try:
                results[chunk_index] = future.result()
            except Exception:
                for pending in futures:
                    pending.cancel()
                raise
            completed_records += len(chunks[chunk_index])
            if progress is not None:
                progress(completed_records, len(records))
            submit_next()

    if cancellation is not None and cancellation.cancelled:
        return CollectionSearchResult([], 0, cancelled=True)
    collector = TopKSearchHits(MAX_RESULTS)
    failed_records = 0
    for chunk_index in range(len(chunks)):
        result = results[chunk_index]
        collector.extend(result.hits)
        failed_records += result.failed_records
    return CollectionSearchResult(collector.results(), failed_records)


class CollectionSearchExecutor:
    """Own the dashboard's reusable spawn-based collection search pool."""

    def __init__(
        self,
        max_workers: int = DEFAULT_SEARCH_WORKERS,
        *,
        executor: Executor | None = None,
    ) -> None:
        """Create a lazy persistent pool; workers start on the first submission."""
        self.max_workers = max(1, max_workers)
        self._executor = executor or ProcessPoolExecutor(
            max_workers=self.max_workers,
            mp_context=get_context("spawn"),
        )
        self._closed = False
        self._lock = threading.Lock()

    def search(
        self,
        records: Sequence[RecordRef],
        options: SearchOptions,
        *,
        component_label_key: str,
        cancellation: CancellationToken | None = None,
        progress: Callable[[int, int], None] | None = None,
    ) -> CollectionSearchResult:
        """Coordinate one search from a non-GUI thread."""
        with self._lock:
            if self._closed:
                message = "Collection search executor is closed"
                raise RuntimeError(message)
        return search_collection(
            self._executor,
            records,
            options,
            component_label_key=component_label_key,
            worker_count=self.max_workers,
            cancellation=cancellation,
            progress=progress,
        )

    def shutdown(self, *, wait_for_workers: bool = False) -> None:
        """Reject new work and cancel queued chunks; running chunks finish normally."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(
            wait=wait_for_workers,
            cancel_futures=True,
        )
