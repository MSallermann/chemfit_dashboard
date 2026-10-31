from __future__ import annotations

import json
from contextlib import suppress

from chemfit import EvaluateContext, component, pipeline
from chemfit.fitter_callbacks import SaveMetaData
from PySide6.QtCore import QMimeData, QModelIndex, QPoint, Qt, QUrl
from PySide6.QtWidgets import QHeaderView, QLabel, QPushButton

from chemfit_dashboard import main_window as main_window_module
from chemfit_dashboard.appearance import (
    COLOR_SCHEME_SETTING,
    COMPONENT_LABEL_METADATA_KEY_SETTING,
    DENSITY_SETTING,
    FONT_SCALE_SETTING,
    SETTINGS_FILENAME,
    STYLE_PROFILE_SETTING,
    application_settings,
    color_scheme,
)
from chemfit_dashboard.context_search import ContextSearchIndex
from chemfit_dashboard.extraction import FieldSelector
from chemfit_dashboard.graph_view import ContextGraphPanel
from chemfit_dashboard.main_window import MainWindow
from chemfit_dashboard.models import (
    ContextTreeModel,
    EvaluationListModel,
    JsonValueModel,
)
from chemfit_dashboard.source import JsonFileSource
from chemfit_dashboard.workers import TaskRunner


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


def test_file_menu_closes_loaded_collection_without_quitting(tmp_path, monkeypatch):
    context_path = tmp_path / "step_1.json"
    context_path.write_text('{"output": 4}', encoding="utf-8")
    source = JsonFileSource()
    window = MainWindow(source=source)
    monkeypatch.setattr(
        window.tasks,
        "submit",
        lambda function, on_success, _on_error: on_success(function()),
    )

    assert not window.close_collection_action.isEnabled()
    assert not window.close_collection_action.icon().isNull()

    window._records_discovered(source.discover([context_path]))  # noqa: SLF001
    assert window.close_collection_action.isEnabled()
    assert window._current_document is not None  # noqa: SLF001
    collection_generation = window._collection_generation  # noqa: SLF001

    window.close_collection_action.trigger()

    assert window.file_model.records == ()
    assert window._current_document is None  # noqa: SLF001
    assert window.tree_model.document is None
    assert window.graph.view.document is None
    assert window.file_count.text() == "No files loaded"
    assert window.inspector.title.text() == "Context details"
    assert not window.close_collection_action.isEnabled()
    assert "Closed" in window.statusBar().currentMessage()

    # A scan that was already running when Close was chosen cannot reopen it.
    window._records_discovered(  # noqa: SLF001
        source.discover([context_path]), collection_generation
    )
    assert window.file_model.records == ()


def test_dashboard_accepts_dropped_json_files_and_directories(tmp_path):
    contexts = tmp_path / "contexts"
    contexts.mkdir()
    context = tmp_path / "step_1.json"
    context.write_text("{}", encoding="utf-8")
    ignored = tmp_path / "notes.txt"
    ignored.write_text("not a context", encoding="utf-8")
    mime_data = QMimeData()
    mime_data.setUrls(
        [
            QUrl.fromLocalFile(str(contexts)),
            QUrl.fromLocalFile(str(context)),
            QUrl.fromLocalFile(str(ignored)),
        ]
    )
    window = MainWindow()

    assert window.acceptDrops()
    assert window._drop_locations(mime_data) == [  # noqa: SLF001
        str(contexts.resolve()),
        str(context.resolve()),
    ]


def test_dashboard_accepts_remote_thunar_urls():
    mime_data = QMimeData()
    mime_data.setUrls([QUrl("smb://server/share/step_1.json")])

    assert MainWindow._drop_locations(mime_data) == [  # noqa: SLF001
        "smb://server/share/step_1.json"
    ]


def test_deleting_last_search_character_stays_in_search_workspace():
    window = MainWindow()
    window.workspace_tabs.setCurrentWidget(window.collection_analysis)
    window.find_action.trigger()
    window.field_search.setText("x")

    window.field_search.setText("")

    assert window.workspace_tabs.currentWidget() is window.search_workspace
    assert window.search_scope.currentData() == "collection"


