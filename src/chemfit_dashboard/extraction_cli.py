from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from chemfit_dashboard.extraction import export_csv, export_jsonl, load_config
from chemfit_dashboard.source import ContextSourceError, JsonFileSource

if TYPE_CHECKING:
    from collections.abc import Sequence

OutputFormat = Literal["csv", "jsonl"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chemfit-extract",
        description=(
            "Extract configured fields from serialized ChemFit evaluation contexts "
            "without starting the dashboard."
        ),
        epilog=(
            "Example: chemfit-extract runs/ --config extraction.json "
            "--format csv --output values.csv"
        ),
    )
    parser.add_argument(
        "paths",
        nargs="+",
        metavar="PATH",
        help="Context JSON file or directory; only immediate JSON files are used.",
    )
    parser.add_argument(
        "-c",
        "--config",
        required=True,
        type=Path,
        metavar="FILE",
        help="Extraction configuration JSON saved by the dashboard.",
    )
    parser.add_argument(
        "-o",
        "--output",
        required=True,
        type=Path,
        metavar="FILE",
        help="Destination CSV or JSONL file.",
    )
    parser.add_argument(
        "-f",
        "--format",
        choices=("csv", "jsonl"),
        help="Output format; inferred from the output filename when omitted.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing output file.",
    )
    return parser


def _output_format(
    parser: argparse.ArgumentParser, arguments: argparse.Namespace
) -> OutputFormat:
    if arguments.format is not None:
        return arguments.format
    suffix = arguments.output.suffix.casefold().lstrip(".")
    if suffix in {"csv", "jsonl"}:
        return suffix
    parser.error("--format is required when --output does not end in .csv or .jsonl")
    return "csv"  # pragma: no cover


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    output_format = _output_format(parser, arguments)
    output: Path = arguments.output
    if output.exists() and not arguments.force:
        parser.error(f"output already exists: {output} (use --force to overwrite)")

    try:
        config = load_config(arguments.config)
        if not config.selectors:
            message = "Extraction configuration contains no selectors"
            raise ValueError(message)
        source = JsonFileSource()
        records = source.discover(arguments.paths)
        config_path = arguments.config.expanduser().resolve()
        records = [record for record in records if record.path.resolve() != config_path]
        if not records:
            message = "No context JSON files were found in the supplied paths"
            raise ValueError(message)
        output.parent.mkdir(parents=True, exist_ok=True)
        exporter = export_csv if output_format == "csv" else export_jsonl
        count = exporter(output, source, records, config)
    except (ContextSourceError, OSError, ValueError) as error:
        parser.exit(1, f"chemfit-extract: error: {error}\n")

    print(f"Wrote {count:,} row{'s' if count != 1 else ''} to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
