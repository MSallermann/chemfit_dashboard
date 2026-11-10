import pytest

import chemfit_dashboard.app as dashboard_app
from chemfit_dashboard.cli import build_parser, main
from chemfit_dashboard.extraction import (
    ExtractionConfig,
    FieldSelector,
    OutputDescriptor,
    save_config,
)


def test_each_cli_path_is_an_independent_location():
    arguments = build_parser().parse_args(
        ["context_collection", "runs/initial_params_01/meta_data"]
    )

    assert arguments.paths == [
        "context_collection",
        "runs/initial_params_01/meta_data",
    ]


def test_gui_cli_preloads_extraction_configuration(tmp_path, monkeypatch):
    config = ExtractionConfig(
        ((FieldSelector((), ("output", "loss")), OutputDescriptor("loss")),)
    )
    config_path = tmp_path / "extraction.json"
    save_config(config_path, config)
    received = []

    def run(paths, *, extraction_config):
        received.append((paths, extraction_config))
        return 17

    monkeypatch.setattr(dashboard_app, "run", run)

    assert main(["runs", "--extraction-config", str(config_path)]) == 17
    assert received == [(["runs"], config)]


def test_gui_cli_rejects_invalid_extraction_configuration(tmp_path, capsys):
    config_path = tmp_path / "invalid.json"
    config_path.write_text("not JSON", encoding="utf-8")

    with pytest.raises(SystemExit) as raised:
        main(["--extraction-config", str(config_path)])

    assert raised.value.code == 2
    assert "could not load extraction configuration" in capsys.readouterr().err
