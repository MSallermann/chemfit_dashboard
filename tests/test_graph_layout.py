from __future__ import annotations

import json
import time

from PySide6.QtCore import QRectF
from PySide6.QtWidgets import QApplication

from chemfit_dashboard.graph_layout import (
    LayoutNode,
    dependency_layered_layout,
    hierarchy_outline_layout,
)
from chemfit_dashboard.graph_view import ContextGraphPanel
from chemfit_dashboard.source import JsonFileSource


def test_dependency_layout_is_deterministic_and_respects_pipeline_direction():
    nodes = [LayoutNode((), None), LayoutNode((0,), ()), LayoutNode((1,), ())]
    hierarchy = [((), (0,)), ((), (1,))]
    dependencies = [((0,), (1,))]

    first = dependency_layered_layout(nodes, hierarchy, dependencies)
    second = dependency_layered_layout(reversed(nodes), hierarchy, dependencies)

    assert first == second
    assert first.positions[(0,)][1] < first.positions[(1,)][1]
    dependency = next(edge for edge in first.edges if edge.kind == "dependency")
    assert (dependency.source, dependency.target) == ((0,), (1,))


def test_join_layout_keeps_all_dependency_edges():
    nodes = [
        LayoutNode((), None),
        LayoutNode((0,), ()),
        LayoutNode((1,), ()),
        LayoutNode((2,), ()),
    ]
    layout = dependency_layered_layout(
        nodes,
        [((), (0,)), ((), (1,)), ((), (2,))],
        [((0,), (2,)), ((1,), (2,))],
    )

    assert {
        (edge.source, edge.target) for edge in layout.edges if edge.kind == "dependency"
    } == {((0,), (2,)), ((1,), (2,))}


def test_hierarchy_outline_is_preordered_indented_and_readable():
    nodes = [
        LayoutNode((1,), ()),
        LayoutNode((0, 0), (0,)),
        LayoutNode((), None),
        LayoutNode((0,), ()),
    ]
    layout = hierarchy_outline_layout(
        nodes,
        [((), (0,)), ((0,), (0, 0)), ((), (1,))],
        [((0,), (1,))],
    )

    assert list(layout.positions) == [(), (0,), (0, 0), (1,)]
    assert layout.positions[()][0] < layout.positions[(0,)][0]
    assert layout.positions[(0,)][0] < layout.positions[(0, 0)][0]
    assert [layout.positions[path][1] for path in layout.positions] == sorted(
        layout.positions[path][1] for path in layout.positions
    )


def test_collapsed_graph_reveals_hidden_semantic_selection(tmp_path):
    value = {
        "meta": {"chemfit.structure": {"children": [{"index": 0, "name": "group"}]}},
        "children": [
            {
                "meta": {
                    "chemfit.structure": {"children": [{"index": 0, "name": "leaf"}]}
                },
                "children": [{"output": 1}],
            }
        ],
    }
    path = tmp_path / "graph.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    source = JsonFileSource()
    document = source.load(source.discover([path])[0])
    panel = ContextGraphPanel()
    panel.set_document(document)
    panel.ensure_rendered()

    assert panel.view.rendered_node_count == 2
    assert panel.view.toggle_paths == frozenset({(), (0,)})
    assert panel.view._nodes[()].data(1) == "structural"  # noqa: SLF001
    assert panel.view._nodes[(0,)].data(1) == "structural"  # noqa: SLF001
    panel.select_path((0, 0))

    assert panel.view.rendered_node_count == 3
    assert panel.view._nodes[(0, 0)].data(1) == "atomic"  # noqa: SLF001
    assert [item.data(0) for item in panel.view.scene().selectedItems()] == [(0, 0)]


def test_inline_structural_toggle_expands_and_collapses_component(tmp_path):
    value = {
        "children": [{"children": [{"output": 1}]}],
    }
    path = tmp_path / "inline-toggle.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    source = JsonFileSource()
    document = source.load(source.discover([path])[0])
    panel = ContextGraphPanel()
    panel.set_document(document)
    panel.ensure_rendered()

    panel.view.toggle_path((0,))
    assert panel.view.rendered_node_count == 3
    assert (0,) in panel.view.expanded_paths

    panel.view.toggle_path((0,))
    assert panel.view.rendered_node_count == 2
    assert (0,) not in panel.view.expanded_paths
    assert [item.data(0) for item in panel.view.scene().selectedItems()] == [(0,)]


def test_inline_expansion_preserves_readable_zoom_and_focus(tmp_path):
    value = {"children": [{"children": [{"output": index} for index in range(4)]}]}
    path = tmp_path / "readable-expansion.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    source = JsonFileSource()
    document = source.load(source.discover([path])[0])
    panel = ContextGraphPanel()
    panel.resize(440, 700)
    panel.show()
    panel.set_document(document)
    panel.ensure_rendered()
    initial_zoom = panel.view.transform().m11()

    panel.view.toggle_path((0,))
    QApplication.processEvents()

    assert panel.view.transform().m11() == initial_zoom
    viewport_center = panel.view.viewport().rect().center()
    component_center = panel.view.mapFromScene(
        panel.view._nodes[(0,)].sceneBoundingRect().center()  # noqa: SLF001
    )
    assert abs(viewport_center.x() - component_center.x()) <= 1
    assert abs(90 - component_center.y()) <= 10


