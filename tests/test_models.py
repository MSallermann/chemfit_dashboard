from __future__ import annotations

import json
import time
from unittest.mock import Mock

from PySide6.QtCore import QMimeData, QSettings, Qt, QUrl
from PySide6.QtWidgets import QMainWindow, QSplitter, QTabWidget, QWidget

from chemfit_dashboard.appearance import (
    COMPONENT_LABEL_METADATA_KEY_SETTING,
    FONT_SCALE_SETTING,
)
from chemfit_dashboard.evaluation_panel import EvaluationPanel
from chemfit_dashboard.extraction import (
    ExtractionConfig,
    FieldSelector,
    OutputDescriptor,
)
from chemfit_dashboard.main_window import MainWindow
from chemfit_dashboard.models import (
    ContextTreeModel,
    EvaluationListModel,
    JsonValueModel,
)
from chemfit_dashboard.navigation_history import ContextNavigationHistory
from chemfit_dashboard.session import WORKSPACE_SETTING, DashboardSessionController
from chemfit_dashboard.settings_ui import (
    BACKGROUND_THREADS_SETTING,
    SEARCH_PROCESSES_SETTING,
    DashboardSettingsController,
)
from chemfit_dashboard.source import JsonFileSource


def _isolated_main_window(tmp_path, monkeypatch, **kwargs):
    settings = QSettings(
        str(tmp_path / "dashboard.ini"),
        QSettings.Format.IniFormat,
    )
    monkeypatch.setattr(
        "chemfit_dashboard.settings_ui.application_settings",
        lambda: settings,
    )
    return MainWindow(**kwargs)


def test_main_window_preloads_extraction_configuration(tmp_path, monkeypatch):
    config = ExtractionConfig(
        ((FieldSelector((), ("output", "loss")), OutputDescriptor("loss")),)
    )

    window = _isolated_main_window(
        tmp_path,
        monkeypatch,
        extraction_config=config,
    )

    assert window.extraction_panel.config == config
    extraction_index = window.center_tabs.indexOf(window.extraction_panel)
    assert window.center_tabs.tabText(extraction_index) == "Data Extraction (1)"


def test_main_window_accepts_local_file_drags(tmp_path, monkeypatch):
    window = _isolated_main_window(tmp_path, monkeypatch)
    path = tmp_path / "context.json"
    mime_data = QMimeData()
    mime_data.setUrls([QUrl.fromLocalFile(str(path))])
    event = Mock()
    event.mimeData.return_value = mime_data

    window.dragEnterEvent(event)

    event.acceptProposedAction.assert_called_once_with()
    event.ignore.assert_not_called()


def test_main_window_drop_loads_all_distinct_local_locations(tmp_path, monkeypatch):
    window = _isolated_main_window(tmp_path, monkeypatch)
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    mime_data = QMimeData()
    mime_data.setUrls(
        [
            QUrl.fromLocalFile(str(first)),
            QUrl("https://example.com/context.json"),
            QUrl.fromLocalFile(str(first)),
            QUrl.fromLocalFile(str(second)),
        ]
    )
    loaded = []
    monkeypatch.setattr(
        window.coordinator,
        "load_collection",
        lambda locations, *, append=False: loaded.append((locations, append)),
    )
    event = Mock()
    event.mimeData.return_value = mime_data

    window.dropEvent(event)

    assert loaded == [([str(first), str(second)], False)]
    event.acceptProposedAction.assert_called_once_with()
    event.ignore.assert_not_called()


def test_main_window_drop_appends_to_existing_collection(tmp_path, monkeypatch):
    window = _isolated_main_window(tmp_path, monkeypatch)
    existing = tmp_path / "existing.json"
    existing.write_text("{}", encoding="utf-8")
    window.coordinator.collection.replace(JsonFileSource().discover([existing]))
    dropped = tmp_path / "dropped.json"
    mime_data = QMimeData()
    mime_data.setUrls([QUrl.fromLocalFile(str(dropped))])
    loaded = []
    monkeypatch.setattr(
        window.coordinator,
        "load_collection",
        lambda locations, *, append=False: loaded.append((locations, append)),
    )
    event = Mock()
    event.mimeData.return_value = mime_data

    window.dropEvent(event)

    assert loaded == [([str(dropped)], True)]
    event.acceptProposedAction.assert_called_once_with()
    event.ignore.assert_not_called()


