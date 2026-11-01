from __future__ import annotations

import json

from PySide6.QtCharts import QBarSeries, QLogValueAxis, QScatterSeries
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QCheckBox, QLabel, QPushButton, QToolButton

from chemfit_dashboard.collection_plot import (
    CollectionPlotPanel,
    CollectionSeries,
    NumberSelector,
    SeriesPoint,
    build_collection_series,
    ordered_records,
    selector_for_document,
)
from chemfit_dashboard.context_search import SearchHit
from chemfit_dashboard.inspector import Inspector
from chemfit_dashboard.main_window import MainWindow
from chemfit_dashboard.source import JsonFileSource


def test_builds_series_from_nested_intermediate_context_outputs():
    source = JsonFileSource()
    records = ordered_records(source.discover(["examples/context_collection"]))
    documents = [(record, source.load(record)) for record in records]
    selector = selector_for_document(
        documents[0][1],
        NumberSelector(
            node_path=(1, 0, 0, 0, 0),
            section="fields",
            value_path=("output",),
            label="repulsive_energy · output",
            value_label="output",
        ),
    )

    series = build_collection_series(selector, documents)

    assert series.labels == (
        "step_0_ctx_0.json",
        "step_0_ctx_1.json",
        "step_1_ctx_0.json",
        "step_1_ctx_1.json",
        "step_2_ctx_0.json",
        "step_2_ctx_1.json",
    )
    assert len(series.points) == 6
    assert series.missing_count == 0
    assert len({point.value for point in series.points}) > 1

    panel = CollectionPlotPanel()
    panel.set_series(series)
    assert len(panel.chart_view.chart().series()) == 2
    assert panel.status.text() == "6 of 6 contexts in collection"
    assert "step_0_ctx_0" not in panel.selection_label.text()
    assert (
        panel.chart_view.chart().axes(Qt.Orientation.Vertical)[0].titleText()
        == "output"
    )

    panel.log_y.setChecked(True)
    log_axis = panel.chart_view.chart().axes(Qt.Orientation.Vertical)[0]
    assert isinstance(log_axis, QLogValueAxis)
    assert log_axis.titleText() == "output (log scale)"
    panel.chart_view.chart().zoom(2)
    panel.reset_zoom_button.click()


def test_inspector_requests_plot_for_selected_intermediate_number(
    chemfit_document,
):
    inspector = Inspector()
    inspector.set_details(chemfit_document.details((1,)))
    output_index = inspector.summary.model().index(1, 1)
    requested = []
    add_requested = []
    order_requested = []
    inspector.plot_requested.connect(requested.append)
    inspector.plot_add_requested.connect(add_requested.append)
    inspector.order_requested.connect(order_requested.append)

    inspector.summary.setCurrentIndex(output_index)
    inspector.plot_button.click()
    inspector.plot_add_button.click()
    inspector.order_button.click()
    descending_requested = []
    inspector.order_descending_requested.connect(descending_requested.append)
    inspector.order_descending_button.click()

    assert len(requested) == 1
    assert requested[0].node_path == (1,)
    assert requested[0].section == "fields"
    assert requested[0].value_path == ("output",)
    assert add_requested == requested
    assert order_requested == requested
    assert descending_requested == requested


def test_inspector_actions_are_compact_grouped_and_explained(chemfit_document):
    inspector = Inspector()
    inspector.set_details(chemfit_document.details((1,)))

    assert inspector.plot_button.text() == "Plot"
    assert inspector.plot_add_button.text() == "+ Series"
    assert inspector.order_button.text() == "Sort ↑"
    assert inspector.order_descending_button.text() == "Sort ↓"
    assert inspector.extract_button.text() == "+ Field"
    assert inspector.extract_component_button.text() == "+ Component"
    assert inspector.plot_button.property("primaryAction") == "true"
    assert inspector.extract_button.property("extractAction") == "true"
    assert not inspector.order_button.isHidden()
    assert not inspector.extract_component_button.isHidden()

    output_index = inspector.summary.model().index(1, 1)
    inspector.summary.setCurrentIndex(output_index)

    assert inspector.plot_selection.text() == "Selected: output"
    assert inspector.field_selection.text() == "Selected: output"
    assert inspector.order_button.isEnabled()
    assert inspector.order_descending_button.isEnabled()
    assert inspector.extract_component_button.isEnabled()
    assert "across the collection" in inspector.plot_button.toolTip()
    assert "Data Extraction" in inspector.extract_button.toolTip()


