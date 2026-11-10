"""
Reusable field extraction, configuration persistence, and streaming exports.

Path-based ``FieldSelector`` objects address values consistently across a
collection while preserving explicit missing/skipped/failure statuses. This
module contains no Qt code and is shared by the dashboard and headless CLI.
"""

from __future__ import annotations

# ruff: noqa: EM101, PLR0911
import csv
import json
import tempfile
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

import pydictnest

from chemfit_dashboard.context_document import (
    ContextDocument,
    ContextDocumentError,
    NodePath,
)
from chemfit_dashboard.source import JsonFileSource
from chemfit_dashboard.traversal import CancellationToken, iter_collection
from chemfit_dashboard.util import (
    format_combined_path,
    format_json_path,
    format_named_node_path,
)

get_nested = pydictnest.get_nested
_pydictnest_get_path = getattr(pydictnest, "get_path", None)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence

    from chemfit_dashboard.source import ContextSource, RecordRef
    from chemfit_dashboard.workers import TaskContext

JsonPath = tuple[str | int, ...]
NodePathSelection = tuple[int | Literal["*"], ...]
NODE_WILDCARD: Literal["*"] = "*"
CONFIG_VERSION = 6
_MISSING = object()


def export_json_file_process(
    path: str,
    format_name: str,
    records: tuple[RecordRef, ...],
    config: ExtractionConfig,
    *,
    task_context: TaskContext,
) -> int:
    """Export local JSON records atomically inside one worker process."""

    return export_file_atomically(
        path,
        format_name,
        JsonFileSource(),
        records,
        config,
        task_context=task_context,
    )


