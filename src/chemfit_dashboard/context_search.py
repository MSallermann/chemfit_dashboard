"""
Coordinator-backed context search and Qt result presentation.

Search indexing and ranking live in :mod:`search_core`; this module owns result
controls, asynchronous execution, result tables, match highlighting, clipboard
actions, and result navigation.
"""

from __future__ import annotations

import json
import logging
import re
from html import escape
from typing import cast

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from rapidfuzz.fuzz import ratio

from chemfit_dashboard.appearance import active_color_scheme
from chemfit_dashboard.context_document import ContextDocument, RecordRef
from chemfit_dashboard.coordinator import DashboardCoordinator
from chemfit_dashboard.process_search import (
    CollectionSearchResult,
    SearchOptions,
    search_collection,
)
from chemfit_dashboard.search_core import (
    ContextSearchCache,
    SearchHit,
    SearchMode,
    _normalize,
)
from chemfit_dashboard.traversal import CancellationToken
from chemfit_dashboard.ui_components import add_clear_action, muted_label, panel_layout
from chemfit_dashboard.workers import TaskContext

logger = logging.getLogger(__name__)

HIT_ROLE = Qt.ItemDataRole.UserRole.value


class ContextSearchPanel(QWidget):
    """
    Search the current evaluation or collection and navigate ranked results.

    Search-specific state and processing live here. Shared collection and
    selection state remain authoritative in ``DashboardCoordinator``.
    """

    hit_selected = Signal(object)
    hit_previewed = Signal(object)
    copy_completed = Signal(str)

    def __init__(
        self,
        *,
        coordinator: DashboardCoordinator,
        parent: QWidget | None = None,
    ):
        """Initialize search controls, background resources, and result view."""
        super().__init__(parent)
        self.coordinator = coordinator
        self._index_cache = ContextSearchCache(capacity=32)
        self._cancellation: CancellationToken | None = None
        self._pending_hit: SearchHit | None = None
        self._pending_activation = False
        self._shut_down = False

        layout = panel_layout(self)
        self._build_controls(layout)
        self._build_results(layout)
        self._connect_search_signals()

    def _build_controls(self, layout: QVBoxLayout) -> None:
        """Build query, scope, mode, and searchable-dimension controls."""

        controls = QFrame()
        controls.setObjectName("contextSearchControls")
        controls_layout = QVBoxLayout(controls)
        controls_layout.setContentsMargins(8, 8, 8, 8)
        controls_layout.setSpacing(7)

        self.query = QLineEdit()
        self.query.setPlaceholderText("Search fields, values, and component hierarchy…")
        add_clear_action(self.query)
        controls_layout.addWidget(self.query)

        search_controls = QHBoxLayout()
        search_controls.setSpacing(8)
        self.mode = QComboBox()
        self.mode.addItem("Fuzzy", "fuzzy")
        self.mode.addItem("Exact", "exact")
        self.mode.addItem("Regex", "regex")
        self.mode.setToolTip("Choose fuzzy, literal substring, or regex matching")
        search_controls.addWidget(self.mode)
        self.scope = QComboBox()
        self.scope.addItem("Whole collection", "collection")
        self.scope.addItem("Current evaluation", "current")
        self.scope.setToolTip("Search one evaluation or every loaded context file")
        search_controls.addWidget(self.scope)
        search_controls.addStretch()
        controls_layout.addLayout(search_controls)

        dimensions = QGridLayout()
        dimensions.setSpacing(8)
        self.include_keys = QCheckBox("Include keys")
        self.include_keys.setChecked(True)
        self.include_keys.setToolTip("Match nested field paths and section names")
        dimensions.addWidget(self.include_keys, 0, 0)
        self.include_values = QCheckBox("Include values")
        self.include_values.setChecked(True)
        self.include_values.setToolTip("Match serialized field values")
        dimensions.addWidget(self.include_values, 0, 1)
        self.include_metadata = QCheckBox("Include metadata")
        self.include_metadata.setChecked(True)
        self.include_metadata.setToolTip(
            "Include component metadata fields and values in search results"
        )
        dimensions.addWidget(self.include_metadata, 1, 0)
        self.include_components = QCheckBox("Component names")
        self.include_components.setChecked(False)
        self.include_components.setToolTip(
            "Include component names and hierarchy breadcrumbs when matching"
        )
        dimensions.addWidget(self.include_components, 1, 1)
        self._add_field_type_controls(dimensions)
        dimensions.setColumnStretch(0, 1)
        dimensions.setColumnStretch(1, 1)
        controls_layout.addLayout(dimensions)
        layout.addWidget(controls)

    def _add_field_type_controls(self, dimensions: QGridLayout) -> None:
        """Add independent input/output filters, enabled by default."""
        self.include_inputs = QCheckBox("Input fields")
        self.include_inputs.setChecked(True)
        self.include_inputs.setToolTip("Include input fields and their nested values")
        dimensions.addWidget(self.include_inputs, 2, 0)
        self.include_outputs = QCheckBox("Output fields")
        self.include_outputs.setChecked(True)
        self.include_outputs.setToolTip("Include output fields and their nested values")
        dimensions.addWidget(self.include_outputs, 2, 1)

    def _build_results(self, layout: QVBoxLayout) -> None:
        """Build the status and ranked-result presentation."""
        self.status = muted_label(
            "Type above to search context fields and values.", word_wrap=True
        )
        layout.addWidget(self.status)
        self.results = QTreeWidget()
        self.results.setHeaderLabels(["Field / owning context", "Value", "Match"])
        self.results.setRootIsDecorated(False)
        self.results.setAlternatingRowColors(True)
        self.results.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.results.setUniformRowHeights(False)
        self.results.setHorizontalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        self.results.header().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Interactive
        )
        self.results.header().resizeSection(0, 330)
        self.results.header().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Interactive
        )
        self.results.header().resizeSection(1, 150)
        self.results.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.results.header().resizeSection(2, 72)
        self.results.currentItemChanged.connect(self._item_highlighted)
        self.results.itemDoubleClicked.connect(self._item_activated)
        self.results.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.results.customContextMenuRequested.connect(self._show_context_menu)
        self._copy_shortcut = QShortcut(QKeySequence.StandardKey.Copy, self.results)
        self._copy_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._copy_shortcut.activated.connect(self._copy_current_result)
        self._activate_shortcut = QShortcut(
            QKeySequence(Qt.Key.Key_Return), self.results
        )
        self._activate_shortcut.setContext(
            Qt.ShortcutContext.WidgetWithChildrenShortcut
        )
        self._activate_shortcut.activated.connect(self._activate_current_result)
        self._activate_keypad_shortcut = QShortcut(
            QKeySequence(Qt.Key.Key_Enter), self.results
        )
        self._activate_keypad_shortcut.setContext(
            Qt.ShortcutContext.WidgetWithChildrenShortcut
        )
        self._activate_keypad_shortcut.activated.connect(self._activate_current_result)
        layout.addWidget(self.results, 1)

    def _connect_search_signals(self) -> None:
        """Connect controls and coordinator state to search invalidation."""
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(120)
        self._debounce.timeout.connect(self._run_search)
        self.query.textChanged.connect(self._search_controls_changed)
        self.mode.currentIndexChanged.connect(self._search_controls_changed)
        self.scope.currentIndexChanged.connect(self._search_controls_changed)
        for option in (
            self.include_keys,
            self.include_values,
            self.include_metadata,
            self.include_components,
            self.include_inputs,
            self.include_outputs,
        ):
            option.toggled.connect(self._search_controls_changed)

        self.coordinator.selected_record_changed.connect(self._record_changed)
        self.coordinator.selected_record_change_requested.connect(
            self._record_change_requested
        )
        self.coordinator.collection_changed.connect(self._collection_changed)
        self.coordinator.operation_failed.connect(self._operation_failed)
        self.coordinator.component_label_metadata_key_changed.connect(
            self._component_labels_changed
        )
        self.coordinator.task_runner.process_workers_changed.connect(
            self._process_workers_changed
        )

    def focus_query(self) -> None:
        """Focus and select the query for the application Find action."""
        self.query.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.query.selectAll()

    def clear_query(self) -> None:
        """Clear the active query and invalidate unfinished searches."""
        self.query.clear()

    def shutdown(self) -> None:
        """Cancel search work before the shared runner is shut down."""
        if self._shut_down:
            return
        self._shut_down = True
        self._debounce.stop()
        self._cancel_search()

    def _schedule_search(self, _value: object = None) -> None:
        """Invalidate active work immediately and debounce its replacement."""
        self._cancel_search()
        if not self.query.text().strip():
            self._debounce.stop()
            self._show_message("Type above to search context fields and values.")
            return
        self.results.clear()
        self.status.setText("Updating search…")
        self._debounce.start()

    def _search_controls_changed(self, value: object = None) -> None:
        """Invalidate both results and navigation tied to an older query."""
        self._clear_pending_hit()
        self._schedule_search(value)

    def _cancel_search(self) -> None:
        if self._cancellation is not None:
            self._cancellation.cancel()
            self._cancellation = None
        self.coordinator.task_runner.invalidate("context_search")

    def _run_search(self) -> None:
        """Snapshot controls and submit one freshness-guarded background search."""
        if self._shut_down:
            return
        query = self.query.text().strip()
        if not query:
            return
        mode = cast("SearchMode", self.mode.currentData())
        if mode == "regex":
            try:
                re.compile(query, re.IGNORECASE)
            except re.error as error:
                self._show_message(f"Invalid regular expression: {error}")
                return

        options = SearchOptions(
            query=query,
            include_keys=self.include_keys.isChecked(),
            include_values=self.include_values.isChecked(),
            include_metadata=self.include_metadata.isChecked(),
            include_components=self.include_components.isChecked(),
            include_inputs=self.include_inputs.isChecked(),
            include_outputs=self.include_outputs.isChecked(),
            mode=mode,
        )
        scope = self.scope.currentData()
        current_document = self.coordinator.selected_document
        if scope == "current":
            if current_document is None:
                self._show_message("Select an evaluation to search it.")
                return
            records = (current_document.record,)
            scope_label = "current evaluation"
        else:
            records = tuple(self.coordinator.collection)
            scope_label = "whole collection"
            if not records:
                self._show_message("No context collection is loaded.")
                return

        cancellation = CancellationToken()
        self._cancellation = cancellation
        self.status.setText(
            f"Searching {len(records):,} evaluation"
            f"{'s' if len(records) != 1 else ''} in the background…"
        )
        component_label_key = self.coordinator.component_label_metadata_key
        process_executor = (
            self.coordinator.task_runner.process_executor
            if scope == "collection"
            else None
        )

        def task(*, task_context: TaskContext) -> CollectionSearchResult:
            if scope == "collection":
                assert process_executor is not None
                return search_collection(
                    process_executor,
                    records,
                    options,
                    component_label_key=component_label_key,
                    worker_count=self.coordinator.task_runner.process_workers,
                    cancellation=cancellation,
                    progress=task_context.report_progress,
                )
            assert current_document is not None
            index = self._index_cache.index_for(
                current_document,
                cooperative=True,
                cancelled=lambda: cancellation.cancelled,
            )
            if cancellation.cancelled:
                return CollectionSearchResult([], 0, cancelled=True)
            hits = index.search(
                options.query,
                include_keys=options.include_keys,
                include_values=options.include_values,
                include_metadata=options.include_metadata,
                include_components=options.include_components,
                include_inputs=options.include_inputs,
                include_outputs=options.include_outputs,
                mode=options.mode,
                cooperative=True,
                cancelled=lambda: cancellation.cancelled,
            )
            return CollectionSearchResult(
                hits,
                0,
                cancelled=cancellation.cancelled,
            )

        def succeeded(result: CollectionSearchResult) -> None:
            if self._cancellation is cancellation:
                self._cancellation = None
            if result.cancelled:
                return
            self._show_results(
                result.hits,
                options.query,
                include_keys=options.include_keys,
                include_values=options.include_values,
                include_metadata=options.include_metadata,
                include_components=options.include_components,
                include_inputs=options.include_inputs,
                include_outputs=options.include_outputs,
                mode=options.mode,
                scope_label=scope_label,
                searched_records=len(records),
                failed_records=result.failed_records,
            )

        def failed(details: str) -> None:
            if self._cancellation is cancellation:
                self._cancellation = None
            logger.error("Could not search context collection\n%s", details)
            self._show_message("Could not search the context collection.")

        self.coordinator.task_runner.submit_latest(
            key="context_search",
            function=task,
            on_success=succeeded,
            on_error=failed,
            label="Searching contexts",
            with_context=True,
        )

    def _record_changed(self, document: ContextDocument | None) -> None:
        pending = self._pending_hit
        if pending is not None:
            if document is not None and document.record.id == pending.record_id:
                self._apply_pending_hit(pending, document)
            else:
                # A different committed record supersedes pending navigation.
                self._clear_pending_hit()
        if self.scope.currentData() == "current" and self.query.text().strip():
            self._schedule_search()

    def _record_change_requested(self, request: object) -> None:
        """Discard navigation if another record request supersedes its target."""
        pending = self._pending_hit
        if pending is None:
            return
        record = getattr(request, "ref", None)
        if record is None or getattr(record, "id", None) != pending.record_id:
            self._clear_pending_hit()

    def _collection_changed(self, records: tuple[RecordRef, ...]) -> None:
        self._index_cache.clear()
        pending = self._pending_hit
        if pending is not None and not any(
            record.id == pending.record_id for record in records
        ):
            self._clear_pending_hit()
        if self.scope.currentData() == "collection" and self.query.text().strip():
            self._schedule_search()

    def _component_labels_changed(self, _key: str) -> None:
        self._index_cache.clear()
        if self.query.text().strip():
            self._schedule_search()

    def _process_workers_changed(self, _count: int) -> None:
        if self.scope.currentData() == "collection" and self.query.text().strip():
            self._schedule_search()

    def _operation_failed(self, operation: str, _details: str) -> None:
        if operation == "Record selection" and self._pending_hit is not None:
            self._clear_pending_hit()

    def _navigate_to_hit(self, hit: SearchHit, *, activate: bool) -> None:
        document = self.coordinator.selected_document
        pending = self._pending_hit
        if pending is hit:
            self._pending_activation = self._pending_activation or activate
            return
        if pending is not None:
            self._clear_pending_hit()
            if document is not None and document.record.id == hit.record_id:
                # Re-selecting the authoritative record invalidates the older
                # coordinator load before this local hit is applied.
                self.coordinator.select_record(document.record)
        if document is not None and document.record.id == hit.record_id:
            self._apply_hit(hit, document, activate=activate)
            return
        record = next(
            (item for item in self.coordinator.collection if item.id == hit.record_id),
            None,
        )
        if record is None:
            self._show_message(
                "The selected evaluation is no longer in the collection."
            )
            return
        self._pending_hit = hit
        self._pending_activation = activate
        self.coordinator.select_location(
            record,
            hit.node_path,
            _coordinator_field_path(hit),
        )

    def _clear_pending_hit(self) -> None:
        self._pending_hit = None
        self._pending_activation = False

    def _apply_pending_hit(self, hit: SearchHit, document: ContextDocument) -> None:
        if (
            self._pending_hit is not hit
            or self.coordinator.selected_document is not document
        ):
            return
        activate = self._pending_activation
        self._clear_pending_hit()
        self._apply_hit(hit, document, activate=activate)

    def _apply_hit(
        self,
        hit: SearchHit,
        document: ContextDocument,
        *,
        activate: bool,
    ) -> None:
        """Commit a hit only after its exact document is authoritative."""
        if document.record.id != hit.record_id or not document.has_path(hit.node_path):
            return
        try:
            field_path = _coordinator_field_path(hit)
            self.coordinator.select_location(document.record, hit.node_path, field_path)
        except ValueError:
            self._show_message("The selected search result is no longer available.")
            return
        (self.hit_selected if activate else self.hit_previewed).emit(hit)

    def _show_message(self, text: str) -> None:
        """Clear stale hits and display an informational or error state."""
        self.results.clear()
        self.status.setText(text)

    def _show_results(
        self,
        hits: list[SearchHit],
        query: str,
        *,
        include_keys: bool,
        include_values: bool,
        mode: SearchMode,
        include_metadata: bool = True,
        include_components: bool = True,
        include_inputs: bool = True,
        include_outputs: bool = True,
        scope_label: str = "current evaluation",
        searched_records: int = 1,
        failed_records: int = 0,
    ) -> None:
        """Render already-computed results on the UI thread."""
        self.results.clear()
        if not hits:
            self.status.setText(
                f"No {_mode_label(mode).lower()} matches for “{query.strip()}” "
                f"in {scope_label}."
            )
            return

        for hit in hits:
            location = hit.section.title() if hit.section is not None else "Context"
            score_label = f"{hit.score:.1%}"
            item = QTreeWidgetItem([hit.field_label, hit.preview, score_label])
            item.setData(0, HIT_ROLE, hit)
            item.setToolTip(
                0,
                f"{hit.breadcrumb}\n{location}: {hit.field_label}",
            )
            item.setToolTip(1, hit.preview)
            item.setToolTip(2, f"Relevance score: {hit.score:.1%}")
            self.results.addTopLevelItem(item)
            field_label = self._set_result_label(item, hit, location, query, mode)
            value_label = self._highlight_cell(item, 1, hit.preview, query, mode)
            row_height = max(
                42,
                field_label.sizeHint().height() + 10,
                value_label.sizeHint().height() + 10,
            )
            for column in range(self.results.columnCount()):
                item.setSizeHint(column, QSize(0, row_height))
        dimensions = ["fields"]
        if include_inputs:
            dimensions.append("input fields")
        if include_outputs:
            dimensions.append("output fields")
        if include_components:
            dimensions.append("component names")
        if include_metadata:
            dimensions.append("metadata")
        if include_keys:
            dimensions.append("keys")
        if include_values:
            dimensions.append("values")
        context_count = len({(hit.record_id, hit.node_path) for hit in hits})
        file_count = len({hit.record_id for hit in hits if hit.record_id})
        scope_details = (
            f"{file_count:,} matched files / {searched_records:,} searched"
            if searched_records > 1
            else scope_label
        )
        failure_suffix = (
            f" · {failed_records:,} unreadable files skipped" if failed_records else ""
        )
        self.status.setText(
            f"{len(hits):,} matches in {context_count:,} contexts · best match first · "
            f"{scope_details} · {_mode_label(mode)} · {' + '.join(dimensions)}"
            f"{failure_suffix}"
        )

    def _set_result_label(
        self,
        item: QTreeWidgetItem,
        hit: SearchHit,
        location: str,
        query: str,
        mode: SearchMode,
    ) -> QLabel:
        field = _highlighted_html(hit.field_label, query, mode=mode, bold=True)
        record = f"{hit.record_name} · " if hit.record_name else ""
        context = f"{location} · {record}{_compact_breadcrumb(hit.breadcrumb)}"
        contextual_html = _highlighted_html(context, query, mode=mode)
        muted = active_color_scheme().muted
        html = (
            f"{field}<br>"
            f'<span style="color: {muted}; font-size: 85%;">'
            f"{contextual_html}</span>"
        )
        return self._install_label(item, 0, html)

    def _highlight_cell(
        self,
        item: QTreeWidgetItem,
        column: int,
        value: str,
        query: str,
        mode: SearchMode,
        *,
        bold: bool = False,
    ) -> QLabel:
        html = _highlighted_html(value, query, mode=mode, bold=bold)
        return self._install_label(item, column, html)

    def _install_label(self, item: QTreeWidgetItem, column: int, html: str) -> QLabel:
        label = QLabel(html, self.results)
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        label.setStyleSheet("background: transparent; padding: 0 4px;")
        label.setToolTip(item.toolTip(column))
        self.results.setItemWidget(item, column, label)
        item.setText(column, "")
        return label

    def _item_activated(self, item: QTreeWidgetItem, _column: int) -> None:
        hit = item.data(0, HIT_ROLE)
        if isinstance(hit, SearchHit):
            self._navigate_to_hit(hit, activate=True)

    def _item_highlighted(
        self,
        item: QTreeWidgetItem | None,
        _previous: QTreeWidgetItem | None,
    ) -> None:
        if item is None:
            return
        hit = item.data(0, HIT_ROLE)
        if isinstance(hit, SearchHit):
            self._navigate_to_hit(hit, activate=False)

    def _activate_current_result(self) -> None:
        item = self.results.currentItem()
        if item is not None:
            self._item_activated(item, self.results.currentColumn())

    def _copy_current_result(self) -> None:
        item = self.results.currentItem()
        if item is None:
            return
        hit = item.data(0, HIT_ROLE)
        if isinstance(hit, SearchHit):
            self._copy_hit_json(hit)

    def _show_context_menu(self, position) -> None:  # noqa: ANN001
        item = self.results.itemAt(position)
        if item is None:
            return
        hit = item.data(0, HIT_ROLE)
        if not isinstance(hit, SearchHit):
            return
        menu = QMenu(self.results)
        copy_value = menu.addAction("Copy value preview")
        copy_field_path = menu.addAction("Copy field path")
        copy_context = menu.addAction("Copy context breadcrumb")
        copy_filename = menu.addAction("Copy evaluation filename")
        copy_filename.setEnabled(bool(hit.record_name))
        menu.addSeparator()
        copy_json = menu.addAction("Copy result as JSON")
        selected = menu.exec(self.results.viewport().mapToGlobal(position))
        if selected is copy_value:
            self._copy_hit_value(hit)
        elif selected is copy_field_path:
            self._copy_hit_field_path(hit)
        elif selected is copy_context:
            self._copy_text(hit.breadcrumb, "Copied context breadcrumb")
        elif selected is copy_filename:
            self._copy_text(hit.record_name, "Copied evaluation filename")
        elif selected is copy_json:
            self._copy_hit_json(hit)

    def _copy_hit_value(self, hit: SearchHit) -> None:
        self._copy_text(hit.preview, "Copied search value preview")

    def _copy_hit_field_path(self, hit: SearchHit) -> None:
        self._copy_text(_hit_field_path(hit), "Copied search field path")

    def _copy_hit_json(self, hit: SearchHit) -> None:
        payload = {
            "evaluation": hit.record_name,
            "record_id": hit.record_id,
            "component": hit.breadcrumb,
            "node_path": list(hit.node_path),
            "section": hit.section,
            "value_path": list(hit.value_path),
            "field": hit.field_label,
            "preview": hit.preview,
            "score": hit.score,
        }
        self._copy_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            "Copied search result as JSON",
        )

    def _copy_text(self, text: str, message: str) -> None:
        QApplication.clipboard().setText(text)
        self.copy_completed.emit(message)


