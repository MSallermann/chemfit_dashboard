from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from chemfit_dashboard.extraction import JsonPath


def format_json_path(path: JsonPath) -> str:
    """Render a nested JSON field path, e.g. ``output.values[2]``."""
    result = ""
    for segment in path:
        if isinstance(segment, int):
            result += f"[{segment}]"
        else:
            result += f".{segment}" if result else segment
    return result


def format_node_path(path: tuple[int | str, ...]) -> str:
    return "/" + "/".join(map(str, path))


def format_named_node_path(name: str, node_path: tuple[int | str, ...]) -> str:
    """Qualify a semantic name with its context path."""

    return f"{name}@{format_node_path(node_path)}"


def format_combined_path(node_path: tuple[int | str, ...], json_path: JsonPath) -> str:
    """Render a field-first path, e.g. ``output.force@/2/0``."""

    node = format_node_path(node_path)
    field = format_json_path(json_path)
    return format_named_node_path(field, node_path) if field else node


def format_json_value(value: Any) -> str:
    """Render a JSON-compatible value for ordinary clipboard copying."""
    if isinstance(value, str):
        return value
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)