def test_plot_reports_coordinates_and_can_hide_series(qapp):
    labels = ("step_1.json", "step_2.json")
    record_ids = ("first", "second")
    first = CollectionSeries(
        NumberSelector((), "fields", ("loss",), "loss", "loss"),
        labels,
        (SeriesPoint(0, labels[0], 2.0), SeriesPoint(1, labels[1], 4.0)),
        record_ids,
    )
    second = CollectionSeries(
        NumberSelector((), "fields", ("runtime",), "runtime", "runtime"),
        labels,
        (SeriesPoint(0, labels[0], 10.0), SeriesPoint(1, labels[1], 30.0)),
        record_ids,
    )
    panel = CollectionPlotPanel()
    panel.set_series_collection((first, second))
    copied = []
    panel.status_message.connect(copied.append)
    panel.series_list.setCurrentRow(1)
    panel._series_copy_shortcut.activated.emit()  # noqa: SLF001
    assert qapp.clipboard().text() == "Evaluation root / fields/runtime"
    assert copied[-1] == "Copied plot series field path"
    panel._copy_series_data(1)  # noqa: SLF001
    assert qapp.clipboard().text().splitlines() == [
        "Index,Filename,Record ID,runtime",
        "0,step_1.json,first,10.0",
        "1,step_2.json,second,30.0",
    ]

    panel._cursor_moved(QPointF(0.2, 3.25))  # noqa: SLF001
    assert "step_1.json" in panel.cursor_coordinates.text()
    assert "X: 0.2" in panel.cursor_coordinates.text()
    assert "Y: 3.25" in panel.cursor_coordinates.text()

    second_row = panel.series_list.itemWidget(panel.series_list.item(1))
    assert second_row is not None
    second_visibility = second_row.findChild(QCheckBox, "seriesVisibility")
    assert second_visibility is not None
    second_visibility.setChecked(False)
    assert len(panel.chart_view.chart().series()) == 2
    assert not panel.chart_view.chart().legend().isVisible()
    assert second_row.findChild(QLabel, "seriesColorSwatch") is not None

    first_row = panel.series_list.itemWidget(panel.series_list.item(0))
    assert first_row is not None
    first_visibility = first_row.findChild(QCheckBox, "seriesVisibility")
    assert first_visibility is not None
    first_visibility.setChecked(False)
    assert panel.status.text().startswith("All series are hidden")

    removed = []
    panel.remove_series_requested.connect(removed.append)
    remove = second_row.findChild(QToolButton, "removeSeriesAction")
    assert remove is not None
    remove.click()
    assert removed == [second.selector]


def test_plot_supports_histograms_statistics_and_bounded_large_rendering():
    count = 5_000
    labels = tuple(f"step_{index}.json" for index in range(count))
    data = CollectionSeries(
        NumberSelector((), "fields", ("loss",), "loss", "loss"),
        labels,
        tuple(SeriesPoint(index, labels[index], float(index)) for index in range(count)),
    )
    panel = CollectionPlotPanel()
    panel.set_series(data)

    scatter = next(
        series
        for series in panel.chart_view.chart().series()
        if isinstance(series, QScatterSeries)
    )
    assert scatter.count() <= 4_000

    panel.plot_mode.setCurrentIndex(panel.plot_mode.findData("histogram"))
    assert any(
        isinstance(series, QBarSeries)
        for series in panel.chart_view.chart().series()
    )
    assert "bins" in panel.status.text()

    panel.histogram_bins.setValue(1)

    assert panel.histogram_bins.minimum() == 1
    assert "1 equal-width bin" in panel.status.text()

    panel.plot_mode.setCurrentIndex(panel.plot_mode.findData("statistics"))
    assert panel.statistics_table.rowCount() == 1
    assert panel.statistics_table.item(0, 1).text() == "5,000"