def test_status_footer_expands_to_show_concurrent_operations(qapp):
    window = MainWindow()
    window.show()
    window._begin_operation("search", "searching collection")  # noqa: SLF001
    window.operation_progress.emit("search", 4, 10)
    window._begin_operation("plot", "loading plot values")  # noqa: SLF001
    qapp.processEvents()

    assert window.activity_toggle.isVisible()
    assert "(+1)" in window.activity_status.text()
    window.activity_toggle.setChecked(True)
    qapp.processEvents()
    assert window.activity_panel.isVisible()
    assert window.activity_panel_layout.count() == 3
    search_row = window._activity_rows["search"][0]  # noqa: SLF001
    window.operation_progress.emit("search", 5, 10)
    qapp.processEvents()
    assert window.activity_panel.isVisible()
    assert window._activity_rows["search"][0] is search_row  # noqa: SLF001

    window._finish_operation("plot")  # noqa: SLF001
    assert window._operation_progress["search"] == (5, 10)  # noqa: SLF001

    window._cancel_operation("search")  # noqa: SLF001
    assert window.activity_toggle.isChecked()
    assert not window.activity_panel.isVisible()
    window._begin_operation("search", "searching collection")  # noqa: SLF001
    qapp.processEvents()
    assert window.activity_toggle.isChecked()
    assert window.activity_panel.isVisible()


def test_delayed_activity_avoids_short_operation_footer_flash():
    window = MainWindow()

    quick_token = window._begin_operation(  # noqa: SLF001
        "document", "loading quick.json", display_delay_ms=200
    )
    assert "document" not in window._operation_labels  # noqa: SLF001
    window._finish_operation("document")  # noqa: SLF001
    window._show_delayed_operation(  # noqa: SLF001
        "document", "loading quick.json", quick_token
    )
    assert "document" not in window._operation_labels  # noqa: SLF001

    slow_token = window._begin_operation(  # noqa: SLF001
        "document", "loading slow.json", display_delay_ms=200
    )
    window._show_delayed_operation(  # noqa: SLF001
        "document", "loading slow.json", slow_token
    )
    assert window._operation_labels["document"] == "loading slow.json"  # noqa: SLF001
    window._finish_operation("document")  # noqa: SLF001


def test_copy_feedback_appears_beside_cursor(monkeypatch, qapp):
    cursor_position = [QPoint(100, 200)]

    class CursorStub:
        @staticmethod
        def pos() -> QPoint:
            return cursor_position[0]

    monkeypatch.setattr(main_window_module, "QCursor", CursorStub)
    window = MainWindow()
    expected_position = window.mapFromGlobal(QPoint(100, 200)) + QPoint(14, 18)

    window._clipboard_copied("Copied field path")  # noqa: SLF001
    assert window._clipboard_toast_show_timer.isActive()  # noqa: SLF001
    qapp.processEvents()

    toast = window._clipboard_toast  # noqa: SLF001
    assert toast.text() == "Copied field path"
    assert toast.pos() == expected_position
    assert not toast.isHidden()
    assert window._clipboard_toast_follow_timer.isActive()  # noqa: SLF001
    assert window._clipboard_toast_timer.isActive()  # noqa: SLF001
    assert window._clipboard_toast_timer.interval() == 500  # noqa: SLF001

    cursor_position[0] = QPoint(180, 260)
    window._position_clipboard_toast()  # noqa: SLF001
    assert toast.pos() == window.mapFromGlobal(QPoint(180, 260)) + QPoint(14, 18)

    window._hide_clipboard_toast()  # noqa: SLF001
    assert toast.isHidden()
    assert not window._clipboard_toast_follow_timer.isActive()  # noqa: SLF001


