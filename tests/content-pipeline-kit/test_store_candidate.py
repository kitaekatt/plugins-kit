"""Tests for content_pipeline.store.candidate.

Port-equivalence baseline: these cases translate the candidate-cell behaviors
pinned by localization ``candidate_store`` into the plugin's neutral
vocabulary -- append rejects duplicate id, promote flips statuses (prior
active demoted), at-most-one-active enforced on load, invalid status /
duplicate-cell rejection, active-candidate accessor, produced-count over
distinct non-retired values, and the pluggable-serialization round trip. No
game/loc concepts appear: a cell has an opaque ``key`` tuple and candidates
carry an opaque ``value``.
"""

import json

import pytest

from content_pipeline.store.candidate import (
    Candidate,
    CandidateCell,
    CandidateError,
    CandidateStatus,
    CandidateStore,
    append_candidate,
    cell_from_dict,
    cell_to_dict,
    dump_store,
    load_store,
    promote_candidate,
    retire_candidate,
    rider_cache_key,
    store_from_doc,
    store_to_doc,
)


def _cell(key=("u", "v"), entries=()):
    return CandidateCell(key=key, entries=tuple(entries))


# -- Candidate invariants -----------------------------------------------------

def test_candidate_rejects_invalid_status():
    with pytest.raises(CandidateError):
        Candidate(id="c0", value="x", status="bogus")


def test_candidate_rejects_empty_id():
    with pytest.raises(CandidateError):
        Candidate(id="", value="x")


def test_candidate_status_enum_accepted():
    c = Candidate(id="c0", value="x", status=CandidateStatus.ACTIVE)
    assert c.status == "active"


# -- append -------------------------------------------------------------------

def test_append_rejects_duplicate_id():
    cell = _cell(entries=[Candidate(id="c0", value="a")])
    with pytest.raises(CandidateError):
        append_candidate(cell, Candidate(id="c0", value="b"))


def test_append_preserves_locked():
    cell = CandidateCell(key=("u", "v"), entries=(Candidate(id="c0", value="a"),), locked=True)
    new = append_candidate(cell, Candidate(id="c1", value="b"))
    assert new.locked is True
    assert len(new.entries) == 2


# -- promote ------------------------------------------------------------------

def test_promote_flips_prior_active_to_shadow():
    cell = _cell(entries=[
        Candidate(id="c0", value="a", status="active"),
        Candidate(id="c1", value="b", status="shadow"),
    ])
    new = promote_candidate(cell, "c1")
    assert new.get("c1").status == "active"
    assert new.get("c0").status == "shadow"  # kept eligible (N-option default)


def test_promote_can_retire_prior_active():
    cell = _cell(entries=[
        Candidate(id="c0", value="a", status="active"),
        Candidate(id="c1", value="b", status="shadow"),
    ])
    new = promote_candidate(cell, "c1", retire_previous=True)
    assert new.get("c1").status == "active"
    assert new.get("c0").status == "retired"


def test_promote_leaves_retired_alone():
    cell = _cell(entries=[
        Candidate(id="c0", value="a", status="active"),
        Candidate(id="c1", value="b", status="retired"),
        Candidate(id="c2", value="c", status="shadow"),
    ])
    new = promote_candidate(cell, "c2")
    assert new.get("c1").status == "retired"


def test_promote_unknown_id_raises():
    cell = _cell(entries=[Candidate(id="c0", value="a")])
    with pytest.raises(CandidateError):
        promote_candidate(cell, "nope")


def test_retire_candidate():
    cell = _cell(entries=[Candidate(id="c0", value="a", status="active")])
    new = retire_candidate(cell, "c0")
    assert new.get("c0").status == "retired"


# -- accessors ----------------------------------------------------------------

def test_active_returns_active():
    cell = _cell(entries=[
        Candidate(id="c0", value="a", status="shadow"),
        Candidate(id="c1", value="b", status="active"),
    ])
    assert cell.active.id == "c1"


def test_active_none_when_no_active():
    cell = _cell(entries=[Candidate(id="c0", value="a", status="shadow")])
    assert cell.active is None


