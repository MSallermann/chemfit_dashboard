from __future__ import annotations

import json
from contextlib import suppress

from chemfit import EvaluateContext, component, pipeline
from chemfit.fitter_callbacks import SaveMetaData
from PySide6.QtCore import QModelIndex, Qt
from PySide6.QtWidgets import QGraphicsSimpleTextItem

from chemfit_dashboard.appearance import (
    COLOR_SCHEME_SETTING,
    FONT_SCALE_SETTING,
    SETTINGS_FILENAME,
    application_settings,
    color_scheme,
)
from chemfit_dashboard.graph_view import ContextGraphPanel
from chemfit_dashboard.main_window import MainWindow
from chemfit_dashboard.models import (
    ContextTreeModel,
    EvaluationListModel,
    JsonValueModel,
)
from chemfit_dashboard.source import JsonFileSource


def test_evaluation_model_uses_natural_filename_order(tmp_path):
    for name in ("step_10.json", "step_1.json", "step_2.json"):
        (tmp_path / name).write_text("{}", encoding="utf-8")
    records = JsonFileSource().discover([tmp_path])
    model = EvaluationListModel()

    model.set_records(list(reversed(records)))

    root = model.index(0, 0)
    visible_names = [
        model.data(model.index(row, 0, root)) for row in range(model.rowCount(root))
    ]
    assert visible_names == ["step_1.json", "step_2.json", "step_10.json"]

    record_by_name = {record.name: record for record in records}
    model.set_record_order(
        [
            record_by_name["step_10.json"].id,
            record_by_name["step_1.json"].id,
            record_by_name["step_2.json"].id,
        ]
    )
    assert [record.name for record in model.records] == [
        "step_10.json",
        "step_1.json",
        "step_2.json",
    ]

    model.set_record_order(None)
    assert [record.name for record in model.records] == [
        "step_1.json",
        "step_2.json",
        "step_10.json",
    ]


def test_evaluation_model_groups_chemfit_contexts_by_folder(
    tmp_path, chemfit_context, chemfit_json_path
):
    nested = tmp_path / "nested"
    SaveMetaData(nested)(9, [chemfit_context])
    records = JsonFileSource().discover([tmp_path])
    assert chemfit_json_path in {record.path for record in records}
    model = EvaluationListModel()
    model.set_records(records)

    root = model.index(0, 0)
    assert model.data(root) == tmp_path.name
    assert model.rowCount(root) == 2

    folder = model.index(0, 0, root)
    assert model.data(folder) == "nested"
    assert model.record(folder) is None

    nested_context = model.index(0, 0, folder)
    assert model.record(nested_context).path == nested / "step_9_ctx_0.json"


def test_context_tree_model_fetches_chemfit_children_only_on_demand(
    chemfit_document,
):
    model = ContextTreeModel()
    model.set_document(chemfit_document)

    root = model.index(0, 0, QModelIndex())
    assert model.rowCount(root) == 0
    assert model.canFetchMore(root)

    model.fetchMore(root)

    assert model.rowCount(root) == 2
    first = model.index(0, 0, root)
    assert model.data(first, Qt.ItemDataRole.DisplayRole) == "compute_quantities"
    assert model.parent(first) == root


def test_graph_view_renders_chemfit_context_hierarchy(chemfit_document):
    graph = ContextGraphPanel()
    graph.set_document(chemfit_document)

    assert graph.view.rendered_node_count == 0
    graph.ensure_rendered()

    assert graph.view.rendered_node_count == chemfit_document.node_count


def test_graph_applies_selection_requested_before_deferred_render(chemfit_document):
    graph = ContextGraphPanel()
    graph.set_document(chemfit_document)

    graph.select_path((0,))
    graph.ensure_rendered()

    selected_paths = [item.data(0) for item in graph.view.scene().selectedItems()]
    assert selected_paths == [(0,)]


def test_graph_node_shows_static_name_beside_structural_name(tmp_path):
    path = tmp_path / "named.json"
    path.write_text(
        json.dumps(
            {
                "meta": {
                    "chemfit.structure": {
                        "children": [{"index": 0, "name": "WrappedComponent"}]
                    }
                },
                "children": [{"output": 1, "meta": {"name": "density observable"}}],
            }
        ),
        encoding="utf-8",
    )
    source = JsonFileSource()
    document = source.load(source.discover([path])[0])
    graph = ContextGraphPanel()
    graph.set_document(document)
    graph.ensure_rendered()

    labels = {
        item.text()
        for item in graph.view.scene().items()
        if isinstance(item, QGraphicsSimpleTextItem)
    }
    assert "WrappedComponent" in labels
    assert "name: density observable" in labels