def _compact_breadcrumb(value: str, maximum_parts: int = 3) -> str:
    parts = value.split(" / ")
    if len(parts) <= maximum_parts:
        return value
    return "... / " + " / ".join(parts[-maximum_parts:])


def _mode_label(mode: SearchMode) -> str:
    return {"fuzzy": "Fuzzy ranked", "exact": "Exact literal", "regex": "Regex"}[mode]


def _highlighted_html(
    value: str,
    query: str,
    *,
    mode: SearchMode = "fuzzy",
    bold: bool = False,
) -> str:
    """Render exact and fuzzy portions of a result as bold red text."""
    if mode == "fuzzy":
        matched = _fuzzy_match_indexes(value, query)
    elif mode == "exact":
        matched = _pattern_match_indexes(value, re.escape(query))
    else:
        matched = _pattern_match_indexes(value, query)
    parts: list[str] = []
    highlighted = False
    highlight_color = active_color_scheme().danger
    for index, character in enumerate(value):
        is_match = index in matched
        if is_match != highlighted:
            parts.append(
                f'<span style="color: {highlight_color}; font-weight: 700;">'
                if is_match
                else "</span>"
            )
            highlighted = is_match
        parts.append(escape(character))
    if highlighted:
        parts.append("</span>")
    text = "".join(parts)
    return f'<span style="font-weight: 700;">{text}</span>' if bold else text


