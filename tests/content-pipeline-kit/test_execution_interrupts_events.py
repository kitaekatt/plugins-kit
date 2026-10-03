"""Interrupt rows in the execution-event projection.

``project_run`` maps the four interrupt attempt row kinds to ``interrupt``
events under schema v2 and leaves every other event on v1. A ``terminal``
event follows a rejection or a lapse only under the ``stop`` policy. The
projection reads the store and calls ``bootstrap_lib.execution_event`` only;
it never calls the interrupt contract. Expected values are literals.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from content_pipeline.execution import events as ev
from content_pipeline.execution import interrupts
from content_pipeline.execution.events import ExecutionEventSupportError, project_run
from content_pipeline.execution.interrupts import InterruptSupportError
from content_pipeline.execution.model import AttemptKind, InterruptRequest, UsageRecord
from content_pipeline.execution.store import ExecutionStore

from bootstrap_lib import execution_event as real_ee

RUN = "r1"
PLUGIN = "content-pipeline-kit"
V1 = "plugins-kit.execution-event/v1"
V2 = "plugins-kit.execution-event/v2"
CONTRACT_MODULE = "bootstrap_lib.interrupt_contract"

_SHARED_LIB = Path(__file__).resolve().parents[2] / "plugins" / "llm-scripting-kit" / "lib"

# Strings that must never reach an event.
SCHEMA_SENTINEL = "SENTINEL-SCHEMA-DESCRIPTION"
PAYLOAD_SENTINEL = "SENTINEL-REQUEST-PAYLOAD"
ANSWER_SENTINEL = "SENTINEL-ANSWER"


def _lsk_names():
    return {n for n in sys.modules if n == "llm_scripting_kit" or n.startswith("llm_scripting_kit.")}


@pytest.fixture(autouse=True)
def lsk(monkeypatch):
    """Link llm-scripting-kit's validator for one test; unload it afterwards."""
    before = _lsk_names()
    monkeypatch.syspath_prepend(str(_SHARED_LIB))
    yield
    for name in _lsk_names() - before:
        del sys.modules[name]


def _store(tmp_path, units=("u0", "u1")) -> ExecutionStore:
    store = ExecutionStore(tmp_path / "run.db")
    store.create_run(
        RUN, driver="inline", backend="mock", model="m1", adapter_version="7", created_at=1000.0
    )
    store.register_units(RUN, list(units), at=1000.0)
    return store


def _request(**overrides) -> InterruptRequest:
    fields = {
        "kind": "approval",
        "request_schema": {"type": "object", "description": SCHEMA_SENTINEL},
        "payload": {"question": PAYLOAD_SENTINEL},
    }
    fields.update(overrides)
    return InterruptRequest(**fields)


def _wait(store, unit="u0", *, at=1001.0, request=None, usage=None, **policies):
    token = store.claim_unit(RUN, unit, "w", at=at).fencing_token
    return store.request_interrupt(
        RUN,
        unit,
        token,
        request if request is not None else _request(),
        usage=usage,
        at=at + 1.0,
        **policies,
    )


def _row(store, kind: AttemptKind, unit="u0"):
    return [r for r in store.list_attempts(RUN, unit) if r.kind is kind][-1]


