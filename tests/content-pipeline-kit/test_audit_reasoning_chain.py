"""Tests for content_pipeline.audit.reasoning_chain.

Pins the per-item append-only sidecar: an in-memory recorder appends events,
record_submission captures a validate-until-valid trail by DUCK-TYPING the
result (no llm import), a null recorder is a safe no-op, and a sidecar recorder
persists append-only through injected I/O callables.
"""

from dataclasses import dataclass, field

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
