"""The inline driver asks: a ``generate`` call may signal a durable wait.

``run_wave`` turns an ``InterruptRequested`` raised while a unit's text is
produced into ``store.request_interrupt`` under the claim's own token, leaves
the unit ``waiting`` and goes on with the rest of the wave. These tests run
the real driver against the real store, the real shared contract and the real
llm-scripting-kit validator. Expected values are literals.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from content_pipeline.execution import interrupts
from content_pipeline.execution.adapter import PreparedRequest, RunAdapter
from content_pipeline.execution.controller import finalize_run, prepare_run, unfinished_units
from content_pipeline.execution.drivers.inline import run_wave
from content_pipeline.execution.interrupts import unit_resolutions, waiting_units
from content_pipeline.execution.model import (
    InterruptRequest,
    InterruptRequested,
    UnitState,
    UsageRecord,
)
from content_pipeline.execution.store import ExecutionStore
from content_pipeline.execution.wave import ready_wave
from content_pipeline.llm.backends import MockBackend
from content_pipeline.llm.platform import PipelineHaltError, ValidationSpec
from content_pipeline.pipeline.workunit import FlatChunkStrategy, GraphWalkStrategy, WorkUnit

RUN = "r1"
FLAT = FlatChunkStrategy(select=lambda store: [])
GRAPH = GraphWalkStrategy(order=lambda store: [])

_SHARED_LIB = Path(__file__).resolve().parents[2] / "plugins" / "llm-scripting-kit" / "lib"

APPROVAL = {
    "type": "object",
    "required": ["approved"],
    "properties": {"approved": {"type": "boolean"}},
    "additionalProperties": False,
}

_TABLES = ("runs", "units", "attempts", "dispatches", "interrupts", "interrupt_resolutions")


def _lsk_names():
    return {n for n in sys.modules if n == "llm_scripting_kit" or n.startswith("llm_scripting_kit.")}


@pytest.fixture
def lsk(monkeypatch):
    """Link llm-scripting-kit's validator for one test; unload it afterwards."""
    before = _lsk_names()
    monkeypatch.syspath_prepend(str(_SHARED_LIB))
    yield
    for name in _lsk_names() - before:
        del sys.modules[name]


def _store(tmp_path, units=("u0", "u1", "u2")) -> ExecutionStore:
    store = ExecutionStore(tmp_path / "run.db")
    store.create_run(
        RUN, driver="inline", backend="mock", model="m1", adapter_version="7", created_at=1000.0
    )
    store.register_units(RUN, list(units), at=1000.0)
    return store


def _wave(store, ids):
    return [store.get_unit(RUN, uid) for uid in ids]


def _request(**overrides) -> InterruptRequest:
    fields = {
        "kind": "approval",
        "request_schema": APPROVAL,
        "payload": {"question": "ship it?"},
    }
    fields.update(overrides)
    return InterruptRequest(**fields)