def test_session_restores_collection_layout_and_recent_location(
    tmp_path, monkeypatch, qapp
):
    contexts = tmp_path / "contexts"
    contexts.mkdir()
    (contexts / "step_1.json").write_text('{"output": 1}', encoding="utf-8")
    (contexts / "step_2.json").write_text('{"output": 2}', encoding="utf-8")
    settings_directory = tmp_path / "settings"

    monkeypatch.setattr(
        TaskRunner,
        "submit",
        lambda _self, function, on_success, _on_error: on_success(function()),
    )
    first = MainWindow(
        source=JsonFileSource(),
        settings=application_settings(settings_directory),
        restore_session=True,
    )
    first.add_locations([contexts])
    second_record = first.file_model.records[1]
    assert first._select_record_by_id(second_record.id)  # noqa: SLF001
    first.back_action.trigger()
    assert first._current_document is not None  # noqa: SLF001
    assert first._current_document.record.name == "step_1.json"  # noqa: SLF001
    first.forward_action.trigger()
    assert first._current_document is not None  # noqa: SLF001
    assert first._current_document.record.name == "step_2.json"  # noqa: SLF001
    first.hierarchy_tabs.setCurrentWidget(first.collection_plot)
    first.workspace_tabs.setCurrentWidget(first.search_workspace)
    first.main_workspace.setSizes([320, 880])
    first.close()
    qapp.processEvents()

    restored = MainWindow(
        source=JsonFileSource(),
        settings=application_settings(settings_directory),
        restore_session=True,
    )

    assert len(restored.file_model.records) == 2
    assert restored._current_document is not None  # noqa: SLF001
    assert restored._current_document.record.name == "step_2.json"  # noqa: SLF001
    assert restored.hierarchy_tabs.currentWidget() is restored.collection_plot
    assert restored.workspace_tabs.currentWidget() is restored.search_workspace
    assert restored.recent_menu.actions()[0].toolTip() == str(contexts.resolve())

    explicit = tmp_path / "explicit.json"
    explicit.write_text('{"output": 3}', encoding="utf-8")
    overridden = MainWindow(
        source=JsonFileSource(),
        initial_paths=[explicit],
        settings=application_settings(settings_directory),
        restore_session=True,
    )
    assert [record.name for record in overridden.file_model.records] == [
        "explicit.json"
    ]
    assert overridden._session_locations == [str(explicit.resolve())]  # noqa: SLF001


def test_find_shortcut_and_navigation_history(chemfit_document):
    window = MainWindow(source=JsonFileSource())
    window._load_generation = 1  # noqa: SLF001
    window._document_loaded(1, chemfit_document)  # noqa: SLF001
    window.hierarchy_tabs.setCurrentWidget(window.tree)
    window.workspace_tabs.setCurrentWidget(window.collection_analysis)

    window.find_action.trigger()
    assert window.workspace_tabs.currentWidget() is window.search_workspace
    window.field_search.setText("output")
    window.clear_search_action.trigger()
    assert window.field_search.text() == ""
    assert window.workspace_tabs.currentWidget() is window.collection_analysis
    assert window.hierarchy_tabs.currentWidget() is window.tree

    assert window._select_context_path((0,), remember=True)  # noqa: SLF001
    assert window._select_context_path((1,), remember=True)  # noqa: SLF001
    assert not window.back_button.icon().isNull()
    assert not window.forward_button.icon().isNull()
    assert window.back_button.isEnabled()
    window.back_button.click()
    selected = window.tree_model.node_info(window.tree.currentIndex())
    assert selected is not None
    assert selected.path == (0,)
    assert window.forward_action.isEnabled()
    assert window.forward_button.isEnabled()
    window.forward_button.click()
    selected = window.tree_model.node_info(window.tree.currentIndex())
    assert selected is not None
    assert selected.path == (1,)


def test_tree_view_ctrl_c_copies_selected_component(chemfit_document, qapp):
    window = MainWindow(source=JsonFileSource())
    window._load_generation = 1  # noqa: SLF001
    window._document_loaded(1, chemfit_document)  # noqa: SLF001
    index = window.tree_model.index_for_path((0,))
    window.tree.setCurrentIndex(index)

    window._tree_copy_shortcut.activated.emit()  # noqa: SLF001

    assert qapp.clipboard().text() == chemfit_document.component_label((0,))


def test_collection_has_separate_ascending_and_descending_sort_buttons(tmp_path):
    for name in ("step_10.json", "step_1.json", "step_2.json"):
        (tmp_path / name).write_text("{}", encoding="utf-8")
    source = JsonFileSource()
    window = MainWindow(source=source)
    window.file_model.set_records(source.discover([tmp_path]))
    window.ascending_order_button.setEnabled(True)
    window.descending_order_button.setEnabled(True)

    window.descending_order_button.click()
    assert [record.name for record in window.file_model.records] == [
        "step_10.json",
        "step_2.json",
        "step_1.json",
    ]

    window.ascending_order_button.click()
    assert [record.name for record in window.file_model.records] == [
        "step_1.json",
        "step_2.json",
        "step_10.json",
    ]


def test_folder_discovery_is_non_recursive_and_explicit_files_can_be_added(
    tmp_path, chemfit_context, chemfit_json_path
):
    nested = tmp_path / "nested"
    SaveMetaData(nested)(9, [chemfit_context])
    source = JsonFileSource()
    records = source.discover([tmp_path])
    assert chemfit_json_path in {record.path for record in records}
    assert not any(record.path.parent == nested for record in records)

    records = source.discover([tmp_path, nested / "step_9_ctx_0.json"])
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


