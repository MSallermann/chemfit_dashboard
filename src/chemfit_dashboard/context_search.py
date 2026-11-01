"""
Qt presentation for context-search results.

Search indexing and ranking live in :mod:`search_core`; this module owns result
tables, match highlighting, clipboard actions, and navigation signals. Core
search names are re-exported for compatibility with earlier callers.
"""

from __future__ import annotations

import json
import re
from html import escape

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHeaderView,
    QLabel,
    QMenu,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from rapidfuzz.fuzz import ratio

from chemfit_dashboard.appearance import active_color_scheme
from chemfit_dashboard.search_core import (
    MAX_RESULTS,
    ContextSearchIndex,
    SearchHit,
    SearchMode,
    SearchQueryError,
    TopKSearchHits,
    _normalize,
    clear_search_caches,
    rank_search_hits,
)

__all__ = [
    "MAX_RESULTS",
    "ContextSearchIndex",
    "ContextSearchPanel",
    "SearchHit",
    "SearchMode",
    "SearchQueryError",
    "TopKSearchHits",
    "clear_search_caches",
    "rank_search_hits",
]

HIT_ROLE = Qt.ItemDataRole.UserRole.value


class ContextSearchPanel(QWidget):
    """
    Globally ranked search results with contextual hierarchy labels.

    The panel may search one installed index directly, but collection-wide work
    is coordinated by MainWindow. It stores rendered ``SearchHit`` objects and
    emits the selected hit without loading or mutating documents.
    """

    hit_selected = Signal(object)
    copy_completed = Signal(str)

    def __init__(self, parent: QWidget | None = None):
        """Initialize an empty field-search result view."""
        super().__init__(parent)
        self._index: ContextSearchIndex | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.status = QLabel("Type above to fuzzy-search context fields and values.")
        self.status.setObjectName("mutedLabel")
        self.status.setWordWrap(True)
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

    def set_index(self, index: ContextSearchIndex | None) -> None:
        """Set the current-evaluation index used by direct panel searches."""
        self._index = index
        self.results.clear()
        self.status.setText(
            "Type above to fuzzy-search context fields and values."
            if index is not None
            else "Select an evaluation to search its fields."
        )

    def set_available(self, available: bool) -> None:
        """Update availability without constructing an index on the UI thread."""
        self._index = None
        self.results.clear()
        self.status.setText(
            "Type above to search context fields and values."
            if available
            else "Select an evaluation to search its fields."
        )

    def show_loading(self, text: str) -> None:
        """Report replacement work without synchronously destroying result widgets."""
        self.status.setText(text)

    def show_message(self, text: str) -> None:
        """Clear stale hits and display an informational or error state."""
        self.results.clear()
        self.status.setText(text)

    def search(
        self,
        query: str,
        *,
        include_keys: bool = True,
        include_values: bool = True,
        include_metadata: bool = True,
        include_components: bool = True,
        mode: SearchMode = "fuzzy",
    ) -> None:
        """Search the installed index and render ranked hits synchronously."""
        self.results.clear()
        if self._index is None:
            self.status.setText("Select an evaluation to search its fields.")
            return
        if not query.strip():
            self.status.setText("Type above to fuzzy-search context fields and values.")
            return
        try:
            hits = self._index.search(
                query,
                include_keys=include_keys,
                include_values=include_values,
                include_metadata=include_metadata,
                include_components=include_components,
                mode=mode,
            )
        except SearchQueryError as error:
            self.status.setText(f"Invalid regular expression: {error}")
            return
        self.show_results(
            hits,
            query,
            include_keys=include_keys,
            include_values=include_values,
            include_metadata=include_metadata,
            include_components=include_components,
            mode=mode,
        )

    def show_results(
        self,
        hits: list[SearchHit],
        query: str,
        *,
        include_keys: bool,
        include_values: bool,
        mode: SearchMode,
        include_metadata: bool = True,
        include_components: bool = True,
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
            self.hit_selected.emit(hit)

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
