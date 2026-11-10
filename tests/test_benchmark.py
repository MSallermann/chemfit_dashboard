from __future__ import annotations

import json

import pytest

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
