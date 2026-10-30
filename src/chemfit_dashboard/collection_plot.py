from __future__ import annotations

import csv
import math
import re
from collections import defaultdict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCharts import (
    QChart,
    QChartView,
    QLineSeries,
    QLogValueAxis,
    QScatterSeries,
    QValueAxis,
)
from PySide6.QtCore import QEvent, QPointF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter, QPen
from PySide6.QtSvg import QSvgGenerator
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from chemfit_dashboard.appearance import ColorScheme, active_color_scheme
from chemfit_dashboard.semantic import (
    ComponentResolution,
    NodeTrail,
    NodeTrailSegment,
    component_trail_for_path,
    resolve_component_path,
    resolve_component_selection,
)
from chemfit_dashboard.source import ContextDocument, ContextSourceError, NodePath

if TYPE_CHECKING:
    from collections.abc import Iterable

    from chemfit_dashboard.source import RecordRef

JsonPath = tuple[str | int, ...]
RASTER_EXPORT_DPI = 300
SCREEN_DPI = 96

__all__ = [
    "CollectionPlotPanel",
    "CollectionSeries",
    "ComponentResolution",
    "NodeTrail",
    "NodeTrailSegment",
    "NumberSelector",
    "SeriesPoint",
    "build_collection_series",
    "build_collection_series_multi",
    "component_trail_for_path",
    "ordered_records",
    "resolve_component_path",
    "resolve_component_selection",
    "selector_for_document",
]


@dataclass(frozen=True, slots=True)
class NumberSelector:
    """Location of a numeric leaf within one context node."""

    node_path: NodePath
    section: str
    value_path: JsonPath
    label: str
    value_label: str = ""
    node_trail: NodeTrail | None = None


@dataclass(frozen=True, slots=True)
class SeriesPoint:
    index: int
    label: str
    value: float
    record_id: str = ""


@dataclass(frozen=True, slots=True)
class CollectionSeries:
    selector: NumberSelector
    labels: tuple[str, ...]
    points: tuple[SeriesPoint, ...]
    record_ids: tuple[str, ...] = ()

    @property
    def missing_count(self) -> int:
        return len(self.labels) - len(self.points)


def ordered_records(records: Iterable[RecordRef]) -> list[RecordRef]:
    """Sort ChemFit collection records by step and context number."""
    return sorted(records, key=_record_sort_key)


def selector_for_document(
    document: ContextDocument, selector: NumberSelector
) -> NumberSelector:
    """Attach a semantic component trail from the selected document."""
    trail = component_trail_for_path(document, selector.node_path)
    component_label = (
        " / ".join(segment.name for segment in trail) if trail else "Evaluation root"
    )
    value_label = selector.value_label or selector.label
    return replace(
        selector,
        label=f"{component_label} / {value_label}",
        value_label=value_label,
        node_trail=trail,
    )


def build_collection_series(
    selector: NumberSelector,
    records_and_documents: Iterable[tuple[RecordRef, ContextDocument | None]],
) -> CollectionSeries:
    """Resolve one selected numeric leaf across a context collection."""
    labels: list[str] = []
    record_ids: list[str] = []
    points: list[SeriesPoint] = []
    for index, (record, document) in enumerate(records_and_documents):
        labels.append(record.name)
        record_ids.append(record.id)
        if document is None:
            continue
        value = _selected_value(document, selector)
        if _is_plottable_number(value):
            points.append(SeriesPoint(index, record.name, float(value), record.id))
    return CollectionSeries(selector, tuple(labels), tuple(points), tuple(record_ids))


