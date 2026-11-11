from __future__ import annotations

import json
import sys

import pytest

from benchmark import benchmark
from benchmark.benchmark import STRESS_PROFILE, create_collection, resolve_profile
from chemfit_dashboard.source import JsonFileSource


@pytest.mark.parametrize("record_format", ["versioned", "legacy"])
def test_stress_collection_generator_scales_graph_tree_and_payload(
    tmp_path, record_format
):
    root = create_collection(
        tmp_path / "collection",
        3,
        5,
        components_per_evaluation=2,
        root_fields=4,
        record_format=record_format,
    )
    first = json.loads((root / "step_0_ctx_0.json").read_text(encoding="utf-8"))
    second = json.loads((root / "step_1_ctx_0.json").read_text(encoding="utf-8"))
    if record_format == "versioned":
        assert first["format"] == "chemfit.evaluate_context"
        assert first["schema_version"] == 1
        first = first["root"]
        second = second["root"]

    assert len(first["meta"]["chemfit.structure"]["children"]) == 5
    assert len(first["children"]) == 5
    assert len(second["children"]) == 2
    assert len(second["metrics"]) == 4
    assert second["children"][1]["meta"]["tag"] == "tag_1"

    source = JsonFileSource()
    records = source.discover([root])
    assert source.load(records[0]).node_count == 6
    assert source.load(records[1]).node_count == 3


def test_collection_generator_rejects_unknown_record_format(tmp_path):
    with pytest.raises(ValueError, match="unsupported record format"):
        create_collection(
            tmp_path,
            1,
            1,
            record_format="unknown",  # pyright: ignore[reportArgumentType]
        )


def test_collection_generator_defaults_to_versioned_format(tmp_path):
    root = create_collection(tmp_path, 1, 0)

    payload = json.loads((root / "step_0_ctx_0.json").read_text(encoding="utf-8"))

    assert payload["format"] == "chemfit.evaluate_context"
    assert payload["root"]["context_type"] == "fit"


def test_collection_generator_writes_jsonl_trajectory(tmp_path):
    path = create_collection(
        tmp_path / "collection",
        3,
        5,
        components_per_evaluation=2,
        root_fields=4,
        input_format="jsonl",
    )

    payloads = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
    ]
    source = JsonFileSource()
    records = source.discover([path])

    assert path.name == "trajectory.jsonl"
    assert [payload["step"] for payload in payloads] == [0, 1, 2]
    assert [payload["slot"] for payload in payloads] == [0, 0, 0]
    assert {payload["record"]["format"] for payload in payloads} == {
        "chemfit.evaluate_context"
    }
    assert [record.position_in_file for record in records] == [1, 2, 3]
    assert [source.load(record).node_count for record in records] == [6, 3, 3]


def test_jsonl_collection_rejects_legacy_records(tmp_path):
    with pytest.raises(ValueError, match="require versioned records"):
        create_collection(
            tmp_path,
            1,
            1,
            record_format="legacy",
            input_format="jsonl",
        )


def test_benchmark_cli_forwards_persistent_collection_directory(tmp_path, monkeypatch):
    collection_directory = tmp_path / "interactive-collection"
    captured = {}

    def fake_run(*_args, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(benchmark, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "benchmark.py",
            "--evaluations",
            "1",
            "--graph-nodes",
            "0",
            "--collection-directory",
            str(collection_directory),
        ],
    )

    benchmark.main()

    assert captured["collection_directory"] == collection_directory


def test_profile_overrides_are_validated():
    assert STRESS_PROFILE.evaluations >= 50_000
    assert STRESS_PROFILE.graph_nodes >= 25_000
    assert STRESS_PROFILE.root_fields >= 80
    configured = resolve_profile(
        "stress",
        evaluations=12,
        graph_nodes=30,
        components_per_evaluation=4,
        root_fields=6,
    )

    assert configured.evaluations == 12
    assert configured.graph_nodes == 30
    assert configured.components_per_evaluation == 4
    assert configured.root_fields == 6
    assert configured.search_queries == STRESS_PROFILE.search_queries

    with pytest.raises(ValueError, match="greater than zero"):
        resolve_profile("stress", evaluations=0)
    with pytest.raises(ValueError, match="cannot be negative"):
        resolve_profile("stress", root_fields=-1)
