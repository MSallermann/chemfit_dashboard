from __future__ import annotations

import json

from chemfit_dashboard.data_flow import (
    INPUT_NODE,
    OUTPUT_NODE,
    build_data_flow,
    child_node,
)
from chemfit_dashboard.graph_layout import data_flow_layout
from chemfit_dashboard.source import JsonFileSource


def _document(tmp_path, value, name="flow.json"):
    path = tmp_path / name
    path.write_text(json.dumps(value), encoding="utf-8")
    source = JsonFileSource()
    return source.load(source.discover([path])[0])


def _context(structure, children=()):
    return {"meta": {"chemfit.structure": structure}, "children": list(children)}


def _edges(graph):
    return [(edge.source, edge.target) for edge in graph.edges]


def test_genuine_chemfit_pipeline_structure(chemfit_document):
    graph = build_data_flow(chemfit_document)

    assert _edges(graph) == [
        (INPUT_NODE, child_node(0)),
        (child_node(0), child_node(1)),
        (child_node(1), OUTPUT_NODE),
    ]


def test_parallel_branches_multiple_sources_and_outputs(tmp_path):
    document = _document(
        tmp_path,
        _context(
            {
                "children": [
                    {"index": 0, "name": "left"},
                    {"index": 1, "name": "right"},
                    {"index": 2, "name": "join"},
                ],
                "inputs": [
                    [{"kind": "parent_input"}],
                    [{"kind": "parent_input"}],
                    [
                        {"kind": "child", "index": 0},
                        {"kind": "child", "index": 1},
                    ],
                ],
                "outputs": [
                    {"kind": "child", "index": 2},
                    {"kind": "child", "index": 0},
                ],
                "dependencies": [[], [], []],
            }
        ),
    )

    assert _edges(build_data_flow(document)) == [
        (INPUT_NODE, child_node(0)),
        (INPUT_NODE, child_node(1)),
        (child_node(0), child_node(2)),
        (child_node(1), child_node(2)),
        (child_node(2), OUTPUT_NODE),
        (child_node(0), OUTPUT_NODE),
    ]


def test_static_children_survive_failed_and_missing_runtime_contexts(tmp_path):
    document = _document(
        tmp_path,
        _context(
            {
                "children": [
                    {"index": 0, "name": "fails"},
                    {"index": 1, "name": "not_run"},
                ],
                "inputs": [
                    [{"kind": "parent_input"}],
                    [{"kind": "child", "index": 0}],
                ],
                "outputs": [{"kind": "child", "index": 1}],
                "dependencies": [[], [0]],
            },
            [{"output": "ValueError: boom"}],
        ),
    )

    children = [
        node for node in build_data_flow(document).nodes if node.kind == "child"
    ]

    assert [node.status for node in children] == ["failed", "not recorded"]
    assert not children[1].recorded


def test_skipped_component_remains_visible(tmp_path):
    document = _document(
        tmp_path,
        _context(
            {
                "children": [{"index": 0, "name": "optional"}],
                "inputs": [[{"kind": "parent_input"}]],
                "outputs": [{"kind": "child", "index": 0}],
                "dependencies": [[]],
            },
            [{"output": {"skipped": True}}],
        ),
    )

    child = build_data_flow(document).nodes[1]

    assert child.status == "skipped"
    assert child.recorded


def test_nested_components_are_marked_without_name_heuristics(tmp_path):
    nested = _context(
        {
            "children": [{"index": 0, "name": "leaf"}],
            "inputs": [[{"kind": "parent_input"}]],
            "outputs": [{"kind": "child", "index": 0}],
            "dependencies": [[]],
        },
        [{"output": 1}],
    )
    root = _context(
        {
            "children": [{"index": 0, "name": "innocent_name"}],
            "inputs": [[{"kind": "parent_input"}]],
            "outputs": [{"kind": "child", "index": 0}],
            "dependencies": [[]],
        },
        [nested],
    )

    child = build_data_flow(_document(tmp_path, root)).nodes[1]

    assert child.nested


def test_missing_structure_has_no_inferred_flow(tmp_path):
    document = _document(tmp_path, {"children": [{"output": 1}]})

    graph = build_data_flow(document)

    assert not graph.available
    assert graph.nodes == ()
    assert "No declared data flow" in (graph.message or "")


def test_layout_is_left_to_right_deterministic_and_keeps_disconnected_nodes():
    nodes = [INPUT_NODE, child_node(0), child_node(1), child_node(2), OUTPUT_NODE]
    edges = [
        (INPUT_NODE, child_node(0)),
        (INPUT_NODE, child_node(1)),
        (child_node(0), OUTPUT_NODE),
        (child_node(1), OUTPUT_NODE),
    ]

    first = data_flow_layout(nodes, edges)
    second = data_flow_layout(reversed(nodes), reversed(edges))

    assert first == second
    assert set(first.positions) == set(nodes)
    assert first.positions[INPUT_NODE][0] < first.positions[child_node(0)][0]
    assert first.positions[child_node(0)][0] < first.positions[OUTPUT_NODE][0]
