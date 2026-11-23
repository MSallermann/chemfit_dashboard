from __future__ import annotations

import time

import pytest
from PySide6.QtCharts import QBarSeries, QLineSeries, QLogValueAxis, QScatterSeries
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QCheckBox, QToolButton

from chemfit_dashboard.collection_plot import (
    MAX_RENDERED_POINTS_PER_SERIES,
    CollectionPlotPanel,
    CollectionSeries,
    SeriesPoint,
)
from chemfit_dashboard.extraction import FieldSelector
from chemfit_dashboard.inspector import Inspector
from chemfit_dashboard.ui_components import ActionRow
from chemfit_dashboard.workers import TaskState


def _wait_for(qapp, predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        qapp.processEvents()
        if time.monotonic() >= deadline:
            message = "Timed out waiting for collection plot"
            raise AssertionError(message)
        time.sleep(0.005)


def _series(
    field: str = "loss",
    values: tuple[float, ...] = (2.0, 4.0),
) -> CollectionSeries:
    labels = tuple(f"step_{index}.json" for index in range(len(values)))
    return CollectionSeries(
        FieldSelector((), (field,)),
        labels,
        tuple(
            SeriesPoint(index, label, value, f"record-{index}")
            for index, (label, value) in enumerate(zip(labels, values, strict=True))
        ),
        tuple(f"record-{index}" for index in range(len(values))),
    )


def test_plot_renders_series_and_log_axis(coordinator):
    panel = CollectionPlotPanel(coordinator=coordinator)

    panel.set_series_collection((_series(),))

    assert len(panel.chart_view.chart().series()) == 2
    assert panel.selection_label.text() == "Currently plotting: loss@/"
    assert panel.status.text() == "2 of 2 contexts in collection"

    panel.log_y.setChecked(True)
    axis = panel.chart_view.chart().axes(Qt.Orientation.Vertical)[0]
    assert isinstance(axis, QLogValueAxis)


def test_plot_build_reports_collection_progress(loaded_coordinator, qapp):
    panel = CollectionPlotPanel(coordinator=loaded_coordinator)
    events = []
    loaded_coordinator.task_runner.lifecycle_changed.connect(events.append)

    panel.build_series((FieldSelector((1,), ("output",)),))

    _wait_for(qapp, lambda: bool(panel._data))  # noqa: SLF001
    plot_events = [event for event in events if event.task.key == "collection_plot"]
    assert any(
        event.state == TaskState.PROGRESS and event.progress == (1, 1)
        for event in plot_events
    )
    assert plot_events[-1].state == TaskState.COMPLETED


def test_plot_supports_multiple_series_histograms_and_statistics(coordinator):
    panel = CollectionPlotPanel(coordinator=coordinator)
    panel.set_series_collection((_series(), _series("runtime", (10.0, 30.0))))

    assert panel.series_list.count() == 2
    panel.plot_mode.setCurrentIndex(panel.plot_mode.findData("histogram"))
    assert any(
        isinstance(series, QBarSeries) for series in panel.chart_view.chart().series()
    )

    panel.plot_mode.setCurrentIndex(panel.plot_mode.findData("statistics"))
    assert panel.statistics_table.rowCount() == 2


def test_plot_series_rows_hide_and_remove_values(coordinator):
    panel = CollectionPlotPanel(coordinator=coordinator)
    loss = _series()
    runtime = _series("runtime", (10.0, 30.0))
    panel._set_requested_selectors((loss.selector, runtime.selector))  # noqa: SLF001
    panel.set_series_collection((loss, runtime))

    row = panel.series_list.itemWidget(panel.series_list.item(1))
    assert isinstance(row, ActionRow)
    visibility = row.findChild(QCheckBox, "seriesVisibility")
    remove = row.findChild(QToolButton, "removeSeriesAction")
    assert visibility is not None
    assert remove is not None

    visibility.setChecked(False)
    assert len(panel.chart_view.chart().series()) == 2

    remove.click()
    assert panel.series_list.count() == 1
    assert panel._data == (loss,)  # noqa: SLF001


def test_plot_copies_canonical_series_path(coordinator, qapp):
    panel = CollectionPlotPanel(coordinator=coordinator)
    data = CollectionSeries(
        FieldSelector((1, 0), ("output", "energy")),
        ("step.json",),
        (SeriesPoint(0, "step.json", 3.5),),
    )
    messages = []
    panel.status_message.connect(messages.append)
    panel.set_series_collection((data,))
    panel.series_list.setCurrentRow(0)

    panel._series_copy_shortcut.activated.emit()  # noqa: SLF001

    assert qapp.clipboard().text() == "output.energy@/1/0"
    assert messages == ["Copied plot series field path"]


def test_plot_bounds_rendered_points_for_large_series(coordinator, qapp):
    count = MAX_RENDERED_POINTS_PER_SERIES * 2
    panel = CollectionPlotPanel(coordinator=coordinator)

    panel.set_series_collection((_series(values=tuple(map(float, range(count)))),))
    chart = panel.chart_view.chart()

    markers = max(
        chart.series(),
        key=lambda item: item.count(),
    )
    assert markers.count() <= MAX_RENDERED_POINTS_PER_SERIES

    chart.axes(Qt.Orientation.Horizontal)[0].setRange(10.5, count - 10.5)
    qapp.processEvents()

    line = next(series for series in chart.series() if isinstance(series, QLineSeries))
    assert line.count() <= MAX_RENDERED_POINTS_PER_SERIES
    assert line.points()[0].x() == 10
    assert line.points()[-1].x() == count - 10


def test_plot_zoom_renders_every_point_in_small_visible_window(coordinator, qapp):
    count = MAX_RENDERED_POINTS_PER_SERIES * 2
    panel = CollectionPlotPanel(coordinator=coordinator)
    panel.set_series_collection((_series(values=tuple(map(float, range(count)))),))
    chart = panel.chart_view.chart()
    markers = next(
        series for series in chart.series() if isinstance(series, QScatterSeries)
    )
    initial_indexes = {round(point.x()) for point in markers.points()}
    assert 1 not in initial_indexes

    x_axis = chart.axes(Qt.Orientation.Horizontal)[0]
    x_axis.setRange(100, 200)
    x_axis.setRange(0, 10)

    assert {round(point.x()) for point in markers.points()} == initial_indexes
    qapp.processEvents()
    assert {round(point.x()) for point in markers.points()} == set(range(11))


def test_plot_selection_marker_is_reused_during_its_click(coordinator):
    panel = CollectionPlotPanel(coordinator=coordinator)
    panel.set_series_collection((_series(),))
    panel.select_record("record-0")
    selection = panel._selection_series  # noqa: SLF001
    assert selection is not None
    chart = panel.chart_view.chart()
    observed = []
    selection.clicked.connect(lambda _point: panel.select_record("record-0"))
    selection.clicked.connect(
        lambda _point: observed.append(
            panel._selection_series is selection and selection.chart() is chart  # noqa: SLF001
        )
    )

    selection.clicked.emit(QPointF(0, 2))

    assert observed == [True]


def test_plot_drag_zoom_and_reset_preserve_chart(coordinator, qapp):
    panel = CollectionPlotPanel(coordinator=coordinator)
    panel.resize(900, 700)
    panel.set_series_collection((_series(),))
    panel.show()
    qapp.processEvents()
    view = panel.chart_view
    chart = view.chart()
    markers = next(
        series for series in chart.series() if isinstance(series, QScatterSeries)
    )
    try:
        for _iteration in range(5):
            plot = chart.plotArea()
            rectangle = QRectF(
                plot.left() + plot.width() * 0.2,
                plot.top() + plot.height() * 0.2,
                plot.width() * 0.5,
                plot.height() * 0.5,
            )
            start = view.mapFromScene(chart.mapToScene(rectangle.topLeft()))
            end = view.mapFromScene(chart.mapToScene(rectangle.bottomRight()))
            QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
            QTest.mouseMove(view.viewport(), end)
            QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)
            qapp.processEvents()
            assert markers.count() == 0
            lines = [
                series for series in chart.series() if isinstance(series, QLineSeries)
            ]
            assert len(lines) == 1
            assert [point.x() for point in lines[0].points()] == [0, 1]

            panel.reset_zoom_button.click()
            qapp.processEvents()
            assert markers.count() == 2
            assert len(chart.series()) == 2
    finally:
        panel.close()
        panel.deleteLater()