def test_active_graph_rebuilds_when_evaluation_changes(tmp_path, chemfit_document):
    partial_path = tmp_path / "partial.json"
    partial_path.write_text(json.dumps({"output": 1}), encoding="utf-8")
    source = JsonFileSource()
    partial_document = source.load(source.discover([partial_path])[0])
    window = MainWindow(source=source)
    window.hierarchy_tabs.setCurrentWidget(window.graph)

    window._load_generation = 1  # noqa: SLF001
    window._document_loaded(1, chemfit_document)  # noqa: SLF001
    assert window.graph.view.document is chemfit_document
    assert window.graph.view.rendered_node_count == chemfit_document.node_count

    window._load_generation = 2  # noqa: SLF001
    window._document_loaded(2, partial_document)  # noqa: SLF001
    assert window.graph.view.document is partial_document
    assert window.graph.view.rendered_node_count == partial_document.node_count == 1


def test_child_selection_follows_component_identity_across_evaluations(
    tmp_path, monkeypatch
):
    contexts = {
        "step_1.json": {
            "children": [
                {"output": 1, "meta": {"component_name": "upstream"}},
                {
                    "output": 2,
                    "meta": {"component_name": "target", "stage": "initial"},
                },
            ]
        },
        "step_2.json": {
            "children": [{"output": 3, "meta": {"component_name": "unrelated"}}]
        },
        "step_3.json": {
            "children": [
                {
                    "output": 4,
                    "meta": {"component_name": "target", "stage": "updated"},
                }
            ]
        },
    }
    for name, value in contexts.items():
        (tmp_path / name).write_text(json.dumps(value), encoding="utf-8")
    source = JsonFileSource()
    records = source.discover([tmp_path])
    window = MainWindow(source=source)
    monkeypatch.setattr(
        window.tasks,
        "submit",
        lambda function, on_success, _on_error: on_success(function()),
    )
    window._records_discovered(records)  # noqa: SLF001

    target_index = window.tree_model.index_for_path((1,))
    window.tree.setCurrentIndex(target_index)
    assert window.inspector.title.text() == "target"

    assert window._select_record_by_id(records[1].id)  # noqa: SLF001
    assert window.tree_model.node_info(window.tree.currentIndex()).path == ()

    assert window._select_record_by_id(records[2].id)  # noqa: SLF001
    selected = window.tree_model.node_info(window.tree.currentIndex())
    assert selected is not None
    assert selected.path == (0,)
    assert selected.label == "target"
    root_index = window.tree_model.index_for_path(())
    assert window.tree.isExpanded(root_index)
    assert "restored target" in window.statusBar().currentMessage()


def _write_failure_and_skip_collection(tmp_path):
    @component()
    def gate(value):
        if value < 0:
            message = "deliberate failure"
            raise ValueError(message)
        return value

    @component()
    def stage(value):
        return value + 1

    @component()
    def leaf(value):
        return value * 2

    @component()
    def final(value):
        return value + 3

    nested = pipeline(leaf, final)
    middle = pipeline(stage, nested)
    objective = pipeline(gate, middle)
    for step, value in enumerate((1, -1)):
        context = EvaluateContext()
        with suppress(ValueError):
            objective(value, context)
        SaveMetaData(tmp_path)(step, [context])

    source = JsonFileSource()
    return source, source.discover([tmp_path])


def test_chemfit_skip_serialization_retains_only_immediate_declarations(tmp_path):
    source, records = _write_failure_and_skip_collection(tmp_path)
    successful_document = source.load(records[0])
    failed_document = source.load(records[1])

    assert failed_document.details((1,)).fields["input"] is None
    assert failed_document.details((1,)).fields["output"] is None
    assert [child.path for child in failed_document.children((1,))] == [
        (1, 0),
        (1, 1),
    ]
    assert not failed_document.details((1, 1)).info.recorded
    assert failed_document.children((1, 1)) == ()
    assert [child.path for child in successful_document.children((1, 1))] == [
        (1, 1, 0),
        (1, 1, 1),
    ]


