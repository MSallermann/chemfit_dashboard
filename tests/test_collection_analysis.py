from __future__ import annotations

import json

from chemfit_dashboard.collection_analysis import (
    MAX_FAILURE_TREE_ITEMS,
    CollectionAnalysisPanel,
    analyze_collection,
)
from chemfit_dashboard.source import JsonFileSource


def test_collection_summary_groups_primary_failures_and_numeric_fields(tmp_path):
    (tmp_path / "step_1.json").write_text(
        json.dumps({"step": 1, "loss": 2.0, "output": 0.5}),
        encoding="utf-8",
    )
    (tmp_path / "step_2.json").write_text(
        json.dumps(
            {
                "step": 2,
                "loss": 4.0,
                "output": "RuntimeError: optimization exploded",
                "meta": {
                    "chemfit.structure": {
                        "children": [
                            {"index": 0, "name": "broken_model"},
                            {"index": 1, "name": "downstream"},
                            {"index": 2, "name": "never_run"},
                        ]
                    }
                },
                "children": [
                    {"output": "RuntimeError: optimization exploded"},
                    {"output": {"skipped": True}},
                ],
            }
        ),
        encoding="utf-8",
    )
    source = JsonFileSource()
    records = source.discover([tmp_path])

    summary = analyze_collection((record, source.load(record)) for record in records)

    assert summary.total_records == 2
    assert summary.successful_records == 1
    assert summary.failed_records == 1
    assert summary.node_count == 5
    loss = next(field for field in summary.numeric_fields if field.path == "loss")
    assert (loss.count, loss.minimum, loss.maximum, loss.mean) == (2, 2, 4, 3)
    assert len(summary.failure_groups) == 1
    failure = summary.failure_groups[0]
    assert failure.component == "broken_model"
    assert failure.exception_type == "RuntimeError"
    assert failure.message == "optimization exploded"
    assert failure.occurrences[0].node_path == (0,)
    assert summary.skipped_components[0].component == "downstream"
    assert summary.missing_components[0].component == "never_run"


def test_failure_navigator_cycles_through_occurrences(tmp_path):
    for index in range(2):
        (tmp_path / f"step_{index}.json").write_text(
            json.dumps({"output": "ValueError: invalid value"}),
            encoding="utf-8",
        )
    source = JsonFileSource()
    records = source.discover([tmp_path])
    summary = analyze_collection((record, source.load(record)) for record in records)
    panel = CollectionAnalysisPanel()
    selected = []
    panel.failure_selected.connect(selected.append)
    panel.set_summary(summary)

    panel.next_button.click()
    panel.next_button.click()
    panel.previous_button.click()

    assert [item.record_name for item in selected] == [
        "step_0.json",
        "step_1.json",
        "step_0.json",
    ]


def test_example_collection_reports_originating_failure_not_propagated_parent():
    source = JsonFileSource()
    records = source.discover(["examples/context_collection"])

    summary = analyze_collection((record, source.load(record)) for record in records)

    assert summary.failed_records == 1
    assert len(summary.failure_occurrences) == 1
    assert summary.failure_occurrences[0].component == "failing_structure_error"

    panel = CollectionAnalysisPanel()
    panel.set_summary(summary)
    assert not panel.previous_button.isEnabled()
    assert not panel.next_button.isEnabled()


def test_failure_tree_materialization_is_bounded(tmp_path):
    for index in range(MAX_FAILURE_TREE_ITEMS + 20):
        (tmp_path / f"failure_{index}.json").write_text(
            json.dumps({"output": f"ValueError: failure {index}"}),
            encoding="utf-8",
        )
    source = JsonFileSource(cache_capacity=0)
    records = source.discover([tmp_path])
    summary = analyze_collection((record, source.load(record)) for record in records)
    panel = CollectionAnalysisPanel()

    panel.set_summary(summary)

    tree_item_count = panel.failure_tree.topLevelItemCount() + sum(
        panel.failure_tree.topLevelItem(index).childCount()
        for index in range(panel.failure_tree.topLevelItemCount())
    )
    assert len(summary.failure_groups) == MAX_FAILURE_TREE_ITEMS + 20
    assert tree_item_count <= MAX_FAILURE_TREE_ITEMS