def test_plot_can_expand_into_window_and_return(qapp):
    window = MainWindow()
    window.show()
    qapp.processEvents()
    window.hierarchy_tabs.setCurrentWidget(window.collection_plot)

    window.collection_plot.expand_button.click()
    qapp.processEvents()

    dialog = window._expanded_plot_dialog  # noqa: SLF001
    assert dialog is not None
    assert window.hierarchy_tabs.indexOf(window.collection_plot) == -1
    placeholder = window._plot_placeholder  # noqa: SLF001
    assert placeholder is not None
    message = placeholder.findChild(QLabel, "plotDetachedMessage")
    assert message is not None
    assert "detached window" in message.text()
    assert window.collection_plot.expand_button.text() == "Collapse"

    return_button = placeholder.findChild(QPushButton, "returnPlotButton")
    assert return_button is not None
    return_button.click()
    qapp.processEvents()

    assert window._expanded_plot_dialog is None  # noqa: SLF001
    assert window.hierarchy_tabs.indexOf(window.collection_plot) == 2
    assert window.collection_plot.expand_button.text() == "Expand"
    window.close()


def test_multi_series_plot_normalizes_exports_and_selects_context(
    tmp_path, monkeypatch
):
    selector_a = NumberSelector((), "fields", ("loss",), "loss", "loss")
    selector_b = NumberSelector((), "fields", ("runtime",), "runtime", "runtime")
    labels = ("step_1.json", "step_2.json")
    record_ids = ("first", "second")
    first = CollectionSeries(
        selector_a,
        labels,
        (
            SeriesPoint(0, labels[0], 2.0, record_ids[0]),
            SeriesPoint(1, labels[1], 4.0, record_ids[1]),
        ),
        record_ids,
    )
    second = CollectionSeries(
        selector_b,
        labels,
        (
            SeriesPoint(0, labels[0], 10.0, record_ids[0]),
            SeriesPoint(1, labels[1], 30.0, record_ids[1]),
        ),
        record_ids,
    )
    panel = CollectionPlotPanel()
    selected = []
    selected_points = []
    panel.context_selected.connect(selected.append)
    panel.point_selected.connect(
        lambda record_id, selector: selected_points.append((record_id, selector))
    )

    panel.set_series_collection((first, second))
    assert panel.series_list.count() == 2
    assert panel.chart_view.chart().legend().isVisible()

    panel.normalize.setChecked(True)
    y_axis = panel.chart_view.chart().axes(Qt.Orientation.Vertical)[0]
    assert y_axis.titleText() == "Normalized value (0-1)"
    assert not panel.log_y.isEnabled()

    panel.normalize.setChecked(False)
    panel.select_record("second")
    assert len(panel.chart_view.chart().series()) == 5
    plotted_marker = next(
        series
        for series in panel.chart_view.chart().series()
        if isinstance(series, QScatterSeries) and series is not panel._selection_series  # noqa: SLF001
    )
    plotted_marker.clicked.emit(QPointF(0, 0))
    assert selected == ["first"]

    selection_marker = panel._selection_series  # noqa: SLF001
    assert selection_marker is not None
    hovered = []
    monkeypatch.setattr(
        panel,
        "_point_hovered",
        lambda series, point, state, label="": hovered.append(
            (series, point, state, label)
        ),
    )
    selection_marker.hovered.emit(QPointF(1, 30), True)  # noqa: FBT003
    selection_marker.clicked.emit(QPointF(1, 30))

    assert selected == ["first", "second"]
    assert selected_points == [("first", selector_a), ("second", selector_b)]
    assert hovered[0][:3] == (selection_marker, QPointF(1, 30), True)
    assert hovered[0][3] == "runtime"

    export_path = tmp_path / "plot.csv"
    panel.export_csv(export_path)
    exported = export_path.read_text(encoding="utf-8")
    assert "loss,runtime" in exported
    assert "step_2.json,second,4.0,30.0" in exported

    custom_export_path = tmp_path / "custom_plot.csv"
    panel.export_csv(
        custom_export_path,
        ("iteration", "evaluation", "context_key", "objective", "wall_time"),
    )
    custom_export = custom_export_path.read_text(encoding="utf-8")
    assert custom_export.startswith(
        "iteration,evaluation,context_key,objective,wall_time\n"
    )

    png_path = tmp_path / "plot.png"
    svg_path = tmp_path / "plot.svg"
    panel.export_image(png_path)
    panel.export_image(svg_path)
    assert png_path.stat().st_size > 0
    assert svg_path.stat().st_size > 0


