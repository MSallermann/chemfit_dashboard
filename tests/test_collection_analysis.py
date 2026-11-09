from __future__ import annotations

import json

from chemfit_dashboard.collection_analysis import (
    CollectionAnalysisPanel,
    analyze_collection,
)
from chemfit_dashboard.source import JsonFileSource


def _analysis_collection(tmp_path):
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
    return source, records


def test_collection_summary_reports_health_failures_and_numeric_fields(tmp_path):
    source, records = _analysis_collection(tmp_path)

    summary = analyze_collection((record, source.load(record)) for record in records)

    assert (
        summary.total_records,
        summary.successful_records,
        summary.failed_records,
    ) == (
        2,
        1,
        1,
    )
    loss = next(field for field in summary.numeric_fields if field.path == "loss")
    assert (loss.count, loss.minimum, loss.maximum, loss.mean) == (2, 2, 4, 3)
    failure = summary.failure_groups[0]
    assert (failure.component, failure.exception_type, failure.message) == (
        "broken_model",
        "RuntimeError",
        "optimization exploded",
    )
    assert summary.skipped_components[0].component == "downstream"
    assert summary.missing_components[0].component == "never_run"


def test_collection_analysis_panel_renders_and_copies_summary(
    tmp_path, coordinator, qapp
):
    source, records = _analysis_collection(tmp_path)
    summary = analyze_collection((record, source.load(record)) for record in records)
    panel = CollectionAnalysisPanel(coordinator=coordinator)
    messages = []
    panel.status_message.connect(messages.append)

    panel.set_summary(summary)
    panel.numeric_table.selectRow(0)
    panel._numeric_copy_shortcut.activated.emit()  # noqa: SLF001

    assert "2 evaluations" in panel.summary_label.text()
    assert "Field,Count,Minimum,Maximum,Mean" in qapp.clipboard().text()
    assert messages[-1] == "Copied collection summary rows as CSV"


def test_collection_analysis_can_be_deferred_and_refreshed(coordinator, monkeypatch):
    panel = CollectionAnalysisPanel(coordinator=coordinator)
    refreshed = []
    monkeypatch.setattr(
        panel,
        "refresh_collection_analysis",
        lambda *, force=False: refreshed.append(force),
    )

    panel.show_deferred(50_000)
    panel.refresh_button.click()

    assert "50,000 evaluations loaded" in panel.summary_label.text()
    assert "deferred" in panel.summary_label.text()
    assert refreshed == [True]
