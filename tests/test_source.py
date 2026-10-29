from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import sleep

from chemfit.fitter import FitContext
from chemfit.fitter_callbacks import CheckpointBestParameters, SaveMetaData

from chemfit_dashboard.example import (
    build_example_context,
    build_failed_example_context,
    write_example_collection,
)
from chemfit_dashboard.source import JsonFileSource


def test_folder_discovery_orders_records_alphanumerically_by_filename(tmp_path):
    names = [
        "step_10_ctx_0.json",
        "alpha_10.json",
        "step_2_ctx_0.json",
        "alpha_2.json",
    ]
    for name in names:
        (tmp_path / name).write_text("{}", encoding="utf-8")

    records = JsonFileSource().discover([tmp_path])

    assert [record.name for record in records] == [
        "alpha_2.json",
        "alpha_10.json",
        "step_2_ctx_0.json",
        "step_10_ctx_0.json",
    ]


def test_loads_context_written_by_chemfit(chemfit_json_path, chemfit_context):
    source = JsonFileSource()
    record = source.discover([chemfit_json_path])[0]
    document = source.load(record)

    assert document.node_count == 3
    assert [node.label for node in document.children(())] == [
        "compute_quantities",
        "compute_loss",
    ]
    assert document.details(()).fields["input"] == chemfit_context.input
    assert document.details(()).fields["output"] == chemfit_context.output
    assert document.details((0,)).fields["output"] == chemfit_context.children[0].output
    assert document.details(()).structure == chemfit_context.meta["chemfit.structure"]


def test_loads_chemfit_checkpoint_and_reuses_cached_document(tmp_path, chemfit_context):
    path = tmp_path / "best.json"
    best = FitContext()
    best.input = chemfit_context.input
    best.output = chemfit_context.children[0].output
    best.meta = chemfit_context.meta
    best.children = chemfit_context.children
    best.loss = chemfit_context.output
    context = FitContext()
    context.opt_context = best
    CheckpointBestParameters(path)(12, [context])

    source = JsonFileSource()
    record = source.discover([path])[0]
    first = source.load(record)
    second = source.load(record)

    assert first is second
    assert first.root.kind == "evaluation checkpoint"
    assert first.details(()).fields["parameters"] == best.parameters
    assert first.details(()).fields["quantities"] == best.output
    assert first.details(()).fields["loss"] == best.loss


def test_partial_and_unfamiliar_json_remains_inspectable(tmp_path):
    path = tmp_path / "partial.json"
    path.write_text(
        json.dumps({"legacy_value": [1, 2, 3], "meta": "not-a-mapping"}),
        encoding="utf-8",
    )
    source = JsonFileSource()
    document = source.load(source.discover([path])[0])

    details = document.details(())
    assert details.info.status == "partial"
    assert details.metadata == {}
    assert details.fields["legacy_value"] == [1, 2, 3]


def test_included_example_matches_current_chemfit_output(tmp_path):
    SaveMetaData(tmp_path)(0, [build_example_context()])
    generated = json.loads((tmp_path / "step_0_ctx_0.json").read_text())
    example_path = Path(__file__).parents[1] / "examples" / "step_0_ctx_0.json"
    included = json.loads(example_path.read_text())

    assert included == generated


def test_included_collection_matches_current_chemfit_output(tmp_path):
    write_example_collection(tmp_path)
    example_directory = Path(__file__).parents[1] / "examples" / "context_collection"
    generated_paths = sorted(tmp_path.glob("*.json"))
    included_paths = sorted(example_directory.glob("*.json"))

    assert [path.name for path in included_paths] == [
        path.name for path in generated_paths
    ]
    for generated_path, included_path in zip(
        generated_paths, included_paths, strict=True
    ):
        assert json.loads(included_path.read_text()) == json.loads(
            generated_path.read_text()
        )


def test_failed_example_records_the_component_error(tmp_path):
    SaveMetaData(tmp_path)(0, [build_failed_example_context()])
    source = JsonFileSource()
    document = source.load(source.discover([tmp_path])[0])

    assert document.root.status == "failed"
    assert document.details(()).fields["output"].startswith("RuntimeError:")
    failed_term = document.details((2, 0, 2))
    assert failed_term.info.label == "failing_structure_error"
    assert failed_term.info.status == "failed"
    assert failed_term.fields["output"] == (
        "RuntimeError: intentional failure in the structure loss term"
    )


def test_large_example_exercises_a_deep_real_chemfit_hierarchy():
    context = build_example_context()
    pending = [(context, 0)]
    node_count = 0
    maximum_depth = 0

    while pending:
        node, depth = pending.pop()
        node_count += 1
        maximum_depth = max(maximum_depth, depth)
        pending.extend((child, depth + 1) for child in node.children)

    assert node_count >= 30
    assert maximum_depth >= 5
    assert context.output == context.children[-1].output


def test_bounded_lru_cache_evicts_without_invalidating_live_document(tmp_path):
    for index in range(3):
        (tmp_path / f"{index}.json").write_text(
            json.dumps({"output": index}), encoding="utf-8"
        )
    source = JsonFileSource(cache_capacity=2, cache_memory_budget=10_000)
    records = source.discover([tmp_path])
    first = source.load(records[0])
    source.load(records[1])
    source.load(records[2])

    assert source.cache_info[0] == 2
    assert first.details(()).fields["output"] == 0
    assert source.load(records[0]) is not first


def test_cache_does_not_retain_document_larger_than_budget(tmp_path):
    path = tmp_path / "large.json"
    path.write_text(json.dumps({"value": "x" * 2_000}), encoding="utf-8")
    source = JsonFileSource(cache_capacity=4, cache_memory_budget=100)
    record = source.discover([path])[0]

    assert source.load(record) is not source.load(record)
    assert source.cache_info == (0, 0)


def test_cache_access_is_thread_safe(tmp_path):
    path = tmp_path / "threaded.json"
    path.write_text(json.dumps({"output": list(range(100))}), encoding="utf-8")
    source = JsonFileSource(cache_capacity=2)
    record = source.discover([path])[0]

    with ThreadPoolExecutor(max_workers=8) as executor:
        documents = list(executor.map(lambda _index: source.load(record), range(40)))

    assert all(
        document.details(()).fields["output"][50] == 50 for document in documents
    )
    assert source.cache_info[0] == 1


def test_cache_invalidates_when_file_signature_changes(tmp_path):
    path = tmp_path / "changing.json"
    path.write_text('{"output": 1}', encoding="utf-8")
    source = JsonFileSource()
    record = source.discover([path])[0]
    first = source.load(record)
    sleep(0.002)
    path.write_text('{"output": 200}', encoding="utf-8")

    second = source.load(record)

    assert second is not first
    assert second.details(()).fields["output"] == 200
