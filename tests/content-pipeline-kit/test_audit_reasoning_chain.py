"""Tests for content_pipeline.audit.reasoning_chain.

Pins the per-item append-only sidecar: an in-memory recorder appends events,
record_submission captures a validate-until-valid trail by DUCK-TYPING the
result (no llm import), a null recorder is a safe no-op, and a sidecar recorder
persists append-only through injected I/O callables.
"""

import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

import content_pipeline.audit.reasoning_chain as reasoning_chain
from content_pipeline.audit.reasoning_chain import (
    InMemoryRecorder,
    NullRecorder,
    SidecarRecorder,
    build_event,
    record_chain,
    record_submission,
)


def test_in_memory_recorder_appends():
    rec = InMemoryRecorder()
    rec.record("e1", {"stage": "grade"})
    rec.record("e1", {"stage": "fill"})
    chain = rec.chain("e1")
    assert [ev["stage"] for ev in chain] == ["grade", "fill"]


def test_record_chain_records_each_step():
    rec = InMemoryRecorder()
    record_chain(rec, "e1", [{"attempt": 1}, {"attempt": 2}])
    assert [ev["attempt"] for ev in rec.chain("e1")] == [1, 2]


def test_build_event_drops_absent_fields():
    ev = build_event(stage="fill", final={"pick": "v"})
    assert ev["stage"] == "fill"
    assert ev["final"] == {"pick": "v"}
    assert "attempt" not in ev and "rejections" not in ev


# A duck-typed submission result: no llm import needed.
@dataclass
class _Resp:
    text: str


@dataclass
class _Submit:
    payload: object
    responses: list = field(default_factory=list)
    rejections: list = field(default_factory=list)
    attempts: int = 0


@dataclass
class _Rej:
    kind: str


def test_record_submission_captures_attempts_and_final():
    rec = InMemoryRecorder()
    submit = _Submit(
        payload={"answer": 42},
        responses=[_Resp("try1"), _Resp("try2")],
        rejections=[_Rej("parse_error")],
        attempts=2,
    )
    record_submission(rec, "e1", submit, inputs={"prompt": "p"})
    chain = rec.chain("e1")
    # One event per attempt + a final event.
    attempt_events = [ev for ev in chain if "attempt" in ev]
    assert [ev["response_text"] for ev in attempt_events] == ["try1", "try2"]
    assert attempt_events[0]["inputs"] == {"prompt": "p"}  # inputs on first only
    final = [ev for ev in chain if "final" in ev][0]
    assert final["final"] == {"answer": 42}
    assert final["rejections"] == ["parse_error"]


def test_null_recorder_is_noop():
    rec = NullRecorder()
    rec.record("e1", {"stage": "x"})
    assert rec.chain("e1") == []


def test_at_stamp_orders_correctly_across_a_simulated_process_restart(monkeypatch):
    """`at` must be a wall-clock stamp: SidecarRecorder persists a chain
    across process restarts, and a per-process clock (time.monotonic, whose
    epoch resets on each process start) would sort a chain wrong the moment a
    later event is recorded by a fresh process with a smaller monotonic
    reading than an earlier one recorded by a longer-lived process."""
    # Process A: its monotonic clock has been running a while; wall time T0.
    monkeypatch.setattr(reasoning_chain.time, "monotonic", lambda: 500000.0)
    monkeypatch.setattr(reasoning_chain.time, "time", lambda: 1_700_000_000.0)
    first = build_event(stage="a")

    # Simulated restart: a FRESH process's monotonic clock resets near zero
    # while wall-clock time keeps advancing.
    monkeypatch.setattr(reasoning_chain.time, "monotonic", lambda: 3.0)
    monkeypatch.setattr(reasoning_chain.time, "time", lambda: 1_700_000_010.0)
    second = build_event(stage="b")

    assert first["at"] < second["at"]


