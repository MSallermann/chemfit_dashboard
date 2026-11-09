"""Back/forward navigation across selected records and context paths."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QHBoxLayout, QMenu, QToolButton

from chemfit_dashboard.context_document import NodePath, RecordRef
from chemfit_dashboard.coordinator import (
    CollectionLoadRequest,
    DashboardCoordinator,
    RecordSelectionRequest,
)
from chemfit_dashboard.icons import set_themed_icon

if TYPE_CHECKING:
    from collections.abc import Sequence


@dataclass(frozen=True, slots=True)
class _HistoryEntry:
    record_id: str
    context_path: NodePath


@dataclass(frozen=True, slots=True)
class _PendingReplay:
    entry: _HistoryEntry
    previous_index: int


class ContextNavigationHistory(QObject):
    """Record context selections and replay them through one coordinator."""

    def __init__(
        self,
        *,
        coordinator: DashboardCoordinator,
        parent: QObject | None = None,
        limit: int = 200,
    ) -> None:
        """Create bounded history and listen to coordinator selection changes."""
        super().__init__(parent)
        self.coordinator = coordinator
        self._limit = max(1, limit)
        self._history: list[_HistoryEntry] = []
        self._index = -1
        self._replaying = False
        self._pending: _PendingReplay | None = None
        self._replace_collection_pending = False

        self.back_action = QAction("&Back", self)
        self.back_action.setShortcut(QKeySequence("Alt+Left"))
        self.back_action.setToolTip("Return to the previous context (Alt+Left)")
        set_themed_icon(self.back_action, "chevron_left")
        self.back_action.triggered.connect(lambda: self._navigate(-1))

        self.forward_action = QAction("&Forward", self)
        self.forward_action.setShortcut(QKeySequence("Alt+Right"))
        self.forward_action.setToolTip("Advance to the next context (Alt+Right)")
        set_themed_icon(self.forward_action, "chevron_right")
        self.forward_action.triggered.connect(lambda: self._navigate(1))

        coordinator.selected_context_changed.connect(self._context_changed)
        coordinator.selected_record_change_requested.connect(
            self._record_change_requested
        )
        coordinator.collection_load_requested.connect(self._collection_load_requested)
        coordinator.collection_changed.connect(self._collection_changed)
        coordinator.operation_failed.connect(self._operation_failed)
        self._update_actions()

    def add_header_controls(self, layout: QHBoxLayout) -> None:
        """Add compact history controls to an application header."""
        back_button = QToolButton()
        back_button.setObjectName("historyBackButton")
        back_button.setDefaultAction(self.back_action)
        layout.addWidget(back_button)

        forward_button = QToolButton()
        forward_button.setObjectName("historyForwardButton")
        forward_button.setDefaultAction(self.forward_action)
        layout.addWidget(forward_button)

    def populate_menu(self, menu: QMenu) -> None:
        """Add history actions to a navigation menu."""
        menu.addAction(self.back_action)
        menu.addAction(self.forward_action)
        menu.addSeparator()

    def clear(self) -> None:
        """Discard all navigation entries and pending replay state."""
        self._history.clear()
        self._index = -1
        self._replaying = False
        self._pending = None
        self._update_actions()

    def _context_changed(self, path: NodePath | None) -> None:
        document = self.coordinator.selected_document
        pending = self._pending
        if pending is not None:
            if document is not None and document.record.id == pending.entry.record_id:
                self._pending = None
                self._replaying = False
                self._update_actions()
            return
        if self._replaying or document is None or path is None:
            return
        entry = _HistoryEntry(document.record.id, path)
        if (
            0 <= self._index < len(self._history)
            and self._history[self._index] == entry
        ):
            return
        del self._history[self._index + 1 :]
        self._history.append(entry)
        if len(self._history) > self._limit:
            self._history.pop(0)
        self._index = len(self._history) - 1
        self._update_actions()

    def _navigate(self, offset: int) -> None:
        target_index = self._index + offset
        if not 0 <= target_index < len(self._history):
            return
        entry = self._history[target_index]
        document = self.coordinator.selected_document
        if document is not None and document.record.id == entry.record_id:
            self._index = target_index
            self._replaying = True
            try:
                self.coordinator.select_location(document.record, entry.context_path)
            finally:
                self._replaying = False
            self._update_actions()
            return

        record = self._record(entry.record_id)
        if record is None:
            return
        previous_index = self._index
        self._index = target_index
        self._replaying = True
        self._pending = _PendingReplay(entry, previous_index)
        self._update_actions()
        self.coordinator.select_location(record, entry.context_path)

    def _record_change_requested(self, request: RecordSelectionRequest) -> None:
        pending = self._pending
        if pending is None:
            return
        record = request.ref
        if (
            record is None
            or record.id != pending.entry.record_id
            or request.context_path != pending.entry.context_path
        ):
            self._cancel_pending()

    def _collection_load_requested(self, request: CollectionLoadRequest) -> None:
        self._replace_collection_pending = not request.append

    def _collection_changed(self, records: Sequence[RecordRef]) -> None:
        if self._replace_collection_pending:
            self._replace_collection_pending = False
            self.clear()
            return
        record_ids = {record.id for record in records}
        if not record_ids:
            self.clear()
            return
        current_index = self._index
        self._history = [
            entry for entry in self._history if entry.record_id in record_ids
        ]
        self._index = min(current_index, len(self._history) - 1)
        if (
            self._pending is not None
            and self._pending.entry.record_id not in record_ids
        ):
            self._cancel_pending()
        self._update_actions()

    def _operation_failed(self, operation: str, _details: str) -> None:
        if operation == "Record selection" and self._pending is not None:
            self._cancel_pending()

    def _cancel_pending(self) -> None:
        pending = self._pending
        if pending is not None:
            self._index = pending.previous_index
        self._pending = None
        self._replaying = False
        self._update_actions()

    def _record(self, record_id: str) -> RecordRef | None:
        return next(
            (
                record
                for record in self.coordinator.collection
                if record.id == record_id
            ),
            None,
        )

    def _update_actions(self) -> None:
        self.back_action.setEnabled(self._index > 0 and self._pending is None)
        self.forward_action.setEnabled(
            self._pending is None and 0 <= self._index < len(self._history) - 1
        )