def _from_row(events, row):
    return [e for e in events if e["seq"] // 4 == row.id and e["seq"] != 0]


def _shape(event) -> tuple:
    return (event["seq"] % 4, event["schema"], event["event"], event["payload"])


# -- the four row kinds --------------------------------------------------------


def test_request_row_yields_result_then_interrupt_requested(tmp_path):
    store = _store(tmp_path)
    record = _wait(store, request=_request(expires_in_s=60), usage=UsageRecord(5, 6, None))
    row = _row(store, AttemptKind.INTERRUPT_REQUESTED)
    events = _from_row(project_run(store, RUN), row)
    assert [_shape(e) for e in events] == [
        (
            0,
            V1,
            "usage",
            {"input_tokens": 5, "output_tokens": 6, "cache_hit_tokens": None, "total_tokens": None},
        ),
        (1, V1, "result", {"status": "interrupt_requested"}),
        (
            2,
            V2,
            "interrupt",
            {
                "interrupt_id": "1",
                "kind": "approval",
                "phase": "requested",
                "expires_at": "1970-01-01T00:17:42Z",
            },
        ),
    ]
    assert record.expires_at == 1062.0
    for event in events:
        assert event["identity"] == {"run_id": RUN, "unit_id": "u0", "attempt_id": "1"}
        assert event["at"] == "1970-01-01T00:16:42Z"


def test_request_without_usage_or_expiry_yields_two_events(tmp_path):
    store = _store(tmp_path)
    _wait(store)
    row = _row(store, AttemptKind.INTERRUPT_REQUESTED)
    assert [_shape(e) for e in _from_row(project_run(store, RUN), row)] == [
        (1, V1, "result", {"status": "interrupt_requested"}),
        (2, V2, "interrupt", {"interrupt_id": "1", "kind": "approval", "phase": "requested"}),
    ]


@pytest.mark.parametrize("closing", ["resolved", "rejected", "expired"])
def test_resolution_rows_yield_their_events(tmp_path, closing):
    store = _store(tmp_path)
    record = _wait(store, request=_request(expires_in_s=60))
    if closing == "resolved":
        store.resolve_interrupt(RUN, record.id, decision="answer", input={"a": 1}, now=1003.0)
        expected = [(1, V2, "interrupt", {"interrupt_id": "1", "kind": "approval", "phase": "resolved"})]
    elif closing == "rejected":
        store.resolve_interrupt(RUN, record.id, decision="reject", reason="not now", now=1003.0)
        expected = [
            (1, V2, "interrupt", {"interrupt_id": "1", "kind": "approval", "phase": "rejected"}),
            (2, V1, "terminal", {"state": "operator_rejected", "reason": "not now"}),
        ]
    else:
        store.expire_interrupts(RUN, now=1062.0)
        expected = [
            (1, V2, "interrupt", {"interrupt_id": "1", "kind": "approval", "phase": "expired"}),
            (2, V1, "terminal", {"state": "interrupt_expired"}),
        ]
    row = _row(store, AttemptKind(f"interrupt_{closing}"))
    events = _from_row(project_run(store, RUN), row)
    assert [_shape(e) for e in events] == expected
    assert events[0]["identity"] == {"run_id": RUN, "unit_id": "u0", "attempt_id": "1"}
    if len(events) == 2:
        assert events[1]["identity"] == {"run_id": RUN, "unit_id": "u0"}


def test_rejection_without_a_reason_has_a_bare_terminal(tmp_path):
    store = _store(tmp_path)
    record = _wait(store)
    store.resolve_interrupt(RUN, record.id, decision="reject", now=1003.0)
    terminal = [e for e in project_run(store, RUN) if e["event"] == "terminal"]
    assert [e["payload"] for e in terminal] == [{"state": "operator_rejected"}]


def test_terminal_reason_is_cut_to_1000_characters(tmp_path):
    store = _store(tmp_path)
    record = _wait(store)
    store.resolve_interrupt(RUN, record.id, decision="reject", reason="r" * 1500, now=1003.0)
    terminal = [e for e in project_run(store, RUN) if e["event"] == "terminal"]
    assert terminal[0]["payload"] == {"state": "operator_rejected", "reason": "r" * 1000}


@pytest.mark.parametrize("outcome", ["rejected", "expired"])
def test_release_emits_no_terminal(tmp_path, outcome):
    store = _store(tmp_path)
    record = _wait(
        store, request=_request(expires_in_s=60), on_rejected="release", on_expired="release"
    )
    if outcome == "rejected":
        store.resolve_interrupt(RUN, record.id, decision="reject", reason="no", now=1003.0)
    else:
        store.expire_interrupts(RUN, now=1062.0)
    events = project_run(store, RUN)
    assert [e for e in events if e["event"] == "terminal"] == []
    row = _row(store, AttemptKind(f"interrupt_{outcome}"))
    assert [_shape(e) for e in _from_row(events, row)] == [
        (1, V2, "interrupt", {"interrupt_id": "1", "kind": "approval", "phase": outcome})
    ]
    # The released unit runs again; its next attempt is a new attempt id.
    token = store.claim_unit(RUN, "u0", "w", at=1100.0).fencing_token
    store.accept_unit(RUN, "u0", token, at=1101.0)
    events = project_run(store, RUN)
    assert [e["payload"] for e in events if e["event"] == "terminal"] == [{"state": "accepted"}]
    assert real_ee.validate_stream(events) == events


def test_projected_phases_are_execution_event_phases():
    assert ev._INTERRUPT_PHASES == {
        AttemptKind.INTERRUPT_REQUESTED: "requested",
        AttemptKind.INTERRUPT_RESOLVED: "resolved",
        AttemptKind.INTERRUPT_REJECTED: "rejected",
        AttemptKind.INTERRUPT_EXPIRED: "expired",
    }
    assert set(ev._INTERRUPT_PHASES.values()) == real_ee.INTERRUPT_PHASES
    assert ev.REQUIRED_SCHEMA_V2 == real_ee.SCHEMA_V2 == V2


# -- the projection's edges ------------------------------------------------------


def _full_lifecycle(tmp_path) -> ExecutionStore:
    """Answered, rejected (stop), expired (release) and still waiting."""
    store = _store(tmp_path, units=("u0", "u1", "u2", "u3"))
    answered = _wait(store, "u0", at=1001.0, usage=UsageRecord(1, 2, 3))
    store.resolve_interrupt(
        RUN, answered.id, decision="answer", input={"note": ANSWER_SENTINEL}, now=1003.0
    )
    token = store.claim_unit(RUN, "u0", "w", at=1004.0).fencing_token
    store.accept_unit(RUN, "u0", token, usage=UsageRecord(4, 5, None), at=1005.0)
    rejected = _wait(store, "u1", at=1006.0)
    store.resolve_interrupt(RUN, rejected.id, decision="reject", reason="not now", now=1008.0)
    _wait(store, "u2", at=1009.0, request=_request(expires_in_s=5), on_expired="release")
    store.expire_interrupts(RUN, now=1100.0)
    _wait(store, "u3", at=1101.0, request=_request(expires_in_s=600))
    return store


def test_projection_needs_no_interrupt_contract(tmp_path, monkeypatch):
    store = _full_lifecycle(tmp_path)
    expected = project_run(store, RUN)
    # The contract and the validator are gone; the projection is unaffected.
    blocked = (
        CONTRACT_MODULE,
        "llm_scripting_kit",
        "llm_scripting_kit.completion",
        "llm_scripting_kit.completion.json_schema",
    )
    saved = {name: sys.modules[name] for name in blocked}
    with monkeypatch.context() as patch:
        for name in blocked:
            patch.setitem(sys.modules, name, None)
        with pytest.raises(InterruptSupportError):
            interrupts.support()
        assert project_run(store, RUN) == expected
    # Later consumers must see the exact shared modules collected earlier,
    # rather than a second copy with different class and exception identities.
    assert all(sys.modules[name] is module for name, module in saved.items())
    assert len([e for e in expected if e["event"] == "interrupt"]) == 7


def test_fail_then_wait_then_reject_emits_one_terminal(tmp_path):
    store = _store(tmp_path)
    first = store.claim_unit(RUN, "u0", "w", at=1001.0).fencing_token
    store.fail_unit(RUN, "u0", first, error="again", at=1002.0)
    record = _wait(store, "u0", at=1003.0)
    store.resolve_interrupt(RUN, record.id, decision="reject", now=1005.0)
    events = project_run(store, RUN)
    terminal = [e for e in events if e["event"] == "terminal"]
    assert [e["payload"] for e in terminal] == [{"state": "operator_rejected"}]
    assert terminal[0]["seq"] // 4 == _row(store, AttemptKind.INTERRUPT_REJECTED).id
    assert real_ee.validate_stream(events) == events


def test_interrupt_events_carry_the_owning_token(tmp_path):
    store = _store(tmp_path)
    # The second claim (token 2) makes the request.
    first = store.claim_unit(RUN, "u0", "w", at=1001.0).fencing_token
    store.fail_unit(RUN, "u0", first, error="again", at=1002.0)
    record = _wait(store, "u0", at=1003.0)
    store.resolve_interrupt(RUN, record.id, decision="answer", input={}, now=1005.0)
    # The claim after the answer is token 3; the interrupt stays with token 2.
    assert store.claim_unit(RUN, "u0", "w", at=1006.0).fencing_token == 3
    found = [e for e in project_run(store, RUN) if e["event"] == "interrupt"]
    assert [(e["payload"]["phase"], e["identity"]["attempt_id"]) for e in found] == [
        ("requested", "2"),
        ("resolved", "2"),
    ]
    assert {e["payload"]["interrupt_id"] for e in found} == {"1"}


def test_only_interrupt_events_are_v2(tmp_path):
    events = project_run(_full_lifecycle(tmp_path), RUN)
    by_schema = {}
    for event in events:
        by_schema.setdefault(event["schema"], set()).add(event["event"])
    assert by_schema == {
        V2: {"interrupt"},
        V1: {
            f"{PLUGIN}:run-created",
            "call-started",
            "usage",
            "result",
            "terminal",
        },
    }


def _v1_only_module():
    """The event module as bootstrap 0.135.0 shipped it: schema v1, and a
    ``make_event`` with no ``schema`` keyword."""

    def make_event(
        *,
        seq,
        run_id,
        event,
        plugin,
        at=None,
        unit_id=None,
        attempt_id=None,
        adapter=None,
        model=None,
        payload=None,
    ):
        return real_ee.make_event(
            seq=seq,
            run_id=run_id,
            event=event,
            plugin=plugin,
            at=at,
            unit_id=unit_id,
            attempt_id=attempt_id,
            adapter=adapter,
            model=model,
            payload=payload,
        )

    return types.SimpleNamespace(
        SUPPORTED_SCHEMAS=frozenset({V1}),
        make_event=make_event,
        utc_timestamp=real_ee.utc_timestamp,
        usage_payload=real_ee.usage_payload,
        validate_stream=real_ee.validate_stream,
    )


def _use_event_module(monkeypatch, module):
    def fake(name):
        return types.SimpleNamespace() if name == "bootstrap_lib" else module

    monkeypatch.setattr(ev, "_import_module", fake)


def test_plain_run_projects_under_a_v1_only_module(tmp_path, monkeypatch):
    store = _store(tmp_path)
    token = store.claim_unit(RUN, "u0", "w", at=1001.0).fencing_token
    store.accept_unit(RUN, "u0", token, usage=UsageRecord(1, 2, None), at=1002.0)
    expected = project_run(store, RUN)
    _use_event_module(monkeypatch, _v1_only_module())
    assert project_run(store, RUN) == expected
    assert [e["event"] for e in expected] == [
        f"{PLUGIN}:run-created",
        "call-started",
        "usage",
        "result",
        "terminal",
    ]


def test_interrupt_run_refuses_a_v1_only_module(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _wait(store)
    _use_event_module(monkeypatch, _v1_only_module())
    with pytest.raises(ExecutionEventSupportError) as info:
        project_run(store, RUN)
    assert str(info.value) == (
        "this run has interrupt rows, which content-pipeline-kit projects as interrupt "
        "events under plugins-kit.execution-event/v2; that needs bootstrap 0.136.0 or "
        "newer: the installed module does not support plugins-kit.execution-event/v2. "
        "Run `claude plugin update bootstrap@plugins-kit` and restart the session."
    )
    # Not the v1 probe's message, which names 0.135.0.
    assert ev.EXECUTION_EVENT_BOOTSTRAP == "0.135.0"
    assert "0.135.0" not in str(info.value)


def test_interrupt_run_refuses_a_make_event_without_the_schema_keyword(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _wait(store)
    module = _v1_only_module()
    module.SUPPORTED_SCHEMAS = frozenset({V1, V2})
    _use_event_module(monkeypatch, module)
    with pytest.raises(ExecutionEventSupportError, match="does not accept a schema keyword"):
        project_run(store, RUN)


def test_no_event_carries_request_or_answer_content(tmp_path):
    store = _full_lifecycle(tmp_path)
    # The sentinels are in the store, so the test can fail.
    records = store.list_interrupts(RUN)
    assert SCHEMA_SENTINEL in json.dumps(records[0].request_schema)
    assert PAYLOAD_SENTINEL in json.dumps(records[0].payload)
    assert ANSWER_SENTINEL in json.dumps(records[0].resolution.input)
    text = json.dumps(project_run(store, RUN))
    for sentinel in (SCHEMA_SENTINEL, PAYLOAD_SENTINEL, ANSWER_SENTINEL):
        assert sentinel not in text
    for event in project_run(store, RUN):
        if event["event"] == "interrupt":
            assert set(event["payload"]) <= {"interrupt_id", "kind", "phase", "expires_at"}


def test_waiting_run_events_validate_as_stream(tmp_path):
    store = _full_lifecycle(tmp_path)
    events = project_run(store, RUN)
    assert real_ee.validate_stream(events) == events
    assert [
        (e["identity"]["unit_id"], e["payload"]["interrupt_id"], e["payload"]["phase"])
        for e in events
        if e["event"] == "interrupt"
    ] == [
        ("u0", "1", "requested"),
        ("u0", "1", "resolved"),
        ("u1", "2", "requested"),
        ("u1", "2", "rejected"),
        ("u2", "3", "requested"),
        ("u2", "3", "expired"),
        ("u3", "4", "requested"),
    ]
    assert [
        (e["identity"]["unit_id"], e["payload"])
        for e in events
        if e["event"] == "terminal"
    ] == [
        ("u0", {"state": "accepted"}),
        ("u1", {"state": "operator_rejected", "reason": "not now"}),
    ]
    assert [
        (e["identity"]["unit_id"], e["identity"]["attempt_id"], e["payload"]["status"])
        for e in events
        if e["event"] == "result"
    ] == [
        ("u0", "1", "interrupt_requested"),
        ("u0", "2", "accepted"),
        ("u1", "1", "interrupt_requested"),
        ("u2", "1", "interrupt_requested"),
        ("u3", "1", "interrupt_requested"),
    ]


def test_seq_is_stable_across_reprojection(tmp_path):
    store = _store(tmp_path, units=("u0", "u1"))
    # A second run shares the attempts id sequence, so this run's row ids have
    # gaps that position-based numbering would close.
    store.create_run("r2", driver="inline", backend="mock", model="m1", adapter_version="7")
    store.register_units("r2", ["x"])
    other = store.claim_unit("r2", "x", "w", at=1000.5).fencing_token
    record = _wait(store, "u0", request=_request(expires_in_s=60))
    before = project_run(store, RUN)
    # More rows land: the other run, another unit, and the same unit.
    store.renew_lease("r2", "x", other, at=1009.0)
    token = store.claim_unit(RUN, "u1", "w", at=1010.0).fencing_token
    store.request_interrupt("r2", "x", other, _request(), at=1010.5)
    store.accept_unit(RUN, "u1", token, at=1011.0)
    store.resolve_interrupt(RUN, record.id, decision="reject", reason="no", now=1012.0)
    after = project_run(store, RUN)
    assert after[: len(before)] == before
    assert len(after) > len(before)
    seqs = [e["seq"] for e in after]
    assert len(set(seqs)) == len(seqs)
    assert {s // 4 for s in seqs if s} == {r.id for r in store.list_attempts(RUN)}
    assert project_run(store, RUN) == after