def export_file_atomically(
    path: str,
    format_name: str,
    source: ContextSource,
    records: tuple[RecordRef, ...],
    config: ExtractionConfig,
    *,
    task_context: TaskContext,
) -> int:
    """Write a temporary sibling and publish it only after full success."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
        delete=False,
    ) as stream:
        temporary = Path(stream.name)

    passes = 1
    if any(selector.has_wildcard for selector, _output in config.fields):
        passes += 1
    if format_name == "csv" and any(
        output.flatten or output.array_expansion_depth for _, output in config.fields
    ):
        passes += 1
    total = len(records) * passes
    highest_progress = 0

    def progress(completed: int, _record: RecordRef) -> None:
        nonlocal highest_progress
        task_context.raise_if_cancelled()
        highest_progress = max(highest_progress, completed)
        task_context.report_progress(highest_progress, max(1, total))

    try:
        if format_name == "csv":
            count = export_csv(
                temporary,
                source,
                records,
                config,
                cancellation=cast("CancellationToken", task_context),
                progress=progress,
            )
        elif format_name == "jsonl":
            count = export_jsonl(
                temporary,
                source,
                records,
                config,
                cancellation=cast("CancellationToken", task_context),
                progress=progress,
            )
        else:
            message = f"Unknown extraction format: {format_name}"
            raise ValueError(message)
        task_context.raise_if_cancelled()
        task_context.report_progress(max(1, total), max(1, total))
        temporary.replace(destination)
        return count
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


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
    """A reusable component/value path independent of any collection."""

    # Describes the where the context sits within the record context hierarchy
    node_path: NodePathSelection
    # Describes where in the context dict the required field is
    value_path: JsonPath = ()

    def __post_init__(self) -> None:
        invalid = tuple(
            part
            for part in self.node_path
            if not isinstance(part, int) and part != NODE_WILDCARD
        )
        if invalid:
            message = (
                "Selector node paths may contain only integer indexes and '*' "
                f"wildcards, not {invalid[0]!r}"
            )
            raise ValueError(message)

    @property
    def has_wildcard(self) -> bool:
        """Whether this selector expands to matching concrete component paths."""

        return NODE_WILDCARD in self.node_path

    @property
    def concrete_node_path(self) -> NodePath:
        """Return the exact path, rejecting an unresolved wildcard selection."""

        if self.has_wildcard:
            raise ValueError("Wildcard selector has no single concrete node path")
        return cast("NodePath", self.node_path)

    def format(self) -> str:
        return format_combined_path(self.node_path, self.value_path)


@dataclass(slots=True)
class OutputDescriptor:
    """A reusable output description for extractions."""

    # optional explicit column label; None uses the selector's complete path
    column: str | None = None
    # if a dict shaped field should be flattened
    flatten: bool = False
    # to what level arrya fields should be expanded
    array_expansion_depth: int = 0
    # these paths are excluded from the extraction
    excluded_paths: tuple[JsonPath, ...] = ()
    # if any of the flattened columns should be renamed
    flattened_column_names: tuple[tuple[JsonPath, str], ...] = ()


@dataclass(frozen=True, slots=True)
class ResolvedField:
    """One concrete extraction field derived from a configured field."""

    selector: FieldSelector
    output: OutputDescriptor
    source_index: int
    configured_output: OutputDescriptor
    include_node_path: bool


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
    include_node_paths: bool = True
    version: int = CONFIG_VERSION

    def to_dict(self) -> dict[str, Any]:
        """Serialize the stable public configuration representation."""
        return {
            "version": self.version,
            "include_identifiers": self.include_identifiers,
            "include_node_paths": self.include_node_paths,
            "fields": [
                field_to_dict(selector, output) for selector, output in self.fields
            ],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ExtractionConfig:
        """Validate and construct a configuration from decoded JSON."""
        version = value.get("version")
        if version not in {3, 4, 5, CONFIG_VERSION}:
            message = f"Unsupported extraction configuration version: {version!r}"
            raise ValueError(message)

        raw_fields = value.get("fields")
        if not isinstance(raw_fields, list):
            msg = "Extraction configuration selectors must be a list"
            raise ValueError(msg)
        return cls(
            fields=tuple(_field_from_dict(item) for item in raw_fields),
            include_identifiers=bool(value.get("include_identifiers", True)),
            include_node_paths=bool(value.get("include_node_paths", True)),
        )


def resolve_fields(
    source: ContextSource,
    records: Sequence[RecordRef],
    fields: Sequence[tuple[FieldSelector, OutputDescriptor]],
    *,
    include_node_paths: bool = True,
    cancellation: CancellationToken | None = None,
    progress: Callable[[int, RecordRef], None] | None = None,
) -> tuple[ResolvedField, ...]:
    """Expand wildcard selectors to the union of concrete paths in records."""

    wildcard_indexes = {
        index
        for index, (selector, _output) in enumerate(fields)
        if selector.has_wildcard
    }
    if not wildcard_indexes:
        return tuple(
            ResolvedField(
                selector,
                _resolved_output(
                    output,
                    selector,
                    include_node_path=include_node_paths,
                ),
                index,
                output,
                include_node_paths,
            )
            for index, (selector, output) in enumerate(fields)
        )

    matches: dict[int, set[NodePath]] = {
        index: (set() if index in wildcard_indexes else {selector.concrete_node_path})
        for index, (selector, _output) in enumerate(fields)
    }
    for item in iter_collection(
        source,
        records,
        cancellation=cancellation,
        progress=progress,
    ):
        if item.document is None:
            continue
        for index, (selector, _output) in enumerate(fields):
            concrete_paths = (
                _matching_node_paths(item.document, selector.node_path)
                if selector.has_wildcard
                else (
                    (selector.concrete_node_path,)
                    if item.document.has_path(selector.concrete_node_path)
                    else ()
                )
            )
            matches[index].update(concrete_paths)

    resolved: list[ResolvedField] = []
    for index, (selector, output) in enumerate(fields):
        for node_path in sorted(matches[index]):
            concrete = FieldSelector(node_path, selector.value_path)
            resolved.append(
                ResolvedField(
                    concrete,
                    _resolved_output(
                        output,
                        concrete,
                        include_node_path=include_node_paths,
                    ),
                    index,
                    output,
                    include_node_paths,
                )
            )
    return tuple(resolved)


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
    if any(selector.has_wildcard for selector in selected):
        message = (
            "Wildcard selectors must be resolved against a collection with "
            "resolve_fields() before extraction"
        )
        raise ValueError(message)

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
        details = document.details(selector.concrete_node_path)
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


def _matching_node_paths(
    document: ContextDocument,
    pattern: NodePathSelection,
) -> tuple[NodePath, ...]:
    candidates: tuple[NodePath, ...] = ((),)
    for part in pattern:
        if part == NODE_WILDCARD:
            candidates = tuple(
                child.path
                for parent in candidates
                for child in document.children(parent)
            )
        else:
            child_index = cast("int", part)
            candidates = tuple(
                (*parent, child_index)
                for parent in candidates
                if document.has_path((*parent, child_index))
            )
        if not candidates:
            break
    return candidates


def _resolved_output(
    output: OutputDescriptor,
    selector: FieldSelector,
    *,
    include_node_path: bool = True,
) -> OutputDescriptor:
    return replace(
        output,
        column=_qualified_output_column(
            output.column,
            selector,
            include_node_path=include_node_path,
        ),
    )


def _qualified_output_column(
    column: str | None,
    selector: FieldSelector,
    *,
    include_node_path: bool,
) -> str:
    if column is None:
        selector_name = (
            format_combined_path(
                selector.concrete_node_path,
                selector.value_path,
            )
            if include_node_path
            else (format_json_path(selector.value_path) or "value")
        )
    else:
        selector_name = (
            format_named_node_path(column, selector.concrete_node_path)
            if include_node_path
            else column
        )
    return selector_name


def resolved_flattened_column_name(
    field: ResolvedField,
    path: JsonPath,
) -> str:
    """Name one flattened column using its resolved field's naming policy."""

    configured = field.configured_output
    renamed = dict(configured.flattened_column_names).get(path)
    suffix = format_json_path(path)
    column = (
        renamed
        if renamed is not None
        else (
            _append_json_suffix(configured.column, suffix)
            if configured.column is not None and suffix
            else configured.column
        )
    )
    selector = FieldSelector(
        field.selector.concrete_node_path,
        (
            field.selector.value_path
            if renamed is not None or configured.column is not None
            else (*field.selector.value_path, *path)
        ),
    )
    return _qualified_output_column(
        column,
        selector,
        include_node_path=field.include_node_path,
    )