@pytest.mark.parametrize(
    ("minimum", "maximum", "expected_line_indexes"),
    [
        (1.2, 3.8, [1, 2, 3, 4]),
        (2.0, 3.0, [1, 2, 3, 4]),
        (-1.0, 0.5, [0, 1]),
        (4.5, 6.0, [4, 5]),
        (-2.0, -1.0, []),
        (6.0, 7.0, []),
    ],
)
def test_plot_lines_extend_to_neighbors_outside_window(
    coordinator, qapp, minimum, maximum, expected_line_indexes
):
    panel = CollectionPlotPanel(coordinator=coordinator)
    panel.set_series_collection((_series(values=tuple(map(float, range(6)))),))
    chart = panel.chart_view.chart()

    chart.axes(Qt.Orientation.Horizontal)[0].setRange(minimum, maximum)
    qapp.processEvents()

    lines = [series for series in chart.series() if isinstance(series, QLineSeries)]
    assert [
        point.x() for line in lines for point in line.points()
    ] == expected_line_indexes
    markers = next(
        series for series in chart.series() if isinstance(series, QScatterSeries)
    )
    assert [point.x() for point in markers.points()] == [
        index for index in range(6) if minimum <= index <= maximum
    ]


def test_plot_boundary_neighbors_preserve_missing_data_gaps(coordinator, qapp):
    panel = CollectionPlotPanel(coordinator=coordinator)
    data = _series(values=tuple(map(float, range(6))))
    data = CollectionSeries(
        data.selector,
        data.labels,
        tuple(point for point in data.points if point.index != 2),
    )
    panel.set_series_collection((data,))
    chart = panel.chart_view.chart()
    axis = chart.axes(Qt.Orientation.Horizontal)[0]

    axis.setRange(0.5, 3.5)
    qapp.processEvents()

    lines = [series for series in chart.series() if isinstance(series, QLineSeries)]
    assert [[point.x() for point in line.points()] for line in lines] == [
        [0, 1],
        [3, 4],
    ]

    axis.setRange(1.5, 2.5)
    qapp.processEvents()
    assert not any(isinstance(series, QLineSeries) for series in chart.series())


