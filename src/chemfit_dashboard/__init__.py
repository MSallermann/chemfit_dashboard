"""Native dashboard for inspecting serialized ChemFit contexts."""

from importlib.metadata import PackageNotFoundError, version

from chemfit_dashboard.extraction import (
    ExtractionConfig,
    ExtractionRow,
    ExtractionStatus,
    ExtractionValue,
    FieldSelector,
    NodePathSelection,
    OutputDescriptor,
    ResolvedField,
    export_csv,
    export_jsonl,
    extract_fields,
    load_config,
    resolve_fields,
    save_config,
)

__all__ = [
    "ExtractionConfig",
    "ExtractionRow",
    "ExtractionStatus",
    "ExtractionValue",
    "FieldSelector",
    "NodePathSelection",
    "OutputDescriptor",
    "ResolvedField",
    "export_csv",
    "export_jsonl",
    "extract_fields",
    "load_config",
    "resolve_fields",
    "save_config",
]

try:
    __version__ = version("chemfit-dashboard")
except PackageNotFoundError:
    __version__ = "0+unknown"
