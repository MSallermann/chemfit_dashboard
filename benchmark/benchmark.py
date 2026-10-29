from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
import tracemalloc
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from chemfit_dashboard.collection_analysis import analyze_collection
from chemfit_dashboard.context_search import ContextSearchIndex, TopKSearchHits
from chemfit_dashboard.extraction import (
    ExtractionConfig,
    FieldSelector,
    export_csv,
    export_jsonl,
    extract_fields,
)
from chemfit_dashboard.graph_layout import LayoutNode, dependency_layered_layout
from chemfit_dashboard.graph_view import ContextGraphPanel
from chemfit_dashboard.source import JsonFileSource
from chemfit_dashboard.traversal import iter_collection


@dataclass(frozen=True, slots=True)
class Measurement:
    name: str
    seconds: float
    peak_megabytes: float


def measure(name: str, operation: Callable[[], object]) -> Measurement:
    tracemalloc.start()
    started = time.perf_counter()
    operation()
    duration = time.perf_counter() - started
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return Measurement(name, duration, peak / (1024 * 1024))


def create_collection(directory: Path, count: int, graph_nodes: int) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    structure = {
        "children": [
            {"index": index, "name": f"component_{index}"}
            for index in range(graph_nodes)
        ],
        "dependencies": [
            ([] if index == 0 else [index - 1]) for index in range(graph_nodes)
        ],
    }
    for index in range(count):
        value = {
            "input": {"parameters": [index, index + 1]},
            "output": {"loss": index / max(1, count), "tags": ["synthetic", index]},
            "meta": {"chemfit.structure": structure if index == 0 else {}},
            "children": (
                [{"output": child + index} for child in range(graph_nodes)]
                if index == 0
                else []
            ),
        }
        (directory / f"step_{index}_ctx_0.json").write_text(
            json.dumps(value, separators=(",", ":")), encoding="utf-8"
        )
    return directory


def run(count: int, graph_nodes: int) -> list[Measurement]:
    measurements: list[Measurement] = []
    with tempfile.TemporaryDirectory(
        prefix="chemfit-dashboard-benchmark-"
    ) as temporary:
        root = create_collection(Path(temporary) / "collection", count, graph_nodes)
        source = JsonFileSource(
            cache_capacity=32, cache_memory_budget=128 * 1024 * 1024
        )
        records = []

        def discover() -> None:
            nonlocal records
            records = source.discover([root])

        measurements.append(measure("directory discovery", discover))
        measurements.append(
            measure(
                "context parsing and indexing",
                lambda: [source.load(record) for record in records],
            )
        )
        measurements.append(
            measure(
                "collection analysis",
                lambda: analyze_collection(
                    (item.record, item.document)
                    for item in iter_collection(source, records)
                ),
            )
        )

        def search() -> None:
            results = TopKSearchHits(120)
            for item in iter_collection(source, records):
                if item.document is not None:
                    results.extend(ContextSearchIndex(item.document).search("loss"))
            results.results()

        measurements.append(measure("collection search", search))
        selectors = (
            FieldSelector((), "fields", ("output", "loss"), "loss"),
            FieldSelector((), "fields", ("input", "parameters"), "parameters"),
        )
        measurements.append(
            measure(
                "multi-field extraction",
                lambda: list(extract_fields(source, records, selectors)),
            )
        )
        config = ExtractionConfig(selectors)
        measurements.append(
            measure(
                "CSV export",
                lambda: export_csv(
                    Path(temporary) / "data.csv", source, records, config
                ),
            )
        )
        measurements.append(
            measure(
                "JSONL export",
                lambda: export_jsonl(
                    Path(temporary) / "data.jsonl", source, records, config
                ),
            )
        )
        document = source.load(records[0])
        paths = [node.path for node in document.nodes]
        hierarchy = [(path[:-1], path) for path in paths if path]
        dependencies = [((index - 1,), (index,)) for index in range(1, graph_nodes)]
        measurements.append(
            measure(
                "graph layout",
                lambda: dependency_layered_layout(
                    [LayoutNode(path, path[:-1] if path else None) for path in paths],
                    hierarchy,
                    dependencies,
                ),
            )
        )

        application = QApplication.instance() or QApplication([])

        def render() -> None:
            panel = ContextGraphPanel()
            panel.set_document(document)
            panel.view.expand_all()
            panel.ensure_rendered()
            deadline = time.monotonic() + 30
            while panel.view.rendered_node_count == 0 and time.monotonic() < deadline:
                application.processEvents()

        measurements.append(measure("graph rendering", render))
    return measurements


def main() -> None:
    parser = argparse.ArgumentParser(description="ChemFit Dashboard scaling benchmark")
    parser.add_argument("--evaluations", type=int, default=1_000)
    parser.add_argument("--graph-nodes", type=int, default=2_000)
    args = parser.parse_args()
    print(f"evaluations={args.evaluations:,} graph_nodes={args.graph_nodes:,}")
    for result in run(args.evaluations, args.graph_nodes):
        print(
            f"{result.name:30} {result.seconds:9.3f} s  "
            f"peak={result.peak_megabytes:9.2f} MiB"
        )


if __name__ == "__main__":
    main()
