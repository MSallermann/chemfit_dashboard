from __future__ import annotations

import json

import pytest

from benchmark.benchmark import STRESS_PROFILE, create_collection, resolve_profile
from chemfit_dashboard.source import JsonFileSource


def test_stress_profile_is_intentionally_large():
    assert STRESS_PROFILE.evaluations >= 50_000
    assert STRESS_PROFILE.graph_nodes >= 25_000
    assert (
        STRESS_PROFILE.evaluations * STRESS_PROFILE.components_per_evaluation
        >= 2_000_000
    )
    assert STRESS_PROFILE.root_fields >= 80
    assert len(STRESS_PROFILE.search_queries) >= 3


def test_stress_collection_generator_scales_graph_tree_and_payload(tmp_path):
    root = create_collection(
        tmp_path / "collection",
        3,
        5,
        components_per_evaluation=2,
        root_fields=4,
    )
    first = json.loads((root / "step_0_ctx_0.json").read_text(encoding="utf-8"))
    second = json.loads((root / "step_1_ctx_0.json").read_text(encoding="utf-8"))

    assert len(first["meta"]["chemfit.structure"]["children"]) == 5
    assert len(first["children"]) == 5
    assert len(second["children"]) == 2
    assert len(second["metrics"]) == 4
    assert second["children"][1]["meta"]["tag"] == "tag_1"

    source = JsonFileSource()
    records = source.discover([root])
    assert source.load(records[0]).node_count == 6
    assert source.load(records[1]).node_count == 3


def test_profile_overrides_are_validated():
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
