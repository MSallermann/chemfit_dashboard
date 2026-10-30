from __future__ import annotations

import argparse
import json
import os
import resource
import tempfile
import time
import tracemalloc
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from chemfit_dashboard.collection_analysis import analyze_collection
from chemfit_dashboard.collection_plot import (
    CollectionPlotPanel,
    CollectionSeries,
    NumberSelector,
    SeriesPoint,
)
from chemfit_dashboard.context_search import (
    ContextSearchIndex,
    TopKSearchHits,
    clear_search_caches,
)
from chemfit_dashboard.data_flow import (
    INPUT_NODE,
    OUTPUT_NODE,
    build_data_flow,
    child_node,
)
from chemfit_dashboard.extraction import (
    ExtractionConfig,
    FieldSelector,
    export_csv,
    export_jsonl,
    extract_fields,
)
from chemfit_dashboard.graph_layout import data_flow_layout
from chemfit_dashboard.graph_view import DataFlowPanel
from chemfit_dashboard.models import EvaluationListModel
from chemfit_dashboard.source import JsonFileSource
from chemfit_dashboard.traversal import iter_collection


@dataclass(frozen=True, slots=True)
class BenchmarkProfile:
    evaluations: int
    graph_nodes: int
    components_per_evaluation: int
    root_fields: int
    search_queries: tuple[str, ...]
    render_timeout: float


STANDARD_PROFILE = BenchmarkProfile(1_000, 2_000, 0, 0, ("loss",), 30.0)
STRESS_PROFILE = BenchmarkProfile(
    50_000,
    25_000,
    40,
    80,
    ("loss", "component_39", "tag_7"),
    300.0,
)


@dataclass(frozen=True, slots=True)
class Measurement:
    name: str
    seconds: float
    peak_megabytes: float
    process_peak_megabytes: float = 0.0
    max_gui_stall_ms: float | None = None


@dataclass(frozen=True, slots=True)
class _GuiMeasurementResult:
    max_stall_ms: float


def resolve_profile(
    name: str,
    *,
    evaluations: int | None = None,
    graph_nodes: int | None = None,
    components_per_evaluation: int | None = None,
    root_fields: int | None = None,
) -> BenchmarkProfile:
    base = STRESS_PROFILE if name == "stress" else STANDARD_PROFILE
    profile = BenchmarkProfile(
        evaluations if evaluations is not None else base.evaluations,
        graph_nodes if graph_nodes is not None else base.graph_nodes,
        (
            components_per_evaluation
            if components_per_evaluation is not None
            else base.components_per_evaluation
        ),
        root_fields if root_fields is not None else base.root_fields,
        base.search_queries,
        base.render_timeout,
    )
    if profile.evaluations <= 0:
        message = "evaluations must be greater than zero"
        raise ValueError(message)
    if (
        min(
            profile.graph_nodes,
            profile.components_per_evaluation,
            profile.root_fields,
        )
        < 0
    ):
        message = "graph and payload sizes cannot be negative"
        raise ValueError(message)
    return profile


def measure(
    name: str,
    operation: Callable[[], object],
    *,
    trace_allocations: bool = True,
) -> Measurement:
    if trace_allocations:
        tracemalloc.start()
    started = time.perf_counter()
    result = operation()
    duration = time.perf_counter() - started
    peak = 0
    if trace_allocations:
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    process_peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    gui_stall = result.max_stall_ms if isinstance(result, _GuiMeasurementResult) else None
    return Measurement(
        name,
        duration,
        peak / (1024 * 1024),
        process_peak,
        gui_stall,
    )


