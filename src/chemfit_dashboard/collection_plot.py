"""
Collection-series construction and interactive Qt chart presentation.

Selectors identify numeric leaves semantically across evaluations. The pure
series builders resolve those selectors; ``CollectionPlotPanel`` renders line,
histogram, and statistics modes and emits record-selection requests.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCharts import (
    QBarCategoryAxis,
    QBarSeries,
    QBarSet,
    QChart,
    QChartView,
    QLineSeries,
    QLogValueAxis,
    QScatterSeries,
    QValueAxis,
)
from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QImage,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPen,
    QShortcut,
    QWheelEvent,
)
from PySide6.QtSvg import QSvgGenerator
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QRubberBand,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QToolTip,
    QWidget,
)

from chemfit_dashboard.appearance import ColorScheme, active_color_scheme
from chemfit_dashboard.chart_style import (
    create_chart,
    finish_cartesian_chart,
    series_color,
)
from chemfit_dashboard.context_document import ContextDocument
from chemfit_dashboard.coordinator import DashboardCoordinator
from chemfit_dashboard.extraction import FieldSelector, extract_fields
from chemfit_dashboard.icons import make_icon_only, set_themed_icon
from chemfit_dashboard.ui_components import (
    ActionRow,
    expand_button,
    interactive_zoom_factor,
    muted_label,
    panel_layout,
    set_expand_button_state,
)
from chemfit_dashboard.util import format_combined_path

if TYPE_CHECKING:
    from collections.abc import Sequence

JsonPath = tuple[str | int, ...]
RASTER_EXPORT_DPI = 300
SCREEN_DPI = 96
MAX_RENDERED_POINTS_PER_SERIES = 4_000


@dataclass(frozen=True, slots=True)
class SeriesPoint:
    """One available numeric value and its original collection position."""

    index: int
    label: str
    value: float
    record_id: str = ""


@dataclass(frozen=True, slots=True)
class CollectionSeries:
    """Resolved values and missing-value context for one numeric selector."""

    selector: FieldSelector
    labels: tuple[str, ...]
    points: tuple[SeriesPoint, ...]
    record_ids: tuple[str, ...] = ()

    @property
    def missing_count(self) -> int:
        """Return evaluations for which the selector had no numeric value."""
        return len(self.labels) - len(self.points)


@dataclass(frozen=True, slots=True)
class _PresentedSeries:
    """A series paired with its stable position in the complete plot."""

    index: int
    data: CollectionSeries


@dataclass(slots=True)
class _RenderedPointLookup:
    """
    Keep the three indexes for rendered points in lockstep.

    The chart may sample a large collection before drawing it. Selection and
    hover handling must therefore consult the points that were actually drawn,
    not the complete source series. Keeping these indexes behind one object
    makes rebuilding or clearing only part of that derived state impossible.
    """

    points_by_index: dict[int, list[QPointF]] = field(
        default_factory=lambda: defaultdict(list)
    )
    labels_by_coordinate: dict[tuple[int, float], list[str]] = field(
        default_factory=lambda: defaultdict(list)
    )
    selectors_by_coordinate: dict[tuple[int, float], list[FieldSelector]] = field(
        default_factory=lambda: defaultdict(list)
    )

    def clear(self) -> None:
        """Discard every index when the chart no longer represents data."""
        self.points_by_index.clear()
        self.labels_by_coordinate.clear()
        self.selectors_by_coordinate.clear()

    def add(
        self,
        point: SeriesPoint,
        rendered_point: QPointF,
        selector: FieldSelector,
    ) -> None:
        """Index one point using the coordinate identity used by Qt Charts."""
        coordinate = (point.index, point.value)
        self.points_by_index[point.index].append(rendered_point)
        self.labels_by_coordinate[coordinate].append(
            format_combined_path(selector.node_path, selector.value_path)
        )
        self.selectors_by_coordinate[coordinate].append(selector)


class CollectionPlotPanel(QWidget):
    """
    Interactive multi-series chart across loaded evaluations.

    The panel owns resolved immutable series, visibility/color choices, plot
    mode, and the selected-record marker. MainWindow remains responsible for
    loading series and translating point clicks into document navigation.
    """

    context_selected = Signal(str)
    point_selected = Signal(str, object)
    expand_requested = Signal()
    status_message = Signal(str)

    def __init__(  # noqa: PLR0915
        self, *, coordinator: DashboardCoordinator, parent: QWidget | None = None
    ):
        """Initialize an empty interactive collection chart."""
        super().__init__(parent)
        self.coordinator = coordinator
        layout = panel_layout(self)
        self.selection_label = QLabel("Currently plotting: no value selected")
        self.selection_label.setObjectName("plotSelection")
        self.selection_label.setWordWrap(True)
        layout.addWidget(self.selection_label)
        self.status = muted_label(
            "Select a numeric field in the inspector to plot it.", word_wrap=True
        )
        layout.addWidget(self.status)
        self.series_list = QListWidget()
        self.series_list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.series_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.series_list.customContextMenuRequested.connect(
            self._show_series_context_menu
        )
        self._series_copy_shortcut = QShortcut(
            QKeySequence.StandardKey.Copy, self.series_list
        )
        self._series_copy_shortcut.setContext(
            Qt.ShortcutContext.WidgetWithChildrenShortcut
        )
        self._series_copy_shortcut.activated.connect(self._copy_current_series_path)
        self.series_list.setMaximumHeight(110)
        self.series_list.hide()
        layout.addWidget(self.series_list)
        series_controls = QHBoxLayout()
        self.clear_button = QPushButton("Clear plot")
        make_icon_only(
            self.clear_button, "trash", "Clear every plotted series", role="danger"
        )
        self.clear_button.setEnabled(False)
        self.clear_button.clicked.connect(lambda _checked=False: self.clear())
        series_controls.addWidget(self.clear_button)
        series_controls.addStretch()
        self.export_button = QToolButton()
        self.export_button.setText("Export")
        self.export_button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self.export_button.setProperty("buttonLike", "true")
        self.export_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        set_themed_icon(self.export_button, "save")
        export_menu = QMenu(self.export_button)
        export_image = export_menu.addAction("Image…")
        export_image.triggered.connect(self._choose_image_export)
        self.export_button.setMenu(export_menu)
        self.export_button.setEnabled(False)
        series_controls.addWidget(self.export_button)
        layout.addLayout(series_controls)

        view_controls = QHBoxLayout()
        self.plot_mode = QComboBox()
        self.plot_mode.addItem("Line", "line")
        self.plot_mode.addItem("Histogram", "histogram")
        self.plot_mode.addItem("Statistics", "statistics")
        self.plot_mode.setToolTip("Choose a line plot, binned histogram, or statistics")
        self.plot_mode.currentIndexChanged.connect(self._plot_mode_changed)
        view_controls.addWidget(self.plot_mode)
        self.histogram_bins = QSpinBox()
        self.histogram_bins.setRange(1, 100)
        self.histogram_bins.setValue(30)
        self.histogram_bins.setPrefix("Bins: ")
        self.histogram_bins.setToolTip("Number of equal-width histogram bins")
        self.histogram_bins.valueChanged.connect(lambda _value: self._render_series())
        self.histogram_bins.hide()
        view_controls.addWidget(self.histogram_bins)
        self.histogram_opacity = QSpinBox()
        self.histogram_opacity.setRange(0, 100)
        self.histogram_opacity.setValue(70)
        self.histogram_opacity.setPrefix("Opacity: ")
        self.histogram_opacity.setSuffix("%")
        self.histogram_opacity.setToolTip(
            "Opacity of histogram bar fills; borders remain more prominent"
        )
        self.histogram_opacity.valueChanged.connect(
            lambda _value: self._render_series()
        )
        self.histogram_opacity.hide()
        view_controls.addWidget(self.histogram_opacity)
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
        make_icon_only(self.reset_zoom_button, "fit", "Restore the full data range")
        self.reset_zoom_button.clicked.connect(self._reset_zoom)
        view_controls.addWidget(self.reset_zoom_button)
        self.expand_button = expand_button("Plot")
        self.expand_button.clicked.connect(self.expand_requested.emit)
        view_controls.addWidget(self.expand_button)
        layout.addLayout(view_controls)
        self.chart_view = CoordinateChartView()
        self.chart_view.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.chart_view.setInteractive(True)
        self.chart_view.coordinate_changed.connect(self._cursor_moved)
        layout.addWidget(self.chart_view, 1)
        self.statistics_table = QTableWidget(0, 7)
        self.statistics_table.setHorizontalHeaderLabels(
            ("Series", "Count", "Missing", "Minimum", "Maximum", "Mean", "Std dev")
        )
        self.statistics_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.statistics_table.hide()
        layout.addWidget(self.statistics_table, 1)
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
        self._point_lookup = _RenderedPointLookup()
        self._hidden_selectors: set[FieldSelector] = set()
        self._scheme = active_color_scheme()
        self._set_empty_chart("Collection plot")

        self._requested_selectors: tuple[FieldSelector, ...] = ()
        self._pending_point_selection = None
        self.point_selected.connect(self._select_point)

        self.coordinator.selected_record_changed.connect(self._on_record_changed)
        self.coordinator.collection_order_changed.connect(self.refresh_series)
        self.coordinator.collection_changed.connect(
            lambda _records: self.refresh_series()
        )

    def show_loading(
        self, selectors: FieldSelector | tuple[FieldSelector, ...], count: int
    ) -> None:
        """Present a loading state without discarding the active selectors."""
        selected = (selectors,) if isinstance(selectors, FieldSelector) else selectors
        description = (
            selected[0].format() if len(selected) == 1 else f"{len(selected)} values"
        )
        self.selection_label.setText(f"Currently plotting: {description}")
        self.status.setText(f"Loading values from {count:,} contexts…")
        self._set_empty_chart("Values across context collection")

    def remove_series(self, selector: FieldSelector) -> None:
        """Remove a series."""

        self._requested_selectors = tuple(
            s for s in self._requested_selectors if s != selector
        )

        if not self._requested_selectors:
            self.clear()
            return

        remaining = tuple(
            series for series in self._data if series.selector != selector
        )

        if remaining:
            self.set_series_collection(remaining)

    def add_series(self, selector: FieldSelector) -> None:
        if selector not in self._requested_selectors:
            self.build_series((*self._requested_selectors, selector))

    def refresh_series(self) -> None:
        """Rebuild the active plot after collection changes."""

        if self._requested_selectors:
            self.build_series(self._requested_selectors)

    def build_series(
        self,
        selectors: Sequence[FieldSelector],
    ) -> None:
        """Extract multiple numeric series in a single collection traversal."""

        selectors = tuple(selectors)
        self._requested_selectors = selectors

        records = tuple(self.coordinator.collection)

        if not selectors or not records:
            self.clear()
            return

        def task():
            labels = []
            record_ids = []
            points = [[] for _ in selectors]
            for row in extract_fields(self.coordinator.source, records, selectors):
                labels.append(row.filename)
                record_ids.append(row.record_id)

                for index, extracted in enumerate(row.values):
                    if not extracted.is_numeric:
                        continue

                    value = float(extracted.value)
                    if not math.isfinite(value):
                        continue

                    points[index].append(
                        SeriesPoint(
                            index=row.index,
                            label=row.filename,
                            value=value,
                            record_id=row.record_id,
                        )
                    )

            return tuple(
                CollectionSeries(
                    selector=selector,
                    labels=tuple(labels),
                    points=tuple(series_points),
                    record_ids=tuple(record_ids),
                )
                for selector, series_points in zip(selectors, points, strict=True)
            )

        def on_success(data: tuple[CollectionSeries, ...]) -> None:
            requested = set(self._requested_selectors)

            remaining = tuple(series for series in data if series.selector in requested)

            if remaining:
                self.set_series_collection(remaining)

        def on_error(details: str):
            self.show_error(details)

        self.show_loading(selectors, len(records))

        self.coordinator.task_runner.submit_latest(
            key="collection_plot",
            function=task,
            on_success=on_success,
            on_error=on_error,
        )

    def set_series_collection(self, data: tuple[CollectionSeries, ...]) -> None:
        """Install resolved series and rebuild the active visualization mode."""
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
        self.export_button.setEnabled(enabled)
        self._render_series()

    def _render_series(self) -> None:  # noqa: PLR0912, PLR0915
        data_collection = self._data
        if not data_collection:
            return
        data = data_collection[0]
        visible_series = self._visible_series()
        mode = self.plot_mode.currentData()
        self._apply_mode_controls(mode)
        self.selection_label.setText(
            f"Currently plotting: {data.selector.format()}"
            if len(data_collection) == 1
            else f"Currently plotting: {len(data_collection)} values"
        )
        if not visible_series:
            self._show_all_series_hidden(mode)
            return
        if mode == "statistics":
            self._render_statistics(visible_series)
            return
        if mode == "histogram":
            self._render_histogram(visible_series)
            return
        self._labels = data.labels
        visible_collection = tuple(item.data for item in visible_series)
        axis_data = visible_collection[0]
        chart = create_chart("Values across context collection", self._scheme)
        all_points: list[SeriesPoint] = []
        nonpositive = 0
        self._point_lookup.clear()
        for presented in visible_series:
            data_index = presented.index
            series_data = presented.data
            transformed = self._transformed_points(series_data)
            visible = tuple(
                point
                for point in transformed
                if not self.log_y.isChecked() or point.value > 0
            )
            nonpositive += len(transformed) - len(visible)
            all_points.extend(visible)
            rendered = _sample_points(visible, MAX_RENDERED_POINTS_PER_SERIES)
            color = series_color(self._scheme, data_index)
            markers = QScatterSeries()
            markers.setName(series_data.selector.format())
            markers.setMarkerSize(9.0)
            markers.setColor(color)
            markers.setBorderColor(color.lighter(145))
            marker_points = [QPointF(point.index, point.value) for point in rendered]
            markers.replace(marker_points)
            for point in rendered:
                self._point_lookup.add(
                    point,
                    QPointF(point.index, point.value),
                    series_data.selector,
                )
            lines: list[QLineSeries] = []
            contiguous: list[SeriesPoint] = []
            run_by_index: dict[int, int] = {}
            run = 0
            previous_index: int | None = None
            for point in visible:
                if previous_index is not None and point.index != previous_index + 1:
                    run += 1
                run_by_index[point.index] = run
                previous_index = point.index
            for point in rendered:
                if (
                    contiguous
                    and run_by_index[point.index] != run_by_index[contiguous[-1].index]
                ):
                    _append_line(chart, lines, contiguous, color)
                    contiguous = []
                contiguous.append(point)
            _append_line(chart, lines, contiguous, color)
            label = series_data.selector.format()
            markers.hovered.connect(
                lambda point,
                state,
                marker_series=markers,
                label=label: self._point_hovered(marker_series, point, state, label)
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
            else axis_data.selector.format() or axis_data.selector.format()
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
        finish_cartesian_chart(
            chart,
            x_axis,
            y_axis,
            self._scheme,
            legend_entries=len(visible_series),
        )
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
        rendered_count = sum(
            min(len(series.points), MAX_RENDERED_POINTS_PER_SERIES)
            for series in visible_collection
        )
        if rendered_count < len(all_points):
            suffix += f" · {rendered_count:,} representative points rendered"
        self.status.setText(
            f"{len(all_points):,} points across {len(data.labels):,} contexts{suffix}"
            if len(visible_collection) > 1
            else f"{len(all_points):,} of {len(data.labels):,} contexts in collection{suffix}"
        )

    def show_error(self, message: str) -> None:
        """Clear rendered chart data and show a user-facing load error."""
        self.status.setText(message)

    def clear(self) -> None:
        """Remove every plotted series and restore the empty state."""

        self.coordinator.task_runner.invalidate("collection_plot")
        self._requested_selectors = ()

        self._data = ()
        self._hidden_selectors.clear()
        self.series_list.clear()
        self.series_list.hide()
        self.clear_button.setEnabled(False)
        self.export_button.setEnabled(False)
        self.selection_label.setText("Currently plotting: no value selected")
        self.status.setText("Select a numeric field in the inspector to plot it.")
        self.cursor_coordinates.setText("Cursor: —")
        self._set_empty_chart("Collection plot")

    def set_expanded(self, expanded: bool) -> None:
        """Reflect whether the plot is hosted in the expanded window."""
        set_expand_button_state(self.expand_button, "Plot", expanded=expanded)

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

    def _plot_mode_changed(self, _index: int) -> None:
        self._render_series()

    def _visible_series(self) -> tuple[_PresentedSeries, ...]:
        """Return visible data with its immutable full-collection palette index."""
        return tuple(
            _PresentedSeries(index, series)
            for index, series in enumerate(self._data)
            if series.selector not in self._hidden_selectors
        )

    def _apply_mode_controls(self, mode: str) -> None:
        """Apply the complete control-availability policy for one plot mode."""
        is_histogram = mode == "histogram"
        is_statistics = mode == "statistics"
        self.chart_view.setVisible(not is_statistics)
        self.statistics_table.setVisible(is_statistics)
        self.histogram_bins.setVisible(is_histogram)
        self.histogram_opacity.setVisible(is_histogram)
        self.normalize.setEnabled(not is_statistics)
        self.normalize.setToolTip(
            "Scale each series' bin counts to sum to 1"
            if is_histogram
            else "Scale every series independently to the 0-1 value range"
        )
        self.log_y.setEnabled(mode == "line" and not self.normalize.isChecked())
        self.reset_zoom_button.setEnabled(not is_statistics)
        if mode != "line":
            self._labels = ()
            self._selection_series = None
            self.cursor_coordinates.setText("Cursor: —")

    def _show_all_series_hidden(self, mode: str) -> None:
        """Present one shared all-hidden state in every plot mode."""
        if mode == "statistics":
            self.statistics_table.setRowCount(0)
        else:
            self._set_empty_chart("All series hidden")
        self.status.setText(
            "All series are hidden; check one in the series list to show it."
        )

    def _render_statistics(self, visible_series: tuple[_PresentedSeries, ...]) -> None:
        self.statistics_table.setRowCount(len(visible_series))
        for row, presented in enumerate(visible_series):
            series = presented.data
            values = [point.value for point in series.points]
            cells: tuple[str, ...] = (
                series.selector.format(),
                f"{len(values):,}",
                f"{series.missing_count:,}",
                f"{min(values):.8g}" if values else "—",
                f"{max(values):.8g}" if values else "—",
                f"{statistics.fmean(values):.8g}" if values else "—",
                f"{statistics.pstdev(values):.8g}" if len(values) > 1 else "0",
            )
            for column, value in enumerate(cells):
                self.statistics_table.setItem(row, column, QTableWidgetItem(value))
        self.statistics_table.resizeColumnsToContents()
        self.status.setText(f"Statistics for {len(visible_series):,} visible series")

    def _render_histogram(self, visible_series: tuple[_PresentedSeries, ...]) -> None:
        normalized = self.normalize.isChecked()
        chart = create_chart(
            "Probability distribution" if normalized else "Binned value distribution",
            self._scheme,
        )
        domain_values = [
            point.value for series in self._data for point in series.points
        ]
        if not domain_values:
            self.chart_view.setChart(chart)
            self.status.setText("No numeric values are available for a histogram.")
            return
        minimum, maximum = min(domain_values), max(domain_values)
        bins = self.histogram_bins.value()
        width = (maximum - minimum) / bins if maximum != minimum else 1.0
        bar_series = QBarSeries()
        maximum_bin_value = 0.0
        for presented in visible_series:
            series = presented.data
            counts = [0] * bins
            for point in series.points:
                index = min(bins - 1, max(0, int((point.value - minimum) / width)))
                counts[index] += 1
            bar_set = QBarSet(series.selector.format())
            bar_values = (
                [count / len(series.points) for count in counts]
                if normalized and series.points
                else counts
            )
            maximum_bin_value = max(maximum_bin_value, max(bar_values, default=0))
            bar_set.append(bar_values)
            fill_alpha = round(255 * self.histogram_opacity.value() / 100)
            color = series_color(self._scheme, presented.index, alpha=fill_alpha)
            border_color = series_color(
                self._scheme, presented.index, alpha=max(fill_alpha, 225)
            )
            bar_set.setColor(color)
            bar_set.setBorderColor(border_color)
            bar_series.append(bar_set)
        chart.addSeries(bar_series)
        categories = [f"{minimum + index * width:.4g}" for index in range(bins)]
        x_axis = QBarCategoryAxis()
        x_axis.append(categories)
        x_axis.setTitleText("Bin lower bound")
        y_axis = QValueAxis()
        y_axis.setTitleText("Probability" if normalized else "Count")
        y_axis.setLabelFormat("%.2f" if normalized else "%d")
        y_axis.setRange(
            0,
            max(0.01 if normalized else 1.0, maximum_bin_value),
        )
        y_axis.applyNiceNumbers()
        finish_cartesian_chart(
            chart,
            x_axis,
            y_axis,
            self._scheme,
            legend_entries=len(visible_series),
        )
        self.chart_view.setChart(chart)
        bin_label = "bin" if bins == 1 else "bins"
        self.status.setText(
            f"{sum(len(item.data.points) for item in visible_series):,} values "
            f"in {bins} equal-width {bin_label} · "
            f"range {minimum:.8g} to {maximum:.8g}"
            + (" · each series sums to probability 1" if normalized else "")
        )

    def _reset_zoom(self) -> None:
        self.chart_view.chart().zoomReset()

    def _series_row(self, index: int, series: CollectionSeries) -> ActionRow:
        row = ActionRow(margins=(5, 2, 3, 2), spacing=7)
        swatch = QLabel()
        swatch.setObjectName("seriesColorSwatch")
        swatch.setFixedSize(20, 20)
        swatch.setProperty("seriesIndex", index)
        self._set_swatch_color(swatch, index)
        row.add_widget(swatch)
        visible = QCheckBox(series.selector.format())
        visible.setObjectName("seriesVisibility")
        visible.setChecked(series.selector not in self._hidden_selectors)
        visible.setToolTip("Show or hide this series without removing it")
        visible.clicked.connect(
            lambda _checked=False, target=index: self.series_list.setCurrentRow(target)
        )
        visible.toggled.connect(
            lambda checked, selector=series.selector: self._set_series_visible(
                selector, checked
            )
        )
        row.add_widget(visible, 1)
        remove = row.add_action(
            "removeSeriesAction",
            "\u00d7",
            f"Remove {series.selector.format()} from the plot",
            compact=True,
            icon_name="trash",
            icon_role="danger",
            icon_only=True,
        )
        remove.clicked.connect(
            lambda _checked=False, selector=series.selector: (
                self.remove_series(selector)
            )
        )
        return row

    def _show_series_context_menu(self, position) -> None:  # noqa: ANN001
        item = self.series_list.itemAt(position)
        if item is None:
            return
        row = self.series_list.row(item)
        if not 0 <= row < len(self._data):
            return
        self.series_list.setCurrentRow(row)
        menu = QMenu(self.series_list)
        copy_path = menu.addAction("Copy series field path")
        selected = menu.exec(self.series_list.viewport().mapToGlobal(position))
        if selected is copy_path:
            self._copy_series_path(row)

    def _copy_current_series_path(self) -> None:
        self._copy_series_path(self.series_list.currentRow())

    def _select_point(self, record_id: str, selector: FieldSelector) -> None:
        record = next(
            (ref for ref in self.coordinator.collection if ref.id == record_id),
            None,
        )
        if record is None:
            return

        self._pending_point_selection = (record_id, selector)

        document = self.coordinator.selected_document

        if document is not None and document.record.id == record_id:
            self._on_record_changed(document)
        else:
            self.coordinator.select_record(record)

    def _on_record_changed(self, document: ContextDocument) -> None:
        """Synchronize the selection marker and finish pending point navigation."""

        self.select_record(document.record.id if document is not None else None)

        pending = self._pending_point_selection
        if pending is None:
            return

        self._pending_point_selection = None

        if document is None:
            return

        record_id, selector = pending

        if document.record.id != record_id:
            return

        if not document.has_path(selector.node_path):
            return

        self.coordinator.select_context(selector.node_path)
        self.coordinator.select_field(selector.value_path)

    def _copy_series_path(self, row: int) -> None:
        if not 0 <= row < len(self._data):
            return
        self._copy_text(
            self._data[row].selector.format(),
            "Copied plot series field path",
        )

    def _copy_text(self, text: str, message: str) -> None:
        QApplication.clipboard().setText(text)
        self.status_message.emit(message)

    def _set_series_visible(self, selector: FieldSelector, visible: bool) -> None:
        if visible:
            self._hidden_selectors.discard(selector)
        else:
            self._hidden_selectors.add(selector)
        self._render_series()

    def _set_swatch_color(self, swatch: QLabel, index: int) -> None:
        color = series_color(self._scheme, index).name()
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
            f"{context}X: {point.x():.6g} · Y: {point.y():.6g}"
        )

    def _set_empty_chart(self, title: str) -> None:
        self._labels = ()
        self._point_lookup.clear()
        self._selection_series = None
        self.cursor_coordinates.setText("Cursor: —")
        self.chart_view.setChart(create_chart(title, self._scheme))

    def select_record(self, record_id: str | None) -> None:
        """Highlight the selected evaluation without rebuilding the chart."""
        self._selected_record_id = record_id
        self._update_selection_marker()

    def export_image(self, path: str | Path) -> None:
        """Render the current chart view to PNG or SVG based on its suffix."""
        destination = Path(path)
        source_rect = self.chart_view.viewport().rect()
        if destination.suffix.casefold() == ".svg":
            generator = QSvgGenerator()
            generator.setFileName(str(destination))
            generator.setSize(source_rect.size())
            generator.setViewBox(source_rect)
            generator.setResolution(RASTER_EXPORT_DPI)
            painter = QPainter(generator)
            self.chart_view.render(
                painter,
                QRectF(source_rect),
                source_rect,
                Qt.AspectRatioMode.IgnoreAspectRatio,
            )
            painter.end()
            return
        scale = RASTER_EXPORT_DPI / SCREEN_DPI
        source_size = source_rect.size()
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
        self.chart_view.render(
            painter,
            QRectF(image.rect()),
            source_rect,
            Qt.AspectRatioMode.IgnoreAspectRatio,
        )
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
        points = self._point_lookup.points_by_index.get(index, [])
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
        selectors = self._point_lookup.selectors_by_coordinate.get(
            (index, point.y()), ()
        )
        selector = selectors[0] if selectors else self._data[0].selector
        record_id = record_ids[index]
        self.context_selected.emit(record_id)
        self.point_selected.emit(record_id, selector)

    def _selection_point_hovered(
        self, series: QScatterSeries, point: QPointF, state: bool
    ) -> None:
        labels = self._point_lookup.labels_by_coordinate.get(
            (round(point.x()), point.y()), ()
        )
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
            f"X: {point.x():.6g} · Y: {point.y():.6g}"
        )


class CoordinateChartView(QChartView):
    """Chart view that exposes the current mouse position in axis coordinates."""

    coordinate_changed = Signal(object)

    def __init__(self, parent: QWidget | None = None):
        """Initialize the coordinate chart view."""
        super().__init__(parent)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self._zoom_origin: QPoint | None = None
        self._zoom_band = QRubberBand(QRubberBand.Shape.Rectangle, self.viewport())

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        """Start a possible zoom drag while preserving ordinary point clicks."""
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self.chart().axes()
            and self._plot_area_rect().contains(event.position().toPoint())
        ):
            self._zoom_origin = event.position().toPoint()
            self._zoom_band.setGeometry(QRect(self._zoom_origin, self._zoom_origin))
        else:
            self._zoom_origin = None
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        chart = self.chart()
        scene_position = self.mapToScene(event.position().toPoint())
        chart_position = chart.mapFromScene(scene_position)
        self.coordinate_changed.emit(chart.mapToValue(chart_position))
        if (
            self._zoom_origin is not None
            and event.buttons() & Qt.MouseButton.LeftButton
        ):
            rectangle = QRect(
                self._zoom_origin, event.position().toPoint()
            ).normalized()
            rectangle = rectangle.intersected(self._plot_area_rect())
            if (
                rectangle.width() >= QApplication.startDragDistance()
                and rectangle.height() >= QApplication.startDragDistance()
            ):
                self._zoom_band.setGeometry(rectangle)
                self._zoom_band.show()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        """Apply a dragged plot rectangle after Qt finishes click dispatch."""
        zoom_rectangle = self._zoom_band.geometry()
        zooming = (
            event.button() == Qt.MouseButton.LeftButton and self._zoom_band.isVisible()
        )
        self._zoom_band.hide()
        self._zoom_origin = None
        super().mouseReleaseEvent(event)
        if not zooming:
            return
        top_left = self.chart().mapFromScene(self.mapToScene(zoom_rectangle.topLeft()))
        bottom_right = self.chart().mapFromScene(
            self.mapToScene(zoom_rectangle.bottomRight())
        )
        self.chart().zoomIn(QRectF(top_left, bottom_right).normalized())
        event.accept()

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        """Zoom interactively around the cursor while keeping axes synchronized."""
        if not self.chart().axes():
            super().wheelEvent(event)
            return
        steps = event.angleDelta().y() / 120
        if not steps:
            return
        factor = interactive_zoom_factor(steps)
        self.chart().zoom(factor)
        event.accept()

    def leaveEvent(self, event: QEvent) -> None:  # noqa: N802
        self.coordinate_changed.emit(None)
        super().leaveEvent(event)

    def _plot_area_rect(self) -> QRect:
        plot_area = self.chart().mapRectToScene(self.chart().plotArea())
        return QRect(
            self.mapFromScene(plot_area.topLeft()),
            self.mapFromScene(plot_area.bottomRight()),
        ).normalized()


def _sample_points(
    points: tuple[SeriesPoint, ...], maximum: int
) -> tuple[SeriesPoint, ...]:
    """Bound Qt scene complexity while retaining endpoints and the full range."""
    if len(points) <= maximum:
        return points
    step = (len(points) - 1) / (maximum - 1)
    indexes = {0, len(points) - 1}
    indexes.update(round(index * step) for index in range(1, maximum - 1))
    required = {
        0,
        len(points) - 1,
        min(range(len(points)), key=lambda index: points[index].value),
        max(range(len(points)), key=lambda index: points[index].value),
    }
    indexes.update(required)
    if len(indexes) > maximum:
        removable = [index for index in sorted(indexes) if index not in required]
        for index in removable[: len(indexes) - maximum]:
            indexes.remove(index)
    return tuple(points[index] for index in sorted(indexes))


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
