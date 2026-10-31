from __future__ import annotations

import json
import threading
from concurrent.futures import Executor, ThreadPoolExecutor

import pytest

from chemfit_dashboard.process_search import (
    CollectionSearchExecutor,
    CollectionSearchResult,
    SearchBatchResult,
    SearchOptions,
    search_collection,
    search_record_batch,
)
from chemfit_dashboard.search_core import (
    MAX_RESULTS,
    ContextSearchIndex,
    TopKSearchHits,
)
from chemfit_dashboard.source import ContextSourceError, JsonFileSource
from chemfit_dashboard.traversal import CancellationToken


def _write_collection(tmp_path, count: int = 6):
    for index in range(count):
        value = {
            "meta": {"tag": f"group_{index % 2}"},
            "loss": index / 10,
            "output": {"message": f"alpha result {index}"},
            "children": [
                {
                    "meta": {"name": f"component_{index}"},
                    "output": {"energy": index * 2},
                }
            ],
        }
        (tmp_path / f"step_{index}.json").write_text(
            json.dumps(value), encoding="utf-8"
        )
    source = JsonFileSource(cache_capacity=0)
    return source, source.discover([tmp_path])


def _serial_search(source, records, options):
    collector = TopKSearchHits(MAX_RESULTS)
    failed = 0
    for record in records:
        try:
            index = ContextSearchIndex(source.load(record))
        except ContextSourceError:
            failed += 1
            continue
        collector.extend(
            index.search(
                options.query,
                include_keys=options.include_keys,
                include_values=options.include_values,
                include_metadata=options.include_metadata,
                include_components=options.include_components,
                mode=options.mode,
            )
        )
    return CollectionSearchResult(collector.results(), failed)


@pytest.mark.parametrize(
    "options",
    [
        SearchOptions("alhpa"),
        SearchOptions("alpha result", mode="exact"),
        SearchOptions(r"component_[135]", mode="regex"),
        SearchOptions(
            "group_1",
            include_keys=False,
            include_values=True,
            include_metadata=True,
            include_components=False,
            mode="exact",
        ),
        SearchOptions(
            "energy",
            include_keys=True,
            include_values=False,
            include_metadata=False,
            include_components=False,
        ),
    ],
)
def test_spawn_process_search_exactly_matches_serial(tmp_path, options):
    source, records = _write_collection(tmp_path)
    expected = _serial_search(source, records, options)
    executor = CollectionSearchExecutor(max_workers=2)
    try:
        actual = executor.search(
            records,
            options,
            component_label_key="name",
        )
    finally:
        executor.shutdown(wait_for_workers=True)

    assert actual == expected


def test_batch_search_counts_unreadable_files_and_empty_collection(tmp_path):
    source, records = _write_collection(tmp_path, 2)
    broken = tmp_path / "step_broken.json"
    broken.write_text("{", encoding="utf-8")
    records = source.discover([tmp_path])
    options = SearchOptions("output")

    batch = search_record_batch(tuple(records), options, "name")
    with ThreadPoolExecutor(max_workers=2) as pool:
        empty = search_collection(
            pool,
            [],
            options,
            component_label_key="name",
            worker_count=2,
        )

    assert batch.failed_records == 1
    assert batch.hits
    assert empty == CollectionSearchResult([], 0)


def test_global_top_k_matches_serial_with_duplicate_scores(tmp_path):
    source, records = _write_collection(tmp_path, 40)
    options = SearchOptions("output", include_values=False)
    expected = _serial_search(source, records, options)

    with ThreadPoolExecutor(max_workers=4) as pool:
        actual = search_collection(
            pool,
            records,
            options,
            component_label_key="name",
            worker_count=4,
        )

    assert actual.hits == expected.hits
    assert len(actual.hits) <= MAX_RESULTS


def test_cancelled_search_stops_submitting_chunks(tmp_path, monkeypatch):
    _source, records = _write_collection(tmp_path, 40)
    started = threading.Event()
    release = threading.Event()

    def slow_batch(*_args, **_kwargs):
        started.set()
        release.wait(timeout=2)
        return SearchBatchResult((), 0)

    monkeypatch.setattr(
        "chemfit_dashboard.process_search.search_record_batch", slow_batch
    )
    cancellation = CancellationToken()
    result: list[CollectionSearchResult] = []
    pool = ThreadPoolExecutor(max_workers=2)
    coordinator = threading.Thread(
        target=lambda: result.append(
            search_collection(
                pool,
                records,
                SearchOptions("output"),
                component_label_key="name",
                worker_count=2,
                cancellation=cancellation,
            )
        )
    )
    coordinator.start()
    assert started.wait(timeout=1)

    cancellation.cancel()
    coordinator.join(timeout=1)
    release.set()
    pool.shutdown(wait=True, cancel_futures=True)

    assert not coordinator.is_alive()
    assert result == [CollectionSearchResult([], 0, cancelled=True)]


def test_worker_exception_is_reported(tmp_path, monkeypatch):
    _source, records = _write_collection(tmp_path, 20)

    def broken_batch(*_args, **_kwargs):
        message = "worker failed"
        raise RuntimeError(message)

    monkeypatch.setattr(
        "chemfit_dashboard.process_search.search_record_batch", broken_batch
    )
    with (
        ThreadPoolExecutor(max_workers=2) as pool,
        pytest.raises(RuntimeError, match="worker failed"),
    ):
        search_collection(
            pool,
            records,
            SearchOptions("output"),
            component_label_key="name",
            worker_count=2,
        )


class _RecordingExecutor(Executor):
    def __init__(self):
        self.shutdown_calls = []

    def shutdown(self, wait=True, *, cancel_futures=False):
        self.shutdown_calls.append((wait, cancel_futures))


def test_executor_shutdown_is_clean_and_idempotent():
    backend = _RecordingExecutor()
    executor = CollectionSearchExecutor(max_workers=2, executor=backend)

    executor.shutdown(wait_for_workers=False)
    executor.shutdown(wait_for_workers=True)

    assert backend.shutdown_calls == [(False, True)]


def test_cancelled_before_start_does_not_submit(tmp_path):
    _source, records = _write_collection(tmp_path, 2)
    cancellation = CancellationToken()
    cancellation.cancel()
    backend = _RecordingExecutor()

    result = search_collection(
        backend,
        records,
        SearchOptions("output"),
        component_label_key="name",
        worker_count=2,
        cancellation=cancellation,
    )

    assert result.cancelled
    assert backend.shutdown_calls == []
