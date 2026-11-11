"""
Background-operation lifecycle and status-bar presentation.

``ActivityStatus`` presents lifecycle events emitted by the shared task runner.
It owns only status-bar widget state; execution, cancellation, and freshness
remain the runner's responsibility.
"""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QProgressBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from chemfit_dashboard.workers import TaskEvent, TaskState


class ActivityStatus:
    """
    Present active-operation state in status-bar widgets.

    Widgets are parented to ``window`` and follow Qt's ownership lifecycle.
    ``position_panel`` must be called when the window is resized because the
    expanded panel floats above the status bar.
    """

    def __init__(self, window: QMainWindow) -> None:
        """Create operation state and install its widgets into ``window``."""
        self.window = window
        self._pending: dict[str, object] = {}
        self.labels: dict[str, str] = {}
        self.progress_values: dict[str, tuple[int, int] | None] = {}
        self.active_progress_name: str | None = None

        self.toggle = QToolButton()
        self.toggle.setCheckable(True)
        self.toggle.setAutoRaise(True)
        self.toggle.setText("\N{BLACK UP-POINTING TRIANGLE}")
        self.toggle.setToolTip("Show all background operations")
        self.toggle.toggled.connect(self.set_expanded)
        self.toggle.hide()
        window.statusBar().addPermanentWidget(self.toggle)

        self.status = QLabel()
        self.status.setObjectName("activityStatus")
        window.statusBar().addPermanentWidget(self.status)

        self.progress = QProgressBar()
        self.progress.setFixedWidth(150)
        self.progress.setTextVisible(True)
        self.progress.hide()
        window.statusBar().addPermanentWidget(self.progress)

        self.panel = QFrame(window)
        self.panel.setObjectName("activityPanel")
        self.panel.setFrameShape(QFrame.Shape.StyledPanel)
        self.panel_layout = QVBoxLayout(self.panel)
        self.panel_layout.setContentsMargins(10, 8, 10, 8)
        self.panel_layout.setSpacing(6)
        heading = QLabel("BACKGROUND OPERATIONS")
        heading.setObjectName("sectionLabel")
        self.panel_layout.addWidget(heading)
        self.rows: dict[str, tuple[QWidget, QLabel, QProgressBar]] = {}
        self._task_names: dict[int, str] = {}
        self.panel.hide()

    def task_changed(self, event: TaskEvent) -> None:
        """Reflect one runner lifecycle transition in the status widgets."""
        task = event.task
        name = f"task:{task.id}"
        if event.state == TaskState.SUBMITTED:
            if task.label is None:
                return
            self._task_names[task.id] = name
            self._begin(
                name,
                task.label,
                display_delay_ms=task.display_delay_ms,
            )
            return
        if task.id not in self._task_names:
            return
        if event.state == TaskState.PROGRESS and event.progress is not None:
            self._set_progress(name, *event.progress)
            return
        if event.state == TaskState.FAILED:
            self._task_names.pop(task.id, None)
            self._finish(name, f"Failed: {task.label}")
            return
        if event.state in {
            TaskState.COMPLETED,
            TaskState.CANCELLED,
            TaskState.INVALIDATED,
        }:
            self._task_names.pop(task.id, None)
            self._finish(name)

    def _begin(self, name: str, label: str, *, display_delay_ms: int = 0) -> None:
        """Present work after an optional delay that avoids status-bar flashing."""
        marker = object()
        self._pending[name] = marker
        self.progress_values[name] = None
        if display_delay_ms > 0 and name not in self.labels:
            QTimer.singleShot(
                display_delay_ms,
                lambda: self._show_if_current(name, label, marker),
            )
        else:
            self._show_if_current(name, label, marker)

    def _finish(self, name: str, message: str | None = None) -> None:
        """Forget a completed operation and optionally show a transient result."""
        self.labels.pop(name, None)
        self.progress_values.pop(name, None)
        self._pending.pop(name, None)
        self._choose_active_after_removal(name)
        self._update()
        if message:
            self.show_message(message, 4000)

    def _set_progress(self, name: str, value: int, total: int) -> None:
        """Retain progress while the named operation is still current."""
        if name not in self._pending:
            return
        self.progress_values[name] = (value, max(1, total))
        if name not in self.labels:
            return
        # Do not let concurrent workers steal the compact progress bar from one
        # another on every update. The most recently shown task remains active
        # until it finishes; all task rows still update in the expanded panel.
        if self.active_progress_name not in self.labels:
            self.active_progress_name = name
        self._update()

    def set_expanded(self, expanded: bool) -> None:
        """Show or hide the list of all concurrent operations."""
        self.toggle.setText(
            "\N{BLACK DOWN-POINTING TRIANGLE}"
            if expanded
            else "\N{BLACK UP-POINTING TRIANGLE}"
        )
        if expanded and self.labels:
            self.refresh_panel()
            self.position_panel()
            self.panel.show()
            self.panel.raise_()
        else:
            self.panel.hide()

    def position_panel(self) -> None:
        """Anchor the expanded operation list above the status bar."""
        status_geometry = self.window.statusBar().geometry()
        x = max(8, self.window.width() - self.panel.width() - 8)
        y = max(8, status_geometry.top() - self.panel.height() - 4)
        self.panel.move(x, y)

    def show_message(self, message: str, timeout: int = 0) -> None:
        """Route transient application messages through the universal footer."""
        self.window.statusBar().showMessage(message, timeout)

    def _show_if_current(self, name: str, label: str, marker: object) -> None:
        """Reveal delayed work only while its exact task remains current."""
        # A delayed operation may finish or be superseded before this callback
        # reaches the GUI thread. Marker identity distinguishes that stale work.
        if self._pending.get(name) is not marker:
            return
        self.labels[name] = label
        self.active_progress_name = name
        self._update()
        self.show_message(f"Started: {label}")

    def _choose_active_after_removal(self, removed_name: str) -> None:
        if self.active_progress_name == removed_name:
            self.active_progress_name = next(reversed(self.labels), None)

    def _update(self) -> None:
        labels = tuple(self.labels.values())
        active_name = self.active_progress_name
        if active_name not in self.labels:
            active_name = next(reversed(self.labels), None)
            self.active_progress_name = active_name

        text = ""
        if labels and active_name is not None:
            text = f"● Working: {self.labels[active_name]}"
            if len(labels) > 1:
                text += f" (+{len(labels) - 1})"
        self.status.setText(text)

        has_operations = bool(labels)
        self.toggle.setVisible(has_operations)
        self.progress.setVisible(has_operations)
        if active_name is not None:
            self._set_progress_bar(
                self.progress, self.progress_values.get(active_name), compact=True
            )

        if not has_operations:
            self.refresh_panel()
            self.panel.hide()
        elif self.toggle.isChecked():
            self.refresh_panel()
            if not self.panel.isVisible():
                self.position_panel()
                self.panel.show()
                self.panel.raise_()

    def refresh_panel(self) -> None:
        """Update rows in place so progress cannot collapse an expanded panel."""
        structure_changed = False
        for name in tuple(self.rows):
            if name in self.labels:
                continue
            row, _label, _progress_bar = self.rows.pop(name)
            self.panel_layout.removeWidget(row)
            row.deleteLater()
            structure_changed = True

        for name, label in self.labels.items():
            widgets = self.rows.get(name)
            if widgets is None:
                row = QWidget(self.panel)
                layout = QHBoxLayout(row)
                layout.setContentsMargins(0, 0, 0, 0)
                operation_label = QLabel()
                layout.addWidget(operation_label, 1)
                progress_bar = QProgressBar()
                progress_bar.setFixedWidth(180)
                layout.addWidget(progress_bar)
                self.panel_layout.addWidget(row)
                widgets = (row, operation_label, progress_bar)
                self.rows[name] = widgets
                structure_changed = True
            _row, operation_label, progress_bar = widgets
            operation_label.setText(label)
            self._set_progress_bar(
                progress_bar, self.progress_values.get(name), compact=False
            )

        if structure_changed:
            self.panel.setFixedWidth(min(520, max(360, self.window.width() // 3)))
            self.panel.adjustSize()
            if self.panel.isVisible():
                self.position_panel()

    @staticmethod
    def _set_progress_bar(
        bar: QProgressBar,
        progress: tuple[int, int] | None,
        *,
        compact: bool,
    ) -> None:
        if progress is None:
            bar.setRange(0, 0)
            bar.setFormat("")
            return
        value, total = progress
        bar.setRange(0, max(1, total))
        bar.setValue(value)
        bar.setFormat("%p%" if compact else f"{value:,}/{total:,}  %p%")
