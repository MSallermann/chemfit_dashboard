"""Shared visual and identity policy for Qt Charts used by the dashboard."""

from __future__ import annotations

from PySide6.QtCharts import QAbstractAxis, QChart
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor

from chemfit_dashboard.appearance import ColorScheme


def series_color(scheme: ColorScheme, index: int, *, alpha: int = 255) -> QColor:
    """Return the stable palette color for one logical series index."""
    color = QColor(scheme.series[index % len(scheme.series)])
    color.setAlpha(max(0, min(alpha, 255)))
    return color


def create_chart(title: str, scheme: ColorScheme) -> QChart:
    """Create a chart with the dashboard's shared background and title style."""
    chart = QChart()
    chart.setTitle(title)
    chart.setBackgroundBrush(QColor(scheme.chart))
    chart.setPlotAreaBackgroundBrush(QColor(scheme.chart))
    chart.setPlotAreaBackgroundVisible(True)
    chart.setTitleBrush(QColor(scheme.text))
    return chart


def finish_cartesian_chart(
    chart: QChart,
    x_axis: QAbstractAxis,
    y_axis: QAbstractAxis,
    scheme: ColorScheme,
    *,
    legend_entries: int,
) -> None:
    """Install and style axes and legend identically for every chart mode."""
    for axis, alignment in (
        (x_axis, Qt.AlignmentFlag.AlignBottom),
        (y_axis, Qt.AlignmentFlag.AlignLeft),
    ):
        style_axis(axis, scheme)
        chart.addAxis(axis, alignment)
    for chart_series in chart.series():
        chart_series.attachAxis(x_axis)
        chart_series.attachAxis(y_axis)
    chart.legend().setVisible(legend_entries > 1)
    chart.legend().setLabelColor(QColor(scheme.text))


def style_axis(axis: QAbstractAxis, scheme: ColorScheme) -> None:
    """Apply the same subdued axis treatment in every chart mode."""
    axis.setLabelsBrush(QColor(scheme.text))
    axis.setTitleBrush(QColor(scheme.text))
    axis.setGridLineColor(QColor(scheme.border))
