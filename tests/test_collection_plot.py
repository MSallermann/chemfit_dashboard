from __future__ import annotations

import json

from PySide6.QtCharts import QLogValueAxis, QScatterSeries
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from chemfit_dashboard.collection_plot import (
    CollectionPlotPanel,
    CollectionSeries,
    NumberSelector,
    SeriesPoint,
    build_collection_series,
    ordered_records,
    selector_for_document,
)
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

    assert len(requested) == 1
    assert requested[0].node_path == (1,)
    assert requested[0].section == "fields"
    assert requested[0].value_path == ("output",)
    assert add_requested == requested
    assert order_requested == requested


def test_inspector_actions_are_compact_grouped_and_explained(chemfit_document):
    inspector = Inspector()
    inspector.set_details(chemfit_document.details((1,)))

    assert inspector.plot_button.text() == "Plot"
    assert inspector.plot_add_button.text() == "+ Series"
    assert inspector.order_button.text() == "Sort ↑"
    assert inspector.extract_button.text() == "+ Field"
    assert inspector.extract_component_button.text() == "+ Component"
    assert inspector.plot_button.property("primaryAction") == "true"
    assert inspector.extract_button.property("extractAction") == "true"

    output_index = inspector.summary.model().index(1, 1)
    inspector.summary.setCurrentIndex(output_index)

    assert inspector.plot_selection.text() == "Selected: output"
    assert inspector.field_selection.text() == "Selected: output"
    assert "across the collection" in inspector.plot_button.toolTip()
    assert "Data Extraction" in inspector.extract_button.toolTip()


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
        if isinstance(series, QScatterSeries)
        and series is not panel._selection_series  # noqa: SLF001
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
    assert exported_image.width() == round(panel.chart_view.width() * (300 / 96))
    assert exported_image.height() == round(panel.chart_view.height() * (300 / 96))
    assert abs(exported_image.dotsPerMeterX() - round(300 / 0.0254)) <= 1
    assert abs(exported_image.dotsPerMeterY() - round(300 / 0.0254)) <= 1


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
        chart_position = chart.mapToPosition(
            QPointF(point.index, point.value), markers
        )
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


def test_main_window_orders_collection_by_selected_numeric_value(tmp_path):
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

    window.reverse_order_button.click()
    assert [record.name for record in window.file_model.records] == [
        "step_1.json",
        "step_10.json",
        "step_2.json",
    ]
    assert window.order_status.text().endswith("↓")

    window.reset_order_button.click()
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