def test_graph_defaults_to_data_flow_and_offers_hierarchy_outline(tmp_path):
    path = tmp_path / "dependencies.json"
    path.write_text('{"children": [{"output": 1}]}', encoding="utf-8")
    source = JsonFileSource()
    document = source.load(source.discover([path])[0])
    panel = ContextGraphPanel()
    panel.set_document(document)
    panel.ensure_rendered()

    assert panel.view.dependencies_visible
    panel.dependencies.click()
    assert not panel.view.dependencies_visible


def test_fit_recenters_after_repeated_expand_and_collapse(tmp_path):
    value = {
        "children": [
            {"children": [{"output": index} for index in range(5)]}
            for _group in range(3)
        ]
    }
    path = tmp_path / "fit.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    source = JsonFileSource()
    document = source.load(source.discover([path])[0])
    panel = ContextGraphPanel()
    panel.resize(900, 600)
    panel.show()
    panel.set_document(document)
    panel.ensure_rendered()
    panel.view.expand_all()
    panel.view.toggle_path((0,))
    panel.view.toggle_path((0,))
    panel.view.scale(2.5, 2.5)
    panel.view.horizontalScrollBar().setValue(
        panel.view.horizontalScrollBar().maximum()
    )

    panel.view.fit_graph()
    QApplication.processEvents()

    bounds = QRectF()
    for item in panel.view._nodes.values():  # noqa: SLF001
        bounds = bounds.united(item.sceneBoundingRect())
    viewport_center = panel.view.viewport().rect().center()
    graph_center = panel.view.mapFromScene(bounds.center())
    graph_top = panel.view.mapFromScene(bounds.topLeft()).y()
    assert abs(viewport_center.x() - graph_center.x()) <= 1
    assert 0 <= graph_top <= 40
    assert panel.view.transform().m11() >= 0.78


def test_repeated_collapse_removes_expanded_descendants_without_mutating_iteration(
    tmp_path,
):
    value = {
        "children": [
            {
                "children": [
                    {
                        "children": [{"output": 1}],
                        "meta": {
                            "chemfit.structure": {
                                "children": [{"index": 0, "name": "leaf"}]
                            }
                        },
                    }
                ],
                "meta": {
                    "chemfit.structure": {"children": [{"index": 0, "name": "nested"}]}
                },
            }
        ],
        "meta": {"chemfit.structure": {"children": [{"index": 0, "name": "group"}]}},
    }
    path = tmp_path / "nested.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    source = JsonFileSource()
    document = source.load(source.discover([path])[0])
    panel = ContextGraphPanel()
    panel.set_document(document)
    panel.ensure_rendered()
    panel.view.expand_all()
    panel.select_path((0,))

    panel.view.collapse_selected()
    panel.view.collapse_selected()

    assert panel.view.expanded_paths == frozenset({()})
    assert panel.view.rendered_node_count == 2
    assert [item.data(0) for item in panel.view.scene().selectedItems()] == [(0,)]


def test_expanding_a_leaf_is_a_noop(tmp_path):
    path = tmp_path / "leaf.json"
    path.write_text('{"children": [{"output": 1}]}', encoding="utf-8")
    source = JsonFileSource()
    document = source.load(source.discover([path])[0])
    panel = ContextGraphPanel()
    panel.set_document(document)
    panel.ensure_rendered()
    panel.select_path((0,))
    before = panel.view.expanded_paths

    panel.view.expand_selected()

    assert panel.view.expanded_paths == before


def test_graph_larger_than_old_limit_remains_navigable(tmp_path):
    children = [{"output": index} for index in range(1_050)]
    path = tmp_path / "large.json"
    path.write_text(json.dumps({"children": children}), encoding="utf-8")
    source = JsonFileSource(cache_memory_budget=32 * 1024 * 1024)
    document = source.load(source.discover([path])[0])
    panel = ContextGraphPanel()
    panel.set_document(document)
    panel.ensure_rendered()

    deadline = time.monotonic() + 10
    while panel.view.rendered_node_count == 0 and time.monotonic() < deadline:
        QApplication.processEvents()

    assert panel.view.rendered_node_count == 1_051
    panel.select_path((1_049,))
    assert panel.view.scene().selectedItems()[0].data(0) == (1_049,)


def test_outdated_large_graph_layout_is_discarded(tmp_path):
    large_path = tmp_path / "large.json"
    small_path = tmp_path / "small.json"
    large_path.write_text(
        json.dumps({"children": [{"output": index} for index in range(600)]}),
        encoding="utf-8",
    )
    small_path.write_text('{"output": 1}', encoding="utf-8")
    source = JsonFileSource()
    records = {record.name: record for record in source.discover([tmp_path])}
    large = source.load(records["large.json"])
    small = source.load(records["small.json"])
    panel = ContextGraphPanel()
    panel.set_document(large)
    panel.ensure_rendered()

    panel.set_document(small)
    panel.ensure_rendered()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if panel.view.rendered_node_count == 1:
            break

    assert panel.view.document is small
    assert panel.view.rendered_node_count == 1
