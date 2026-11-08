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
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pydictnest

from chemfit_dashboard.context_document import (
    ContextDocument,
    ContextDocumentError,
    NodePath,
)
from chemfit_dashboard.traversal import CancellationToken, iter_collection
from chemfit_dashboard.util import format_json_path

get_nested = pydictnest.get_nested
_pydictnest_get_path = getattr(pydictnest, "get_path", None)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence

    from chemfit_dashboard.source import ContextSource, RecordRef

JsonPath = tuple[str | int, ...]
CONFIG_VERSION = 3
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

    # Describes the where the context sits within the record context hierarchy
    node_path: NodePath
    # Describes where in the context dict the required field is
    value_path: JsonPath = ()


@dataclass(slots=True)
class OutputDescriptor:
    """A reusable output description for extractions."""

    # the column label to be used
    column: str = "value"
    # if a dict shaped field should be flattened
    flatten: bool = False
    # to what level arrya fields should be expanded
    array_expansion_depth: int = 0
    # these paths are excluded from the extraction
    excluded_paths: tuple[JsonPath, ...] = ()
    # if any of the flattened columns should be renamed
    flattened_column_names: tuple[tuple[JsonPath, str], ...] = ()


@dataclass(frozen=True, slots=True)
class ExtractionValue:
    """Extracted payload paired with its explicit availability status."""

    value: Any
    status: ExtractionStatus

    @property
    def is_numeric(self) -> bool:
        """Whether the extracted value is an available number."""

        return (
            self.status == ExtractionStatus.AVAILABLE
            and isinstance(self.value, (int, float))
            and not isinstance(self.value, bool)
        )


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

    fields: tuple[tuple[FieldSelector, OutputDescriptor], ...]

    include_identifiers: bool = True
    include_status_columns: bool = True
    version: int = CONFIG_VERSION

    def to_dict(self) -> dict[str, Any]:
        """Serialize the stable public configuration representation."""
        return {
            "version": self.version,
            "include_identifiers": self.include_identifiers,
            "include_status_columns": self.include_status_columns,
            "fields": [
                field_to_dict(selector, output) for selector, output in self.fields
            ],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ExtractionConfig:
        """Validate and construct a configuration from decoded JSON."""
        version = value.get("version")
        if version not in {3}:
            message = f"Unsupported extraction configuration version: {version!r}"
            raise ValueError(message)

        raw_fields = value.get("fields")
        if not isinstance(raw_fields, list):
            msg = "Extraction configuration selectors must be a list"
            raise ValueError(msg)
        return cls(
            fields=tuple(_field_from_dict(item) for item in raw_fields),
            include_identifiers=bool(value.get("include_identifiers", True)),
            include_status_columns=bool(value.get("include_status_columns", True)),
        )


def extract_fields(
    source: ContextSource,
    records: Iterable[RecordRef],
    selectors: Sequence[FieldSelector],
    *,
    cancellation: CancellationToken | None = None,
    progress: Callable[[int, RecordRef], None] | None = None,
) -> Iterator[ExtractionRow]:
    """Incrementally extract arbitrary values from a collection of records."""

    selected = tuple(selectors)

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
    """Extract one value from a context."""

    try:
        details = document.details(selector.node_path)
    except ContextDocumentError:
        return ExtractionValue(None, ExtractionStatus.MISSING_COMPONENT)

    if not details.info.recorded:
        return ExtractionValue(None, ExtractionStatus.DECLARED_UNEXECUTED)

    if details.info.status == "skipped":
        return ExtractionValue(None, ExtractionStatus.SKIPPED_COMPONENT)

    value = _get_json_path(details.raw, selector.value_path, _MISSING)

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
) -> int:
    """Stream one extraction row per evaluation to JSON Lines."""

    selectors = tuple(selector for selector, _ in config.fields)
    _validate_output_descriptors([o for _, o in config.fields])

    count = 0
    with Path(path).open("w", encoding="utf-8") as stream:
        for row in extract_fields(
            source,
            records,
            selectors,
            cancellation=cancellation,
            progress=progress,
        ):
            payload = {}

            if config.include_identifiers:
                payload.update(
                    {
                        "collection_index": row.index,
                        "filename": row.filename,
                        "record_id": row.record_id,
                    }
                )

            payload["values"] = {
                descriptor.column: extracted.value
                for (_, descriptor), extracted in zip(
                    config.fields, row.values, strict=True
                )
            }

            payload["status"] = {
                descriptor.column: extracted.status.value
                for (_, descriptor), extracted in zip(
                    config.fields, row.values, strict=True
                )
            }

            stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
            count += 1

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
) -> int:
    """Stream extraction rows to CSV, expanding dictionaries and arrays into columns."""

    record_list = list(records)
    selectors = tuple(selector for selector, _ in config.fields)
    _validate_output_descriptors([o for _, o in config.fields])

    # First pass: discover the columns needed for flattened or expanded values.
    flat_paths = _collect_flat_paths(
        source,
        record_list,
        config.fields,
        cancellation=cancellation,
        progress=progress,
        maximum=max_flattened_columns,
    )

    completed_offset = len(record_list) if flat_paths else 0

    def export_progress(completed: int, record: RecordRef) -> None:
        if progress is not None:
            progress(completed_offset + completed, record)

    # Build the output columns: (selector index, relative value path, column name).
    value_columns: list[tuple[int, JsonPath | None, str]] = []

    for index, (_, descriptor) in enumerate(config.fields):
        if descriptor.flatten or descriptor.array_expansion_depth:
            value_columns.extend(
                (
                    index,
                    nested_path,
                    flattened_column_name(descriptor, nested_path),
                )
                for nested_path in flat_paths.get(index, ())
            )
        else:
            value_columns.append((index, None, descriptor.column))

    # Build and validate the CSV header.
    headers = (
        ["collection_index", "filename", "record_id"]
        if config.include_identifiers
        else []
    )

    headers.extend(column for _, _, column in value_columns)

    if config.include_status_columns:
        headers.extend(
            f"{descriptor.column}__status" for _, descriptor in config.fields
        )

    if len({header.casefold() for header in headers}) != len(headers):
        raise ValueError("Extraction column names are not unique")

    # Second pass: extract values and write the CSV.
    count = 0

    with Path(path).open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(headers)

        for row in extract_fields(
            source,
            record_list,
            selectors,
            cancellation=cancellation,
            progress=export_progress,
        ):
            output: list[Any] = (
                [row.index, row.filename, row.record_id]
                if config.include_identifiers
                else []
            )

            for selector_index, nested_path, _ in value_columns:
                extracted = row.values[selector_index]
                value = extracted.value

                if nested_path is not None:
                    value = _get_json_path(value, nested_path, _MISSING)

                output.append(_csv_value(value, extracted.status))

            if config.include_status_columns:
                output.extend(extracted.status.value for extracted in row.values)

            writer.writerow(output)
            count += 1

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