def test_sidecar_recorder_appends_through_io(tmp_path):
    import json

    def load(entity_id):
        path = tmp_path / f"{entity_id}.json"
        return json.loads(path.read_text()) if path.exists() else []

    def store(entity_id, chain):
        (tmp_path / f"{entity_id}.json").write_text(json.dumps(chain))

    rec = SidecarRecorder(load=load, store=store)
    rec.record("e1", {"stage": "a"})
    rec.record("e1", {"stage": "b"})  # append-only, not overwrite
    assert [ev["stage"] for ev in rec.chain("e1")] == ["a", "b"]


# -- output-contract identity -------------------------------------------------

_SCHEMA = {
    "type": "object",
    "required": ["title"],
    "properties": {"title": {"type": "string"}},
    "additionalProperties": False,
}
_SHARED_LIB = Path(__file__).resolve().parents[2] / "plugins" / "llm-scripting-kit" / "lib"
_CONTRACT_KEYS = {"id", "schema_version", "schema_digest", "policy", "delivery", "disposition"}


@pytest.fixture
def lsk(monkeypatch):
    """The real ``llm_scripting_kit.completion``, unloaded again afterwards."""

    def names():
        return {n for n in sys.modules if n == "llm_scripting_kit" or n.startswith("llm_scripting_kit.")}

    before = names()
    monkeypatch.syspath_prepend(str(_SHARED_LIB))
    import llm_scripting_kit.completion as completion  # noqa: PLC0415

    yield completion
    for name in names() - before:
        del sys.modules[name]


def _report(delivery="native", disposition="valid"):
    return {
        "contract_id": "cpk.t",
        "schema_version": "1",
        "schema_digest": "d" * 8,
        "policy": "validated-result",
        "delivery": delivery,
        "disposition": disposition,
        "errors": [["/title", "type"]],
        "schema": _SCHEMA,
    }


@dataclass
class _ReportedResp:
    text: str
    output_contract: object = None


def test_record_submission_records_contract_identity_without_schema_body():
    rec = InMemoryRecorder()
    submit = _Submit(
        payload={"title": "x"},
        responses=[
            _ReportedResp("bad", _report(disposition="invalid")),
            _ReportedResp("ok", _report()),
        ],
        attempts=2,
    )
    record_submission(rec, "e1", submit)
    first, second, final = rec.chain("e1")
    assert set(first["contract"]) == _CONTRACT_KEYS
    assert first["contract"]["id"] == "cpk.t"
    assert first["contract"]["schema_digest"] == "d" * 8
    assert first["contract"]["disposition"] == "invalid"
    assert second["contract"]["disposition"] == "valid"
    assert final["contract"] == second["contract"]  # the final disposition
    for event in (first, second, final):
        assert "schema" not in event["contract"]
        assert "errors" not in event["contract"]
        assert "additionalProperties" not in str(event)


def test_record_submission_without_report_adds_no_contract_key():
    rec = InMemoryRecorder()
    record_submission(rec, "e1", _Submit(payload=1, responses=[_Resp("t")], attempts=1))
    assert all("contract" not in ev for ev in rec.chain("e1"))


def test_record_submission_records_unreported_delivery_on_no_report_path(lsk):
    """A backend with no seam report still records delivery=unreported: the
    value comes from the response submit_validated stored, not from
    EvaluationResult."""
    from content_pipeline.llm.backends import MockBackend
    from content_pipeline.llm.platform import submit_validated

    output_contract = lsk.OutputContract(
        id="cpk.t", policy=lsk.POLICY_VALIDATED_RESULT, schema=_SCHEMA
    )
    result = submit_validated(
        backend=MockBackend(responses=['{"title": "ok"}']),
        system="s",
        user="u",
        model="m",
        output_contract=output_contract,
    )
    rec = InMemoryRecorder()
    record_submission(rec, "e1", result)
    attempt = [ev for ev in rec.chain("e1") if "attempt" in ev][0]
    assert attempt["contract"]["delivery"] == "unreported"
    assert attempt["contract"]["disposition"] == "valid"
    assert attempt["contract"]["id"] == "cpk.t"
    assert attempt["contract"]["schema_digest"] == output_contract.identity()[2]
    assert "schema" not in attempt["contract"]