def _rows(store) -> dict:
    conn = sqlite3.connect(str(store.db_path))
    try:
        conn.row_factory = sqlite3.Row
        return {
            table: [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in _TABLES
        }
    finally:
        conn.close()


def _digest(store, accepted) -> str:
    body = json.dumps({"accepted": accepted, "rows": _rows(store)}, sort_keys=True, default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _kinds(store, unit) -> list:
    return [a.kind.value for a in store.list_attempts(RUN, unit)]


# -- a run that never signals is unchanged -----------------------------------------


def _plain_scenario(tmp_path) -> str:
    store = _store(tmp_path)
    accepted = run_wave(
        store, RUN, _wave(store, ["u0", "u1", "u2"]), generate=lambda wu: f"text-{wu.id}", at=1001.0
    )
    assert accepted == ["u0", "u1", "u2"]
    return _digest(store, accepted)


def _halt_scenario(tmp_path) -> str:
    store = _store(tmp_path)

    def generate(work_unit):
        if work_unit.id == "u1":
            raise PipelineHaltError("rate_limit", "hit your limit")
        return f"text-{work_unit.id}"

    accepted = run_wave(store, RUN, _wave(store, ["u0", "u1", "u2"]), generate=generate, at=1001.0)
    assert accepted == ["u0"]
    return _digest(store, accepted)


# sha256 of the accepted list and every table row, captured against the driver
# before the signal clause existed.
BASE_DIGESTS = {
    "plain": "e9048b9f3f7c4ecc35c7bf0c4d0b0879e6060ef918cde01d662162df458cec3a",
    "halt": "e5b1e596fa7b4b205e8e238a0640249482270cd683276ab8cca16ac67c3eaf20",
}


@pytest.mark.parametrize("scenario", ["plain", "halt"])
def test_wave_without_signal_matches_the_base_digests(tmp_path, scenario):
    run = {"plain": _plain_scenario, "halt": _halt_scenario}[scenario]
    assert run(tmp_path) == BASE_DIGESTS[scenario]


# -- the signal requests a wait -----------------------------------------------------


def _signalling(*unit_ids, **kwargs):
    """A ``generate`` that signals for ``unit_ids`` and answers for the rest."""

    def generate(work_unit):
        if work_unit.id in unit_ids:
            raise InterruptRequested(_request(), **kwargs)
        return f"text-{work_unit.id}"

    return generate


def test_generate_signal_puts_the_unit_waiting(tmp_path, lsk):
    store = _store(tmp_path)
    run_wave(store, RUN, _wave(store, ["u0", "u1", "u2"]), generate=_signalling("u1"), at=1001.0)
    unit = store.get_unit(RUN, "u1")
    assert unit.state is UnitState.WAITING
    assert (unit.claimed_by, unit.lease_expires_at) == (None, None)
    record = store.open_interrupt(RUN, "u1")
    assert (record.kind, record.payload, record.created_at) == (
        "approval",
        {"question": "ship it?"},
        1001.0,
    )
    assert _kinds(store, "u1") == ["claim", "interrupt_requested"]


def test_signalling_unit_is_not_accepted_and_is_not_in_the_return(tmp_path, lsk):
    store = _store(tmp_path)
    accepted = run_wave(
        store, RUN, _wave(store, ["u0", "u1", "u2"]), generate=_signalling("u1"), at=1001.0
    )
    assert accepted == ["u0", "u2"]
    assert "accept" not in _kinds(store, "u1")
    assert store.get_unit(RUN, "u1").accepted_text is None


def test_wave_continues_after_a_waiting_unit(tmp_path, lsk):
    store = _store(tmp_path)
    accepted = run_wave(
        store, RUN, _wave(store, ["u0", "u1", "u2"]), generate=_signalling("u0"), at=1001.0
    )
    assert accepted == ["u1", "u2"]
    assert [store.get_unit(RUN, u).state for u in ("u0", "u1", "u2")] == [
        UnitState.WAITING,
        UnitState.ACCEPTED,
        UnitState.ACCEPTED,
    ]


def test_inline_request_carries_the_claim_token(tmp_path, lsk, monkeypatch):
    store = _store(tmp_path)
    seen = []
    real = store.request_interrupt

    def spy(run_id, unit_id, fencing_token, request, **kwargs):
        seen.append((unit_id, fencing_token))
        return real(run_id, unit_id, fencing_token, request, **kwargs)

    monkeypatch.setattr(store, "request_interrupt", spy)
    run_wave(store, RUN, _wave(store, ["u0"]), generate=_signalling("u0"), at=1001.0)
    first = store.open_interrupt(RUN, "u0")
    store.resolve_interrupt(RUN, first.id, decision="answer", input={"approved": True}, now=1002.0)

    # The second claim takes token 2; the request must carry it, not token 1.
    run_wave(store, RUN, _wave(store, ["u0"]), generate=_signalling("u0"), at=1003.0)
    second = store.open_interrupt(RUN, "u0")
    assert seen == [("u0", 1), ("u0", 2)]
    assert (first.fencing_token, second.fencing_token) == (1, 2)
    assert [(a.kind.value, a.fencing_token) for a in store.list_attempts(RUN, "u0")] == [
        ("claim", 1),
        ("interrupt_requested", 1),
        ("interrupt_resolved", 1),
        ("claim", 2),
        ("interrupt_requested", 2),
    ]


def test_signal_policies_and_usage_are_recorded(tmp_path, lsk):
    store = _store(tmp_path)
    generate = _signalling(
        "u0", on_rejected="release", on_expired="release", usage=UsageRecord(5, 6, None)
    )
    run_wave(store, RUN, _wave(store, ["u0"]), generate=generate, at=1001.0)
    record = store.open_interrupt(RUN, "u0")
    assert (record.on_rejected, record.on_expired) == ("release", "release")
    assert store.list_attempts(RUN, "u0")[-1].usage == UsageRecord(5, 6, None)


def test_default_signal_records_the_stop_policies(tmp_path, lsk):
    store = _store(tmp_path)
    run_wave(store, RUN, _wave(store, ["u0"]), generate=_signalling("u0"), at=1001.0)
    record = store.open_interrupt(RUN, "u0")
    assert (record.on_rejected, record.on_expired) == ("stop", "stop")


def _prepared(work_unit):
    return PreparedRequest(unit=work_unit, system="sys", user=f"user-{work_unit.id}")


@pytest.mark.parametrize("builder", ["build_request", "validation_spec_for"])
def test_adapter_request_builder_may_signal(tmp_path, lsk, builder):
    store = _store(tmp_path, units=("u0", "u1"))

    def signal_for_u0(work_unit):
        if work_unit.id == "u0":
            raise InterruptRequested(_request())

    if builder == "build_request":

        def build_request(work_unit):
            signal_for_u0(work_unit)
            return _prepared(work_unit)

        adapter = RunAdapter(build_request=build_request, parse_fn=lambda text: text)
    else:

        def validation_spec_for(work_unit):
            signal_for_u0(work_unit)
            return ValidationSpec(parse_fn=lambda text: text)

        adapter = RunAdapter(build_request=_prepared, validation_spec_for=validation_spec_for)
    backend = MockBackend(responses=["answer-1"])
    accepted = run_wave(
        store, RUN, _wave(store, ["u0", "u1"]), adapter, backend=backend, model="m1", at=1001.0
    )
    assert accepted == ["u1"]
    assert store.get_unit(RUN, "u0").state is UnitState.WAITING
    assert len(backend.calls) == 1


def test_inline_signal_without_edges_raises_support_error(tmp_path, monkeypatch):
    store = _store(tmp_path)

    def no_edge(name):
        raise ModuleNotFoundError(f"No module named {name!r}", name=name)

    monkeypatch.setattr(interrupts, "_import_module", no_edge)
    with pytest.raises(interrupts.InterruptSupportError):
        run_wave(store, RUN, _wave(store, ["u0", "u1"]), generate=_signalling("u1"), at=1001.0)
    unit = store.get_unit(RUN, "u1")
    assert unit.state is UnitState.CLAIMED
    assert unit.claimed_by == "inline"
    assert _kinds(store, "u1") == ["claim"]
    rows = _rows(store)
    assert rows["interrupts"] == rows["interrupt_resolutions"] == rows["dispatches"] == []
    assert store.get_unit(RUN, "u0").state is UnitState.ACCEPTED


def test_inline_wait_writes_no_dispatch_row(tmp_path, lsk):
    store = _store(tmp_path)
    run_wave(store, RUN, _wave(store, ["u0", "u1"]), generate=_signalling("u0"), at=1001.0)
    assert store.get_unit(RUN, "u0").state is UnitState.WAITING
    assert _rows(store)["dispatches"] == []


# -- the drain loop on a waiting run ------------------------------------------------


def _pass_is_waiting(store, applied) -> bool:
    """The documented stop rule: an empty wave, nothing applied, a unit waiting."""
    return not applied and bool(waiting_units(store, RUN))


def _drain(store, strategy, adapter, generate, *, limit=12) -> str:
    """The documented loop, with the waiting-run stop rule. Bounded."""
    for _ in range(limit):
        wave = ready_wave(store, RUN, strategy)
        if wave:
            run_wave(store, RUN, wave, adapter, generate=generate)
            continue
        applied = finalize_run(store, RUN, adapter)
        if not unfinished_units(store, RUN):
            return "complete"
        if _pass_is_waiting(store, applied):
            return "waiting"
    raise AssertionError(f"the loop did not settle in {limit} iterations")


def _drain_fixture(tmp_path, strategy_name):
    store = _store(tmp_path)
    strategy = {"flat": FLAT, "graph": GRAPH}[strategy_name]
    applied = []
    adapter = RunAdapter(
        parse_fn=lambda text: text, apply=lambda unit_id, payload: applied.append(unit_id)
    )

    def generate(work_unit):
        if work_unit.id == "u1" and not unit_resolutions(store, RUN, "u1"):
            raise InterruptRequested(_request())
        return f"text-{work_unit.id}"

    return store, strategy, adapter, generate, applied


@pytest.mark.parametrize("strategy_name", ["flat", "graph"])
def test_drain_loop_ends_the_pass_on_a_waiting_run(tmp_path, lsk, strategy_name):
    store, strategy, adapter, generate, applied = _drain_fixture(tmp_path, strategy_name)
    assert _drain(store, strategy, adapter, generate) == "waiting"
    assert [u.unit_id for u in waiting_units(store, RUN)] == ["u1"]
    if strategy_name == "graph":
        # The chain stops at the waiting unit; its successor was never claimed.
        assert applied == ["u0"]
        assert store.get_unit(RUN, "u2").state is UnitState.PENDING
    else:
        assert applied == ["u0", "u2"]


@pytest.mark.parametrize("strategy_name", ["flat", "graph"])
def test_drain_loop_completes_after_an_answer(tmp_path, lsk, strategy_name):
    store, strategy, adapter, generate, applied = _drain_fixture(tmp_path, strategy_name)
    assert _drain(store, strategy, adapter, generate) == "waiting"
    record = store.open_interrupt(RUN, "u1")
    store.resolve_interrupt(RUN, record.id, decision="answer", input={"approved": True})
    assert _drain(store, strategy, adapter, generate) == "complete"
    assert sorted(applied) == ["u0", "u1", "u2"]
    assert [store.get_unit(RUN, u).state for u in ("u0", "u1", "u2")] == [UnitState.ACCEPTED] * 3
    assert store.get_unit(RUN, "u1").accepted_text == "text-u1"
    assert waiting_units(store, RUN) == []


@pytest.mark.parametrize("outcome", ["rejected", "expired"])
def test_released_outcome_reaches_generate(tmp_path, lsk, outcome):
    store = _store(tmp_path, units=("u0",))

    def first(work_unit):
        raise InterruptRequested(
            _request(expires_in_s=60), on_rejected="release", on_expired="release"
        )

    run_wave(store, RUN, _wave(store, ["u0"]), generate=first, at=1001.0)
    record = store.open_interrupt(RUN, "u0")
    if outcome == "rejected":
        store.resolve_interrupt(RUN, record.id, decision="reject", reason="no thanks", now=1002.0)
    else:
        store.expire_interrupts(RUN, now=2000.0)

    seen = []

    def second(work_unit):
        seen.append(unit_resolutions(store, RUN, work_unit.id))
        return "text-after-outcome"

    wave = ready_wave(store, RUN, FLAT)
    assert [u.unit_id for u in wave] == ["u0"]
    assert run_wave(store, RUN, wave, generate=second, at=3000.0) == ["u0"]
    assert [[r["outcome"] for r in rows] for rows in seen] == [[outcome]]
    assert store.get_unit(RUN, "u0").state is UnitState.ACCEPTED


@pytest.mark.parametrize("outcome", ["rejected", "expired"])
def test_stopped_unit_is_never_offered_again(tmp_path, lsk, outcome):
    store = _store(tmp_path, units=("u0", "u1"))

    def generate(work_unit):
        if work_unit.id == "u0":
            raise InterruptRequested(_request(expires_in_s=60))
        return f"text-{work_unit.id}"

    run_wave(store, RUN, _wave(store, ["u0", "u1"]), generate=generate, at=1001.0)
    record = store.open_interrupt(RUN, "u0")
    if outcome == "rejected":
        store.resolve_interrupt(RUN, record.id, decision="reject", now=1002.0)
        stopped = UnitState.OPERATOR_REJECTED
    else:
        store.expire_interrupts(RUN, now=2000.0)
        stopped = UnitState.INTERRUPT_EXPIRED
    assert store.get_unit(RUN, "u0").state is stopped
    assert ready_wave(store, RUN, FLAT) == []
    assert ready_wave(store, RUN, FLAT, reclaim_at=9000.0) == []


def test_reclaim_does_not_offer_a_waiting_unit(tmp_path, lsk):
    store = _store(tmp_path)
    run_wave(store, RUN, _wave(store, ["u0", "u1", "u2"]), generate=_signalling("u1"), at=1001.0)
    wave = prepare_run(
        store,
        RUN,
        FLAT,
        [WorkUnit(id=u) for u in ("u0", "u1", "u2")],
        at=9000.0,
        reclaim_at=9000.0,
    )
    assert [u.unit_id for u in wave] == []
    assert store.get_unit(RUN, "u1").state is UnitState.WAITING
    assert store.open_interrupt(RUN, "u1") is not None
    assert _kinds(store, "u1") == ["claim", "interrupt_requested"]


def test_module_docstring_documents_the_signal_and_the_drain_rule():
    import content_pipeline.execution.drivers.inline as inline

    for phrase in ("InterruptRequested", "waiting_units", "parse_fn", "request_interrupt"):
        assert phrase in inline.__doc__, phrase
