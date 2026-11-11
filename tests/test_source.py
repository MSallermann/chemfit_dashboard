from __future__ import annotations

import json
import os

import pytest

from chemfit_dashboard.example import (
    build_example_context,
    build_failed_example_context,
    write_example_collection,
)
from chemfit_dashboard.source import ContextSourceError, JsonFileSource


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


def test_explicit_unsupported_file_type_is_rejected(tmp_path):
    path = tmp_path / "evaluation.xyz"
    path.write_text("not a ChemFit record", encoding="utf-8")

    with pytest.raises(ContextSourceError, match=r"Unsupported file type.*\.xyz"):
        JsonFileSource().discover([path])


def test_malformed_json_is_discovered_and_rejected_during_load(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text('{"output":', encoding="utf-8")
    source = JsonFileSource()

    records = source.discover([path])

    assert [record.name for record in records] == ["broken.json"]
    with pytest.raises(ContextSourceError, match=r"Cannot load broken\.json"):
        source.load(records[0])


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


def test_loads_versioned_context_record(tmp_path, chemfit_context):
    path = tmp_path / "record.json"
    path.write_text(json.dumps(chemfit_context.to_record()), encoding="utf-8")
    source = JsonFileSource()

    document = source.load(source.discover([path])[0])

    assert document.node_count == 3
    assert document.root.status == "complete"
    assert document.details(()).fields["context_type"] == "generic"
    assert document.details(()).fields["input"] == chemfit_context.input
    assert document.details(()).fields["output"] == chemfit_context.output
    assert [node.label for node in document.children(())] == [
        "compute_quantities",
        "compute_loss",
    ]


def test_example_collection_is_written_as_versioned_records(tmp_path):
    write_example_collection(tmp_path)

    payloads = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(tmp_path.glob("*.json"))
    ]

    assert len(payloads) == 6
    assert {payload["format"] for payload in payloads} == {"chemfit.evaluate_context"}
    assert {payload["schema_version"] for payload in payloads} == {1}


def test_jsonl_trajectory_is_discovered_as_one_collection(tmp_path, chemfit_context):
    path = tmp_path / "trajectory.jsonl"
    record = chemfit_context.to_record()
    path.write_text(
        "\n".join(
            (
                json.dumps({"step": 4, "slot": 0, "record": record}),
                "",
                json.dumps({"step": 5, "slot": 1, "record": record}),
                "",
            )
        ),
        encoding="utf-8",
    )
    source = JsonFileSource()

    records = source.discover([path])
    documents = [source.load(record_ref) for record_ref in records]

    assert [record_ref.position_in_file for record_ref in records] == [1, 3]
    assert [record_ref.name for record_ref in records] == [
        "trajectory.jsonl:1",
        "trajectory.jsonl:3",
    ]
    assert len({record_ref.id for record_ref in records}) == 2
    assert [document.details(()).fields["step"] for document in documents] == [4, 5]
    assert [document.details(()).fields["slot"] for document in documents] == [0, 1]


def test_jsonl_discovery_preserves_invalid_records_offsets_and_valid_access(
    tmp_path, chemfit_context
):
    path = tmp_path / "trajectory.jsonl"
    valid_first = json.dumps(
        {"step": 4, "slot": 0, "record": chemfit_context.to_record()}
    ).encode()
    invalid = b'{"record": invalid}\n'
    valid_second = json.dumps(
        {"step": 5, "slot": 1, "record": chemfit_context.to_record()}
    ).encode()
    path.write_bytes(valid_first + b"\n\n" + invalid + valid_second + b"\n")
    source = JsonFileSource()

    records = source.discover([path])

    assert [record.position_in_file for record in records] == [1, 3, 4]
    assert [record.byte_offset for record in records] == [
        0,
        len(valid_first) + 2,
        len(valid_first) + 2 + len(invalid),
    ]
    assert [record.size for record in records] == [
        len(valid_first) + 1,
        len(invalid),
        len(valid_second) + 1,
    ]
    assert source.load(records[0]).details(()).fields["step"] == 4
    with pytest.raises(
        ContextSourceError,
        match=r"Cannot load trajectory\.jsonl line 3",
    ):
        source.load(records[1])
    assert source.load(records[2]).details(()).fields["step"] == 5