def build_collection_series_multi(
    selectors: Iterable[NumberSelector],
    records_and_documents: Iterable[tuple[RecordRef, ContextDocument | None]],
) -> tuple[CollectionSeries, ...]:
    """Resolve several plot series during one collection traversal."""
    selected = tuple(selectors)
    labels: list[str] = []
    record_ids: list[str] = []
    points: list[list[SeriesPoint]] = [[] for _selector in selected]
    for index, (record, document) in enumerate(records_and_documents):
        labels.append(record.name)
        record_ids.append(record.id)
        if document is None:
            continue
        for selector_index, selector in enumerate(selected):
            value = _selected_value(document, selector)
            if _is_plottable_number(value):
                points[selector_index].append(
                    SeriesPoint(index, record.name, float(value), record.id)
                )
    shared_labels = tuple(labels)
    shared_ids = tuple(record_ids)
    return tuple(
        CollectionSeries(selector, shared_labels, tuple(series_points), shared_ids)
        for selector, series_points in zip(selected, points, strict=True)
    )


class CollectionPlotPanel(QWidget):
    """Interactive multi-series chart across loaded evaluations."""

    context_selected = Signal(str)
    point_selected = Signal(str, object)
    remove_series_requested = Signal(object)
    clear_series_requested = Signal()
    expand_requested = Signal()

    def __init__(self, parent: QWidget | None = None):  # noqa: PLR0915
        """Initialize an empty interactive collection chart."""
        super().__init__(parent)
        layout = QVBoxLayout(self)

        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.selection_label = QLabel("Currently plotting: no value selected")
        self.selection_label.setObjectName("plotSelection")
        self.selection_label.setWordWrap(True)
        layout.addWidget(self.selection_label)
        self.status = QLabel("Select a numeric field in the inspector to plot it.")
        self.status.setObjectName("mutedLabel")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.series_list = QListWidget()
        self.series_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.series_list.setMaximumHeight(110)
        self.series_list.hide()
        layout.addWidget(self.series_list)
        series_controls = QHBoxLayout()
        self.clear_button = QPushButton("Clear plot")
        self.clear_button.setEnabled(False)
        self.clear_button.clicked.connect(
            lambda _checked=False: self.clear_series_requested.emit()
        )
        series_controls.addWidget(self.clear_button)
        series_controls.addStretch()
        self.export_csv_button = QPushButton("Export CSV")
        self.export_csv_button.setEnabled(False)
        self.export_csv_button.clicked.connect(self._choose_csv_export)
        series_controls.addWidget(self.export_csv_button)
        self.export_image_button = QPushButton("Export image")
        self.export_image_button.setEnabled(False)
        self.export_image_button.clicked.connect(self._choose_image_export)
        series_controls.addWidget(self.export_image_button)
        layout.addLayout(series_controls)

        view_controls = QHBoxLayout()
        view_controls.addStretch()
        self.normalize = QCheckBox("Normalize")
        self.normalize.setToolTip("Scale every series independently to the 0-1 range")
        self.normalize.toggled.connect(self._normalize_toggled)
        view_controls.addWidget(self.normalize)
        self.log_y = QCheckBox("Log Y")
        self.log_y.setToolTip("Use a base-10 logarithmic Y-axis")
        self.log_y.toggled.connect(self._log_y_toggled)
        view_controls.addWidget(self.log_y)
        self.reset_zoom_button = QPushButton("Reset zoom")
        self.reset_zoom_button.setText("Fit data")
        self.reset_zoom_button.setToolTip("Restore the full data range")
        self.reset_zoom_button.clicked.connect(self._reset_zoom)
        view_controls.addWidget(self.reset_zoom_button)
        self.expand_button = QPushButton("Expand")
        self.expand_button.setToolTip("Open Plot in a large separate window")
        self.expand_button.clicked.connect(self.expand_requested.emit)
        view_controls.addWidget(self.expand_button)
        layout.addLayout(view_controls)
        self.chart_view = CoordinateChartView()
        self.chart_view.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.chart_view.setRubberBand(
            QChartView.RubberBand.RectangleRubberBand
            | QChartView.RubberBand.ClickThroughRubberBand
        )
        self.chart_view.coordinate_changed.connect(self._cursor_moved)
        layout.addWidget(self.chart_view, 1)
        self.cursor_coordinates = QLabel("Cursor: —")
        self.cursor_coordinates.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        self.cursor_coordinates.setMinimumWidth(0)
        self.cursor_coordinates.setWordWrap(False)
        self.cursor_coordinates.setObjectName("plotCoordinates")
        self.cursor_coordinates.setToolTip(
            "Cursor position in chart coordinates; points include their context name"
        )
        layout.addWidget(self.cursor_coordinates)
        self._labels: tuple[str, ...] = ()
        self._data: tuple[CollectionSeries, ...] = ()
        self._selected_record_id: str | None = None
        self._selection_series: QScatterSeries | None = None
        self._rendered_points: dict[int, list[QPointF]] = {}
        self._rendered_point_labels: dict[tuple[int, float], list[str]] = {}
        self._rendered_point_selectors: dict[
            tuple[int, float], list[NumberSelector]
        ] = {}
        self._hidden_selectors: set[NumberSelector] = set()
        self._scheme = active_color_scheme()
        self._set_empty_chart("Collection plot")

    def show_loading(
        self, selectors: NumberSelector | tuple[NumberSelector, ...], count: int
    ) -> None:
        selected = (selectors,) if isinstance(selectors, NumberSelector) else selectors
        description = (
            selected[0].label if len(selected) == 1 else f"{len(selected)} values"
        )
        self.selection_label.setText(f"Currently plotting: {description}")
        self.status.setText(f"Loading values from {count:,} contexts…")
        self._set_empty_chart("Values across context collection")

    def set_series(self, data: CollectionSeries) -> None:
        self.set_series_collection((data,))

    def set_series_collection(self, data: tuple[CollectionSeries, ...]) -> None:
        self._data = data
        available = {series.selector for series in data}
        self._hidden_selectors.intersection_update(available)
        self.series_list.blockSignals(True)  # noqa: FBT003
        self.series_list.clear()
        for index, series in enumerate(data):
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, series.selector)
            self.series_list.addItem(item)
            row = self._series_row(index, series)
            item.setSizeHint(row.sizeHint())
            self.series_list.setItemWidget(item, row)
        self.series_list.blockSignals(False)  # noqa: FBT003
        self.series_list.setVisible(bool(data))
        enabled = bool(data)
        self.clear_button.setEnabled(enabled)
        self.export_csv_button.setEnabled(enabled)
        self.export_image_button.setEnabled(enabled)
        self._render_series()

    def _render_series(self) -> None:  # noqa: PLR0912, PLR0915
        data_collection = self._data
        if not data_collection:
            return
        data = data_collection[0]
        visible_collection = tuple(
            series
            for series in data_collection
            if series.selector not in self._hidden_selectors
        )
        self._labels = data.labels
        self.selection_label.setText(
            f"Currently plotting: {data.selector.label}"
            if len(data_collection) == 1
            else f"Currently plotting: {len(data_collection)} values"
        )
        if not visible_collection:
            self._rendered_points = {}
            self._rendered_point_labels = {}
            self._rendered_point_selectors = {}
            self._selection_series = None
            self.chart_view.setChart(_base_chart("All series hidden", self._scheme))
            self.status.setText(
                "All series are hidden; check one in the series list to show it."
            )
            self.cursor_coordinates.setText("Cursor: —")
            return
        axis_data = visible_collection[0]
        chart = _base_chart("Values across context collection", self._scheme)
        all_points: list[SeriesPoint] = []
        nonpositive = 0
        self._rendered_points = defaultdict(list)
        self._rendered_point_labels = defaultdict(list)
        self._rendered_point_selectors = defaultdict(list)
        for data_index, series_data in enumerate(data_collection):
            if series_data.selector in self._hidden_selectors:
                continue
            transformed = self._transformed_points(series_data)
            visible = tuple(
                point
                for point in transformed
                if not self.log_y.isChecked() or point.value > 0
            )
            nonpositive += len(transformed) - len(visible)
            all_points.extend(visible)
            series_colors = self._scheme.series
            color = QColor(series_colors[data_index % len(series_colors)])
            markers = QScatterSeries()
            markers.setName(series_data.selector.label)
            markers.setMarkerSize(9.0)
            markers.setColor(color)
            markers.setBorderColor(color.lighter(145))
            marker_points = [QPointF(point.index, point.value) for point in visible]
            markers.replace(marker_points)
            for point in visible:
                self._rendered_points[point.index].append(
                    QPointF(point.index, point.value)
                )
                self._rendered_point_labels[(point.index, point.value)].append(
                    series_data.selector.label
                )
                self._rendered_point_selectors[(point.index, point.value)].append(
                    series_data.selector
                )
            lines: list[QLineSeries] = []
            contiguous: list[SeriesPoint] = []
            for point in visible:
                if contiguous and point.index != contiguous[-1].index + 1:
                    _append_line(chart, lines, contiguous, color)
                    contiguous = []
                contiguous.append(point)
            _append_line(chart, lines, contiguous, color)
            markers.hovered.connect(
                lambda point, state, marker_series=markers, label=series_data.selector.label: self._point_hovered(
                    marker_series, point, state, label
                )
            )
            markers.clicked.connect(
                lambda point, series_data=series_data: self._point_clicked(
                    point, series_data
                )
            )
            chart.addSeries(markers)
            for line in lines:
                legend_markers = chart.legend().markers(line)
                if legend_markers:
                    legend_markers[0].setVisible(False)

        x_axis = QValueAxis()
        x_axis.setTitleText("Context index (hover a point for its file)")
        x_axis.setLabelFormat("%d")
        x_axis.setTickCount(max(2, min(len(data.labels), 11)))
        x_axis.setRange(0, max(1, len(data.labels) - 1))
        axis_label = (
            "Normalized value (0-1)"
            if self.normalize.isChecked()
            else axis_data.selector.value_label or axis_data.selector.label
        )
        if len(visible_collection) > 1 and not self.normalize.isChecked():
            axis_label = "Value (multiple fields)"
        if self.log_y.isChecked():
            y_axis: QValueAxis | QLogValueAxis = QLogValueAxis()
            y_axis.setBase(10)
            y_axis.setLabelFormat("%.3g")
            y_axis.setTitleText(f"{axis_label} (log scale)")
            if all_points:
                minimum = min(point.value for point in all_points)
                maximum = max(point.value for point in all_points)
                lower_power = max(-323, math.floor(math.log10(minimum)))
                upper_power = min(308, math.ceil(math.log10(maximum)))
                if lower_power == upper_power:
                    lower_power = max(-323, lower_power - 1)
                    upper_power = min(308, upper_power + 1)
                y_axis.setRange(10.0**lower_power, 10.0**upper_power)
        else:
            y_axis = QValueAxis()
            y_axis.setTitleText(axis_label)
            if all_points:
                minimum = min(point.value for point in all_points)
                maximum = max(point.value for point in all_points)
                padding = (maximum - minimum) * 0.08 or max(abs(minimum) * 0.08, 0.5)
                y_axis.setRange(minimum - padding, maximum + padding)
                y_axis.applyNiceNumbers()
        for axis in (x_axis, y_axis):
            axis.setLabelsBrush(QColor(self._scheme.text))
            axis.setTitleBrush(QColor(self._scheme.text))
            axis.setGridLineColor(QColor(self._scheme.border))
            chart.addAxis(
                axis,
                (
                    Qt.AlignmentFlag.AlignBottom
                    if axis is x_axis
                    else Qt.AlignmentFlag.AlignLeft
                ),
            )
        for chart_series in chart.series():
            chart_series.attachAxis(x_axis)
            chart_series.attachAxis(y_axis)
        chart.legend().setVisible(len(visible_collection) > 1)
        chart.legend().setLabelColor(QColor(self._scheme.text))
        self.chart_view.setChart(chart)
        self._selection_series = None
        self._update_selection_marker()

        missing = sum(series.missing_count for series in visible_collection)
        notes = []
        if missing:
            notes.append(f"{missing:,} missing or nonnumeric values")
        if nonpositive:
            notes.append(f"{nonpositive:,} nonpositive omitted for log scale")
        suffix = f" · {' · '.join(notes)}" if notes else ""
        self.status.setText(
            f"{len(all_points):,} points across {len(data.labels):,} contexts{suffix}"
            if len(visible_collection) > 1
            else f"{len(all_points):,} of {len(data.labels):,} contexts in collection{suffix}"
        )

    def show_error(self, message: str) -> None:
        self.status.setText(message)

    def clear(self) -> None:
        """Remove every plotted series and restore the empty state."""
        self._data = ()
        self._hidden_selectors.clear()
        self.series_list.clear()
        self.series_list.hide()
        self.clear_button.setEnabled(False)
        self.export_csv_button.setEnabled(False)
        self.export_image_button.setEnabled(False)
        self.selection_label.setText("Currently plotting: no value selected")
        self.status.setText("Select a numeric field in the inspector to plot it.")
        self.cursor_coordinates.setText("Cursor: —")
        self._set_empty_chart("Collection plot")

    def set_expanded(self, expanded: bool) -> None:
        """Reflect whether the plot is hosted in the expanded window."""
        self.expand_button.setText("Collapse" if expanded else "Expand")
        self.expand_button.setToolTip(
            "Return Plot to the dashboard"
            if expanded
            else "Open Plot in a large separate window"
        )

    def refresh_fonts(self) -> None:
        """Apply the current application font to non-widget chart text."""
        application = QApplication.instance()
        if not isinstance(application, QApplication):
            return
        font = application.font()
        chart = self.chart_view.chart()
        chart.setTitleFont(font)
        for axis in chart.axes():
            axis.setLabelsFont(font)
            axis.setTitleFont(font)

    def _log_y_toggled(self, _checked: bool) -> None:
        self._render_series()

    def _normalize_toggled(self, checked: bool) -> None:
        if checked and self.log_y.isChecked():
            self.log_y.setChecked(False)
        self.log_y.setEnabled(not checked)
        self._render_series()

    def _reset_zoom(self) -> None:
        self.chart_view.chart().zoomReset()

    def _series_row(self, index: int, series: CollectionSeries) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(5, 2, 3, 2)
        layout.setSpacing(7)
        swatch = QLabel()
        swatch.setObjectName("seriesColorSwatch")
        swatch.setFixedSize(20, 20)
        swatch.setProperty("seriesIndex", index)
        self._set_swatch_color(swatch, index)
        layout.addWidget(swatch)
        visible = QCheckBox(series.selector.label)
        visible.setObjectName("seriesVisibility")
        visible.setChecked(series.selector not in self._hidden_selectors)
        visible.setToolTip("Show or hide this series without removing it")
        visible.toggled.connect(
            lambda checked, selector=series.selector: self._set_series_visible(
                selector, checked
            )
        )
        layout.addWidget(visible, 1)
        remove = QToolButton()
        remove.setObjectName("removeSeriesAction")
        remove.setText("\N{MULTIPLICATION SIGN}")
        remove.setToolTip(f"Remove {series.selector.label} from the plot")
        remove.clicked.connect(
            lambda _checked=False, selector=series.selector: (
                self.remove_series_requested.emit(selector)
            )
        )
        layout.addWidget(remove)
        return row

    def _set_series_visible(self, selector: NumberSelector, visible: bool) -> None:
        if visible:
            self._hidden_selectors.discard(selector)
        else:
            self._hidden_selectors.add(selector)
        self._render_series()

    def _set_swatch_color(self, swatch: QLabel, index: int) -> None:
        color = self._scheme.series[index % len(self._scheme.series)]
        swatch.setStyleSheet(
            f"background: {color}; border: 1px solid {self._scheme.text_strong}; "
            "border-radius: 5px;"
        )

    def _cursor_moved(self, point: QPointF | None) -> None:
        if point is None or not self.chart_view.chart().axes():
            self.cursor_coordinates.setText("Cursor: —")
            return
        index = round(point.x())
        context = (
            f"Context {index:,} · {self._labels[index]} · "
            if 0 <= index < len(self._labels)
            else ""
        )
        self.cursor_coordinates.setText(
            f"{context}X: {point.x():.2e} · Y: {point.y():.2e}"
        )

    def _set_empty_chart(self, title: str) -> None:
        self._labels = ()
        self._rendered_points = {}
        self._rendered_point_labels = {}
        self._rendered_point_selectors = {}
        self._selection_series = None
        self.cursor_coordinates.setText("Cursor: —")
        self.chart_view.setChart(_base_chart(title, self._scheme))

    def select_record(self, record_id: str | None) -> None:
        """Highlight the selected evaluation without rebuilding the chart."""
        self._selected_record_id = record_id
        self._update_selection_marker()

    def csv_column_names(self) -> tuple[str, ...]:
        """Return the default headers for the current wide CSV export."""
        return (
            "context_index",
            "context",
            "record_id",
            *(series.selector.label for series in self._data),
        )

    def export_csv(
        self, path: str | Path, column_names: tuple[str, ...] | None = None
    ) -> None:
        """Export the current plot as a wide, collection-ordered CSV table."""
        if not self._data:
            return
        requested_headers = (
            column_names if column_names is not None else self.csv_column_names()
        )
        headers = _validated_csv_column_names(requested_headers, 3 + len(self._data))
        first = self._data[0]
        values = [
            {point.index: point.value for point in series.points}
            for series in self._data
        ]
        with Path(path).open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(headers)
            for index, label in enumerate(first.labels):
                record_id = (
                    first.record_ids[index] if index < len(first.record_ids) else ""
                )
                writer.writerow(
                    [index, label, record_id]
                    + [series_values.get(index, "") for series_values in values]
                )

    def export_image(self, path: str | Path) -> None:
        """Render the current chart view to PNG or SVG based on its suffix."""
        destination = Path(path)
        if destination.suffix.casefold() == ".svg":
            generator = QSvgGenerator()
            generator.setFileName(str(destination))
            generator.setSize(self.chart_view.size())
            generator.setViewBox(self.chart_view.rect())
            generator.setResolution(RASTER_EXPORT_DPI)
            painter = QPainter(generator)
            self.chart_view.render(painter)
            painter.end()
            return
        scale = RASTER_EXPORT_DPI / SCREEN_DPI
        source_size = self.chart_view.size()
        image = QImage(
            max(1, round(source_size.width() * scale)),
            max(1, round(source_size.height() * scale)),
            QImage.Format.Format_ARGB32,
        )
        dots_per_meter = round(RASTER_EXPORT_DPI / 0.0254)
        image.setDotsPerMeterX(dots_per_meter)
        image.setDotsPerMeterY(dots_per_meter)
        image.fill(QColor(self._scheme.chart))
        painter = QPainter(image)
        painter.scale(scale, scale)
        self.chart_view.render(painter)
        painter.end()
        image.save(str(destination))

    def _transformed_points(self, data: CollectionSeries) -> tuple[SeriesPoint, ...]:
        if not self.normalize.isChecked() or not data.points:
            return data.points
        minimum = min(point.value for point in data.points)
        maximum = max(point.value for point in data.points)
        span = maximum - minimum
        return tuple(
            replace(point, value=(point.value - minimum) / span if span else 0.5)
            for point in data.points
        )

    def _point_clicked(self, point: QPointF, data: CollectionSeries) -> None:
        index = round(point.x())
        if 0 <= index < len(data.record_ids):
            record_id = data.record_ids[index]
            self.context_selected.emit(record_id)
            self.point_selected.emit(record_id, data.selector)

    def _choose_csv_export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Export collection plot data", "collection_plot.csv", "CSV (*.csv)"
        )
        if not path:
            return
        if not Path(path).suffix:
            path += ".csv"
        column_names = self._prompt_csv_column_names()
        if column_names is not None:
            self.export_csv(path, column_names)

    def _prompt_csv_column_names(self) -> tuple[str, ...] | None:
        dialog = QDialog(self)
        dialog.setWindowTitle("CSV column names")
        layout = QVBoxLayout(dialog)
        explanation = QLabel(
            "Rename the columns written to the wide CSV file. Names must be "
            "non-empty and unique."
        )
        explanation.setWordWrap(True)
        explanation.setObjectName("mutedLabel")
        layout.addWidget(explanation)
        form = QFormLayout()
        defaults = self.csv_column_names()
        descriptions = ["Context index", "Context filename", "Record ID"] + [
            f"Series {index}: {series.selector.label}"
            for index, series in enumerate(self._data, start=1)
        ]
        editors: list[QLineEdit] = []
        for description, default in zip(descriptions, defaults, strict=True):
            editor = QLineEdit(default)
            editor.selectAll()
            form.addRow(description, editor)
            editors.append(editor)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        while dialog.exec() == QDialog.DialogCode.Accepted:
            names = tuple(editor.text().strip() for editor in editors)
            try:
                return _validated_csv_column_names(names, len(defaults))
            except ValueError as exc:
                QMessageBox.warning(dialog, "Invalid CSV column names", str(exc))
        return None

    def set_color_scheme(self, scheme: ColorScheme) -> None:
        """Apply a scheme to chart elements painted outside Qt stylesheets."""
        self._scheme = scheme
        for index in range(self.series_list.count()):
            row = self.series_list.itemWidget(self.series_list.item(index))
            if row is None:
                continue
            swatch = row.findChild(QLabel, "seriesColorSwatch")
            if swatch is not None:
                self._set_swatch_color(swatch, index)
        if self._data:
            self._render_series()
        else:
            self._set_empty_chart("Collection plot")

    def _choose_image_export(self) -> None:
        path, selected_filter = QFileDialog.getSaveFileName(
            self,
            "Export collection plot image",
            "collection_plot.png",
            "PNG image (*.png);;SVG image (*.svg)",
        )
        if not path:
            return
        if "SVG" in selected_filter and not path.casefold().endswith(".svg"):
            path += ".svg"
        elif not Path(path).suffix:
            path += ".png"
        self.export_image(path)

    def _update_selection_marker(self) -> None:
        chart = self.chart_view.chart()
        if self._selection_series is not None:
            chart.removeSeries(self._selection_series)
            self._selection_series = None
        if self._selected_record_id is None or not self._data:
            return
        record_ids = self._data[0].record_ids
        try:
            index = record_ids.index(self._selected_record_id)
        except ValueError:
            return
        points = self._rendered_points.get(index, [])
        if not points or not chart.axes():
            return
        selection = QScatterSeries()
        selection.setName("Selected context")
        selection.setMarkerSize(15.0)
        selection.setColor(QColor(self._scheme.danger))
        selection.setBorderColor(QColor(self._scheme.danger_border))
        selection.replace(points)
        selection.hovered.connect(
            lambda point, state, marker_series=selection: self._selection_point_hovered(
                marker_series, point, state
            )
        )
        selection.clicked.connect(self._selection_point_clicked)
        chart.addSeries(selection)
        horizontal = chart.axes(Qt.Orientation.Horizontal)
        vertical = chart.axes(Qt.Orientation.Vertical)
        if horizontal:
            selection.attachAxis(horizontal[0])
        if vertical:
            selection.attachAxis(vertical[0])
        markers = chart.legend().markers(selection)
        if markers:
            markers[0].setVisible(False)
        self._selection_series = selection

    def _selection_point_clicked(self, point: QPointF) -> None:
        if not self._data:
            return
        index = round(point.x())
        record_ids = self._data[0].record_ids
        if not 0 <= index < len(record_ids):
            return
        selectors = self._rendered_point_selectors.get((index, point.y()), ())
        selector = selectors[0] if selectors else self._data[0].selector
        record_id = record_ids[index]
        self.context_selected.emit(record_id)
        self.point_selected.emit(record_id, selector)

    def _selection_point_hovered(
        self, series: QScatterSeries, point: QPointF, state: bool
    ) -> None:
        labels = self._rendered_point_labels.get((round(point.x()), point.y()), ())
        series_label = " · ".join(labels) if labels else "Selected context"
        self._point_hovered(series, point, state, series_label)

    def _point_hovered(
        self,
        series: QScatterSeries,
        point: QPointF,
        state: bool,
        series_label: str = "",
    ) -> None:
        if not state:
            QToolTip.hideText()
            return
        index = round(point.x())
        label = self._labels[index] if 0 <= index < len(self._labels) else str(index)
        chart_position = self.chart_view.chart().mapToPosition(point, series)
        viewport_position = self.chart_view.mapFromScene(chart_position)
        QToolTip.showText(
            self.chart_view.mapToGlobal(viewport_position),
            f"{label}\n{series_label}\n{point.y():.12g}",
            self.chart_view,
        )
        self.cursor_coordinates.setText(
            f"Context {index:,} · {label} · {series_label} · "
            f"X: {point.x():.2e} · Y: {point.y():.2e}"
        )


