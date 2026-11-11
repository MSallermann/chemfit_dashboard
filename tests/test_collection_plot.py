from __future__ import annotations

import time

from PySide6.QtCharts import QBarSeries, QLogValueAxis, QScatterSeries
from PySide6.QtCore import Qt
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


def test_plot_bounds_rendered_points_for_large_series(coordinator):
    count = MAX_RENDERED_POINTS_PER_SERIES * 2
    panel = CollectionPlotPanel(coordinator=coordinator)

    panel.set_series_collection((_series(values=tuple(map(float, range(count)))),))

    markers = max(
        panel.chart_view.chart().series(),
        key=lambda item: item.count(),
    )
    assert markers.count() <= MAX_RENDERED_POINTS_PER_SERIES


def test_plot_zoom_renders_every_point_in_small_visible_window(coordinator):
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
    x_axis.setRange(0, 10)

    assert {round(point.x()) for point in markers.points()} == set(range(11))


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
