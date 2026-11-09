from __future__ import annotations

import gc
import json
import weakref
from pathlib import Path

from PySide6.QtWidgets import QLabel

from chemfit_dashboard.context_search import ContextSearchPanel
from chemfit_dashboard.search_core import (
    ContextSearchCache,
    ContextSearchIndex,
    SearchHit,
    TopKSearchHits,
)
from chemfit_dashboard.source import JsonFileSource


def test_search_modes_find_nested_fields(chemfit_document):
    index = ContextSearchIndex(chemfit_document)

    fuzzy = index.search("quantites ouput", include_values=False)
    exact = index.search("OUTput", mode="exact", include_values=False)
    regex = index.search(r"outp.t$", mode="regex", include_values=False)

    assert fuzzy[0].node_path == (0,)
    assert "compute_quantities" in fuzzy[0].breadcrumb
    assert exact
    assert exact[0].field_label == "output"
    assert regex
    assert all(hit.field_label == "output" for hit in regex)


def test_search_dimensions_can_exclude_values_metadata_and_components(
    chemfit_document,
):
    index = ContextSearchIndex(chemfit_document)

    assert index.search("chemfit.structure", mode="exact", include_values=False)
    assert not index.search(
        "chemfit.structure",
        mode="exact",
        include_values=False,
        include_metadata=False,
    )
    assert index.search("compute_quantities", mode="exact", include_values=False)
    assert not index.search(
        "compute_quantities",
        mode="exact",
        include_values=False,
        include_components=False,
    )


def test_value_search_can_be_disabled():
    source = JsonFileSource()
    path = Path("examples/context_collection/step_2_ctx_1.json")
    document = source.load(source.discover([path])[0])
    index = ContextSearchIndex(document)

    assert index.search("intentional failure", include_values=True)
    assert not index.search("intentional failure", include_values=False)


def test_top_k_results_are_bounded_and_deterministic():
    hits = [
        SearchHit((), "root", "complete", "fields", (str(index),), str(index), "", 0.5)
        for index in range(10)
    ]
    collector = TopKSearchHits(3)

    collector.extend(reversed(hits))

    assert len(collector.results()) == 3
    assert [hit.field_label for hit in collector.results()] == ["0", "1", "2"]


def test_search_index_does_not_retain_document(chemfit_json_path):
    source = JsonFileSource(cache_capacity=0)
    document = source.load(source.discover([chemfit_json_path])[0])
    reference = weakref.ref(document)

    index = ContextSearchIndex(document)
    del document
    gc.collect()

    assert reference() is None
    assert index.search("output")


def test_search_cache_reuses_indexes_and_honors_zero_capacity(chemfit_document):
    cache = ContextSearchCache(capacity=1)
    first = cache.index_for(chemfit_document)
    assert cache.index_for(chemfit_document) is first

    uncached = ContextSearchCache(capacity=0)
    assert uncached.index_for(chemfit_document) is not uncached.index_for(
        chemfit_document
    )


def test_search_panel_renders_context_and_copies_canonical_path(coordinator, qapp):
    hit = SearchHit(
        node_path=(1, 0),
        breadcrumb="Evaluation root / optimizer",
        context_status="complete",
        section="fields",
        value_path=("output", 0),
        field_label="output[0]",
        preview="3.5",
        score=0.91,
        record_id="record-1",
        record_name="step_1.json",
    )
    panel = ContextSearchPanel(coordinator=coordinator)

    panel._show_results(  # noqa: SLF001
        [hit],
        "output",
        include_keys=True,
        include_values=True,
        mode="fuzzy",
    )
    item = panel.results.topLevelItem(0)
    label = panel.results.itemWidget(item, 0)
    panel._copy_hit_field_path(hit)  # noqa: SLF001

    assert isinstance(label, QLabel)
    assert "Evaluation root" in label.text()
    assert panel.status.text().startswith("1 matches")
    assert qapp.clipboard().text() == "/1/0 : output[0]"

    panel._copy_hit_json(hit)  # noqa: SLF001
    assert json.loads(qapp.clipboard().text())["record_id"] == "record-1"


def test_search_panel_reports_invalid_regex_without_starting_work(coordinator):
    panel = ContextSearchPanel(coordinator=coordinator)
    panel.query.setText("[unclosed")
    panel.mode.setCurrentIndex(panel.mode.findData("regex"))

    panel._run_search()  # noqa: SLF001

    assert panel.results.topLevelItemCount() == 0
    assert panel.status.text().startswith("Invalid regular expression:")
