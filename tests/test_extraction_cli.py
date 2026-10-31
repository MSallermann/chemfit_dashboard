from __future__ import annotations

import csv
import json

import pytest

from chemfit_dashboard.extraction import ExtractionConfig, FieldSelector, save_config
from chemfit_dashboard.extraction_cli import main


def _config(path) -> None:
    save_config(
        path,
        ExtractionConfig(
            (FieldSelector((), "fields", ("output",), "result"),)
        ),
    )


def test_cli_exports_gui_configuration_to_csv(tmp_path, capsys):
    contexts = tmp_path / "contexts"
    contexts.mkdir()
    (contexts / "step_1.json").write_text('{"output": 4}', encoding="utf-8")
    (contexts / "step_2.json").write_text('{"output": 7}', encoding="utf-8")
    config = tmp_path / "extraction.json"
    output = tmp_path / "out" / "values.csv"
    _config(config)

    assert (
        main(
            [
                str(contexts),
                "--config",
                str(config),
                "--format",
                "csv",
                "--output",
                str(output),
            ]
        )
        == 0
    )

    with output.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert [row["result"] for row in rows] == ["4", "7"]
    assert "Wrote 2 rows" in capsys.readouterr().out


def test_cli_infers_jsonl_and_excludes_config_inside_input_directory(
    tmp_path, capsys
):
    (tmp_path / "step_1.json").write_text('{"output": 4}', encoding="utf-8")
    config = tmp_path / "extraction.json"
    output = tmp_path / "values.jsonl"
    _config(config)

    assert main([str(tmp_path), "-c", str(config), "-o", str(output)]) == 0

    payloads = [
        json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()
    ]
    assert len(payloads) == 1
    assert payloads[0]["values"]["result"] == 4
    assert "Wrote 1 row" in capsys.readouterr().out


def test_cli_refuses_ambiguous_format_and_existing_output(tmp_path, capsys):
    context = tmp_path / "step.json"
    context.write_text('{"output": 4}', encoding="utf-8")
    config = tmp_path / "extraction.json"
    _config(config)

    with pytest.raises(SystemExit, match="2"):
        main([str(context), "-c", str(config), "-o", str(tmp_path / "values")])
    assert "--format is required" in capsys.readouterr().err

    output = tmp_path / "values.csv"
    output.write_text("keep me", encoding="utf-8")
    with pytest.raises(SystemExit, match="2"):
        main([str(context), "-c", str(config), "-o", str(output)])
    assert output.read_text(encoding="utf-8") == "keep me"
    assert "use --force" in capsys.readouterr().err


def test_cli_reports_invalid_config_without_traceback(tmp_path, capsys):
    context = tmp_path / "step.json"
    context.write_text('{"output": 4}', encoding="utf-8")
    config = tmp_path / "bad.json"
    config.write_text('{"version": 999, "selectors": []}', encoding="utf-8")

    with pytest.raises(SystemExit, match="1"):
        main(
            [
                str(context),
                "-c",
                str(config),
                "-o",
                str(tmp_path / "values.csv"),
            ]
        )
    error = capsys.readouterr().err
    assert "Unsupported extraction configuration version" in error
    assert "Traceback" not in error
