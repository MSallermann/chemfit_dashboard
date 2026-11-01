"""
Reusable field extraction, configuration persistence, and streaming exports.

Semantic ``FieldSelector`` objects resolve across differing evaluations while
preserving explicit missing/skipped/failure statuses. This module contains no
Qt code and is shared by the dashboard and the headless extraction CLI.
"""

from __future__ import annotations

# ruff: noqa: EM101, EM102, PLR0911
import csv
import json
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pydictnest

from chemfit_dashboard.semantic import (
    NodeTrail,
    NodeTrailSegment,
    component_trail_for_path,
    resolve_component_selection,
)
from chemfit_dashboard.source import ContextDocument, ContextSourceError, NodePath
from chemfit_dashboard.traversal import CancellationToken, iter_collection

get_nested = pydictnest.get_nested
_pydictnest_get_path = getattr(pydictnest, "get_path", None)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence

    from chemfit_dashboard.source import ContextSource, RecordRef

JsonPath = tuple[str | int, ...]
CONFIG_VERSION = 2
MAX_FLATTENED_COLUMNS = 512
MAX_EXPANDED_ROWS = 1_000_000
_MISSING = object()


class ExtractionStatus(str, Enum):
    """Reason an extracted value is available, absent, or unusable."""

    AVAILABLE = "available"
    EXPLICIT_NULL = "explicit_null"
    MISSING_COMPONENT = "missing_component"
    DECLARED_UNEXECUTED = "declared_unexecuted"
    SKIPPED_COMPONENT = "skipped_component"
    MISSING_FIELD = "missing_field"
    SERIALIZED_EXCEPTION = "serialized_exception"
    UNREADABLE_EVALUATION = "unreadable_evaluation"


@dataclass(frozen=True, slots=True)
class FieldSelector:
    """A reusable semantic field selection independent of any collection."""

    node_path: NodePath
    section: str
    value_path: JsonPath = ()
    column: str = "value"
    node_trail: NodeTrail | None = None
    flatten: bool = False
    array_expansion_depth: int = 0
    # Kept for source/config compatibility; expansion now always creates columns.
    expand_array: bool = False
    excluded_paths: tuple[JsonPath, ...] = ()
    flattened_column_names: tuple[tuple[JsonPath, str], ...] = ()

    @property
    def column_expansion_depth(self) -> int:
        """Return requested array levels, including legacy boolean configs."""
        return max(self.array_expansion_depth, int(self.expand_array))

    @property
    def path_label(self) -> str:
        """Return a human-readable semantic component and JSON path."""
        component = (
            "Evaluation root"
            if not self.node_trail
            else " / ".join(segment.name for segment in self.node_trail)
        )
        nested = format_json_path(self.value_path)
        suffix = f" / {nested}" if nested else ""
        return f"{component} / {self.section}{suffix}"


@dataclass(frozen=True, slots=True)
class ExtractionValue:
    """Extracted payload paired with its explicit availability status."""

    value: Any
    status: ExtractionStatus


@dataclass(frozen=True, slots=True)
class ExtractionRow:
    """Values extracted from one evaluation in source collection order."""

    index: int
    record_id: str
    filename: str
    values: tuple[ExtractionValue, ...]


