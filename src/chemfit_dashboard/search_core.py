from __future__ import annotations

import heapq
import re
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Literal

from rapidfuzz.fuzz import ratio

from chemfit_dashboard.source import ContextDocument, NodePath

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

MAX_INDEX_ENTRIES = 20_000
MAX_LIST_ITEMS = 100
MAX_VALUE_DEPTH = 10
MAX_RESULTS = 120
COOPERATIVE_SEARCH_INTERVAL = 256
SearchMode = Literal["fuzzy", "exact", "regex"]


class SearchQueryError(ValueError):
    """A search expression that cannot be evaluated."""


def clear_search_caches() -> None:
    """Clear bounded process-wide scoring caches for cold-cache measurements."""
    _normalize.cache_clear()
    _similarity.cache_clear()


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
    entry_order: int = 0


class ContextSearchIndex:
    """Bounded fuzzy-search index for one context document."""

    def __init__(
        self,
        document: ContextDocument,
        *,
        cooperative: bool = False,
        cancelled: Callable[[], bool] | None = None,
    ):
        """Index context identities and nested fields without retaining the document."""
        self.entries = tuple(
            _index_document(
                document,
                cooperative=cooperative,
                cancelled=cancelled,
            )
        )

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
        cooperative: bool = False,
        cancelled: Callable[[], bool] | None = None,
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
        threshold = _score_threshold(normalized) if mode == "fuzzy" else 0.0
        for entry_index, entry in enumerate(self.entries, start=1):
            if cooperative and entry_index % COOPERATIVE_SEARCH_INTERVAL == 0:
                if cancelled is not None and cancelled():
                    return []
                time.sleep(0)
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
                        entry_order=entry_index,
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
        hit.entry_order,
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


def _index_document(
    document: ContextDocument,
    *,
    cooperative: bool = False,
    cancelled: Callable[[], bool] | None = None,
):
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
        if cooperative and count % COOPERATIVE_SEARCH_INTERVAL == 0:
            if cancelled is not None and cancelled():
                return
            time.sleep(0)
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
                if cooperative and count % COOPERATIVE_SEARCH_INTERVAL == 0:
                    if cancelled is not None and cancelled():
                        return
                    time.sleep(0)


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


@lru_cache(maxsize=16_384)
def _similarity(query: str, candidate: str) -> float:
    """Score one normalized pair, retaining common collection-wide candidates."""
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
            ratio(query, word) / 100.0
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


@lru_cache(maxsize=16_384)
def _normalize(value: str) -> str:
    """Normalize search text with a bounded cache shared by transient indexes."""
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