def test_large_context_and_json_models_materialize_in_bounded_batches(tmp_path):
    path = tmp_path / "large.json"
    path.write_text(
        json.dumps({"children": [{"output": index} for index in range(1_000)]}),
        encoding="utf-8",
    )
    source = JsonFileSource()
    document = source.load(source.discover([path])[0])
    context_model = ContextTreeModel()
    context_model.set_document(document)
    root = context_model.index(0, 0)

    context_model.fetchMore(root)

    assert context_model.rowCount(root) == context_model.FETCH_BATCH_SIZE
    assert context_model.canFetchMore(root)

    json_model = JsonValueModel(list(range(10_000)))
    assert json_model.rowCount() == json_model.FETCH_BATCH_SIZE
    assert json_model.canFetchMore(QModelIndex())


def test_data_flow_view_renders_one_structural_level(chemfit_document):
    graph = ContextGraphPanel()
    graph.set_document(chemfit_document)

    assert graph.view.rendered_node_count == 0
    graph.ensure_rendered()

    assert graph.view.rendered_node_count == len(chemfit_document.children(())) + 2


def test_data_flow_can_expand_into_window_and_collapse_back(qapp):
    window = MainWindow()
    window.show()
    qapp.processEvents()
    window.hierarchy_tabs.setCurrentWidget(window.graph)
    tab_count = window.hierarchy_tabs.count()
    window.graph.view.scale(1.2, 1.2)
    zoom = window.graph.view.transform().m11()

    window.graph.expand_button.click()
    qapp.processEvents()

    dialog = window._expanded_data_flow_dialog  # noqa: SLF001
    assert dialog is not None
    assert window.hierarchy_tabs.indexOf(window.graph) == -1
    placeholder = window._data_flow_placeholder  # noqa: SLF001
    assert placeholder is not None
    placeholder_index = window.hierarchy_tabs.indexOf(placeholder)
    assert placeholder_index == 1
    assert window.hierarchy_tabs.count() == tab_count
    assert window.hierarchy_tabs.tabText(placeholder_index) == "Data Flow"
    message = placeholder.findChild(QLabel, "dataFlowDetachedMessage")
    assert message is not None
    assert "detached window" in message.text()
    assert window.graph.expand_button.text() == "Collapse"
    assert window.graph.view.transform().m11() == zoom
    assert window.graph.isVisibleTo(dialog)
    assert window.graph.view.isVisibleTo(dialog)
    for control in (
        window.graph.back_button,
        window.graph.fit_button,
        window.graph.zoom_out_button,
        window.graph.zoom_in_button,
        window.graph.expand_button,
    ):
        assert dialog.isAncestorOf(control)

    return_button = placeholder.findChild(QPushButton, "returnDataFlowButton")
    assert return_button is not None
    return_button.click()
    qapp.processEvents()

    assert window._expanded_data_flow_dialog is None  # noqa: SLF001
    assert window.hierarchy_tabs.indexOf(window.graph) == 1
    assert window.hierarchy_tabs.currentWidget() is window.graph
    assert window.graph.expand_button.text() == "Expand"
    assert window.graph.view.transform().m11() == zoom

    window.graph.expand_button.click()
    qapp.processEvents()
    window.graph.expand_button.click()
    qapp.processEvents()

    assert window._expanded_data_flow_dialog is None  # noqa: SLF001
    assert window.hierarchy_tabs.indexOf(window.graph) == 1

    window.graph.expand_button.click()
    dialog = window._expanded_data_flow_dialog  # noqa: SLF001
    assert dialog is not None
    dialog.reject()
    qapp.processEvents()

    assert window._expanded_data_flow_dialog is None  # noqa: SLF001
    assert window.hierarchy_tabs.indexOf(window.graph) == 1
    window.close()


def test_graph_applies_selection_requested_before_deferred_render(chemfit_document):
    graph = ContextGraphPanel()
    graph.set_document(chemfit_document)

    graph.select_path((0,))
    graph.ensure_rendered()

    selected_paths = [item.data(0) for item in graph.view.scene().selectedItems()]
    assert selected_paths == [(0,)]


def test_data_flow_node_shows_custom_name_and_component_type(tmp_path):
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
        item.node.label for item in graph.view.scene().items() if hasattr(item, "node")
    }
    assert "density observable" in labels
    assert any(
        "WrappedComponent" in item.toolTip() for item in graph.view.scene().items()
    )
    assert not any("STRUCTURE" in label or "ATOMIC NODE" in label for label in labels)


