"""Command-line parsing for launching the optional Qt dashboard."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from chemfit_dashboard.extraction import load_config

if TYPE_CHECKING:
    from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chemfit-dashboard",
        description="Inspect serialized ChemFit 4.0.0 evaluation contexts.",
        epilog=(
            "Every supplied PATH is loaded. Example: "
            "chemfit-dashboard runs/initial_params_01/meta_data"
        ),
    )
    parser.add_argument(
        "paths",
        nargs="*",
        metavar="PATH",
        help="JSON file or directory to open; multiple paths load multiple locations.",
    )
    parser.add_argument(
        "-c",
        "--extraction-config",
        type=Path,
        metavar="FILE",
        help="Preload an extraction configuration JSON saved by the dashboard.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(filename="dashboard.log")

    parser = build_parser()
    arguments = parser.parse_args(argv)
    extraction_config = None
    if arguments.extraction_config is not None:
        config_path = arguments.extraction_config.expanduser()
        try:
            extraction_config = load_config(config_path)
        except (OSError, ValueError) as error:
            parser.error(
                f"could not load extraction configuration {config_path}: {error}"
            )
    try:
        from chemfit_dashboard.app import run  # noqa: PLC0415, RUF100
    except ImportError as exc:
        missing_gui_dependency = exc.name == "rapidfuzz" or (
            exc.name == "PySide6" or (exc.name and exc.name.startswith("PySide6."))
        )
        if missing_gui_dependency:
            message = (
                "The dashboard GUI dependencies are not installed. "
                'Run: python -m pip install "chemfit-dashboard[gui]"'
            )
            raise SystemExit(message) from exc
        raise
    return run(arguments.paths, extraction_config=extraction_config)