def _pattern_match_indexes(value: str, expression: str) -> set[int]:
    try:
        matches = re.finditer(expression, value, re.IGNORECASE)
        return {
            index for match in matches for index in range(match.start(), match.end())
        }
    except re.error:
        return set()


def _fuzzy_match_indexes(value: str, query: str) -> set[int]:
    normalized_value, mapping = _normalized_with_mapping(value)
    normalized_query = _normalize(query)
    if not normalized_value or not normalized_query:
        return set()

    exact_start = normalized_value.find(normalized_query)
    if exact_start >= 0:
        return {
            index
            for index in mapping[exact_start : exact_start + len(normalized_query)]
            if index is not None
        }

    indexes: set[int] = set()
    words = list(re.finditer(r"[^ ]+", normalized_value))
    for token in normalized_query.split():
        token_start = normalized_value.find(token)
        if token_start >= 0:
            indexes.update(
                index
                for index in mapping[token_start : token_start + len(token)]
                if index is not None
            )
            continue

        closest = max(
            words,
            key=lambda word: ratio(token, word.group()),
            default=None,
        )
        if closest is not None and ratio(token, closest.group()) >= 68.0:
            indexes.update(
                index
                for index in mapping[closest.start() : closest.end()]
                if index is not None
            )
            continue

        positions: list[int] = []
        start = 0
        for character in token:
            position = normalized_value.find(character, start)
            if position < 0:
                positions = []
                break
            positions.append(position)
            start = position + 1
        if positions:
            span = positions[-1] - positions[0] + 1
            if 0.56 + 0.28 * len(token) / span >= 0.68:
                for position in positions:
                    original_index = mapping[position]
                    if original_index is not None:
                        indexes.add(original_index)
    return indexes


def _normalized_with_mapping(value: str) -> tuple[str, list[int | None]]:
    characters: list[str] = []
    mapping: list[int | None] = []
    pattern = r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)|[0-9]+(?:\.[0-9]+)?"
    for match in re.finditer(pattern, value):
        token = match.group().casefold()
        if characters:
            characters.append(" ")
            mapping.append(None)
        characters.extend(token)
        mapping.extend(range(match.start(), match.end()))
    return "".join(characters), mapping


def _hit_field_path(hit: SearchHit) -> str:
    section = hit.section or "context"
    result = section
    for segment in hit.value_path:
        result += f"[{segment}]" if isinstance(segment, int) else f"/{segment}"
    return result


def _coordinator_field_path(hit: SearchHit) -> tuple[str | int, ...] | None:
    """Translate the index's section-relative path to a context-relative path."""
    if hit.section is None:
        return None
    if hit.section == "metadata":
        return ("meta", *hit.value_path)
    return hit.value_path