def test_png_plot_export_is_rendered_at_300_dpi(tmp_path):
    selector = NumberSelector((), "fields", ("loss",), "loss", "loss")
    panel = CollectionPlotPanel()
    panel.resize(640, 480)
    panel.set_series(
        CollectionSeries(
            selector,
            ("one.json", "two.json"),
            (
                SeriesPoint(0, "one.json", 1.0, "one"),
                SeriesPoint(1, "two.json", 2.0, "two"),
            ),
            ("one", "two"),
        )
    )
    panel.show()
    QApplication.processEvents()
    png_path = tmp_path / "high-resolution.png"

    panel.export_image(png_path)

    exported_image = QImage(str(png_path))
    viewport = panel.chart_view.viewport()
    assert exported_image.width() == round(viewport.width() * (300 / 96))
    assert exported_image.height() == round(viewport.height() * (300 / 96))
    assert abs(exported_image.dotsPerMeterX() - round(300 / 0.0254)) <= 1
    assert abs(exported_image.dotsPerMeterY() - round(300 / 0.0254)) <= 1

    # The lower-right portion must contain rendered chart details rather than
    # being an unused single-color area outside an unscaled upper-left render.
    colors = {
        exported_image.pixelColor(x, y).rgba()
        for x in range(exported_image.width() * 2 // 3, exported_image.width(), 4)
        for y in range(exported_image.height() * 2 // 3, exported_image.height(), 4)
    }
    assert len(colors) > 1


def test_plot_markers_receive_real_single_clicks_through_zoom_rubber_band():
    selector = NumberSelector((), "fields", ("loss",), "loss", "loss")
    labels = ("one.json", "two.json", "three.json")
    record_ids = ("one", "two", "three")
    data = CollectionSeries(
        selector,
        labels,
        tuple(
            SeriesPoint(index, label, float(index + 1), record_ids[index])
            for index, label in enumerate(labels)
        ),
        record_ids,
    )
    panel = CollectionPlotPanel()
    panel.resize(900, 600)
    panel.set_series(data)
    panel.show()
    QApplication.processEvents()
    selected = []
    panel.context_selected.connect(selected.append)
    chart = panel.chart_view.chart()
    markers = next(
        series
        for series in chart.series()
        if isinstance(series, QScatterSeries) and series.name() == "loss"
    )

    for point in data.points:
        chart_position = chart.mapToPosition(QPointF(point.index, point.value), markers)
        viewport_position = panel.chart_view.mapFromScene(chart_position)
        QTest.mouseClick(
            panel.chart_view.viewport(),
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
            viewport_position,
        )
        QApplication.processEvents()

    panel.select_record("two")
    QApplication.processEvents()
    selection = panel._selection_series  # noqa: SLF001
    assert selection is not None
    selected_position = panel.chart_view.mapFromScene(
        chart.mapToPosition(QPointF(1, 2), selection)
    )
    QTest.mouseClick(
        panel.chart_view.viewport(),
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
        selected_position,
    )
    QApplication.processEvents()

    assert selected == ["one", "two", "three", "two"]


def test_main_window_orders_collection_by_selected_numeric_value(
    tmp_path, monkeypatch
):
    for name in ("step_1.json", "step_2.json", "step_10.json"):
        (tmp_path / name).write_text("{}", encoding="utf-8")
    source = JsonFileSource()
    records = source.discover([tmp_path])
    window = MainWindow(source=source)
    window.file_model.set_records(records)
    selector = NumberSelector((), "fields", ("loss",), "Evaluation root / loss")
    data = CollectionSeries(
        selector=selector,
        labels=tuple(record.name for record in records),
        points=(
            SeriesPoint(0, records[0].name, 5.0),
            SeriesPoint(2, records[2].name, 1.0),
        ),
    )
    window._order_generation = 1  # noqa: SLF001

    window._order_loaded(1, records, data)  # noqa: SLF001

    assert [record.name for record in window.file_model.records] == [
        "step_10.json",
        "step_1.json",
        "step_2.json",
    ]
    assert "missing placed last" in window.statusBar().currentMessage()

    sort_indicator_invalidations = []
    monkeypatch.setattr(
        window.extraction,
        "clear_collection_sort",
        lambda: sort_indicator_invalidations.append(True),
    )
    window.reverse_order_button.click()
    assert len(sort_indicator_invalidations) == 1
    assert [record.name for record in window.file_model.records] == [
        "step_1.json",
        "step_10.json",
        "step_2.json",
    ]
    assert window.order_status.text().endswith("↓")

    window.reset_order_button.click()
    assert len(sort_indicator_invalidations) == 2
    assert [record.name for record in window.file_model.records] == [
        "step_1.json",
        "step_2.json",
        "step_10.json",
    ]

    window.reverse_order_button.click()
    assert [record.name for record in window.file_model.records] == [
        "step_10.json",
        "step_2.json",
        "step_1.json",
    ]


def test_reversing_collection_rebuilds_the_active_plot(tmp_path, monkeypatch):
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
    selector = NumberSelector((), "fields", ("loss",), "Evaluation root / loss")

    window._plot_requested(selector)  # noqa: SLF001
    assert window.collection_plot._labels == (  # noqa: SLF001
        "step_1.json",
        "step_2.json",
        "step_10.json",
    )

    window.reverse_order_button.click()

    assert window.collection_plot._labels == (  # noqa: SLF001
        "step_10.json",
        "step_2.json",
        "step_1.json",
    )


def test_main_window_adds_plot_series_and_plot_click_selects_evaluation(
    tmp_path, monkeypatch
):
    for index, name in enumerate(("step_1.json", "step_2.json")):
        (tmp_path / name).write_text(
            json.dumps({"loss": index + 1, "runtime": (index + 1) * 10}),
            encoding="utf-8",
        )
    source = JsonFileSource()
    records = source.discover([tmp_path])
    window = MainWindow(source=source)
    window.file_model.set_records(records)
    window._current_document = source.load(records[0])  # noqa: SLF001
    monkeypatch.setattr(
        window.tasks,
        "submit",
        lambda function, on_success, _on_error: on_success(function()),
    )

    window._plot_requested(  # noqa: SLF001
        NumberSelector((), "fields", ("loss",), "loss", "loss")
    )
    window._plot_add_requested(  # noqa: SLF001
        NumberSelector((), "fields", ("runtime",), "runtime", "runtime")
    )

    assert len(window._active_plot_selectors) == 2  # noqa: SLF001
    assert window.collection_plot.series_list.count() == 2

    plotted_markers = [
        series
        for series in window.collection_plot.chart_view.chart().series()
        if isinstance(series, QScatterSeries)
        and series is not window.collection_plot._selection_series  # noqa: SLF001
    ]
    plotted_markers[1].clicked.emit(QPointF(1, 20))

    assert window._selected_record_id() == records[1].id  # noqa: SLF001
    assert window._current_document is not None  # noqa: SLF001
    assert window._current_document.record.id == records[1].id  # noqa: SLF001
    assert window.inspector.title.text() == "step_2"
    assert window.inspector.plot_selection.text() == "Selected: runtime"
    assert window.inspector.summary.currentIndex().data() == "20"


def test_whole_collection_search_is_dispatched_to_worker(tmp_path, monkeypatch):
    for name in ("step_1.json", "step_2.json"):
        (tmp_path / name).write_text('{"output": 1}', encoding="utf-8")
    source = JsonFileSource()
    window = MainWindow(source=source)
    assert not window.search_components.isChecked()
    window.file_model.set_records(source.discover([tmp_path]))
    window.search_scope.setCurrentIndex(window.search_scope.findData("collection"))
    window.field_search.setText("output")
    submitted = []
    progress_updates = []
    window.operation_progress.connect(
        lambda name, value, total: progress_updates.append((name, value, total))
    )
    monkeypatch.setattr(
        window.tasks,
        "submit",
        lambda function, on_success, on_error: submitted.append(
            (function, on_success, on_error)
        ),
    )

    window._run_field_search()  # noqa: SLF001

    assert len(submitted) == 1
    assert "in the background" in window.search_panel.status.text()
    function, _on_success, _on_error = submitted[0]
    hits, failed_records, cancelled = function()
    assert hits
    assert failed_records == 0
    assert not cancelled
    assert progress_updates == [("search", 0, 2), ("search", 2, 2)]
    window.collection_search.shutdown(wait_for_workers=True)


def test_superseded_collection_search_cannot_replace_current_results():
    window = MainWindow()
    window._search_generation = 2  # noqa: SLF001
    window.search_panel.show_message("Current collection has no search yet")
    stale_hit = SearchHit(
        (),
        "Evaluation root",
        "complete",
        "fields",
        ("output",),
        "output",
        "1",
        score=1.0,
        record_id="old-record",
        record_name="old.json",
    )

    window._search_loaded(  # noqa: SLF001
        1,
        ([stale_hit], 0, False),
        query="output",
        include_keys=True,
        include_values=True,
        include_metadata=True,
        include_components=True,
        mode="fuzzy",
        scope_label="whole collection",
        searched_records=1,
    )

    assert window.search_panel.results.topLevelItemCount() == 0
    assert window.search_panel.status.text() == "Current collection has no search yet"
    window.collection_search.shutdown(wait_for_workers=False)


def test_series_follows_component_identity_when_runtime_tree_shifts(tmp_path):
    first_path = tmp_path / "step_0_ctx_0.json"
    second_path = tmp_path / "step_1_ctx_0.json"
    first_path.write_text(
        json.dumps(
            {
                "children": [
                    {"output": 1, "meta": {"component_name": "upstream"}},
                    {"output": 2, "meta": {"component_name": "target"}},
                ]
            }
        ),
        encoding="utf-8",
    )
    second_path.write_text(
        json.dumps(
            {
                "children": [
                    {"output": 3, "meta": {"component_name": "target"}},
                ]
            }
        ),
        encoding="utf-8",
    )
    source = JsonFileSource()
    records = source.discover([tmp_path])
    documents = [(record, source.load(record)) for record in records]
    selector = selector_for_document(
        documents[0][1],
        NumberSelector(
            node_path=(1,),
            section="fields",
            value_path=("output",),
            label="target · output",
        ),
    )

    series = build_collection_series(selector, documents)

    assert [point.value for point in series.points] == [2, 3]


def test_root_selector_is_collection_scoped_not_named_after_current_file(
    chemfit_document,
):
    selector = selector_for_document(
        chemfit_document,
        NumberSelector(
            node_path=(),
            section="fields",
            value_path=("output",),
            label=f"{chemfit_document.root.label} · output",
            value_label="output",
        ),
    )

    assert selector.label == "Evaluation root / output"
