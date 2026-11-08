from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from chemfit_dashboard.context_document import NodePath

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


def format_node_path(path: NodePath) -> str:
    result = ""
    for segment in path:
        result += f"/{segment}"
    return result


def format_combined_path(node_path: NodePath, json_path: JsonPath) -> str:
    node = format_node_path(node_path) or "/"
    field = format_json_path(json_path)
    return f"{node} : {field}" if field else node


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
