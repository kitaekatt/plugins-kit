"""An inline run that died mid-unit leaves a CLAIMED unit; prepare_run can re-offer it."""

from __future__ import annotations

import pytest

from content_pipeline.execution.controller import RunAdapter, prepare_run
from content_pipeline.execution.drivers.inline import run_wave
from content_pipeline.execution.model import AttemptKind, UnitState
from content_pipeline.execution.store import ExecutionStore
from content_pipeline.pipeline.workunit import FlatChunkStrategy, GraphWalkStrategy, WorkUnit

FLAT = FlatChunkStrategy(select=lambda store: [])


def _store(tmp_path, ids=("u0", "u1", "u2")):
    store = ExecutionStore(tmp_path / "run.db")
    store.create_run("run-1", driver="inline", backend="mock", model="m", adapter_version="1")
    store.register_units("run-1", list(ids))
    return store


def _wus(ids):
    return [WorkUnit(id=i, payload=i) for i in ids]


def _crash_claim(store, uid, *, lease=10.0, at=1000.0):
    store.claim_unit("run-1", uid, "dead-inline", lease_seconds=lease, at=at)


def test_expired_claim_never_reoffered_without_opt_in(tmp_path):
    store = _store(tmp_path)
    _crash_claim(store, "u1")
    wave = prepare_run(store, "run-1", FLAT, _wus(["u0", "u1", "u2"]))
    assert [u.unit_id for u in wave] == ["u0", "u2"]


def test_flat_expired_claim_reoffered_and_reclaimed_with_fence_bump(tmp_path):
    store = _store(tmp_path)
    _crash_claim(store, "u1")
    old = store.get_unit("run-1", "u1").fencing_token
    wave = prepare_run(store, "run-1", FLAT, _wus(["u0", "u1", "u2"]), reclaim_at=2000.0)
    assert [u.unit_id for u in wave] == ["u0", "u1", "u2"]
    done = run_wave(store, "run-1", wave, generate=lambda wu: "t", at=2000.0)
    assert done == ["u0", "u1", "u2"]
    u1 = store.get_unit("run-1", "u1")
    assert u1.state is UnitState.ACCEPTED and u1.fencing_token > old
    assert any(a.kind is AttemptKind.EXPIRE for a in store.list_attempts("run-1", "u1"))


def test_live_claim_never_reoffered(tmp_path):
    store = _store(tmp_path)
    _crash_claim(store, "u1", lease=10000.0)
    wave = prepare_run(store, "run-1", FLAT, _wus(["u0", "u1", "u2"]), reclaim_at=2000.0)
    assert [u.unit_id for u in wave] == ["u0", "u2"]


def test_open_dispatch_claim_never_reoffered(tmp_path):
    store = _store(tmp_path)
    _crash_claim(store, "u1")
    from unittest import mock

    with mock.patch.object(
        ExecutionStore, "open_dispatches", return_value=[mock.Mock(unit_id="u1")]
    ):
        wave = prepare_run(store, "run-1", FLAT, _wus(["u0", "u1", "u2"]), reclaim_at=2000.0)
    assert [u.unit_id for u in wave] == ["u0", "u2"]


def test_max_wave_size_applies_to_reclaimed(tmp_path):
    store = _store(tmp_path)
    _crash_claim(store, "u1")
    wave = prepare_run(
        store, "run-1", FLAT, _wus(["u0", "u1", "u2"]), reclaim_at=2000.0, max_wave_size=2
    )
    assert [u.unit_id for u in wave] == ["u0", "u1"]


def test_reclaim_limit_terminally_fails_exhausted_unit(tmp_path):
    store = _store(tmp_path, ids=("u0",))
    t = 1000.0
    for _ in range(3):  # claim, expire, reclaim: two EXPIRE rows after third claim
        store.claim_unit("run-1", "u0", "dead", lease_seconds=10.0, at=t)
        t += 100.0
    wave = prepare_run(store, "run-1", FLAT, _wus(["u0"]), reclaim_at=t)
    assert wave == []
    unit = store.get_unit("run-1", "u0")
    assert unit.state is UnitState.FAILED
    assert any(
        a.error == "reclaim_exhausted" for a in store.list_attempts("run-1", "u0")
    )


def _graph():
    return GraphWalkStrategy(order=lambda src: ["u0", "u1"], payload_of=lambda src, i: i)


def test_graph_expired_claim_reoffered(tmp_path):
    store = _store(tmp_path, ids=("u0", "u1"))
    _crash_claim(store, "u0")
    assert prepare_run(store, "run-1", _graph(), _wus(["u0", "u1"])) == []
    wave = prepare_run(store, "run-1", _graph(), _wus(["u0", "u1"]), reclaim_at=2000.0)
    assert [u.unit_id for u in wave] == ["u0"]


def test_graph_live_claim_not_reoffered(tmp_path):
    store = _store(tmp_path, ids=("u0", "u1"))
    _crash_claim(store, "u0", lease=10000.0)
    wave = prepare_run(store, "run-1", _graph(), _wus(["u0", "u1"]), reclaim_at=2000.0)
    assert wave == []
