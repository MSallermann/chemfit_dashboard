from __future__ import annotations

import heapq
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from html import escape
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Iterable

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QLabel,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from chemfit_dashboard.appearance import active_color_scheme
from chemfit_dashboard.source import ContextDocument, NodePath

MAX_INDEX_ENTRIES = 20_000
MAX_LIST_ITEMS = 100
MAX_VALUE_DEPTH = 10
MAX_RESULTS = 120
HIT_ROLE = Qt.ItemDataRole.UserRole.value
SearchMode = Literal["fuzzy", "exact", "regex"]


class SearchQueryError(ValueError):
    """A search expression that cannot be evaluated."""


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One searchable context node or nested context value."""

    node_path: NodePath
    breadcrumb: str
    context_status: str
    section: str | None
    value_path: tuple[str | int, ...]
    field_label: str
    preview: str
    score: float = 0.0
    record_id: str = ""
    record_name: str = ""


class ContextSearchIndex:
    """Bounded fuzzy-search index for one context document."""

    def __init__(self, document: ContextDocument):
        """Index context identities and nested fields without mutating the document."""
        self.entries = tuple(_index_document(document))

    def search(
        self,
        query: str,
        limit: int = MAX_RESULTS,
        *,
        include_keys: bool = True,
        include_values: bool = True,
        include_metadata: bool = True,
        include_components: bool = True,
        mode: SearchMode = "fuzzy",
    ) -> list[SearchHit]:
        """Return ranked matches for field, hierarchy, section, and value."""
        query = query.strip()
        if not query:
            return []
        pattern = None
        if mode == "regex":
            try:
                pattern = re.compile(query, re.IGNORECASE)
            except re.error as error:
                raise SearchQueryError(str(error)) from error
        elif mode not in ("fuzzy", "exact"):
            message = f"Unknown search mode: {mode}"
            raise SearchQueryError(message)
        normalized = _normalize(query) if mode == "fuzzy" else query
        if not normalized:
            return []
        matches: list[SearchHit] = []
        for entry in self.entries:
            if not include_metadata and entry.section == "metadata":
                continue
            score = _entry_score(
                normalized,
                entry,
                include_keys=include_keys,
                include_values=include_values,
                include_components=include_components,
                mode=mode,
                pattern=pattern,
            )
            threshold = _score_threshold(normalized) if mode == "fuzzy" else 0.0
            is_match = score >= threshold if mode == "fuzzy" else score > 0.0
            if is_match:
                matches.append(
                    SearchHit(
                        node_path=entry.node_path,
                        breadcrumb=entry.breadcrumb,
                        context_status=entry.context_status,
                        section=entry.section,
                        value_path=entry.value_path,
                        field_label=entry.field_label,
                        preview=entry.preview,
                        score=score,
                        record_id=entry.record_id,
                        record_name=entry.record_name,
                    )
                )
        return rank_search_hits(matches, limit)


def _rank_key(hit: SearchHit) -> tuple[Any, ...]:
    return (
        -hit.score,
        hit.record_name.casefold(),
        len(hit.breadcrumb),
        len(hit.value_path),
        hit.breadcrumb.casefold(),
        hit.field_label.casefold(),
    )


@dataclass(order=False)
class _WorstFirst:
    key: tuple[Any, ...]
    hit: SearchHit

    def __lt__(self, other: _WorstFirst) -> bool:
        return self.key > other.key


class TopKSearchHits:
    """Retain only the globally best deterministic search results."""

    def __init__(self, limit: int = MAX_RESULTS):
        """Create a collector retaining at most ``limit`` results."""
        self.limit = max(0, limit)
        self._heap: list[_WorstFirst] = []

    def add(self, hit: SearchHit) -> None:
        if not self.limit:
            return
        item = _WorstFirst(_rank_key(hit), hit)
        if len(self._heap) < self.limit:
            heapq.heappush(self._heap, item)
        elif item.key < self._heap[0].key:
            heapq.heapreplace(self._heap, item)

    def extend(self, hits: Iterable[SearchHit]) -> None:
        for hit in hits:
            self.add(hit)

    def results(self) -> list[SearchHit]:
        return sorted((item.hit for item in self._heap), key=_rank_key)


def rank_search_hits(
    hits: Iterable[SearchHit], limit: int = MAX_RESULTS
) -> list[SearchHit]:
    """Globally rank hits from one or many context documents."""
    collector = TopKSearchHits(limit)
    collector.extend(hits)
    return collector.results()


class ContextSearchPanel(QWidget):
    """Globally ranked search results with contextual hierarchy labels."""

    hit_selected = Signal(object)

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
        self.results.currentItemChanged.connect(self._current_item_changed)
        layout.addWidget(self.results, 1)

    def set_index(self, index: ContextSearchIndex | None) -> None:
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
        self.results.clear()
        self.status.setText(text)

    def show_message(self, text: str) -> None:
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

    def _current_item_changed(
        self, current: QTreeWidgetItem | None, _previous: QTreeWidgetItem | None
    ) -> None:
        if current is None:
            return
        hit = current.data(0, HIT_ROLE)
        if isinstance(hit, SearchHit):
            self.hit_selected.emit(hit)


def _index_document(document: ContextDocument):
    count = 0
    for node in document.nodes:
        breadcrumb = _breadcrumb(document, node.path)
        yield SearchHit(
            node_path=node.path,
            breadcrumb=breadcrumb,
            context_status=node.status,
            section=None,
            value_path=(),
            field_label="Context",
            preview=f"{node.kind} · {node.status}",
            record_id=document.record.id,
            record_name=document.record.name,
        )
        count += 1
        details = document.details(node.path)
        for section, value in (
            ("fields", details.fields),
            ("metadata", details.metadata),
        ):
            for value_path, nested_value in _walk_values(value):
                if count >= MAX_INDEX_ENTRIES:
                    return
                yield SearchHit(
                    node_path=node.path,
                    breadcrumb=breadcrumb,
                    context_status=node.status,
                    section=section,
                    value_path=value_path,
                    field_label=_format_value_path(value_path),
                    preview=_value_preview(nested_value),
                    record_id=document.record.id,
                    record_name=document.record.name,
                )
                count += 1


def _walk_values(
    value: Any,
    path: tuple[str | int, ...] = (),
    depth: int = 0,
):
    if depth >= MAX_VALUE_DEPTH:
        return
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = (*path, str(key))
            yield child_path, child
            yield from _walk_values(child, child_path, depth + 1)
    elif isinstance(value, list) and len(value) <= MAX_LIST_ITEMS:
        for index, child in enumerate(value):
            child_path = (*path, index)
            yield child_path, child
            yield from _walk_values(child, child_path, depth + 1)


def _breadcrumb(document: ContextDocument, path: NodePath) -> str:
    labels = ["Evaluation root"]
    labels.extend(
        document.component_label(path[:depth]) for depth in range(1, len(path) + 1)
    )
    return " / ".join(labels)


def _format_value_path(path: tuple[str | int, ...]) -> str:
    result = ""
    for segment in path:
        if isinstance(segment, int):
            result += f"[{segment}]"
        else:
            result += f".{segment}" if result else segment
    return result


def _compact_breadcrumb(value: str, maximum_parts: int = 3) -> str:
    parts = value.split(" / ")
    if len(parts) <= maximum_parts:
        return value
    return "... / " + " / ".join(parts[-maximum_parts:])


def _value_preview(value: Any, limit: int = 80) -> str:
    if isinstance(value, dict):
        text = f"{{{len(value)} fields}}"
    elif isinstance(value, list):
        text = f"[{len(value)} items]"
    elif value is None:
        text = "null"
    elif isinstance(value, bool):
        text = "true" if value else "false"
    else:
        text = str(value)
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def _entry_score(
    query: str,
    entry: SearchHit,
    *,
    include_keys: bool,
    include_values: bool,
    include_components: bool,
    mode: SearchMode,
    pattern: re.Pattern[str] | None,
) -> float:
    hierarchy = [(entry.breadcrumb, 0.92)] if include_components else []
    direct: list[tuple[str, float]] = []
    if include_keys:
        direct.extend(
            (
                (entry.field_label, 1.0),
                (entry.section or "context", 0.7),
            )
        )
    if include_values:
        direct.append((entry.preview, 0.98))
    candidates = direct + hierarchy if entry.section is None else direct
    if not candidates:
        return 0.0
    if mode == "exact":
        folded_query = query.casefold()
        scores = [
            _literal_score(folded_query, value.casefold()) * weight
            for value, weight in candidates
        ]
        return max(scores, default=0.0)
    if mode == "regex":
        assert pattern is not None
        scores = [_regex_score(pattern, value) * weight for value, weight in candidates]
        return max(scores, default=0.0)

    direct = [(_normalize(value), weight) for value, weight in direct]
    hierarchy = [(_normalize(value), weight) for value, weight in hierarchy]
    candidates = direct + hierarchy
    full_candidates = candidates if entry.section is None else direct
    full_score = max(
        (_similarity(query, value) * weight for value, weight in full_candidates),
        default=0.0,
    )
    tokens = query.split()
    if entry.section is not None and not any(
        _similarity(token, value) >= 0.68
        for token in tokens
        for value, _weight in direct
    ):
        return 0.0
    raw_token_scores = [
        max(_similarity(token, value) for value, _weight in candidates)
        for token in tokens
    ]
    if any(score < 0.68 for score in raw_token_scores):
        return full_score if full_score >= 0.72 else 0.0
    token_score = sum(
        max(_similarity(token, value) * weight for value, weight in candidates)
        for token in tokens
    ) / len(tokens)
    return max(full_score, token_score * 0.96)


def _similarity(query: str, candidate: str) -> float:
    if not query or not candidate:
        return 0.0
    if query == candidate:
        return 1.0
    if candidate.startswith(query):
        return 0.96
    if query in candidate:
        return 0.9 + 0.05 * len(query) / len(candidate)
    if any(character.isdigit() for character in query):
        return 0.0
    words = candidate.split()
    word_ratio = max(
        (
            SequenceMatcher(None, query, word).ratio()
            for word in words
            if abs(len(query) - len(word)) <= max(2, len(query) // 3)
        ),
        default=0.0,
    )
    positions: list[int] = []
    start = 0
    for character in query:
        position = candidate.find(character, start)
        if position < 0:
            break
        positions.append(position)
        start = position + 1
    subsequence = 0.0
    if len(positions) == len(query):
        span = positions[-1] - positions[0] + 1
        subsequence = 0.56 + 0.28 * len(query) / span
    return max(word_ratio, subsequence)


def _normalize(value: str) -> str:
    camel_spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", value)
    return " ".join(re.findall(r"[a-z]+|[0-9]+(?:\.[0-9]+)?", camel_spaced.casefold()))


def _score_threshold(query: str) -> float:
    return 0.8 if len(query) == 1 else 0.5


def _literal_score(query: str, candidate: str) -> float:
    if query == candidate:
        return 1.0
    if query in candidate:
        return 0.9 + 0.08 * len(query) / max(1, len(candidate))
    return 0.0


def _regex_score(pattern: re.Pattern[str], candidate: str) -> float:
    match = pattern.search(candidate)
    if match is None:
        return 0.0
    return 0.9 + 0.08 * (match.end() - match.start()) / max(1, len(candidate))


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
            key=lambda word: SequenceMatcher(None, token, word.group()).ratio(),
            default=None,
        )
        if (
            closest is not None
            and SequenceMatcher(None, token, closest.group()).ratio() >= 0.68
        ):
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