def test_main_window_shows_collection_rejection_reason(tmp_path, monkeypatch):
    window = _isolated_main_window(tmp_path, monkeypatch)

    window.coordinator.operation_failed.emit(
        "Collection loading",
        "chemfit_dashboard.source.ContextSourceError: Unsupported file type: bad.xyz",
    )

    assert window.statusBar().currentMessage() == "Unsupported file type: bad.xyz"


def test_evaluation_model_uses_coordinator_order_and_folder_hierarchy(
    tmp_path, coordinator
):
    for name in ("step_10.json", "step_1.json", "step_2.json"):
        (tmp_path / name).write_text("{}", encoding="utf-8")
    records = JsonFileSource().discover([tmp_path])
    coordinator.collection.replace(records)
    model = EvaluationListModel(coordinator=coordinator)

    root = model.index(0, 0)
    names = [
        model.data(model.index(row, 0, root)) for row in range(model.rowCount(root))
    ]

    assert names == ["step_1.json", "step_2.json", "step_10.json"]


def test_evaluation_model_shows_only_immediate_parent_folders(tmp_path, coordinator):
    first_folder = tmp_path / "workspace" / "run_2"
    second_folder = tmp_path / "archive" / "run_10"
    first_folder.mkdir(parents=True)
    second_folder.mkdir(parents=True)
    first = first_folder / "first.json"
    second = second_folder / "second.json"
    first.write_text("{}", encoding="utf-8")
    second.write_text("{}", encoding="utf-8")
    coordinator.collection.replace(JsonFileSource().discover([first, second]))

    model = EvaluationListModel(coordinator=coordinator)

    assert model.rowCount() == 2
    first_parent = model.index(0, 0)
    second_parent = model.index(1, 0)
    assert [model.data(first_parent), model.data(second_parent)] == ["run_2", "run_10"]
    assert model.data(model.index(0, 0, first_parent)) == "first.json"
    assert model.data(model.index(0, 0, second_parent)) == "second.json"


def test_evaluation_panel_expands_first_folder_level(tmp_path, coordinator):
    (tmp_path / "step.json").write_text("{}", encoding="utf-8")
    coordinator.collection.replace(JsonFileSource().discover([tmp_path]))
    panel = EvaluationPanel(coordinator)

    panel.on_collection_changed()

    assert panel.file_list.isExpanded(panel.file_proxy.index(0, 0))
    assert panel.invert_order_button.property("chemfitIconName") == "reverse_order"


def test_evaluation_panel_removes_groups_and_records(tmp_path, coordinator):
    first_folder = tmp_path / "first"
    second_folder = tmp_path / "second"
    first_folder.mkdir()
    second_folder.mkdir()
    for path in (
        first_folder / "one.json",
        first_folder / "two.json",
        second_folder / "three.json",
    ):
        path.write_text("{}", encoding="utf-8")
    coordinator.collection.replace(
        JsonFileSource().discover([first_folder, second_folder])
    )
    panel = EvaluationPanel(coordinator)

    panel.file_list.setCurrentIndex(panel.file_proxy.index(0, 0))
    panel._remove_shortcut.activated.emit()  # noqa: SLF001

    assert [record.name for record in coordinator.collection] == ["three.json"]

    parent = panel.file_proxy.index(0, 0)
    panel.file_list.setCurrentIndex(panel.file_proxy.index(0, 0, parent))
    panel._remove_shortcut.activated.emit()  # noqa: SLF001

    assert not coordinator.collection


def test_automatic_evaluation_sync_preserves_filter_and_focus(loaded_coordinator, qapp):
    panel = EvaluationPanel(loaded_coordinator)
    panel.show()
    query = loaded_coordinator.selected_document.record.name
    panel.search.setText(query)
    panel.search.setFocus()
    qapp.processEvents()

    panel.on_collection_order_changed()
    panel.on_selected_record_changed()
    qapp.processEvents()

    assert panel.search.text() == query
    assert panel.search.hasFocus()
    assert not panel.file_list.hasFocus()


