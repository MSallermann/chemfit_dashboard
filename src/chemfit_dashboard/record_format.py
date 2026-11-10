"""Normalization for versioned ChemFit evaluation-context records."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any, NoReturn

if TYPE_CHECKING:
    from collections.abc import Mapping

RECORD_FORMAT = "chemfit.evaluate_context"
SUPPORTED_SCHEMA_VERSION = 1
_CONTEXT_STATUSES = {
    "success",
    "exception",
    "skipped",
    "no_output",
    "not_recorded",
}


class RecordFormatError(ValueError):
    """Raised when a recognized ChemFit record violates its format contract."""


def _invalid(message: str) -> NoReturn:
    raise RecordFormatError(message)


def normalize_record(value: Any, *, require_versioned: bool = False) -> Any:
    """Return a context-shaped value for legacy or versioned ChemFit input."""

    trajectory: Mapping[str, Any] | None = None
    record = value
    if isinstance(value, dict) and _is_versioned_record(value.get("record")):
        trajectory = value
        record = value["record"]

    if not _is_versioned_record(record):
        if require_versioned:
            _invalid("JSONL line is not a versioned ChemFit evaluation record")
        return value
    assert isinstance(record, dict)

    version = record.get("schema_version")
    if version != SUPPORTED_SCHEMA_VERSION:
        message = f"Unsupported ChemFit record schema version: {version!r}"
        _invalid(message)
    if not isinstance(record.get("chemfit_version"), str):
        _invalid("ChemFit record has no valid chemfit_version")

    root = record.get("root")
    normalized = _normalize_context(root, expected_path=())
    if normalized is None:
        _invalid("ChemFit record root cannot be unrecorded")

    if trajectory is not None:
        step = trajectory.get("step")
        slot = trajectory.get("slot")
        if isinstance(step, bool) or not isinstance(step, int):
            _invalid("Trajectory line has no valid integer step")
        if isinstance(slot, bool) or not isinstance(slot, int):
            _invalid("Trajectory line has no valid integer slot")
        normalized["step"] = step
        normalized["slot"] = slot

    return normalized


def _is_versioned_record(value: Any) -> bool:
    return isinstance(value, dict) and value.get("format") == RECORD_FORMAT


def _normalize_context(
    value: Any,
    *,
    expected_path: tuple[int, ...],
) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        _invalid("ChemFit context record must be an object")

    status = value.get("status")
    if status not in _CONTEXT_STATUSES:
        message = f"Invalid ChemFit context status: {status!r}"
        _invalid(message)

    raw_path = value.get("path")
    if (
        not isinstance(raw_path, list)
        or any(isinstance(part, bool) or not isinstance(part, int) for part in raw_path)
        or tuple(raw_path) != expected_path
    ):
        message = (
            f"ChemFit context path {raw_path!r} does not match "
            f"its hierarchy position {list(expected_path)!r}"
        )
        _invalid(message)

    if status == "not_recorded":
        return None

    raw_children = value.get("children")
    if not isinstance(raw_children, list):
        _invalid("ChemFit context children must be a list")

    children = []
    saw_unrecorded = False
    for index, child in enumerate(raw_children):
        normalized = _normalize_context(
            child,
            expected_path=(*expected_path, index),
        )
        if normalized is None:
            saw_unrecorded = True
        else:
            children.append(normalized)
    if saw_unrecorded and children:
        _invalid(
            "ChemFit context cannot mix recorded and unrecorded immediate children"
        )

    result = {
        key: _normalize_value(item) for key, item in value.items() if key != "children"
    }
    result["children"] = children
    return result


def _normalize_value(value: Any) -> Any:  # noqa: PLR0911, PLR0912
    if isinstance(value, list):
        return [_normalize_value(item) for item in value]
    if not isinstance(value, dict):
        return value

    tag = value.get("__type__")
    if tag is None:
        return {key: _normalize_value(item) for key, item in value.items()}
    if tag == "mapping":
        mapping = value.get("value")
        if not isinstance(mapping, dict):
            _invalid("Escaped ChemFit mapping has no object value")
        return {key: _normalize_value(item) for key, item in mapping.items()}
    if tag == "no_output":
        return None
    if tag == "skipped":
        return {
            "skipped": True,
            "reason": _normalize_value(value.get("reason")),
        }
    if tag == "exception":
        exception_class = value.get("class")
        message = value.get("message")
        if not isinstance(exception_class, str) or not isinstance(message, str):
            _invalid("Serialized ChemFit exception is malformed")
        return f"{exception_class}: {message}"
    if tag == "float":
        encoded = value.get("value")
        if encoded == "nan":
            return math.nan
        if encoded == "inf":
            return math.inf
        if encoded == "-inf":
            return -math.inf
        _invalid(f"Invalid serialized float value: {encoded!r}")
    if tag in {"numpy_scalar", "unsupported"}:
        return {key: _normalize_value(item) for key, item in value.items()}

    _invalid(f"Unknown serialized ChemFit value type: {tag!r}")
