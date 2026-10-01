"""Tests for the typed cell state on content_pipeline.store.candidate (CL3)."""

import json
from dataclasses import dataclass

from content_pipeline.store.candidate import (
    Candidate,
    CandidateCell,
    CellState,
    cell_from_dict,
    cell_to_dict,
    dump_store,
    load_store,
    set_locked,
    store_from_doc,
    store_to_doc,
)


@dataclass(frozen=True)
class LineState(CellState):
    blocked: bool = False
    streak: int = 0


# A document in the shape written before CellState existed.
PRE_CHANGE_DOC = {
    "cells": [
        {
            "key": ["a", 1],
            "entries": [{"id": "c1", "value": "x", "status": "active"}],
            "locked": True,
            "extras": {"note": "n"},
        },
        {"key": ["b"], "entries": []},
    ]
}


def _dump(doc):
    return json.dumps(doc, sort_keys=False)


def test_default_state_doc_is_byte_identical():
    text = _dump(PRE_CHANGE_DOC)
    store = load_store(text, yaml_load=json.loads)
    assert dump_store(store, yaml_dump=_dump) == text
    assert "state" not in text and "state" not in dump_store(store, yaml_dump=_dump)


def test_subclass_state_round_trips_typed():
    cell = CandidateCell(key=("k",), state=LineState(blocked=True, streak=2))
    doc = store_to_doc(store_from_doc({"cells": [cell_to_dict(cell)]}, state_type=LineState))
    assert doc["cells"][0]["state"] == {"blocked": True, "streak": 2}
    back = cell_from_dict(cell_to_dict(cell), state_type=LineState)
    assert isinstance(back.state, LineState)
    assert back.state.blocked is True and back.state.streak == 2


def test_unknown_state_keys_passthrough():
    doc = {"key": ["k"], "state": {"lock_reason": "r", "future": {"a": 1}}}
    cell = cell_from_dict(doc)
    assert cell.state.lock_reason == "r"
    assert cell.state.passthrough == {"future": {"a": 1}}
    assert cell_to_dict(cell)["state"] == {"lock_reason": "r", "future": {"a": 1}}
    # a subclass sees its own keys typed and the rest as passthrough
    sub = cell_from_dict({"key": ["k"], "state": {"streak": 3, "x": 1}}, state_type=LineState)
    assert sub.state.streak == 3 and sub.state.passthrough == {"x": 1}


def test_set_locked_records_reason():
    cell = set_locked(CandidateCell(key=("k",)), True, reason="done")
    assert cell.locked is True and cell.state.lock_reason == "done"
    assert cell_to_dict(cell)["state"] == {"lock_reason": "done"}


def test_set_locked_none_clears_reason_keeps_subclass():
    cell = CandidateCell(key=("k",), state=LineState(lock_reason="r", streak=4))
    out = set_locked(cell, False)
    assert out.state.lock_reason is None
    assert isinstance(out.state, LineState) and out.state.streak == 4
    assert out.locked is False


def test_extras_untouched_by_state():
    cell = CandidateCell(
        key=("k",),
        entries=(Candidate(id="c"),),
        extras={"state": "mine", "n": 1},
        state=CellState(lock_reason="r"),
    )
    doc = cell_to_dict(cell)
    assert doc["extras"] == {"state": "mine", "n": 1}
    assert doc["state"] == {"lock_reason": "r"}
    back = cell_from_dict(doc)
    assert dict(back.extras) == {"state": "mine", "n": 1}