def test_automatic_evaluation_sync_clears_selection_when_record_is_filtered(
    tmp_path, loaded_coordinator, qapp
):
    path = tmp_path / "other.json"
    path.write_text("{}", encoding="utf-8")
    record = JsonFileSource().discover([path])[0]
    loaded_coordinator.collection.extend((record,))
    panel = EvaluationPanel(loaded_coordinator)
    query = loaded_coordinator.selected_document.record.name
    panel.search.setText(query)
    qapp.processEvents()
    assert panel.file_list.selectionModel().selectedIndexes()

    loaded_coordinator.select_record(record)
    deadline = time.monotonic() + 2
    while (
        loaded_coordinator.selected_document is None
        or loaded_coordinator.selected_document.record.id != record.id
    ) and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    qapp.processEvents()

    assert loaded_coordinator.selected_document is not None
    assert loaded_coordinator.selected_document.record.id == record.id
    assert panel.search.text() == query
    assert panel.file_list.selectionModel().selectedIndexes() == []


def test_interactive_evaluation_focus_clears_filter_and_focuses_tree(
    loaded_coordinator, qapp
):
    panel = EvaluationPanel(loaded_coordinator)
    panel.show()
    panel.search.setText("does-not-match")
    panel.search.setFocus()
    qapp.processEvents()

    panel.focus_selected_button.click()
    qapp.processEvents()

    assert panel.search.text() == ""
    assert panel.file_list.hasFocus()


def test_programmatic_evaluation_sync_is_idempotent(loaded_coordinator, monkeypatch):
    panel = EvaluationPanel(loaded_coordinator)
    panel.file_list.selectionModel().clear()
    selected = []
    monkeypatch.setattr(
        loaded_coordinator,
        "select_record",
        selected.append,
    )

    panel._focus_selected_record(interactive=False)  # noqa: SLF001

    assert selected == []


def test_evaluation_selection_still_forwards_a_different_record(
    tmp_path, loaded_coordinator, monkeypatch
):
    path = tmp_path / "other.json"
    path.write_text("{}", encoding="utf-8")
    record = JsonFileSource().discover([path])[0]
    loaded_coordinator.collection.extend((record,))
    panel = EvaluationPanel(loaded_coordinator)
    selected = []
    monkeypatch.setattr(
        loaded_coordinator,
        "select_record",
        selected.append,
    )

    source_index = panel.file_model.index_for_record_id(record.id)
    panel.file_list.setCurrentIndex(panel.file_proxy.mapFromSource(source_index))

    assert selected == [record]


def test_context_tree_model_fetches_children_lazily(tmp_path):
    children = [{"output": index} for index in range(450)]
    path = tmp_path / "large.json"
    path.write_text(json.dumps({"children": children}), encoding="utf-8")
    source = JsonFileSource()
    document = source.load(source.discover([path])[0])
    model = ContextTreeModel()
    model.set_document(document)
    root = model.index(0, 0)

    assert model.rowCount(root) == 0
    assert model.canFetchMore(root)
    model.fetchMore(root)
    assert model.rowCount(root) == model.FETCH_BATCH_SIZE
    assert model.canFetchMore(root)


def test_json_value_model_materializes_nested_values_in_batches():
    model = JsonValueModel({"items": list(range(450))})
    items = model.index(0, 0)

    assert model.rowCount(items) == 0
    model.fetchMore(items)
    assert model.rowCount(items) == model.FETCH_BATCH_SIZE
    assert model.value_path(model.index(3, 1, items)) == ("items", 3)


def test_main_window_exposes_six_feature_workspaces_and_navigation_shortcuts(
    tmp_path, monkeypatch
):
    window = _isolated_main_window(tmp_path, monkeypatch)

    assert window.center_tabs.objectName() == "workspaceTabs"
    assert window.center_tabs.tabBar().objectName() == "workspaceTabBar"
    assert [window.center_tabs.tabText(index) for index in range(6)] == [
        "Context Hierarchy",
        "Data Flow",
        "Context Search",
        "Data Extraction",
        "Collection Plot",
        "Collection Overview",
    ]
    shortcuts = {
        shortcut.toString()
        for action in window.findChildren(type(window.find_action))
        for shortcut in action.shortcuts()
    }
    assert {f"Ctrl+{index}" for index in range(1, 7)}.issubset(shortcuts)
    assert "Ctrl+D" in shortcuts


