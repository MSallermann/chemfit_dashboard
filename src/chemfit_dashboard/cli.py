from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        from chemfit_dashboard.app import run
    except ImportError as exc:
        if exc.name == "PySide6" or (exc.name and exc.name.startswith("PySide6.")):
            message = 'PySide6 is not installed. Run: python -m pip install -e "."'
            raise SystemExit(message) from exc
        raise
    return run(arguments.paths)