def test_data_flow_structural_nodes_have_distinct_shape_and_fill(tmp_path):
    path = tmp_path / "node-kinds.json"
    path.write_text(
        json.dumps(
            {
                "meta": {
                    "chemfit.structure": {
                        "children": [
                            {"index": 0, "name": "NestedPipeline"},
                            {"index": 1, "name": "AtomicComponent"},
                        ]
                    }
                },
                "children": [
                    {
                        "output": 1,
                        "meta": {"chemfit.structure": {"children": []}},
                    },
                    {"output": 2},
                ],
            }
        ),
        encoding="utf-8",
    )
    source = JsonFileSource()
    document = source.load(source.discover([path])[0])
    graph = ContextGraphPanel()
    graph.set_document(document)
    graph.ensure_rendered()

    nodes = {
        item.data(1): item
        for item in graph.view.scene().items()
        if item.data(1) in {"structural", "atomic"}
    }
    structural = nodes["structural"]
    atomic = nodes["atomic"]

    assert structural.brush().color() != atomic.brush().color()
    assert structural.path().elementCount() != atomic.path().elementCount()
    assert structural.pen().widthF() > atomic.pen().widthF()
    assert structural.node.nested
    assert not atomic.node.nested


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
    assert (
        window.graph.view.rendered_node_count == len(chemfit_document.children(())) + 2
    )

    window._load_generation = 2  # noqa: SLF001
    window._document_loaded(2, partial_document)  # noqa: SLF001
    assert window.graph.view.document is partial_document
    assert window.graph.view.rendered_node_count == 0


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
    assert window.search_workspace not in hierarchy_widgets
    assert window.workspace_tabs.indexOf(window.collection_analysis) >= 0
    assert window.workspace_tabs.indexOf(window.search_workspace) == (
        window.workspace_tabs.indexOf(window.collection_analysis) + 1
    )
    assert window.workspace_tabs.indexOf(window.extraction) == (
        window.workspace_tabs.indexOf(window.search_workspace) + 1
    )


def test_collection_overview_shows_opening_hints_when_empty(tmp_path, monkeypatch):
    window = MainWindow()

    empty_text = window.collection_analysis.summary_label.text()
    assert "No collection loaded" in empty_text
    assert "drag and drop" in empty_text.casefold()
    assert "Ctrl+O" in empty_text
    assert "Ctrl+Shift+O" in empty_text
    assert window.evaluation_stack.currentWidget() is window.evaluation_empty_state
    evaluation_hint = window.evaluation_empty_state.text()
    assert "No collection loaded" in evaluation_hint
    assert "drag and drop" in evaluation_hint.casefold()
    assert "Ctrl+O" in evaluation_hint
    assert "Ctrl+Shift+O" in evaluation_hint

    context_path = tmp_path / "context.json"
    context_path.write_text('{"value": 1}', encoding="utf-8")
    records = window.source.discover([context_path])
    monkeypatch.setattr(
        window.tasks,
        "submit",
        lambda function, on_success, _on_error: on_success(function()),
    )
    window._records_discovered(records)  # noqa: SLF001
    assert window.evaluation_stack.currentWidget() is window.file_list
    window.close_collection()

    closed_text = window.collection_analysis.summary_label.text()
    assert "No collection loaded" in closed_text
    assert "Ctrl+O" in closed_text
    assert "Ctrl+Shift+O" in closed_text
    assert window.evaluation_stack.currentWidget() is window.evaluation_empty_state


def test_evaluation_sidebar_persists_across_top_level_workspaces(qapp):
    window = MainWindow()
    window.show()

    assert window.main_workspace.indexOf(window.evaluation_panel) == 0
    assert window.context_workspace.indexOf(window.evaluation_panel) == -1
    assert window.workspace_tabs.objectName() == "workspaceTabs"
    assert window.workspace_tabs.tabBar().objectName() == "workspaceTabBar"
    for workspace in (
        window.context_workspace,
        window.collection_analysis,
        window.search_workspace,
        window.extraction,
    ):
        window.workspace_tabs.setCurrentWidget(workspace)
        qapp.processEvents()
        assert window.evaluation_panel.isVisibleTo(window)