def create_collection(
    directory: Path,
    count: int,
    graph_nodes: int,
    *,
    components_per_evaluation: int = 0,
    root_fields: int = 0,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    structure = {
        "children": [
            {"index": index, "name": f"component_{index}"}
            for index in range(graph_nodes)
        ],
        "dependencies": [
            ([] if index == 0 else [index - 1]) for index in range(graph_nodes)
        ],
        "inputs": [
            [
                (
                    {"kind": "parent_input"}
                    if index == 0
                    else {"kind": "child", "index": index - 1}
                )
            ]
            for index in range(graph_nodes)
        ],
        "outputs": (
            [{"kind": "child", "index": graph_nodes - 1}]
            if graph_nodes
            else [{"kind": "parent_input"}]
        ),
    }
    for index in range(count):
        child_count = graph_nodes if index == 0 else components_per_evaluation
        value = {
            "input": {"parameters": [index, index + 1]},
            "output": {"loss": index / max(1, count), "tags": ["synthetic", index]},
            "metrics": {
                f"metric_{field}": ((index + 1) * (field + 3)) % 100_003
                for field in range(root_fields)
            },
            "meta": {
                "chemfit.structure": structure if index == 0 else {},
                "tag": f"evaluation_{index % 128}",
            },
            "children": [
                {
                    "input": {"value": index + child},
                    "output": {
                        "value": index * max(1, child_count) + child,
                        "residual": (index - child) / max(1, child_count),
                    },
                    "meta": {
                        "name": f"component_{child}",
                        "tag": f"tag_{child % 16}",
                        "stage": child % 8,
                    },
                }
                for child in range(child_count)
            ],
        }
        (directory / f"step_{index}_ctx_0.json").write_text(
            json.dumps(value, separators=(",", ":")), encoding="utf-8"
        )
    return directory


def run(  # noqa: PLR0915
    count: int,
    graph_nodes: int,
    *,
    components_per_evaluation: int = 0,
    root_fields: int = 0,
    search_queries: tuple[str, ...] = ("loss",),
    render_timeout: float = 30.0,
    trace_allocations: bool = True,
    on_measurement: Callable[[Measurement], None] | None = None,
) -> list[Measurement]:
    measurements: list[Measurement] = []

    def record(name: str, operation: Callable[[], object]) -> None:
        result = measure(
            name,
            operation,
            trace_allocations=trace_allocations,
        )
        measurements.append(result)
        if on_measurement is not None:
            on_measurement(result)

    with tempfile.TemporaryDirectory(
        prefix="chemfit-dashboard-benchmark-"
    ) as temporary:
        root = Path(temporary) / "collection"
        record(
            "dataset generation",
            lambda: create_collection(
                root,
                count,
                graph_nodes,
                components_per_evaluation=components_per_evaluation,
                root_fields=root_fields,
            ),
        )
        source = JsonFileSource(
            cache_capacity=32, cache_memory_budget=128 * 1024 * 1024
        )
        records = []

        def discover() -> None:
            nonlocal records
            records = source.discover([root])

        record("directory discovery", discover)
        record(
            "context parsing and indexing",
            lambda: [source.load(record) for record in records],
        )
        switch_records = records[: min(32, len(records))]
        source.clear_cache()
        record(
            "context switching (cold)",
            lambda: [source.load(record) for record in switch_records],
        )
        record(
            "context switching (warm)",
            lambda: [source.load(record) for record in switch_records],
        )
        record(
            "collection analysis",
            lambda: analyze_collection(
                (item.record, item.document)
                for item in iter_collection(source, records)
            ),
        )

        record(
            "search-index construction",
            lambda: sum(
                len(ContextSearchIndex(item.document).entries)
                for item in iter_collection(source, records)
                if item.document is not None
            ),
        )

        sample_index = ContextSearchIndex(source.load(records[0]))
        record(
            "search-index querying",
            lambda: [sample_index.search(query) for query in search_queries],
        )
        clear_search_caches()

        def search() -> None:
            results = {query: TopKSearchHits(120) for query in search_queries}
            for item in iter_collection(source, records):
                if item.document is not None:
                    index = ContextSearchIndex(item.document)
                    for query, query_results in results.items():
                        query_results.extend(index.search(query))
            for query_results in results.values():
                query_results.results()

        query_count = len(search_queries)
        query_unit = "query" if query_count == 1 else "queries"
        record(f"collection search ({query_count} {query_unit})", search)
        selectors = (
            FieldSelector((), "fields", ("output", "loss"), "loss"),
            FieldSelector((), "fields", ("input", "parameters"), "parameters"),
            *(
                FieldSelector(
                    (),
                    "fields",
                    ("metrics", f"metric_{field}"),
                    f"metric_{field}",
                )
                for field in range(root_fields)
            ),
        )
        record(
            "multi-field extraction",
            lambda: sum(1 for _row in extract_fields(source, records, selectors)),
        )
        config = ExtractionConfig(selectors)
        record(
            "CSV export",
            lambda: export_csv(Path(temporary) / "data.csv", source, records, config),
        )
        record(
            "JSONL export",
            lambda: export_jsonl(
                Path(temporary) / "data.jsonl", source, records, config
            ),
        )
        document = source.load(records[0])
        flow_nodes = [
            INPUT_NODE,
            *(child_node(index) for index in range(graph_nodes)),
            OUTPUT_NODE,
        ]
        flow_edges = (
            [
                (INPUT_NODE, child_node(0)),
                *(
                    (child_node(index - 1), child_node(index))
                    for index in range(1, graph_nodes)
                ),
                (child_node(graph_nodes - 1), OUTPUT_NODE),
            ]
            if graph_nodes
            else [(INPUT_NODE, OUTPUT_NODE)]
        )
        record(
            "data-flow construction",
            lambda: build_data_flow(document),
        )
        record(
            "data-flow layout",
            lambda: data_flow_layout(flow_nodes, flow_edges),
        )

        application = QApplication.instance() or QApplication([])

        model = EvaluationListModel()
        record("evaluation model build/sort", lambda: model.set_records(records))
        reverse_ids = [record.id for record in reversed(records)]
        record(
            "evaluation model re-sort",
            lambda: model.set_record_order(reverse_ids),
        )

        selector = NumberSelector((), "fields", ("output", "loss"), "loss")
        labels = tuple(record.name for record in records)
        record_ids = tuple(record.id for record in records)
        plot_series = CollectionSeries(
            selector,
            labels,
            tuple(
                SeriesPoint(index, record.name, float(index), record.id)
                for index, record in enumerate(records)
            ),
            record_ids,
        )
        plot_panel = CollectionPlotPanel()
        record("collection plotting", lambda: plot_panel.set_series(plot_series))

        def render() -> _GuiMeasurementResult:
            panel = DataFlowPanel()
            panel.set_document(document)
            expected_nodes = graph_nodes + 2
            finished = False
            heartbeat_times: list[float] = []
            last_heartbeat = time.perf_counter()

            def heartbeat() -> None:
                nonlocal last_heartbeat
                now = time.perf_counter()
                heartbeat_times.append(now - last_heartbeat)
                last_heartbeat = now

            def rendered(_count: int) -> None:
                nonlocal finished
                finished = True

            panel.view.render_finished.connect(rendered)
            timer = QTimer(panel)
            timer.setInterval(10)
            timer.timeout.connect(heartbeat)
            timer.start()
            panel.ensure_rendered()
            deadline = time.monotonic() + render_timeout
            while not finished and time.monotonic() < deadline:
                application.processEvents()
            timer.stop()
            if not finished or panel.view.rendered_node_count != expected_nodes:
                panel.view.cancel_pending_layout()
                message = f"data-flow rendering exceeded {render_timeout:.0f} seconds"
                raise TimeoutError(message)
            tail = time.perf_counter() - last_heartbeat
            maximum_interval = max([tail, *heartbeat_times], default=tail)
            return _GuiMeasurementResult(max(0.0, maximum_interval - 0.010) * 1_000)

        record("data-flow rendering", render)
    return measurements


def main() -> None:
    parser = argparse.ArgumentParser(description="ChemFit Dashboard scaling benchmark")
    parser.add_argument(
        "--profile",
        choices=("standard", "stress"),
        default="standard",
        help="Use the quick standard workload or the intentionally huge stress workload",
    )
    parser.add_argument("--evaluations", type=int)
    parser.add_argument("--graph-nodes", type=int)
    parser.add_argument("--components-per-evaluation", type=int)
    parser.add_argument("--root-fields", type=int)
    parser.add_argument(
        "--tracemalloc",
        action="store_true",
        help="Trace Python allocations (slower; omit for representative timings)",
    )
    args = parser.parse_args()
    try:
        profile = resolve_profile(
            args.profile,
            evaluations=args.evaluations,
            graph_nodes=args.graph_nodes,
            components_per_evaluation=args.components_per_evaluation,
            root_fields=args.root_fields,
        )
    except ValueError as error:
        parser.error(str(error))

    print(
        f"profile={args.profile} evaluations={profile.evaluations:,} "
        f"graph_nodes={profile.graph_nodes:,} "
        f"components_per_evaluation={profile.components_per_evaluation:,} "
        f"root_fields={profile.root_fields:,} "
        f"search_queries={len(profile.search_queries)}",
        flush=True,
    )

    def report(result: Measurement) -> None:
        traced_peak = (
            f"py_peak={result.peak_megabytes:9.2f} MiB"
            if args.tracemalloc
            else "py_peak=      n/a"
        )
        stall = (
            f"  max_gui_stall={result.max_gui_stall_ms:8.1f} ms"
            if result.max_gui_stall_ms is not None
            else ""
        )
        print(
            f"{result.name:30} {result.seconds:9.3f} s  "
            f"{traced_peak}  rss_peak={result.process_peak_megabytes:9.2f} MiB"
            f"{stall}",
            flush=True,
        )

    run(
        profile.evaluations,
        profile.graph_nodes,
        components_per_evaluation=profile.components_per_evaluation,
        root_fields=profile.root_fields,
        search_queries=profile.search_queries,
        render_timeout=profile.render_timeout,
        trace_allocations=args.tracemalloc,
        on_measurement=report,
    )


if __name__ == "__main__":
    main()