class CoordinateChartView(QChartView):
    """Chart view that exposes the current mouse position in axis coordinates."""

    coordinate_changed = Signal(object)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        chart = self.chart()
        scene_position = self.mapToScene(event.position().toPoint())
        chart_position = chart.mapFromScene(scene_position)
        self.coordinate_changed.emit(chart.mapToValue(chart_position))
        super().mouseMoveEvent(event)

    def leaveEvent(self, event: QEvent) -> None:  # noqa: N802
        self.coordinate_changed.emit(None)
        super().leaveEvent(event)


def _selected_value(document: ContextDocument, selector: NumberSelector) -> Any:
    node_path = _resolve_node_path(document, selector)
    if node_path is None:
        return None
    try:
        details = document.details(node_path)
    except ContextSourceError:
        return None
    section_value = {
        "fields": details.fields,
        "metadata": details.metadata,
        "structure": details.structure,
        "raw": details.raw,
    }.get(selector.section)
    value: Any = section_value
    for segment in selector.value_path:
        if isinstance(segment, str) and isinstance(value, dict):
            if segment not in value:
                return None
            value = value[segment]
        elif isinstance(segment, int) and isinstance(value, list):
            if not 0 <= segment < len(value):
                return None
            value = value[segment]
        else:
            return None
    return value


