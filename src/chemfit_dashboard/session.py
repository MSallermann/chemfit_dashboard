"""Dashboard session restoration and recent-location persistence."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QByteArray, QObject, QSettings
from PySide6.QtWidgets import QMainWindow, QMenu, QSplitter, QTabWidget

from chemfit_dashboard.context_document import NodePath, RecordRef
from chemfit_dashboard.coordinator import CollectionLoadRequest, DashboardCoordinator

if TYPE_CHECKING:
    from collections.abc import Sequence


WINDOW_GEOMETRY_SETTING = "session/window_geometry"
MAIN_SPLITTER_SETTING = "session/main_splitter"
WORKSPACE_SETTING = "session/workspace"
COLLECTION_LOCATIONS_SETTING = "session/collection_locations"
SELECTED_RECORD_SETTING = "session/selected_record"
SELECTED_CONTEXT_SETTING = "session/selected_context"
RECENT_LOCATIONS_SETTING = "recent/locations"
MAX_RECENT_LOCATIONS = 10


def _string_list(settings: QSettings, key: str) -> list[str]:
    value = settings.value(key)
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item)]
    return []


def _node_path(settings: QSettings, key: str) -> NodePath | None:
    value = settings.value(key)
    if not isinstance(value, (list, tuple)):
        return None
    try:
        return tuple(int(item) for item in value)
    except (TypeError, ValueError):
        return None


def _normalized_locations(locations: Sequence[str | Path]) -> list[str]:
    return [str(Path(location).expanduser().absolute()) for location in locations]


class DashboardSessionController(QObject):
    """Persist shell layout, collection selection, and recent locations."""

    def __init__(
        self,
        *,
        coordinator: DashboardCoordinator,
        settings: QSettings,
        window: QMainWindow,
        splitter: QSplitter,
        workspace_tabs: QTabWidget,
        parent: QObject | None = None,
    ) -> None:
        """Bind persistent session state to the dashboard shell."""
        super().__init__(parent)
        self.coordinator = coordinator
        self.settings = settings
        self.window = window
        self.splitter = splitter
        self.workspace_tabs = workspace_tabs
        self._locations = _string_list(settings, COLLECTION_LOCATIONS_SETTING)
        self._pending_locations: list[str] | None = None
        self._pending_request: CollectionLoadRequest | None = None
        self._restore_selection = False
        self._recent_menu: QMenu | None = None

        coordinator.collection_load_requested.connect(self._collection_requested)
        coordinator.collection_changed.connect(self._collection_changed)
        coordinator.operation_failed.connect(self._operation_failed)

        self.restore_layout()

    def populate_file_menu(self, menu: QMenu) -> None:
        """Add the persistent recent-location submenu to the File menu."""
        self._recent_menu = menu.addMenu("Open &Recent")
        self._rebuild_recent_menu()

    def restore_collection(self) -> None:
        """Restore the saved collection and its selected location."""
        if not self._locations:
            return
        self._restore_selection = True
        self.coordinator.load_collection(self._locations)

    def restore_layout(self) -> None:
        """Restore window geometry, splitter state, and active workspace."""
        geometry = self.settings.value(WINDOW_GEOMETRY_SETTING)
        if isinstance(geometry, QByteArray):
            self.window.restoreGeometry(geometry)

        splitter_state = self.settings.value(MAIN_SPLITTER_SETTING)
        if isinstance(splitter_state, QByteArray):
            self.splitter.restoreState(splitter_state)

        workspace_value = self.settings.value(WORKSPACE_SETTING)
        if isinstance(workspace_value, (int, str)):
            try:
                workspace_index = int(workspace_value)
            except ValueError:
                workspace_index = -1
            if 0 <= workspace_index < self.workspace_tabs.count():
                self.workspace_tabs.setCurrentIndex(workspace_index)

    def save(self) -> None:
        """Persist the current shell and coordinator state."""
        self.settings.setValue(WINDOW_GEOMETRY_SETTING, self.window.saveGeometry())
        self.settings.setValue(MAIN_SPLITTER_SETTING, self.splitter.saveState())

        self.settings.setValue(WORKSPACE_SETTING, self.workspace_tabs.currentIndex())
        if self._locations:
            self.settings.setValue(COLLECTION_LOCATIONS_SETTING, self._locations)
        else:
            self.settings.remove(COLLECTION_LOCATIONS_SETTING)

        document = self.coordinator.selected_document
        if document is None:
            self.settings.remove(SELECTED_RECORD_SETTING)
            self.settings.remove(SELECTED_CONTEXT_SETTING)
        else:
            self.settings.setValue(SELECTED_RECORD_SETTING, document.record.id)
            context = self.coordinator.selected_context
            self.settings.setValue(
                SELECTED_CONTEXT_SETTING,
                list(context) if context is not None else [],
            )
        self.settings.sync()

    def _collection_requested(self, request: CollectionLoadRequest) -> None:
        locations = _normalized_locations(tuple(request.locations))
        self._pending_request = request
        if request.append:
            self._pending_locations = list(
                dict.fromkeys((*self._locations, *locations))
            )
        else:
            self._pending_locations = locations

    def _collection_changed(self, records: Sequence[RecordRef]) -> None:
        request = self._pending_request
        if request is not None and self._pending_locations is not None:
            self._locations = self._pending_locations
            self._remember_recent(_normalized_locations(tuple(request.locations)))
            self._pending_request = None
            self._pending_locations = None
        elif not records:
            self._locations = []
            self._clear_saved_selection()

        if self._restore_selection:
            self._restore_selection = False
            self._restore_record(records)

    def _restore_record(self, records: Sequence[RecordRef]) -> None:
        record_id = str(self.settings.value(SELECTED_RECORD_SETTING, "") or "")
        record = next((item for item in records if item.id == record_id), None)
        if record is None:
            return
        context_path = _node_path(self.settings, SELECTED_CONTEXT_SETTING)
        if context_path is None:
            self.coordinator.select_record(record)
        else:
            self.coordinator.select_location(record, context_path)

    def _operation_failed(self, operation: str, _details: str) -> None:
        if operation != "Collection loading":
            return
        self._pending_request = None
        self._pending_locations = None
        self._restore_selection = False

    def _remember_recent(self, locations: Sequence[str]) -> None:
        recent = _string_list(self.settings, RECENT_LOCATIONS_SETTING)
        for location in reversed(locations):
            if location in recent:
                recent.remove(location)
            recent.insert(0, location)
        self.settings.setValue(RECENT_LOCATIONS_SETTING, recent[:MAX_RECENT_LOCATIONS])
        self.settings.sync()
        self._rebuild_recent_menu()

    def _rebuild_recent_menu(self) -> None:
        menu = self._recent_menu
        if menu is None:
            return
        menu.clear()
        recent = _string_list(self.settings, RECENT_LOCATIONS_SETTING)
        if not recent:
            empty = menu.addAction("No recent locations")
            empty.setEnabled(False)
            return
        for location in recent:
            action = menu.addAction(Path(location).name or location)
            action.setToolTip(location)
            action.triggered.connect(
                lambda _checked=False, target=location: self._open_recent(target)
            )
        menu.addSeparator()
        menu.addAction("Clear Recent", self._clear_recent)

    def _open_recent(self, location: str) -> None:
        self._restore_selection = False
        self.coordinator.load_collection([location])

    def _clear_recent(self) -> None:
        self.settings.remove(RECENT_LOCATIONS_SETTING)
        self.settings.sync()
        self._rebuild_recent_menu()

    def _clear_saved_selection(self) -> None:
        self.settings.remove(COLLECTION_LOCATIONS_SETTING)
        self.settings.remove(SELECTED_RECORD_SETTING)
        self.settings.remove(SELECTED_CONTEXT_SETTING)
        self.settings.sync()