@pytest.mark.parametrize("replacement", ["clear", "line", "histogram", "statistics"])
def test_plot_pending_zoom_refresh_handles_chart_replacement(
    coordinator, qapp, replacement
):
    panel = CollectionPlotPanel(coordinator=coordinator)
    panel.set_series_collection((_series(),))
    old_chart = panel.chart_view.chart()
    old_axis = old_chart.axes(Qt.Orientation.Horizontal)[0]
    old_axis.setRange(0.2, 0.7)

    if replacement == "clear":
        panel.clear()
    elif replacement == "line":
        panel.set_series_collection((_series(values=(10.0, 20.0, 30.0)),))
    else:
        panel.plot_mode.setCurrentIndex(panel.plot_mode.findData(replacement))
    expected_series = panel.chart_view.chart().series()
    old_axis.setRange(0.3, 0.6)
    qapp.processEvents()

    assert panel.chart_view.chart().series() == expected_series
    if replacement == "line":
        markers = next(
            series for series in expected_series if isinstance(series, QScatterSeries)
        )
        assert [point.y() for point in markers.points()] == [10.0, 20.0, 30.0]


def test_plot_exports_png(coordinator, tmp_path):
    panel = CollectionPlotPanel(coordinator=coordinator)
    panel.resize(640, 480)
    panel.set_series_collection((_series(),))
    destination = tmp_path / "plot.png"

    panel.export_image(destination)

    assert destination.read_bytes().startswith(b"\x89PNG")


def test_inspector_emits_shared_field_selector_for_plot_and_order(
    loaded_coordinator,
    monkeypatch,
):
    inspector = Inspector(coordinator=loaded_coordinator)
    loaded_coordinator.select_context((1,))
    inspector.on_selected_context_changed((1,))
    output = inspector.summary.model().index_for_value_path(("output",))
    inspector.summary.setCurrentIndex(output.siblingAtColumn(1))
    plotted = []
    ordered = []
    inspector.plot_requested.connect(plotted.append)
    monkeypatch.setattr(
        loaded_coordinator,
        "sort_records_by_field",
        lambda **request: ordered.append(request),
    )

    inspector.plot_button.click()
    inspector.order_ascending_button.click()

    assert plotted == [FieldSelector((1,), ("output",))]
    assert ordered == [
        {"field_selector": plotted[0], "ascending": True},
    ]
