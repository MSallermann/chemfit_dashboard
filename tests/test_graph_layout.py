from __future__ import annotations

import json

from PySide6.QtCore import QModelIndex

from chemfit_dashboard.data_flow import (
    INPUT_NODE,
    OUTPUT_NODE,
    build_data_flow,
    child_node,
)
from chemfit_dashboard.graph_layout import data_flow_layout
from chemfit_dashboard.graph_view import DataFlowPanel
from chemfit_dashboard.main_window import MainWindow
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


def test_pipeline_uses_inputs_and_outputs_but_not_dependencies(tmp_path):
    document = _document(
        tmp_path,
        _context(
            {
                "children": [
                    {"index": 0, "name": "prepare"},
                    {"index": 1, "name": "finish"},
                ],
                "inputs": [
                    [{"kind": "parent_input"}],
                    [{"kind": "child", "index": 0}],
                ],
                "outputs": [{"kind": "child", "index": 1}],
                "dependencies": [[1], []],
            },
            [{"output": 1}, {"output": 2}],
        ),
    )

    graph = build_data_flow(document)

    assert _edges(graph) == [
        (INPUT_NODE, child_node(0)),
        (child_node(0), child_node(1)),
        (child_node(1), OUTPUT_NODE),
    ]
    assert (child_node(1), child_node(0)) not in _edges(graph)


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


def test_parent_input_can_pass_directly_to_output(tmp_path):
    graph = build_data_flow(
        _document(
            tmp_path,
            _context(
                {
                    "children": [],
                    "inputs": [],
                    "outputs": [{"kind": "parent_input"}],
                    "dependencies": [],
                }
            ),
        )
    )

    assert _edges(graph) == [(INPUT_NODE, OUTPUT_NODE)]


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

    children = [node for node in build_data_flow(document).nodes if node.kind == "child"]

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


def test_large_layout_contains_every_node():
    nodes = [INPUT_NODE, *(child_node(index) for index in range(2_000)), OUTPUT_NODE]
    edges = [(INPUT_NODE, child_node(index)) for index in range(2_000)]
    edges.extend((child_node(index), OUTPUT_NODE) for index in range(2_000))

    layout = data_flow_layout(nodes, edges)

    assert len(layout.positions) == 2_002
    assert len(layout.edges) == 4_000


def test_graph_batches_noninteractive_scene_items(chemfit_document):
    panel = DataFlowPanel()
    panel.set_document(chemfit_document)
    panel.ensure_rendered()

    # Nodes remain individually interactive, while all edges and arrowheads
    # share two paths and labels/status decoration are painted by each node.
    node_count = panel.view.rendered_node_count
    assert len(panel.view.scene().items()) <= node_count + 3


def test_graph_refuses_oversized_declared_flow_before_building(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("chemfit_dashboard.graph_view.MAX_FLOW_COMPONENTS", 2)
    document = _document(
        tmp_path,
        _context(
            {
                "children": [
                    {"index": 0, "name": "one"},
                    {"index": 1, "name": "two"},
                    {"index": 2, "name": "three"},
                ],
                "inputs": [[], [], []],
                "outputs": [],
            }
        ),
    )
    panel = DataFlowPanel()
    panel.set_document(document)

    panel.ensure_rendered()

    assert panel.view.rendered_node_count == 0
    assert "rendering is limited to 2" in panel.status.text()


def test_breadcrumb_navigation_and_selection(tmp_path, capsys):
    inner = _context(
        {
            "children": [{"index": 0, "name": "leaf"}],
            "inputs": [[{"kind": "parent_input"}]],
            "outputs": [{"kind": "child", "index": 0}],
            "dependencies": [[]],
        },
        [{"output": 1}],
    )
    document = _document(
        tmp_path,
        _context(
            {
                "children": [{"index": 0, "name": "nested"}],
                "inputs": [[{"kind": "parent_input"}]],
                "outputs": [{"kind": "child", "index": 0}],
                "dependencies": [[]],
            },
            [inner],
        ),
    )
    panel = DataFlowPanel()
    panel.set_document(document)
    panel.ensure_rendered()

    panel.view._nodes[(0,)].setSelected(True)  # noqa: SLF001
    panel._enter((0,))  # noqa: SLF001
    assert panel.view.current_path == (0,)
    assert panel.back_button.isEnabled()

    panel.view._nodes[(0, 0)].setSelected(True)  # noqa: SLF001
    panel._back()  # noqa: SLF001
    assert panel.view.current_path == ()
    assert "Internal C++ object" not in capsys.readouterr().err


def test_data_flow_ignores_items_destroyed_by_qt(tmp_path, capsys):
    document = _document(
        tmp_path,
        _context(
            {
                "children": [{"index": 0, "name": "step"}],
                "inputs": [[{"kind": "parent_input"}]],
                "outputs": [{"kind": "child", "index": 0}],
                "dependencies": [[]],
            },
            [{"output": 4}],
        ),
    )
    panel = DataFlowPanel()
    panel.set_document(document)
    panel.ensure_rendered()
    panel.view._nodes[(0,)].setSelected(True)  # noqa: SLF001

    # Qt can own and destroy scene items during widget teardown without going
    # through DataFlowView._clear_scene(). Stale Python wrappers must be ignored.
    panel.view.scene().clear()
    panel.view._selection_changed()  # noqa: SLF001

    assert panel.view.rendered_node_count == 0
    assert "Internal C++ object" not in capsys.readouterr().err


def test_tree_data_flow_and_inspector_selection_are_synchronized(tmp_path):
    document = _document(
        tmp_path,
        _context(
            {
                "children": [{"index": 0, "name": "step"}],
                "inputs": [[{"kind": "parent_input"}]],
                "outputs": [{"kind": "child", "index": 0}],
                "dependencies": [[]],
            },
            [{"output": 4}],
        ),
    )
    window = MainWindow(source=JsonFileSource())
    window._load_generation = 1  # noqa: SLF001
    window._document_loaded(1, document)  # noqa: SLF001
    window.hierarchy_tabs.setCurrentWidget(window.data_flow)
    window.data_flow.ensure_rendered()

    window.data_flow.node_selected.emit((0,))

    info = window.tree_model.node_info(window.tree.currentIndex())
    assert info is not None
    assert info.path == (0,)
    assert window.inspector.title.text() == "step"
    assert window.tree.currentIndex() != QModelIndex()