def test_produced_count_distinct_nonempty_non_retired():
    cell = _cell(entries=[
        Candidate(id="c0", value="same", status="active"),
        Candidate(id="c1", value="same", status="shadow"),   # duplicate value
        Candidate(id="c2", value="other", status="shadow"),
        Candidate(id="c3", value="", status="shadow"),         # empty -> excluded
        Candidate(id="c4", value="gone", status="retired"),    # retired -> excluded
    ])
    assert cell.produced_count == 2


# -- serialization (pluggable engine) -----------------------------------------

def test_cell_round_trip_via_dict():
    cell = CandidateCell(
        key=("u", "v"),
        entries=(
            Candidate(id="c0", value="a", status="active",
                      grade_summary={"axes": []}, riders={"len": {"ok": True}}),
            Candidate(id="c1", value="b", status="shadow"),
        ),
        locked=True,
    )
    doc = cell_to_dict(cell)
    back = cell_from_dict(doc)
    assert back == cell


def test_cell_from_dict_rejects_two_active():
    doc = {"key": ["u", "v"], "entries": [
        {"id": "c0", "value": "a", "status": "active"},
        {"id": "c1", "value": "b", "status": "active"},
    ]}
    with pytest.raises(CandidateError):
        cell_from_dict(doc)


def test_cell_from_dict_rejects_duplicate_id():
    doc = {"key": ["u", "v"], "entries": [
        {"id": "c0", "value": "a", "status": "shadow"},
        {"id": "c0", "value": "b", "status": "shadow"},
    ]}
    with pytest.raises(CandidateError):
        cell_from_dict(doc)


def test_unlocked_cell_omits_locked_key():
    doc = cell_to_dict(_cell(entries=[Candidate(id="c0", value="a")]))
    assert "locked" not in doc


def test_candidate_extras_round_trip():
    c = Candidate(id="c0", value="a", extras={"introduced_at": 0})
    back = cell_from_dict(cell_to_dict(_cell(entries=[c]))).entries[0]
    assert back.extras == {"introduced_at": 0}


def test_unknown_candidate_keys_land_in_extras():
    c = candidate = cell_from_dict(
        {"key": ["u", "v"], "entries": [{"id": "c0", "value": "a", "future": 1}]}
    ).entries[0]
    assert c.extras.get("future") == 1


# -- store --------------------------------------------------------------------

def test_store_from_empty_doc_is_empty():
    assert store_from_doc(None).cells == {}
    assert store_from_doc({}).cells == {}


def test_store_round_trip():
    store = CandidateStore()
    store.add(_cell(key=("u1", "v"), entries=[Candidate(id="c0", value="a", status="active")]))
    store.add(_cell(key=("u2", "v"), entries=[Candidate(id="c0", value="b")]))
    back = store_from_doc(store_to_doc(store))
    assert back.get(("u1", "v")).active.value == "a"
    assert set(back.cells) == {("u1", "v"), ("u2", "v")}


def test_store_add_rejects_duplicate_key():
    store = CandidateStore()
    store.add(_cell(key=("u", "v")))
    with pytest.raises(CandidateError):
        store.add(_cell(key=("u", "v")))


def test_store_from_doc_rejects_duplicate_cell():
    doc = {"cells": [
        {"key": ["u", "v"], "entries": []},
        {"key": ["u", "v"], "entries": []},
    ]}
    with pytest.raises(CandidateError):
        store_from_doc(doc)


def test_load_dump_store_with_injected_yaml_engine():
    # The engine is injected -- here a JSON round-trip stands in for whatever
    # (C-backed) YAML engine a bulk consumer chooses. The store module binds
    # none of its own.
    store = CandidateStore()
    store.add(_cell(key=("u", "v"), entries=[Candidate(id="c0", value="a", status="active")]))
    text = dump_store(store, yaml_dump=json.dumps)
    back = load_store(text, yaml_load=json.loads)
    assert back.get(("u", "v")).active.value == "a"


# -- rider cache keys ---------------------------------------------------------

def test_rider_cache_key_is_stable_and_value_sensitive():
    k1 = rider_cache_key("value-a")
    k2 = rider_cache_key("value-a")
    k3 = rider_cache_key("value-b")
    assert k1 == k2
    assert k1 != k3


def test_rider_cache_key_salt_decorrelates():
    assert rider_cache_key("v", salt="s1") != rider_cache_key("v", salt="s2")