def flattened_column_name(
    descriptor: OutputDescriptor,
    path: JsonPath,
) -> str:
    """Return the configured name for a flattened output column."""
    renamed = dict(descriptor.flattened_column_names).get(path)
    return renamed or f"{descriptor.column}.{format_json_path(path)}"


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
    fields: Sequence[tuple[FieldSelector, OutputDescriptor]],
    *,
    cancellation: CancellationToken | None,
    progress: Callable[[int, RecordRef], None] | None,
    maximum: int,
) -> dict[int, tuple[JsonPath, ...]]:
    """Discover the output paths needed to flatten or expand extracted values."""

    requested = {
        index
        for index, (_, descriptor) in enumerate(fields)
        if descriptor.flatten or descriptor.array_expansion_depth
    }

    if not requested:
        return {}

    selectors = tuple(selector for selector, _ in fields)
    discovered: dict[int, set[JsonPath]] = {index: set() for index in requested}

    for row in extract_fields(
        source,
        records,
        selectors,
        cancellation=cancellation,
        progress=progress,
    ):
        for index in requested:
            descriptor = fields[index][1]
            extracted = row.values[index]

            if extracted.status != ExtractionStatus.AVAILABLE:
                continue

            discovered[index].update(
                column_paths_for_value(
                    extracted.value,
                    flatten_mappings=descriptor.flatten,
                    array_depth=descriptor.array_expansion_depth,
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
            if path not in fields[index][1].excluded_paths
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


def _validate_output_descriptors(outputs: Sequence[OutputDescriptor]) -> None:
    columns = [output.column.casefold() for output in outputs]
    if any(not output.column.strip() for output in outputs):
        raise ValueError("Extraction column names cannot be empty")
    if len(columns) != len(set(columns)):
        raise ValueError("Extraction column names must be unique")
    if any(output.array_expansion_depth < 0 for output in outputs):
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


def field_to_dict(
    selector: FieldSelector,
    output: OutputDescriptor,
) -> dict[str, Any]:
    return {
        "selector": {
            "node_path": list(selector.node_path),
            "value_path": list(selector.value_path),
        },
        "output": {
            "column": output.column,
            "flatten": output.flatten,
            "array_expansion_depth": output.array_expansion_depth,
            "excluded_paths": [list(path) for path in output.excluded_paths],
            "flattened_column_names": [
                {"path": list(path), "column": column}
                for path, column in output.flattened_column_names
            ],
        },
    }


def _field_from_dict(value: Any) -> tuple[FieldSelector, OutputDescriptor]:
    if not isinstance(value, dict):
        msg = "Each extraction field must be an object"
        raise ValueError(msg)

    raw_selector = value.get("selector")
    raw_output = value.get("output")

    if not isinstance(raw_selector, dict):
        msg = "Field selector must be an object"
        raise ValueError(msg)
    if not isinstance(raw_output, dict):
        msg = "Output descriptor must be an object"
        raise ValueError(msg)

    selector = FieldSelector(
        node_path=tuple(raw_selector.get("node_path", ())),
        value_path=tuple(raw_selector.get("value_path", ())),
    )

    raw_names = raw_output.get("flattened_column_names", ())

    output = OutputDescriptor(
        column=str(raw_output.get("column", "value")),
        flatten=bool(raw_output.get("flatten", False)),
        array_expansion_depth=int(raw_output.get("array_expansion_depth", 0)),
        excluded_paths=tuple(
            tuple(path) for path in raw_output.get("excluded_paths", ())
        ),
        flattened_column_names=tuple(
            (tuple(item["path"]), str(item["column"])) for item in raw_names
        ),
    )

    return selector, output


def _is_serialized_exception(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    exception_name, separator, _message = value.partition(": ")
    return bool(separator) and exception_name.endswith(("Error", "Exception"))