def _resolve_node_path(
    document: ContextDocument, selector: NumberSelector
) -> NodePath | None:
    if selector.node_trail is None:
        try:
            document.details(selector.node_path)
        except ContextSourceError:
            return None
        return selector.node_path

    return resolve_component_path(document, selector.node_path, selector.node_trail)


def _is_plottable_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _record_sort_key(record: RecordRef) -> tuple[str, float, float, str]:
    match = re.fullmatch(r"step_(\d+)_ctx_(\d+)\.json", record.name)
    if match is None:
        return (str(record.path.parent).casefold(), math.inf, math.inf, record.name)
    return (
        str(record.path.parent).casefold(),
        int(match.group(1)),
        int(match.group(2)),
        record.name,
    )


def _validated_csv_column_names(
    names: tuple[str, ...], expected_count: int
) -> tuple[str, ...]:
    if len(names) != expected_count:
        message = f"Expected {expected_count} column names, received {len(names)}."
        raise ValueError(message)
    cleaned = tuple(name.strip() for name in names)
    if any(not name for name in cleaned):
        message = "Column names cannot be empty."
        raise ValueError(message)
    folded = [name.casefold() for name in cleaned]
    if len(set(folded)) != len(folded):
        message = "Column names must be unique."
        raise ValueError(message)
    return cleaned


def _base_chart(title: str, scheme: ColorScheme) -> QChart:
    chart = QChart()
    chart.setTitle(title)
    chart.setBackgroundBrush(QColor(scheme.chart))
    chart.setPlotAreaBackgroundBrush(QColor(scheme.chart))
    chart.setPlotAreaBackgroundVisible(True)
    chart.setTitleBrush(QColor(scheme.text))
    return chart


def _append_line(
    chart: QChart,
    lines: list[QLineSeries],
    points: list[SeriesPoint],
    color: QColor,
) -> None:
    if len(points) < 2:
        return
    line = QLineSeries()
    line.setPen(QPen(color, 2.0))
    line.replace([QPointF(point.index, point.value) for point in points])
    chart.addSeries(line)
    lines.append(line)