def test_versioned_exception_and_unrecorded_children_keep_their_semantics(tmp_path):
    context = build_failed_example_context()
    path = tmp_path / "failed.json"
    path.write_text(json.dumps(context.to_record()), encoding="utf-8")
    source = JsonFileSource()

    document = source.load(source.discover([path])[0])

    assert document.root.status == "failed"
    assert document.details(()).fields["output"].startswith("RuntimeError:")
    failed_term = document.details((2, 0, 2))
    assert failed_term.info.status == "failed"
    assert failed_term.fields["output"] == (
        "RuntimeError: intentional failure in the structure loss term"
    )


def test_versioned_unrecorded_child_remains_declared_but_unexecuted(tmp_path):
    no_output = {"__type__": "no_output"}
    path = tmp_path / "partial.json"
    path.write_text(
        json.dumps(
            {
                "format": "chemfit.evaluate_context",
                "schema_version": 1,
                "chemfit_version": "4.0.0",
                "root": {
                    "context_type": "generic",
                    "input": {},
                    "output": no_output,
                    "meta": {
                        "chemfit.structure": {
                            "children": [{"index": 0, "name": "pending_term"}]
                        }
                    },
                    "status": "no_output",
                    "path": [],
                    "children": [
                        {
                            "context_type": None,
                            "input": no_output,
                            "output": no_output,
                            "meta": {},
                            "status": "not_recorded",
                            "path": [0],
                            "children": [],
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    source = JsonFileSource()

    document = source.load(source.discover([path])[0])

    child = document.children(())[0]
    assert child.label == "pending_term"
    assert child.status == "not recorded"
    assert not child.recorded


def test_unsupported_record_schema_reports_the_jsonl_line_during_load(tmp_path):
    path = tmp_path / "trajectory.jsonl"
    path.write_text(
        json.dumps(
            {
                "step": 0,
                "slot": 0,
                "record": {
                    "format": "chemfit.evaluate_context",
                    "schema_version": 999,
                    "chemfit_version": "future",
                    "root": {},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    source = JsonFileSource()
    record = source.discover([path])[0]

    with pytest.raises(
        ContextSourceError,
        match=r"trajectory\.jsonl line 1.*schema version: 999",
    ):
        source.load(record)


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


def test_rediscovery_replaces_stale_cached_document(tmp_path):
    path = tmp_path / "evaluation.json"
    path.write_text(json.dumps({"output": "old"}), encoding="utf-8")
    source = JsonFileSource()
    original_record = source.discover([path])[0]
    original_document = source.load(original_record)

    original_stat = path.stat()
    path.write_text(json.dumps({"output": "new"}), encoding="utf-8")
    os.utime(
        path,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns + 1_000_000_000),
    )
    refreshed_record = source.discover([path])[0]
    refreshed_document = source.load(refreshed_record)

    assert refreshed_record.id == original_record.id
    assert refreshed_record.size == original_record.size
    assert refreshed_record.modified != original_record.modified
    assert refreshed_document is not original_document
    assert refreshed_document.details(()).fields["output"] == "new"
    assert source.load(refreshed_record) is refreshed_document

    refreshed_stat = path.stat()
    path.write_text(json.dumps({"output": "new value"}), encoding="utf-8")
    os.utime(
        path,
        ns=(refreshed_stat.st_atime_ns, refreshed_stat.st_mtime_ns),
    )
    resized_record = source.discover([path])[0]
    resized_document = source.load(resized_record)

    assert resized_record.modified == refreshed_record.modified
    assert resized_record.size != refreshed_record.size
    assert resized_document is not refreshed_document
    assert resized_document.details(()).fields["output"] == "new value"
    assert source.cache_info[0] == 1


def test_cache_does_not_retain_document_larger_than_budget(tmp_path):
    path = tmp_path / "large.json"
    path.write_text(json.dumps({"value": "x" * 2_000}), encoding="utf-8")
    source = JsonFileSource(cache_capacity=4, cache_memory_budget=100)
    record = source.discover([path])[0]

    assert source.load(record) is not source.load(record)
    assert source.cache_info == (0, 0)