def _append_json_suffix(column: str, suffix: str) -> str:
    separator = "" if suffix.startswith("[") else "."
    return f"{column}{separator}{suffix}"


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

    record_list = list(records)
    _validate_output_descriptors([o for _, o in config.fields])
    needs_resolution_pass = any(selector.has_wildcard for selector, _ in config.fields)
    resolved = resolve_fields(
        source,
        record_list,
        config.fields,
        include_node_paths=config.include_node_paths,
        cancellation=cancellation,
        progress=progress,
    )
    resolved_fields = tuple((field.selector, field.output) for field in resolved)
    selectors = tuple(field.selector for field in resolved)
    _validate_output_descriptors([field.output for field in resolved], resolved=True)

    def export_progress(completed: int, record: RecordRef) -> None:
        if progress is not None:
            progress(
                (len(record_list) if needs_resolution_pass else 0) + completed,
                record,
            )

    count = 0
    with Path(path).open("w", encoding="utf-8") as stream:
        for row in extract_fields(
            source,
            record_list,
            selectors,
            cancellation=cancellation,
            progress=export_progress,
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
                _resolved_column(descriptor): extracted.value
                for (_, descriptor), extracted in zip(
                    resolved_fields, row.values, strict=True
                )
            }

            payload["status"] = {
                _resolved_column(descriptor): extracted.status.value
                for (_, descriptor), extracted in zip(
                    resolved_fields, row.values, strict=True
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
) -> int:
    """Stream extraction rows to CSV, expanding dictionaries and arrays into columns."""

    record_list = list(records)
    _validate_output_descriptors([o for _, o in config.fields])
    needs_resolution_pass = any(selector.has_wildcard for selector, _ in config.fields)
    resolved = resolve_fields(
        source,
        record_list,
        config.fields,
        include_node_paths=config.include_node_paths,
        cancellation=cancellation,
        progress=progress,
    )
    resolved_fields = tuple((field.selector, field.output) for field in resolved)
    selectors = tuple(field.selector for field in resolved)
    _validate_output_descriptors([field.output for field in resolved], resolved=True)

    discovery_offset = len(record_list) if needs_resolution_pass else 0

    def discovery_progress(completed: int, record: RecordRef) -> None:
        if progress is not None:
            progress(discovery_offset + completed, record)

    # First pass: discover the columns needed for flattened or expanded values.
    flat_paths = _collect_flat_paths(
        source,
        record_list,
        resolved_fields,
        cancellation=cancellation,
        progress=discovery_progress,
    )

    completed_offset = discovery_offset + (len(record_list) if flat_paths else 0)

    def export_progress(completed: int, record: RecordRef) -> None:
        if progress is not None:
            progress(completed_offset + completed, record)

    # Build the output columns: (selector index, relative value path, column name).
    value_columns: list[tuple[int, JsonPath | None, str]] = []

    for index, (_, descriptor) in enumerate(resolved_fields):
        if descriptor.flatten or descriptor.array_expansion_depth:
            value_columns.extend(
                (
                    index,
                    nested_path,
                    resolved_flattened_column_name(resolved[index], nested_path),
                )
                for nested_path in flat_paths.get(index, ())
            )
        else:
            value_columns.append((index, None, _resolved_column(descriptor)))

    # Build and validate the CSV header.
    headers = (
        ["collection_index", "filename", "record_id"]
        if config.include_identifiers
        else []
    )

    headers.extend(column for _, _, column in value_columns)

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
    if renamed is not None:
        return renamed
    nested = format_json_path(path)
    if descriptor.column is None:
        return nested or "value"
    return (
        _append_json_suffix(descriptor.column, nested) if nested else descriptor.column
    )


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


def _validate_output_descriptors(
    outputs: Sequence[OutputDescriptor],
    *,
    resolved: bool = False,
) -> None:
    columns = [
        output.column.casefold() for output in outputs if output.column is not None
    ]
    if any(
        output.column is not None and not output.column.strip() for output in outputs
    ):
        raise ValueError("Extraction column names cannot be empty")
    if resolved and any(output.column is None for output in outputs):
        raise ValueError("Resolved extraction fields must have column names")
    if len(columns) != len(set(columns)):
        message = (
            "Resolved extraction column names are not unique; enable node paths "
            "for wildcard selections or choose unique explicit names"
            if resolved
            else "Extraction column names must be unique"
        )
        raise ValueError(message)
    if any(output.array_expansion_depth < 0 for output in outputs):
        raise ValueError("Array expansion depth cannot be negative")


def _resolved_column(output: OutputDescriptor) -> str:
    column = output.column
    if column is None:
        raise ValueError("Resolved extraction field has no column name")
    return column


def _csv_value(value: Any, status: ExtractionStatus) -> str | int | float | bool:
    if (
        value is _MISSING
        or status != ExtractionStatus.AVAILABLE
        or _contains_serialized_exception(value)
    ):
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


def _contains_serialized_exception(value: Any) -> bool:
    if _is_serialized_exception(value):
        return True
    if isinstance(value, dict):
        return any(_contains_serialized_exception(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_serialized_exception(item) for item in value)
    return False


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
    raw_column = raw_output.get("column")
    if raw_column is not None and not isinstance(raw_column, str):
        msg = "Output column must be a string or null"
        raise ValueError(msg)

    output = OutputDescriptor(
        column=raw_column,
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