def test_workspace_tab_badges_track_extraction_fields_and_plot_series(
    tmp_path, monkeypatch
):
    window = _isolated_main_window(tmp_path, monkeypatch)

    window.add_extraction_field(
        (FieldSelector((), ("loss",)), OutputDescriptor("loss"))
    )
    window.collection_plot.series_count_changed.emit(2)

    assert window.center_tabs.tabText(3) == "Data Extraction (1)"
    assert window.center_tabs.tabText(4) == "Collection Plot (2)"


def test_data_flow_and_plot_can_detach_and_restore(tmp_path, monkeypatch):
    window = _isolated_main_window(tmp_path, monkeypatch)

    for controller, panel in (
        (window._data_flow_detachment, window.data_flow_panel),  # noqa: SLF001
        (window._plot_detachment, window.collection_plot),  # noqa: SLF001
    ):
        controller.detach()
        assert controller.dialog is not None
        assert window.center_tabs.indexOf(panel) == -1
        controller.restore()
        assert controller.dialog is None
        assert window.center_tabs.indexOf(panel) >= 0


def test_navigation_history_replays_context_selections(loaded_coordinator):
    history = ContextNavigationHistory(coordinator=loaded_coordinator)
    loaded_coordinator.select_context((0,))
    loaded_coordinator.select_context((1,))

    history.back_action.trigger()
    assert loaded_coordinator.selected_context == (0,)
    assert history.forward_action.isEnabled()

    history.forward_action.trigger()
    assert loaded_coordinator.selected_context == (1,)


def test_component_label_key_defaults_empty_and_updates_loaded_document(
    loaded_coordinator,
):
    assert loaded_coordinator.component_label_metadata_key == ""

    loaded_coordinator.set_component_label_metadata_key("name")

    assert loaded_coordinator.component_label_metadata_key == "name"
    assert loaded_coordinator.selected_document.component_label_metadata_key == "name"


def test_settings_restore_font_label_and_worker_counts(tmp_path, coordinator, qapp):
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    settings.setValue(FONT_SCALE_SETTING, 1.2)
    settings.setValue(COMPONENT_LABEL_METADATA_KEY_SETTING, "label")
    settings.setValue(BACKGROUND_THREADS_SETTING, 3)
    settings.setValue(SEARCH_PROCESSES_SETTING, 4)

    controller = DashboardSettingsController(
        coordinator=coordinator,
        settings=settings,
    )

    assert coordinator.component_label_metadata_key == "label"
    assert coordinator.task_runner.thread_workers == 3
    assert coordinator.task_runner.process_workers == 4
    assert settings.value(FONT_SCALE_SETTING, type=float) == 1.2
    assert qapp.font().pointSizeF() > 0
    controller._change_font_scale(-0.1)  # noqa: SLF001
    assert settings.value(FONT_SCALE_SETTING, type=float) == 1.1


def test_session_restores_layout_but_only_restores_collection_explicitly(
    tmp_path, coordinator
):
    settings = QSettings(str(tmp_path / "session.ini"), QSettings.Format.IniFormat)
    settings.setValue(WORKSPACE_SETTING, 1)
    window = QMainWindow()
    splitter = QSplitter(Qt.Orientation.Horizontal)
    splitter.addWidget(QWidget())
    tabs = QTabWidget()
    tabs.addTab(QWidget(), "First")
    tabs.addTab(QWidget(), "Second")
    requested = []
    coordinator.collection_load_requested.connect(requested.append)

    session = DashboardSessionController(
        coordinator=coordinator,
        settings=settings,
        window=window,
        splitter=splitter,
        workspace_tabs=tabs,
    )

    assert tabs.currentIndex() == 1
    assert requested == []
    tabs.setCurrentIndex(0)
    session.save()
    assert settings.value(WORKSPACE_SETTING, type=int) == 0