def test_selection_uses_chemfit_declarations_across_failure_and_skip(
    tmp_path, monkeypatch
):
    source, records = _write_failure_and_skip_collection(tmp_path)
    window = MainWindow(source=source)
    monkeypatch.setattr(
        window.tasks,
        "submit",
        lambda function, on_success, _on_error: on_success(function()),
    )
    window._records_discovered(records)  # noqa: SLF001

    gate_index = window.tree_model.index_for_path((0,))
    window.tree.setCurrentIndex(gate_index)
    assert window.inspector.title.text() == "gate"

    assert window._select_record_by_id(records[1].id)  # noqa: SLF001
    selected = window.tree_model.node_info(window.tree.currentIndex())
    assert selected is not None
    assert selected.path == (0,)
    assert selected.label == "gate"
    assert selected.status == "failed"

    assert window._select_record_by_id(records[0].id)  # noqa: SLF001
    deep_path = (1, 1, 1)
    window.tree.setCurrentIndex(window.tree_model.index_for_path(deep_path))
    assert window.inspector.title.text() == "final"

    assert window._select_record_by_id(records[1].id)  # noqa: SLF001
    selected = window.tree_model.node_info(window.tree.currentIndex())
    assert selected is not None
    assert selected.path == (1, 1)
    assert selected.label == "PipelineComponent"
    assert selected.status == "not recorded"
    assert "final is unavailable" in window.statusBar().currentMessage()
    assert "selection retained" in window.statusBar().currentMessage()

    assert window._select_record_by_id(records[0].id)  # noqa: SLF001
    selected = window.tree_model.node_info(window.tree.currentIndex())
    assert selected is not None
    assert selected.path == deep_path
    assert selected.label == "final"


def test_selection_does_not_conflate_different_declared_components(
    tmp_path, monkeypatch
):
    for step, component_name in enumerate(("original", "replacement")):
        value = {
            "meta": {
                "chemfit.structure": {
                    "children": [{"index": 0, "name": component_name}]
                }
            },
            "children": [{"output": step}],
        }
        (tmp_path / f"step_{step}.json").write_text(json.dumps(value), encoding="utf-8")

    source = JsonFileSource()
    records = source.discover([tmp_path])
    window = MainWindow(source=source)
    monkeypatch.setattr(
        window.tasks,
        "submit",
        lambda function, on_success, _on_error: on_success(function()),
    )
    window._records_discovered(records)  # noqa: SLF001
    window.tree.setCurrentIndex(window.tree_model.index_for_path((0,)))

    assert window._select_record_by_id(records[1].id)  # noqa: SLF001
    selected = window.tree_model.node_info(window.tree.currentIndex())
    assert selected is not None
    assert selected.path == ()
    assert "original is unavailable" in window.statusBar().currentMessage()

    assert window._select_record_by_id(records[0].id)  # noqa: SLF001
    selected = window.tree_model.node_info(window.tree.currentIndex())
    assert selected is not None
    assert selected.path == (0,)
    assert selected.label == "original"


def test_collection_overview_is_a_top_level_workspace():
    window = MainWindow()

    hierarchy_widgets = [
        window.hierarchy_tabs.widget(index)
        for index in range(window.hierarchy_tabs.count())
    ]
    assert window.collection_analysis not in hierarchy_widgets
    assert window.workspace_tabs.indexOf(window.collection_analysis) >= 0


def test_header_controls_scale_application_fonts(qapp, tmp_path):
    settings_directory = tmp_path / "font-settings"
    settings = application_settings(settings_directory)
    window = MainWindow(settings=settings)
    initial_size = qapp.font().pointSizeF()

    window.font_larger_button.click()

    assert window._font_scale == 1.1  # noqa: SLF001
    assert qapp.font().pointSizeF() > initial_size
    assert float(settings.value(FONT_SCALE_SETTING)) == 1.1

    restored = MainWindow(settings=application_settings(settings_directory))
    assert restored._font_scale == 1.1  # noqa: SLF001

    window.font_smaller_button.click()
    assert window._font_scale == 1.0  # noqa: SLF001


def test_color_scheme_is_persisted_in_portable_settings_directory(tmp_path, qapp):
    config_directory = tmp_path / "portable-config"
    settings = application_settings(config_directory)
    window = MainWindow(settings=settings)

    window.color_scheme.setCurrentIndex(window.color_scheme.findData("light"))

    light = color_scheme("light")
    assert settings.value(COLOR_SCHEME_SETTING) == "light"
    assert settings.fileName() == str(config_directory / SETTINGS_FILENAME)
    assert light.window in qapp.styleSheet()
    assert window.graph.view.backgroundBrush().color().name() == light.graph
    assert (
        window.collection_plot.chart_view.chart().backgroundBrush().color().name()
        == light.chart
    )

    restored = MainWindow(settings=application_settings(config_directory))
    assert restored.color_scheme.currentData() == "light"


def test_json_value_model_expands_nested_values_lazily():
    model = JsonValueModel({"parameters": {"epsilon": 0.2}, "loss": 0.01})

    assert model.rowCount() == 2
    parameters = model.index(0, 0)
    assert model.rowCount(parameters) == 0
    assert model.canFetchMore(parameters)

    model.fetchMore(parameters)

    assert model.rowCount(parameters) == 1
    epsilon = model.index(0, 1, parameters)
    assert model.data(epsilon) == "0.2"