def test_extraction_preview_tracks_visible_collection_order(tmp_path, monkeypatch):
    for index, name in enumerate(("step_1.json", "step_2.json", "step_10.json")):
        (tmp_path / name).write_text(json.dumps({"loss": index + 1}), encoding="utf-8")
    source = JsonFileSource()
    window = MainWindow(source=source)
    window.file_model.set_records(source.discover([tmp_path]))
    window.reverse_order_button.setEnabled(True)
    monkeypatch.setattr(
        window.tasks,
        "submit",
        lambda function, on_success, _on_error: on_success(function()),
    )
    window.extraction.add_selector(FieldSelector((), "fields", ("loss",), "loss"))

    def preview_names():
        model = window.extraction.preview_model
        return [model.data(model.index(row, 1)) for row in range(model.rowCount())]

    assert preview_names() == ["step_1.json", "step_2.json", "step_10.json"]

    window.reverse_order_button.click()

    assert preview_names() == ["step_10.json", "step_2.json", "step_1.json"]


def test_adding_extraction_field_keeps_workspace_and_updates_tab_counter():
    window = MainWindow()
    window.workspace_tabs.setCurrentWidget(window.context_workspace)

    window._add_extraction_selector(  # noqa: SLF001
        FieldSelector((), "fields", ("energy",), "energy")
    )

    assert window.workspace_tabs.currentWidget() is window.context_workspace
    assert window.workspace_tabs.tabText(window._extraction_tab_index) == (  # noqa: SLF001
        "Data Extraction (1)"
    )
    window.extraction.clear()
    assert window.workspace_tabs.tabText(window._extraction_tab_index) == (  # noqa: SLF001
        "Data Extraction"
    )


def test_context_tree_columns_are_user_resizable():
    window = MainWindow()
    header = window.tree.header()

    assert all(
        header.sectionResizeMode(column) == QHeaderView.ResizeMode.Interactive
        for column in range(window.tree_model.columnCount())
    )


def test_component_label_metadata_key_applies_across_views_and_persists(tmp_path):
    context_path = tmp_path / "labels.json"
    context_path.write_text(
        json.dumps(
            {
                "meta": {
                    "chemfit.structure": {
                        "children": [{"index": 0, "name": "WrappedComponent"}]
                    }
                },
                "children": [
                    {
                        "output": 1,
                        "meta": {"name": "density observable", "tag": "density"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    settings_directory = tmp_path / "label-settings"
    settings = application_settings(settings_directory)
    source = JsonFileSource()
    document = source.load(source.discover([context_path])[0])
    window = MainWindow(source=source, settings=settings)
    window._load_generation = 1  # noqa: SLF001
    window._document_loaded(1, document)  # noqa: SLF001
    root = window.tree_model.index(0, 0)
    window.tree_model.fetchMore(root)
    child = window.tree_model.index(0, 0, root)
    window.hierarchy_tabs.setCurrentWidget(window.graph)
    window.graph.ensure_rendered()

    assert window.tree_model.data(child) == "density observable"

    window.component_label_key.setEditText("tag")
    window._component_label_key_changed()  # noqa: SLF001
    window._select_context_path((0,), remember=True)  # noqa: SLF001

    assert window.tree_model.data(child) == "density"
    assert window.inspector.title.text() == "density"
    labels = {
        item.node.label
        for item in window.graph.view.scene().items()
        if hasattr(item, "node")
    }
    assert "density" in labels
    assert any(
        hit.breadcrumb.endswith("density")
        for hit in ContextSearchIndex(document).search("density")
    )
    assert settings.value(COMPONENT_LABEL_METADATA_KEY_SETTING) == "tag"

    restored = MainWindow(
        source=JsonFileSource(),
        settings=application_settings(settings_directory),
    )
    assert restored.component_label_key.currentText() == "tag"


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


def test_visual_style_and_spacing_are_persisted_in_settings_menu(tmp_path):
    settings_directory = tmp_path / "settings"
    settings = application_settings(settings_directory)
    window = MainWindow(settings=settings)

    window._set_style_profile("lightweight")  # noqa: SLF001
    window._set_density("compact")  # noqa: SLF001

    assert window.color_scheme.isHidden()
    assert settings.value(STYLE_PROFILE_SETTING) == "lightweight"
    assert settings.value(DENSITY_SETTING) == "compact"
    restored = MainWindow(settings=application_settings(settings_directory))
    assert restored._style_profile == "lightweight"  # noqa: SLF001
    assert restored._density == "compact"  # noqa: SLF001


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
