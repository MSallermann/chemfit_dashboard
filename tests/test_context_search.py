from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QLabel

from chemfit_dashboard.appearance import active_color_scheme
from chemfit_dashboard.context_search import (
    ContextSearchIndex,
    ContextSearchPanel,
    rank_search_hits,
)
from chemfit_dashboard.source import JsonFileSource


def test_fuzzy_search_combines_hierarchy_and_nested_field_name(chemfit_document):
    index = ContextSearchIndex(chemfit_document)

    hits = index.search("quantites ouput", include_values=False)

    assert hits
    assert hits[0].node_path == (0,)
    assert hits[0].field_label == "output"
    assert "compute_quantities" in hits[0].breadcrumb


def test_value_search_can_be_toggled_off():
    source = JsonFileSource()
    path = Path("examples/context_collection/step_2_ctx_1.json")
    document = source.load(source.discover([path])[0])
    index = ContextSearchIndex(document)

    value_hits = index.search("intentional failure", include_values=True)
    key_hits = index.search("intentional failure", include_values=False)

    assert value_hits
    assert all("intentional failure" in hit.preview for hit in value_hits)
    assert key_hits == []


def test_key_search_can_be_toggled_off(chemfit_document):
    index = ContextSearchIndex(chemfit_document)

    key_hits = index.search("output", include_keys=True, include_values=False)
    hierarchy_only_hits = index.search(
        "output", include_keys=False, include_values=False
    )

    assert key_hits
    assert hierarchy_only_hits == []


def test_exact_search_is_literal_and_case_insensitive(chemfit_document):
    index = ContextSearchIndex(chemfit_document)

    exact_hits = index.search("OUTput", mode="exact", include_values=False)
    typo_hits = index.search("ouput", mode="exact", include_values=False)

    assert exact_hits
    assert exact_hits[0].field_label == "output"
    assert typo_hits == []


def test_regex_search_matches_keys(chemfit_document):
    index = ContextSearchIndex(chemfit_document)

    hits = index.search(r"outp.t$", mode="regex", include_values=False)

    assert hits
    assert all(hit.field_label == "output" for hit in hits)


def test_search_panel_ranks_repeated_fields_with_context_labels(chemfit_document):
    panel = ContextSearchPanel()
    panel.set_index(ContextSearchIndex(chemfit_document))

    panel.search("output", include_values=False)

    assert panel.results.topLevelItemCount() >= 2
    first_hit = panel.results.topLevelItem(0)
    first_label = panel.results.itemWidget(first_hit, 0)
    assert first_hit.childCount() == 0
    assert isinstance(first_label, QLabel)
    assert "Evaluation root" in first_label.text()
    assert ">output</span>" in first_label.text()
    assert first_hit.text(2) == "100.0%"
    assert "best match first" in panel.status.text()


def test_search_panel_shows_and_highlights_matching_section(chemfit_document):
    panel = ContextSearchPanel()
    panel.set_index(ContextSearchIndex(chemfit_document))

    panel.search("metadata", include_values=False)

    first_hit = panel.results.topLevelItem(0)
    label = panel.results.itemWidget(first_hit, 0)
    assert isinstance(label, QLabel)
    assert ">Metadata</span>" in label.text()
    assert label.text().index("chemfit.structure") < label.text().index(
        "Evaluation root"
    )


def test_search_panel_highlights_fuzzy_match_in_red_and_bold(chemfit_document):
    panel = ContextSearchPanel()
    panel.set_index(ContextSearchIndex(chemfit_document))

    panel.search("ouput", include_values=False)

    matched_label = None
    for result_index in range(panel.results.topLevelItemCount()):
        item = panel.results.topLevelItem(result_index)
        label = panel.results.itemWidget(item, 0)
        if isinstance(label, QLabel) and ">output</span>" in label.text():
            matched_label = label
            break
    assert isinstance(matched_label, QLabel)
    assert active_color_scheme().danger in matched_label.text()
    assert "font-weight: 700" in matched_label.text()
    assert ">output</span>" in matched_label.text()


def test_selected_search_result_does_not_repaint_plain_text(chemfit_document):
    panel = ContextSearchPanel()
    panel.set_index(ContextSearchIndex(chemfit_document))
    panel.search("output", include_values=False)
    first_hit = panel.results.topLevelItem(0)

    panel.results.setCurrentItem(first_hit)

    assert first_hit.text(0) == ""
    assert first_hit.text(1) == ""
    assert isinstance(panel.results.itemWidget(first_hit, 0), QLabel)


def test_fuzzy_results_are_sorted_by_descending_score(chemfit_document):
    hits = ContextSearchIndex(chemfit_document).search("outpt")

    assert hits
    scores = [hit.score for hit in hits]
    assert scores == sorted(scores, reverse=True)


def test_search_panel_reports_invalid_regex(chemfit_document):
    panel = ContextSearchPanel()
    panel.set_index(ContextSearchIndex(chemfit_document))

    panel.search("[unclosed", mode="regex")

    assert panel.results.topLevelItemCount() == 0
    assert panel.status.text().startswith("Invalid regular expression:")


def test_collection_hits_are_globally_ranked_and_show_filename(tmp_path):
    first_path = tmp_path / "step_2.json"
    second_path = tmp_path / "step_10.json"
    first_path.write_text('{"output": 2}', encoding="utf-8")
    second_path.write_text('{"output": 10}', encoding="utf-8")
    source = JsonFileSource()
    records = source.discover([tmp_path])
    hits = rank_search_hits(
        [
            hit
            for record in records
            for hit in ContextSearchIndex(source.load(record)).search(
                "output", include_values=False
            )
        ]
    )

    assert {hit.record_name for hit in hits} == {"step_2.json", "step_10.json"}
    assert [hit.score for hit in hits] == sorted(
        (hit.score for hit in hits), reverse=True
    )

    panel = ContextSearchPanel()
    panel.show_results(
        hits,
        "output",
        include_keys=True,
        include_values=False,
        mode="fuzzy",
        scope_label="whole collection",
        searched_records=2,
    )
    first_label = panel.results.itemWidget(panel.results.topLevelItem(0), 0)
    assert isinstance(first_label, QLabel)
    assert "step_" in first_label.text()
    assert "files" in panel.status.text()
