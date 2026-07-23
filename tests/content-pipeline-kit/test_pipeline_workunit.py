"""Tests for content_pipeline.pipeline.workunit.

Pins the two work-unit shapes: a flat-chunk strategy (unordered, independently
processable, batched) and a graph-walk strategy (ordered traversal where each
unit carries per-predecessor context). Neutral vocabulary throughout -- a unit
is an opaque id + payload + context.
"""

from content_pipeline.pipeline.workunit import (
    FlatChunkStrategy,
    GraphWalkStrategy,
    WorkUnit,
)


# -- FlatChunkStrategy --------------------------------------------------------

def test_flat_units_preserve_select_order():
    strat = FlatChunkStrategy(select=lambda s: [(k, s[k]) for k in s["order"]])
    store = {"order": ["c", "a", "b"], "c": 3, "a": 1, "b": 2}
    units = strat.units(store)
    assert [u.id for u in units] == ["c", "a", "b"]
    assert [u.payload for u in units] == [3, 1, 2]
    assert all(u.context == {} for u in units)


def test_flat_chunks_split_by_size():
    strat = FlatChunkStrategy(
        select=lambda s: [(str(i), i) for i in s], chunk_size=2
    )
    chunks = strat.chunks([0, 1, 2, 3, 4])
    assert [[u.id for u in c] for c in chunks] == [["0", "1"], ["2", "3"], ["4"]]


def test_flat_chunks_zero_size_is_single_chunk():
    strat = FlatChunkStrategy(select=lambda s: [(str(i), i) for i in s])
    assert len(strat.chunks([1, 2, 3])) == 1


def test_flat_chunks_empty_is_no_chunks():
    strat = FlatChunkStrategy(select=lambda s: [])
    assert strat.chunks([]) == []


# -- GraphWalkStrategy --------------------------------------------------------

def test_graph_walk_visits_in_order():
    strat = GraphWalkStrategy(order=lambda s: s["nodes"])
    units = strat.units({"nodes": ["n1", "n2", "n3"]})
    assert [u.id for u in units] == ["n1", "n2", "n3"]


def test_graph_walk_context_sees_predecessors():
    # Each node's context depends on the units walked before it -- the
    # structural-adjacency property a flat strategy lacks.
    def context_of(store, node, walked):
        return {"predecessor_ids": [u.id for u in walked]}

    strat = GraphWalkStrategy(
        order=lambda s: ["a", "b", "c"], context_of=context_of
    )
    units = strat.units({})
    assert units[0].context["predecessor_ids"] == []
    assert units[1].context["predecessor_ids"] == ["a"]
    assert units[2].context["predecessor_ids"] == ["a", "b"]


def test_graph_walk_predecessors_of_shortcut():
    strat = GraphWalkStrategy(
        order=lambda s: ["a", "b"],
        predecessors_of=lambda s, n: s["preds"].get(n, []),
    )
    units = strat.units({"preds": {"b": ["a"]}})
    assert units[0].context == {"predecessors": []}
    assert units[1].context == {"predecessors": ["a"]}


def test_graph_walk_payload_of():
    strat = GraphWalkStrategy(
        order=lambda s: ["x"], payload_of=lambda s, n: s["data"][n]
    )
    units = strat.units({"data": {"x": 42}})
    assert units[0] == WorkUnit(id="x", payload=42, context={})
