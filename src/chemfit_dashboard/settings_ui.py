"""Persistent dashboard settings and their compact application controls."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QSettings, QSignalBlocker, Signal
from PySide6.QtGui import QActionGroup, QFont
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QMenu,
    QPushButton,
    QSpinBox,
    QWidget,
    QWidgetAction,
)

from chemfit_dashboard.appearance import (
    COLOR_SCHEME_SETTING,
    COLOR_SCHEMES,
    COMPONENT_LABEL_METADATA_KEY_SETTING,
    DEFAULT_COLOR_SCHEME,
    DENSITY_SETTING,
    FONT_SCALE_SETTING,
    STYLE_PROFILE_SETTING,
    application_settings,
    color_scheme,
    set_active_color_scheme,
    stylesheet_for,
)
from chemfit_dashboard.context_document import ContextDocument
from chemfit_dashboard.coordinator import DashboardCoordinator
from chemfit_dashboard.icons import refresh_themed_icons

if TYPE_CHECKING:
    from chemfit_dashboard.collection_plot import CollectionPlotPanel
    from chemfit_dashboard.graph_view import DataFlowPanel

BACKGROUND_THREADS_SETTING = "performance/background_threads"
SEARCH_PROCESSES_SETTING = "performance/search_processes"
MAX_CONFIGURABLE_WORKERS = 64


def _worker_count(value: object, default: int) -> int:
    if not isinstance(value, (int, str)):
        return min(MAX_CONFIGURABLE_WORKERS, max(1, default))
    try:
        count = int(value)
    except (TypeError, ValueError):
        count = default
    return min(MAX_CONFIGURABLE_WORKERS, max(1, count))


class DashboardSettingsController(QObject):
    """Own persisted display/performance settings and apply them globally."""

    status_message = Signal(str)

    def __init__(
        self,
        *,
        coordinator: DashboardCoordinator,
        settings: QSettings | None = None,
        parent: QObject | None = None,
    ) -> None:
        """Restore settings and apply coordinator-owned display/performance state."""
        super().__init__(parent)
        self.coordinator = coordinator
        self.store = settings if settings is not None else application_settings()
        self._root: QWidget | None = None
        self._data_flow_panel: DataFlowPanel | None = None
        self._collection_plot: CollectionPlotPanel | None = None
        self.component_label_key: QComboBox | None = None

        self._scheme_key = color_scheme(
            str(
                self.store.value(COLOR_SCHEME_SETTING, DEFAULT_COLOR_SCHEME)
                or DEFAULT_COLOR_SCHEME
            )
        ).key
        self._style_profile = str(
            self.store.value(STYLE_PROFILE_SETTING, "classic") or "classic"
        )
        self._density = str(
            self.store.value(DENSITY_SETTING, "comfortable") or "comfortable"
        )
        self._font_scale = self._saved_font_scale()
        self._base_font = self._application_base_font()

        runner = coordinator.task_runner
        thread_workers = _worker_count(
            self.store.value(BACKGROUND_THREADS_SETTING, runner.thread_workers),
            runner.thread_workers,
        )
        process_workers = _worker_count(
            self.store.value(SEARCH_PROCESSES_SETTING, runner.process_workers),
            runner.process_workers,
        )
        runner.set_thread_workers(thread_workers)
        runner.set_process_workers(process_workers)

        label_key = str(
            self.store.value(
                COMPONENT_LABEL_METADATA_KEY_SETTING,
                "",
            )
            or ""
        ).strip()
        coordinator.set_component_label_metadata_key(label_key)
        coordinator.component_label_metadata_key_changed.connect(
            self._component_label_key_changed
        )
        coordinator.selected_record_changed.connect(self._selected_document_changed)
        self._apply_appearance()

    def bind_views(
        self,
        *,
        root: QWidget,
        data_flow_panel: DataFlowPanel,
        collection_plot: CollectionPlotPanel,
    ) -> None:
        """Register custom-painted views that need explicit appearance refreshes."""
        self._root = root
        self._data_flow_panel = data_flow_panel
        self._collection_plot = collection_plot
        self._refresh_custom_views(font_changed=True)

    def add_font_controls(self, layout: QHBoxLayout) -> None:
        """Add compact application-font controls to the supplied header."""
        smaller = QPushButton("A-")
        smaller.setToolTip("Decrease interface font sizes")
        smaller.clicked.connect(lambda: self._change_font_scale(-0.1))
        layout.addWidget(smaller)

        larger = QPushButton("A+")
        larger.setToolTip("Increase interface font sizes")
        larger.clicked.connect(lambda: self._change_font_scale(0.1))
        layout.addWidget(larger)

    def populate_menu(self, menu: QMenu) -> None:
        """Populate the Settings menu without exposing persistence to the shell."""
        theme_menu = menu.addMenu("Theme")
        self._theme_actions = QActionGroup(self)
        self._theme_actions.setExclusive(True)
        for scheme in COLOR_SCHEMES:
            action = theme_menu.addAction(scheme.label)
            action.setCheckable(True)
            action.setChecked(scheme.key == self._scheme_key)
            action.triggered.connect(
                lambda _checked=False, key=scheme.key: self._set_color_scheme(key)
            )
            self._theme_actions.addAction(action)

        style_menu = menu.addMenu("Interface style")
        self._style_actions = QActionGroup(self)
        self._style_actions.setExclusive(True)
        for label, profile in (("Classic", "classic"), ("Lightweight", "lightweight")):
            action = style_menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(profile == self._style_profile)
            action.triggered.connect(
                lambda _checked=False, value=profile: self._set_style_profile(value)
            )
            self._style_actions.addAction(action)

        density_menu = menu.addMenu("Spacing")
        self._density_actions = QActionGroup(self)
        self._density_actions.setExclusive(True)
        for label, density in (
            ("Compact", "compact"),
            ("Comfortable", "comfortable"),
            ("Spacious", "spacious"),
        ):
            action = density_menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(density == self._density)
            action.triggered.connect(
                lambda _checked=False, value=density: self._set_density(value)
            )
            self._density_actions.addAction(action)

        self._populate_workers_menu(menu.addMenu("Workers"))
        self._populate_component_labels_menu(menu.addMenu("Component labels"))

    def _populate_component_labels_menu(self, menu: QMenu) -> None:
        self.component_label_key = QComboBox(menu)
        self.component_label_key.setEditable(True)
        self.component_label_key.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.component_label_key.setMinimumWidth(220)
        self.component_label_key.setToolTip(
            "Metadata key used for component labels in every view. "
            "Leave blank to use structural component names."
        )
        editor = self.component_label_key.lineEdit()
        if editor is not None:
            editor.setPlaceholderText("Structural component names")
            editor.editingFinished.connect(self._component_label_key_edited)
        self.component_label_key.activated.connect(
            lambda _index: self._component_label_key_edited()
        )
        action = QWidgetAction(menu)
        action.setDefaultWidget(self.component_label_key)
        menu.addAction(action)
        self._selected_document_changed(self.coordinator.selected_document)

    def _populate_workers_menu(self, menu: QMenu) -> None:
        runner = self.coordinator.task_runner
        thread_workers = QSpinBox(menu)
        thread_workers.setRange(1, MAX_CONFIGURABLE_WORKERS)
        thread_workers.setValue(runner.thread_workers)
        thread_workers.setPrefix("Background threads: ")
        thread_workers.setToolTip(
            "Shared threads used by loading, analysis, plots, extraction, and layout"
        )
        thread_action = QWidgetAction(menu)
        thread_action.setDefaultWidget(thread_workers)
        menu.addAction(thread_action)
        thread_workers.valueChanged.connect(self._set_background_thread_count)

        process_workers = QSpinBox(menu)
        process_workers.setRange(1, MAX_CONFIGURABLE_WORKERS)
        process_workers.setValue(runner.process_workers)
        process_workers.setPrefix("Search processes: ")
        process_workers.setToolTip(
            "Persistent worker processes used for whole-collection search"
        )
        process_action = QWidgetAction(menu)
        process_action.setDefaultWidget(process_workers)
        menu.addAction(process_action)
        process_workers.valueChanged.connect(self._set_search_process_count)

    def _saved_font_scale(self) -> float:
        saved = self.store.value(FONT_SCALE_SETTING, 1.0)
        try:
            scale = float(saved) if isinstance(saved, (int, float, str)) else 1.0
        except (TypeError, ValueError):
            scale = 1.0
        return min(1.6, max(0.8, round(scale, 1)))

    @staticmethod
    def _application_base_font() -> QFont | None:
        application = QApplication.instance()
        if not isinstance(application, QApplication):
            return None
        stored = application.property("chemfitBaseFont")
        if isinstance(stored, QFont):
            return QFont(stored)
        font = QFont(application.font())
        application.setProperty("chemfitBaseFont", font)
        return font

    def _change_font_scale(self, change: float) -> None:
        scale = min(1.6, max(0.8, round(self._font_scale + change, 1)))
        if scale == self._font_scale:
            return
        self._font_scale = scale
        self.store.setValue(FONT_SCALE_SETTING, scale)
        self.store.sync()
        self._apply_appearance(font_changed=True)
        self.status_message.emit(f"Interface font size: {round(scale * 100)}%")

    def _set_color_scheme(self, key: str) -> None:
        self._scheme_key = color_scheme(key).key
        self.store.setValue(COLOR_SCHEME_SETTING, self._scheme_key)
        self.store.sync()
        self._apply_appearance()
        self.status_message.emit(
            f"Color scheme: {color_scheme(self._scheme_key).label}"
        )

    def _set_style_profile(self, profile: str) -> None:
        self._style_profile = profile
        self.store.setValue(STYLE_PROFILE_SETTING, profile)
        self.store.sync()
        self._apply_appearance()

    def _set_density(self, density: str) -> None:
        self._density = density
        self.store.setValue(DENSITY_SETTING, density)
        self.store.sync()
        self._apply_appearance()

    def _set_background_thread_count(self, count: int) -> None:
        count = _worker_count(count, self.coordinator.task_runner.thread_workers)
        self.coordinator.task_runner.set_thread_workers(count)
        self.store.setValue(BACKGROUND_THREADS_SETTING, count)
        self.store.sync()
        self.status_message.emit(f"Background worker threads: {count}")

    def _set_search_process_count(self, count: int) -> None:
        count = _worker_count(count, self.coordinator.task_runner.process_workers)
        self.coordinator.task_runner.set_process_workers(count)
        self.store.setValue(SEARCH_PROCESSES_SETTING, count)
        self.store.sync()
        self.status_message.emit(f"Collection-search worker processes: {count}")

    def _component_label_key_changed(self, key: str) -> None:
        combo = self.component_label_key
        if combo is not None:
            with QSignalBlocker(combo):
                combo.setEditText(key)
        self.store.setValue(COMPONENT_LABEL_METADATA_KEY_SETTING, key)
        self.store.sync()
        shown_key = f"metadata '{key}'" if key else "structural component names"
        self.status_message.emit(f"Component labels now use {shown_key}")

    def _component_label_key_edited(self) -> None:
        combo = self.component_label_key
        if combo is None:
            return
        self.coordinator.set_component_label_metadata_key(combo.currentText())

    def _selected_document_changed(self, document: ContextDocument | None) -> None:
        combo = self.component_label_key
        if combo is None:
            return
        current = self.coordinator.component_label_metadata_key
        known = {combo.itemText(index) for index in range(combo.count())}
        with QSignalBlocker(combo):
            if document is not None:
                for key in document.component_label_metadata_keys:
                    if key not in known:
                        combo.addItem(key)
            combo.setEditText(current)

    def _apply_appearance(self, *, font_changed: bool = False) -> None:
        scheme = set_active_color_scheme(self._scheme_key)
        application = QApplication.instance()
        if isinstance(application, QApplication):
            if self._base_font is not None:
                font = QFont(self._base_font)
                if font.pointSizeF() > 0:
                    font.setPointSizeF(font.pointSizeF() * self._font_scale)
                application.setFont(font)
            stylesheet = stylesheet_for(
                scheme,
                style_profile=self._style_profile,
                density=self._density,
            )
            application.setStyleSheet(
                re.sub(
                    r"(font-size:\s*)(\d+(?:\.\d+)?)px",
                    lambda match: (
                        f"{match.group(1)}"
                        f"{float(match.group(2)) * self._font_scale:.1f}px"
                    ),
                    stylesheet,
                )
            )
        self._refresh_custom_views(font_changed=font_changed)

    def _refresh_custom_views(self, *, font_changed: bool) -> None:
        scheme = color_scheme(self._scheme_key)
        if self._data_flow_panel is not None:
            if font_changed:
                self._data_flow_panel.view.refresh_labels()
            else:
                self._data_flow_panel.view.set_color_scheme(scheme)
        if self._collection_plot is not None:
            if font_changed:
                self._collection_plot.refresh_fonts()
            else:
                self._collection_plot.set_color_scheme(scheme)
        if self._root is not None:
            refresh_themed_icons(self._root, scheme)
            self._root.update()