@dataclass(frozen=True, slots=True)
class ExtractionConfig:
    """Persistent extraction selectors and export-column options."""

    selectors: tuple[FieldSelector, ...]
    include_identifiers: bool = True
    include_status_columns: bool = True
    version: int = CONFIG_VERSION

    def to_dict(self) -> dict[str, Any]:
        """Serialize the stable public configuration representation."""
        return {
            "version": self.version,
            "include_identifiers": self.include_identifiers,
            "include_status_columns": self.include_status_columns,
            "selectors": [_selector_to_dict(selector) for selector in self.selectors],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ExtractionConfig:
        """Validate and construct a configuration from decoded JSON."""
        version = value.get("version")
        if version not in {1, CONFIG_VERSION}:
            message = f"Unsupported extraction configuration version: {version!r}"
            raise ValueError(message)
        raw_selectors = value.get("selectors")
        if not isinstance(raw_selectors, list):
            raise ValueError("Extraction configuration selectors must be a list")
        return cls(
            selectors=tuple(_selector_from_dict(item) for item in raw_selectors),
            include_identifiers=bool(value.get("include_identifiers", True)),
            include_status_columns=bool(value.get("include_status_columns", True)),
        )


def selector_for_document(
    document: ContextDocument, selector: FieldSelector
) -> FieldSelector:
    """Attach a semantic component identity from the selected document."""
    return FieldSelector(
        node_path=selector.node_path,
        section=selector.section,
        value_path=selector.value_path,
        column=selector.column,
        node_trail=component_trail_for_path(document, selector.node_path),
        flatten=selector.flatten,
        array_expansion_depth=selector.array_expansion_depth,
        expand_array=selector.expand_array,
        excluded_paths=selector.excluded_paths,
        flattened_column_names=selector.flattened_column_names,
    )


def extract_fields(
    source: ContextSource,
    records: Iterable[RecordRef],
    selectors: Sequence[FieldSelector],
    *,
    cancellation: CancellationToken | None = None,
    progress: Callable[[int, RecordRef], None] | None = None,
) -> Iterator[ExtractionRow]:
    """Incrementally extract arbitrary values from a collection."""
    selected = tuple(selectors)
    _validate_selectors(selected)
    for index, item in enumerate(
        iter_collection(source, records, cancellation=cancellation)
    ):
        if item.document is None:
            unavailable = ExtractionValue(None, ExtractionStatus.UNREADABLE_EVALUATION)
            values = tuple(unavailable for _selector in selected)
        else:
            values = tuple(
                extract_value(item.document, selector) for selector in selected
            )
        row = ExtractionRow(
            index=index,
            record_id=item.record.id,
            filename=item.record.name,
            values=values,
        )
        if progress is not None:
            progress(index + 1, item.record)
        yield row


def extract_value(
    document: ContextDocument, selector: FieldSelector
) -> ExtractionValue:
    """Resolve one selector with explicit missing-value semantics."""
    if selector.node_trail is None:
        try:
            details = document.details(selector.node_path)
        except ContextSourceError:
            return ExtractionValue(None, ExtractionStatus.MISSING_COMPONENT)
    else:
        resolution = resolve_component_selection(
            document, selector.node_path, selector.node_trail
        )
        if not resolution.complete:
            return ExtractionValue(None, ExtractionStatus.MISSING_COMPONENT)
        details = document.details(resolution.path)

    if not details.info.recorded:
        return ExtractionValue(None, ExtractionStatus.DECLARED_UNEXECUTED)
    if details.info.status == "skipped":
        return ExtractionValue(None, ExtractionStatus.SKIPPED_COMPONENT)

    sections: dict[str, Any] = {
        "fields": details.fields,
        "metadata": details.metadata,
        "structure": details.structure,
        "raw": details.raw,
        "component": {
            "fields": details.fields,
            "metadata": details.metadata,
            "structure": details.structure,
        },
    }
    if selector.section not in sections:
        return ExtractionValue(None, ExtractionStatus.MISSING_FIELD)
    root = sections[selector.section]
    value = _get_json_path(root, selector.value_path, _MISSING)
    if value is _MISSING:
        return ExtractionValue(None, ExtractionStatus.MISSING_FIELD)
    if value is None:
        return ExtractionValue(None, ExtractionStatus.EXPLICIT_NULL)
    if _is_serialized_exception(value):
        return ExtractionValue(value, ExtractionStatus.SERIALIZED_EXCEPTION)
    return ExtractionValue(value, ExtractionStatus.AVAILABLE)


def export_jsonl(
    path: str | Path,
    source: ContextSource,
    records: Iterable[RecordRef],
    config: ExtractionConfig,
    *,
    cancellation: CancellationToken | None = None,
    progress: Callable[[int, RecordRef], None] | None = None,
    max_expanded_rows: int = MAX_EXPANDED_ROWS,
) -> int:
    """Stream one complete extraction row per evaluation to JSON Lines."""
    del max_expanded_rows  # Retained for API compatibility with version-1 callers.
    count = 0
    with Path(path).open("w", encoding="utf-8") as stream:
        for row in extract_fields(
            source,
            records,
            config.selectors,
            cancellation=cancellation,
            progress=progress,
        ):
            count += 1
            payload: dict[str, Any] = {}
            if config.include_identifiers:
                payload.update(
                    {
                        "collection_index": row.index,
                        "filename": row.filename,
                        "record_id": row.record_id,
                    }
                )
            values: dict[str, Any] = {}
            statuses: dict[str, str] = {}
            for selector, extracted in zip(config.selectors, row.values, strict=True):
                values[selector.column] = extracted.value
                statuses[selector.column] = extracted.status.value
            payload["values"] = values
            payload["status"] = statuses
            stream.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
            stream.write("\n")
    return count


def export_csv(
    path: str | Path,
    source: ContextSource,
    records: Iterable[RecordRef],
    config: ExtractionConfig,
    *,
    cancellation: CancellationToken | None = None,
    progress: Callable[[int, RecordRef], None] | None = None,
    max_flattened_columns: int = MAX_FLATTENED_COLUMNS,
    max_expanded_rows: int = MAX_EXPANDED_ROWS,
) -> int:
    """Stream extraction rows to CSV, JSON-encoding structured cells."""
    del max_expanded_rows  # Retained for API compatibility with version-1 callers.
    record_list = list(records)
    flat_paths = _collect_flat_paths(
        source,
        record_list,
        config.selectors,
        cancellation=cancellation,
        progress=progress,
        maximum=max_flattened_columns,
    )
    completed_offset = len(record_list) if flat_paths else 0

    def export_progress(completed: int, record: RecordRef) -> None:
        if progress is not None:
            progress(completed_offset + completed, record)

    value_columns: list[tuple[int, JsonPath | None, str]] = []
    for selector_index, selector in enumerate(config.selectors):
        paths = flat_paths.get(selector_index)
        if selector.flatten or selector.column_expansion_depth:
            value_columns.extend(
                (
                    selector_index,
                    nested_path,
                    flattened_column_name(selector, nested_path),
                )
                for nested_path in paths or ()
            )
        else:
            value_columns.append((selector_index, None, selector.column))
    headers = (
        ["collection_index", "filename", "record_id"]
        if config.include_identifiers
        else []
    )
    headers.extend(column for _index, _path, column in value_columns)
    if config.include_status_columns:
        headers.extend(f"{selector.column}__status" for selector in config.selectors)
    if len({header.casefold() for header in headers}) != len(headers):
        raise ValueError("Extraction column names are not unique")

    count = 0
    with Path(path).open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(headers)
        for row in extract_fields(
            source,
            record_list,
            config.selectors,
            cancellation=cancellation,
            progress=export_progress,
        ):
            count += 1
            output: list[Any] = (
                [row.index, row.filename, row.record_id]
                if config.include_identifiers
                else []
            )
            for selector_index, nested_path, _column in value_columns:
                extracted = row.values[selector_index]
                value = extracted.value
                if nested_path is not None:
                    value = _get_json_path(value, nested_path, _MISSING)
                output.append(_csv_value(value, extracted.status))
            if config.include_status_columns:
                output.extend(value.status.value for value in row.values)
            writer.writerow(output)
    return count


def save_config(path: str | Path, config: ExtractionConfig) -> None:
    Path(path).write_text(
        json.dumps(config.to_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def load_config(path: str | Path) -> ExtractionConfig:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Extraction configuration must be a JSON object")
    return ExtractionConfig.from_dict(value)


def format_json_path(path: JsonPath) -> str:
    """Format a mixed path using collision-safe JSON Pointer escaping."""
    return "/".join(str(part).replace("~", "~0").replace("/", "~1") for part in path)


def flattened_column_name(selector: FieldSelector, path: JsonPath) -> str:
    """Return the configured display/export name for one flattened leaf."""
    renamed = dict(selector.flattened_column_names).get(path)
    return renamed or f"{selector.column}.{format_json_path(path)}"


def _get_json_path(root: Any, path: JsonPath, default: Any) -> Any:
    if _pydictnest_get_path is not None:
        return _pydictnest_get_path(root, path, default)
    if not path:
        return root
    # pydictnest remains the canonical implementation for mapping-only paths.
    if all(isinstance(part, str) for part in path):
        string_path = tuple(str(part) for part in path)
        return (
            get_nested(root, string_path, default=default)
            if isinstance(root, dict)
            else default
        )
    value = root
    for part in path:
        if isinstance(part, str) and isinstance(value, dict):
            if part not in value:
                return default
            value = value[part]
        elif (
            isinstance(part, int) and isinstance(value, list) and 0 <= part < len(value)
        ):
            value = value[part]
        else:
            return default
    return value


def _collect_flat_paths(
    source: ContextSource,
    records: Sequence[RecordRef],
    selectors: Sequence[FieldSelector],
    *,
    cancellation: CancellationToken | None,
    progress: Callable[[int, RecordRef], None] | None,
    maximum: int,
) -> dict[int, tuple[JsonPath, ...]]:
    requested = {
        index
        for index, selector in enumerate(selectors)
        if selector.flatten or selector.column_expansion_depth
    }
    if not requested:
        return {}
    discovered: dict[int, set[JsonPath]] = {index: set() for index in requested}
    for row in extract_fields(
        source,
        records,
        selectors,
        cancellation=cancellation,
        progress=progress,
    ):
        for index in requested:
            value = row.values[index].value
            selector = selectors[index]
            discovered[index].update(
                column_paths_for_value(
                    value,
                    flatten_mappings=selector.flatten,
                    array_depth=selector.column_expansion_depth,
                )
            )
            if sum(map(len, discovered.values())) > maximum:
                raise ValueError(
                    f"Column expansion exceeds the {maximum:,}-column safeguard"
                )
    return {
        index: tuple(
            path
            for path in sorted(paths, key=lambda item: tuple(map(str, item)))
            if path not in selectors[index].excluded_paths
        )
        for index, paths in discovered.items()
    }


def column_paths_for_value(
    value: Any,
    *,
    flatten_mappings: bool,
    array_depth: int,
    path: JsonPath = (),
) -> Iterator[JsonPath]:
    """Yield output-column paths under independent object/array policies."""
    if flatten_mappings and isinstance(value, dict):
        if not value:
            yield path
            return
        for key in sorted(value, key=str):
            yield from column_paths_for_value(
                value[key],
                flatten_mappings=True,
                array_depth=array_depth,
                path=(*path, str(key)),
            )
        return
    if array_depth > 0 and isinstance(value, list):
        if not value:
            yield path
            return
        for index, child in enumerate(value):
            yield from column_paths_for_value(
                child,
                flatten_mappings=flatten_mappings,
                array_depth=array_depth - 1,
                path=(*path, index),
            )
        return
    yield path


def _validate_selectors(selectors: Sequence[FieldSelector]) -> None:
    columns = [selector.column.casefold() for selector in selectors]
    if any(not selector.column.strip() for selector in selectors):
        raise ValueError("Extraction column names cannot be empty")
    if len(columns) != len(set(columns)):
        raise ValueError("Extraction column names must be unique")
    if any(selector.array_expansion_depth < 0 for selector in selectors):
        raise ValueError("Array expansion depth cannot be negative")


def _csv_value(value: Any, status: ExtractionStatus) -> str | int | float | bool:
    if value is _MISSING or status not in {
        ExtractionStatus.AVAILABLE,
        ExtractionStatus.EXPLICIT_NULL,
        ExtractionStatus.SERIALIZED_EXCEPTION,
    }:
        return ""
    if value is None:
        return "null"
    if isinstance(value, (dict, list)):
        return json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def _selector_to_dict(selector: FieldSelector) -> dict[str, Any]:
    return {
        "component": {
            "preferred_path": list(selector.node_path),
            "trail": [asdict(segment) for segment in selector.node_trail]
            if selector.node_trail is not None
            else None,
        },
        "section": selector.section,
        "path": list(selector.value_path),
        "column": selector.column,
        "flatten": selector.flatten,
        "array_expansion_depth": selector.array_expansion_depth,
        "expand_array": selector.expand_array,
        "excluded_paths": [list(path) for path in selector.excluded_paths],
        "flattened_column_names": [
            {"path": list(path), "column": column}
            for path, column in selector.flattened_column_names
        ],
    }


def _selector_from_dict(value: Any) -> FieldSelector:
    if not isinstance(value, dict):
        raise ValueError("Each extraction selector must be an object")
    component = value.get("component")
    if not isinstance(component, dict):
        raise ValueError("Selector component identity must be an object")
    raw_trail = component.get("trail")
    trail = (
        tuple(NodeTrailSegment(**segment) for segment in raw_trail)
        if isinstance(raw_trail, list)
        else None
    )
    raw_names = value.get("flattened_column_names", ())
    flattened_column_names = tuple(
        (tuple(item.get("path", ())), str(item.get("column", "")))
        for item in raw_names
        if isinstance(item, dict) and str(item.get("column", "")).strip()
    )
    return FieldSelector(
        node_path=tuple(component.get("preferred_path", ())),
        section=str(value.get("section", "fields")),
        value_path=tuple(value.get("path", ())),
        column=str(value.get("column", "value")),
        node_trail=trail,
        flatten=bool(value.get("flatten", False)),
        array_expansion_depth=max(
            0,
            int(
                value.get(
                    "array_expansion_depth",
                    1 if value.get("expand_array", False) else 0,
                )
            ),
        ),
        expand_array=bool(value.get("expand_array", False)),
        excluded_paths=tuple(
            tuple(path)
            for path in value.get("excluded_paths", ())
            if isinstance(path, list)
        ),
        flattened_column_names=flattened_column_names,
    )


def _is_serialized_exception(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    exception_name, separator, _message = value.partition(": ")
    return bool(separator) and exception_name.endswith(("Error", "Exception"))
