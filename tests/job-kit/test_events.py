"""Tests for job-kit's execution events: the ledger's events table, the probe,
and the envelope mapping (run_id, job id as unit_id, attempt_no as attempt_id).
"""

from __future__ import annotations

import ast
import importlib
import inspect
import sqlite3
import sys
import time
import types
from pathlib import Path
from typing import Any, Optional

import pytest

from llm_scripting_kit.completion import BackendSelection, Capabilities, LLMResponse

import job_kit.events as events
from job_kit.model import (
    Acceptance,
    Attempt,
    AttemptError,
    Contract,
    Job,
    JobState,
    Prompt,
    Usage,
)
from job_kit.run import run_jobs
from job_kit.store import EventsNotRecordedError, JobStore, _MIGRATIONS

from bootstrap_lib import execution_event as real_module


LIB = Path(__file__).resolve().parents[2] / "plugins" / "job-kit" / "lib" / "job_kit"
ARMED_AT = "2026-09-01T00:00:01Z"
ENDED_AT = "2026-09-01T00:00:02Z"


def _job(directory: Path, job_id: str = "job", *, max_attempts: int = 2) -> Job:
    return Job(
        id=job_id,
        prompt=Prompt(user=f"run {job_id}"),
        models=("fake",),
        directory=directory,
        max_attempts=max_attempts,
        contract=Contract(command=(sys.executable, "-c", "pass"), directory=directory),
    )


def _attempt(
    run_id: str,
    job_id: str,
    attempt_no: int,
    *,
    usage: Optional[Usage] = None,
    accepted: Optional[bool] = True,
    error: Optional[AttemptError] = None,
) -> Attempt:
    acceptance = None
    if accepted is not None:
        acceptance = Acceptance(
            command=("true",),
            directory=Path.cwd(),
            exit_code=0 if accepted else 1,
            stdout="",
            stderr="",
            wall_ms=1,
            accepted=accepted,
        )
    return Attempt(
        run_id=run_id,
        job_id=job_id,
        attempt_no=attempt_no,
        endpoint="fake-endpoint",
        backend="fake-backend",
        model="fake-model",
        status="completed" if error is None else "error",
        started_at=ARMED_AT,
        ended_at=ENDED_AT,
        usage=usage,
        error=error,
        acceptance=acceptance,
    )


def _reserve(store: JobStore, run_id: str, job_id: str) -> int:
    reservation = store.reserve_attempt(
        run_id,
        job_id,
        endpoint="fake-endpoint",
        backend="fake-backend",
        model="fake-model",
        reserved_at="2026-09-01T00:00:00Z",
    )
    return reservation.attempt_no


def _drive(
    store: JobStore,
    run_id: str,
    job_id: str,
    *,
    terminal: Optional[JobState] = None,
    usage: Optional[Usage] = Usage(input_tokens=3, output_tokens=5),
    accepted: Optional[bool] = True,
) -> Attempt:
    """Take one attempt through reserve, arm and append."""
    attempt_no = _reserve(store, run_id, job_id)
    store.arm_reservation(run_id, job_id, attempt_no, invoke_armed_at=ARMED_AT)
    return store.append_attempt(
        _attempt(run_id, job_id, attempt_no, usage=usage, accepted=accepted),
        terminal_state=terminal,
    )


def _of(stream: tuple[dict, ...], event: Optional[str] = None, **identity: str) -> list[dict]:
    """Select events by name and by identity fields."""
    return [
        item
        for item in stream
        if (event is None or item["event"] == event)
        and all(item["identity"].get(key) == value for key, value in identity.items())
    ]


def _names(stream: list[dict]) -> list[str]:
    return [event["event"] for event in stream]


def _event_count(store: JobStore) -> int:
    with sqlite3.connect(str(store.db_path)) as connection:
        return int(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0])


# --------------------------------------------------------------------------
# Identity, lifecycle and atomicity
# --------------------------------------------------------------------------


def test_identity_maps_run_job_attempt_no(tmp_path: Path) -> None:
    """run_id is the run, unit_id the job id, attempt_id str(attempt_no)."""
    store = JobStore(tmp_path / "ledger.sqlite3")
    store.create_run("run-a", [_job(tmp_path, "job-x"), _job(tmp_path, "job-y")])
    _drive(store, "run-a", "job-x")  # attempt 1 of job-x; the job stays pending
    _drive(store, "run-a", "job-x", terminal=JobState.ACCEPTED)  # attempt 2
    _drive(store, "run-a", "job-y", terminal=JobState.ACCEPTED)  # attempt 1, row id 3

    stream = store.list_events("run-a")
    attempts = store.list_attempts("run-a")
    job_y = [attempt for attempt in attempts if attempt.job_id == "job-y"][0]
    assert job_y.id != job_y.attempt_no  # the row id and the attempt number differ

    assert {event["identity"]["run_id"] for event in stream} == {"run-a"}
    assert {event["source"]["plugin"] for event in stream} == {"job-kit"}
    y_attempt_events = [
        event for event in _of(stream, unit_id="job-y") if "attempt_id" in event["identity"]
    ]
    assert _names(y_attempt_events) == [
        "dispatch-selected",
        "call-started",
        "usage",
        "result",
    ]
    assert {event["identity"]["attempt_id"] for event in y_attempt_events} == {"1"}
    x_results = [event for event in _of(stream, unit_id="job-x") if event["event"] == "result"]
    assert [event["identity"]["attempt_id"] for event in x_results] == ["1", "2"]
    for event in y_attempt_events:
        assert event["source"] == {
            "plugin": "job-kit",
            "adapter": "fake-backend",
            "model": "fake-model",
        }
    run_scoped = [event for event in stream if "unit_id" not in event["identity"]]
    assert _names(run_scoped) == ["job-kit:run-created"]


def test_every_attempt_row_has_exactly_one_result_event(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "ledger.sqlite3")
    store.create_run("run", [_job(tmp_path, "a", max_attempts=3), _job(tmp_path, "b")])
    _drive(store, "run", "a", accepted=False)
    _drive(store, "run", "b", terminal=JobState.ACCEPTED)
    _drive(store, "run", "a", accepted=False)
    _drive(store, "run", "a", terminal=JobState.REJECTED, accepted=False)

    stream = store.list_events("run")
    attempts = store.list_attempts("run")
    assert len(attempts) == 4
    for attempt in attempts:
        results = [
            event
            for event in _of(
                stream, unit_id=attempt.job_id, attempt_id=str(attempt.attempt_no)
            )
            if event["event"] == "result"
        ]
        assert len(results) == 1, attempt
        assert results[0]["payload"]["status"] == attempt.status
        assert results[0]["payload"]["acceptance"] == (
            "accepted" if attempt.acceptance.accepted else "rejected"
        )


def test_attempt_lifecycle_order(tmp_path: Path) -> None:
    """Each store verb records exactly its own lifecycle stage."""
    store = JobStore(tmp_path / "ledger.sqlite3")
    store.create_run("run", [_job(tmp_path)])

    def attempt_events() -> list[str]:
        return _names(_of(store.list_events("run"), unit_id="job", attempt_id="1"))

    attempt_no = _reserve(store, "run", "job")
    assert attempt_events() == ["dispatch-selected"]
    store.arm_reservation("run", "job", attempt_no, invoke_armed_at=ARMED_AT)
    assert attempt_events() == ["dispatch-selected", "call-started"]
    store.append_attempt(
        _attempt("run", "job", attempt_no, usage=Usage(input_tokens=1, output_tokens=2)),
        terminal_state=JobState.ACCEPTED,
    )
    assert attempt_events() == ["dispatch-selected", "call-started", "usage", "result"]
    selected = _of(store.list_events("run"), event="dispatch-selected")
    assert selected[0]["payload"] == {"endpoint": "fake-endpoint", "budget_no": 1}
    assert selected[0]["at"] == "2026-09-01T00:00:00Z"


def test_event_and_fact_commit_atomically(tmp_path: Path, monkeypatch: Any) -> None:
    """An event that cannot be recorded rolls its fact back with it."""
    store = JobStore(tmp_path / "ledger.sqlite3")
    store.create_run("run", [_job(tmp_path)])
    attempt_no = _reserve(store, "run", "job")
    store.arm_reservation("run", "job", attempt_no, invoke_armed_at=ARMED_AT)
    before = _event_count(store)
    real_build = events.build_event

    def failing_build(**kwargs: Any) -> dict:
        if kwargs["event"] == "result":
            raise RuntimeError("event insert failed")
        return real_build(**kwargs)

    monkeypatch.setattr(events, "build_event", failing_build)
    with pytest.raises(RuntimeError, match="event insert failed"):
        store.append_attempt(
            _attempt("run", "job", attempt_no, usage=Usage(input_tokens=1, output_tokens=1)),
            terminal_state=JobState.ACCEPTED,
        )
    monkeypatch.setattr(events, "build_event", real_build)

    assert store.list_attempts("run") == []
    assert store.get_job("run", "job").state is JobState.RUNNING
    assert store.get_reservation("run", "job", attempt_no).disposition is None
    assert _event_count(store) == before  # the usage event rolled back too


def test_facts_in_one_transaction_get_increasing_seq_in_insertion_order(
    tmp_path: Path,
) -> None:
    """Premise pin: AUTOINCREMENT seq follows insertion order inside one
    transaction, so one verb's several events keep the order it wrote them."""
    store = JobStore(tmp_path / "ledger.sqlite3")
    store.create_run("run", [_job(tmp_path)])
    names = ["job-kit:c-third", "job-kit:a-first", "job-kit:b-second"]
    with store._writer() as conn:
        seqs = [
            store._record_event(
                conn, run_id="run", event=name, at=ENDED_AT, payload={"n": index}
            )
            for index, name in enumerate(names)
        ]
    assert seqs == sorted(seqs) and len(set(seqs)) == 3
    rendered = [event for event in store.list_events("run") if event["event"] in names]
    assert [event["event"] for event in rendered] == names
    assert [event["seq"] for event in rendered] == seqs


# --------------------------------------------------------------------------
# Terminals and recovery
# --------------------------------------------------------------------------


def test_terminal_emitted_once_when_append_attempt_transitions(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "ledger.sqlite3")
    store.create_run("run", [_job(tmp_path)])
    _drive(store, "run", "job", terminal=JobState.ACCEPTED)

    terminals = _of(store.list_events("run"), unit_id="job", event="terminal")
    assert len(terminals) == 1
    assert terminals[0]["payload"] == {"state": "accepted"}
    assert "attempt_id" not in terminals[0]["identity"]


def test_unroutable_job_emits_single_terminal(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "ledger.sqlite3")
    store.create_run("run", [_job(tmp_path)])
    store.mark_unroutable("run", "job", "no endpoint fits", at=21.0)

    stream = store.list_events("run")
    terminals = _of(stream, unit_id="job", event="terminal")
    assert len(terminals) == 1
    assert terminals[0]["payload"] == {"state": "unroutable", "reason": "no endpoint fits"}
    assert terminals[0]["at"] == "1970-01-01T00:00:21Z"


@pytest.mark.parametrize("verb, state", [("mark_halted", "halted"), ("mark_failed", "failed")])
def test_mark_verbs_emit_one_terminal_with_reason(
    tmp_path: Path, verb: str, state: str
) -> None:
    store = JobStore(tmp_path / "ledger.sqlite3")
    store.create_run("run", [_job(tmp_path)])
    _drive(store, "run", "job", accepted=False)
    getattr(store, verb)("run", "job", "the reason")

    terminals = _of(store.list_events("run"), unit_id="job", event="terminal")
    assert [event["payload"] for event in terminals] == [
        {"state": state, "reason": "the reason"}
    ]


def test_mark_running_records_no_event(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "ledger.sqlite3")
    store.create_run("run", [_job(tmp_path)])
    before = _event_count(store)
    store.mark_running("run", "job")
    assert _event_count(store) == before


def test_recovered_reservation_emits_lost_result(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "ledger.sqlite3")
    store.create_run(
        "run", [_job(tmp_path, "armed", max_attempts=1), _job(tmp_path, "unarmed")]
    )
    armed_no = _reserve(store, "run", "armed")
    store.arm_reservation("run", "armed", armed_no, invoke_armed_at=ARMED_AT)
    _reserve(store, "run", "unarmed")
    store.recover_reservations("run")  # the default ``at`` is an epoch string

    stream = store.list_events("run")
    armed = _of(stream, unit_id="armed")
    assert _names(armed)[-2:] == ["result", "terminal"]
    assert armed[-2]["payload"] == {
        "status": "lost",
        "reason": "process lost after seam invocation was armed",
    }
    assert armed[-1]["payload"]["state"] == "failed"  # its one attempt is spent
    unarmed = _of(stream, unit_id="unarmed")
    assert _names(unarmed) == ["dispatch-selected", "result"]  # back to pending
    assert unarmed[-1]["payload"]["status"] == "lost"
    assert unarmed[-1]["at"].endswith("Z")


def test_pre_invoke_failure_emits_not_invoked_result_and_terminal(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "ledger.sqlite3")
    store.create_run("run", [_job(tmp_path)])
    attempt_no = _reserve(store, "run", "job")
    store.resolve_reservation_before_invoke(
        "run", "job", attempt_no, reason="workspace failed", at="2026-09-01T00:00:05Z"
    )

    job_events = _of(store.list_events("run"), unit_id="job")
    assert _names(job_events) == ["dispatch-selected", "result", "terminal"]
    assert job_events[1]["payload"] == {"status": "not-invoked", "reason": "workspace failed"}
    assert job_events[2]["payload"] == {"state": "failed", "reason": "workspace failed"}
    assert job_events[1]["at"] == "2026-09-01T00:00:05Z"


# --------------------------------------------------------------------------
# Ordering across processes and workers
# --------------------------------------------------------------------------


def test_resume_continues_seq(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.sqlite3"
    first = JobStore(db_path)
    first.create_run("run", [_job(tmp_path, "a"), _job(tmp_path, "b")])
    _drive(first, "run", "a", terminal=JobState.ACCEPTED)
    before = max(event["seq"] for event in first.list_events("run"))

    reopened = JobStore(db_path, create=False)
    _drive(reopened, "run", "b", terminal=JobState.ACCEPTED)
    after = [event for event in reopened.list_events("run") if event["seq"] > before]

    assert after and all(event["identity"]["unit_id"] == "b" for event in after)
    assert len(after) == len(_of(reopened.list_events("run"), unit_id="b"))


class _SlowBackend:
    name = "fake"

    def complete(self, system: str, user: str, *, model: str, options: Any = None) -> LLMResponse:
        time.sleep(0.02)
        return LLMResponse(
            text="answer", model=model, input_tokens=2, output_tokens=3
        )

    def classify_halt(self, exc: BaseException) -> None:
        return None


def test_parallel_run_seq_is_unique_and_recorded_ordered(tmp_path: Path) -> None:
    """At max_parallel 3, seq is unique run-wide and follows commit order."""
    store = JobStore(tmp_path / "ledger.sqlite3")
    backend = _SlowBackend()
    jobs = [_job(tmp_path, f"job-{index}") for index in range(6)]
    snapshot = run_jobs(
        jobs,
        store,
        run_id="parallel",
        max_parallel=3,
        workspace_root=tmp_path / "ws",
        capabilities_provider=lambda: {"fake": Capabilities(adapter="fake")},
        backend_factory=lambda endpoint, **_: BackendSelection(
            endpoint, "fake", backend, "fake-model"
        ),
    )
    assert {job.state for job in snapshot.jobs} == {JobState.ACCEPTED}

    stream = store.list_events("parallel")  # validates as one stream
    seqs = [event["seq"] for event in stream]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    # Results are recorded in the same transaction as their attempt rows, so
    # their seq order is the attempts' commit order.
    results = [event for event in stream if event["event"] == "result"]
    attempts = store.list_attempts("parallel")
    assert [(event["identity"]["unit_id"], event["identity"]["attempt_id"]) for event in results] == [
        (attempt.job_id, str(attempt.attempt_no)) for attempt in attempts
    ]
    for job in jobs:
        names = _names(_of(stream, unit_id=job.id))
        assert names == ["dispatch-selected", "call-started", "usage", "result", "terminal"]


# --------------------------------------------------------------------------
# Validation, usage and timestamps
# --------------------------------------------------------------------------


def test_store_refuses_nonconforming_event(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "ledger.sqlite3")
    store.create_run("run", [_job(tmp_path)])
    before = _event_count(store)
    with pytest.raises(real_module.EventError):
        with store._writer() as conn:
            store._record_event(
                conn, run_id="run", event="contract", at=ENDED_AT, job_id="job"
            )
    with pytest.raises(real_module.EventError):
        with store._writer() as conn:
            store._record_event(
                conn,
                run_id="run",
                event="result",
                at=ENDED_AT,
                job_id="job",
                attempt_no=1,
                payload={"status": float("nan")},
            )
    assert _event_count(store) == before


def test_create_run_refuses_a_job_id_the_envelope_cannot_carry(tmp_path: Path) -> None:
    """A job id over the envelope's identity bound is refused before any write."""
    store = JobStore(tmp_path / "ledger.sqlite3")
    with pytest.raises(real_module.EventError, match="unit_id"):
        store.create_run("run", [_job(tmp_path, "ok"), _job(tmp_path, "j" * 201)])
    assert store.get_run("run") is None


def test_codex_total_only_usage_is_not_zero_split(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "ledger.sqlite3")
    store.create_run("run", [_job(tmp_path)])
    _drive(
        store,
        "run",
        "job",
        terminal=JobState.ACCEPTED,
        usage=Usage(input_tokens=0, output_tokens=0, cache_hit_tokens=0, total_tokens=1234),
    )

    usage = _of(store.list_events("run"), event="usage")
    assert [event["payload"] for event in usage] == [
        {
            "input_tokens": None,
            "output_tokens": None,
            "cache_hit_tokens": None,
            "total_tokens": 1234,
        }
    ]


def test_attempt_without_usage_records_no_usage_event(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "ledger.sqlite3")
    store.create_run("run", [_job(tmp_path)])
    _drive(store, "run", "job", terminal=JobState.ACCEPTED, usage=None)
    assert _of(store.list_events("run"), event="usage") == []


def test_event_at_uses_fact_timestamps_and_falls_back_to_record_time() -> None:
    assert events.event_at("2026-09-01T00:00:00Z") == "2026-09-01T00:00:00Z"
    assert events.event_at("2026-09-01T00:00:00.250Z") == "2026-09-01T00:00:00.250Z"
    assert events.event_at(21.0) == "1970-01-01T00:00:21Z"
    assert events.event_at("21.5") == "1970-01-01T00:00:21.500000Z"
    assert events.event_at("not-a-time", 22.0) == "1970-01-01T00:00:22Z"
    before = real_module.utc_timestamp(time.time() - 1)
    fallback = events.event_at("t0", None)
    assert fallback.endswith("Z") and fallback >= before


# --------------------------------------------------------------------------
# Pre-migration runs
# --------------------------------------------------------------------------


def _events_step() -> int:
    return next(
        index
        for index, step in enumerate(_MIGRATIONS)
        if any("CREATE TABLE events" in statement for statement in step)
    )


def _pre_event_ledger(db_path: Path) -> None:
    """Build a ledger at the schema just before the events step, with one run."""
    with sqlite3.connect(str(db_path)) as connection:
        for index in range(_events_step()):
            for statement in _MIGRATIONS[index]:
                connection.execute(statement)
            if index:
                connection.execute("UPDATE schema_version SET version = ?", (index + 1,))
        connection.execute(
            "INSERT INTO runs(id, created_at, max_parallel) VALUES ('old-run', 1.0, 1)"
        )


def test_pre_event_log_run_refuses_export(tmp_path: Path) -> None:
    db_path = tmp_path / "old.sqlite3"
    _pre_event_ledger(db_path)
    store = JobStore(db_path, create=False)

    with pytest.raises(EventsNotRecordedError, match="old-run"):
        store.list_events("old-run")

    store.create_run("new-run", [_job(tmp_path)])
    assert _names(list(store.list_events("new-run"))) == ["job-kit:run-created"]


# --------------------------------------------------------------------------
# The probe
# --------------------------------------------------------------------------

_DELETE = object()


def _install_fake_module(monkeypatch: Any, **overrides: Any) -> None:
    fake = types.ModuleType("bootstrap_lib.execution_event")
    for name in (
        "SUPPORTED_SCHEMAS",
        "make_event",
        "utc_timestamp",
        "usage_payload",
        "validate_stream",
        "JsonlSink",
    ):
        setattr(fake, name, getattr(real_module, name))
    for name, value in overrides.items():
        if value is _DELETE:
            delattr(fake, name)
        else:
            setattr(fake, name, value)
    package = types.ModuleType("bootstrap_lib")
    package.execution_event = fake  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "bootstrap_lib", package)
    monkeypatch.setitem(sys.modules, "bootstrap_lib.execution_event", fake)


def test_probe_accepts_the_real_module() -> None:
    assert events._execution_event() is importlib.import_module(
        "bootstrap_lib.execution_event"
    )


def test_probe_absent_bootstrap_lib_message(monkeypatch: Any) -> None:
    monkeypatch.setitem(sys.modules, "bootstrap_lib", None)
    with pytest.raises(events.ExecutionEventSupportError) as excinfo:
        events._execution_event()
    message = str(excinfo.value)
    assert "claude plugin install bootstrap@plugins-kit" in message
    assert "update" not in message


def test_probe_too_old_bootstrap_lib_message(monkeypatch: Any) -> None:
    monkeypatch.setitem(sys.modules, "bootstrap_lib", types.ModuleType("bootstrap_lib"))
    monkeypatch.delitem(sys.modules, "bootstrap_lib.execution_event", raising=False)
    with pytest.raises(events.ExecutionEventSupportError) as excinfo:
        events._execution_event()
    message = str(excinfo.value)
    assert "claude plugin update bootstrap@plugins-kit" in message
    assert "install" not in message
    assert isinstance(excinfo.value, ImportError)


def test_probe_rejects_module_without_schema_v1(monkeypatch: Any) -> None:
    _install_fake_module(
        monkeypatch, SUPPORTED_SCHEMAS=frozenset({"plugins-kit.execution-event/v0"})
    )
    with pytest.raises(events.ExecutionEventSupportError, match="update"):
        events._execution_event()


def test_probe_rejects_module_missing_a_called_callable(monkeypatch: Any) -> None:
    _install_fake_module(monkeypatch, JsonlSink=_DELETE)
    with pytest.raises(events.ExecutionEventSupportError, match="update"):
        events._execution_event()


def test_probe_rejects_make_event_that_cannot_bind_jk_keywords(monkeypatch: Any) -> None:
    def make_event(*, seq, run_id, event, plugin, at=None, unit_id=None,  # noqa: ANN001
                   attempt_id=None, model=None, payload=None):
        raise AssertionError("never called")

    assert "adapter" not in inspect.signature(make_event).parameters
    _install_fake_module(monkeypatch, make_event=make_event)
    with pytest.raises(events.ExecutionEventSupportError, match="update"):
        events._execution_event()


def test_too_old_message_names_jk_constant_version(monkeypatch: Any) -> None:
    """The version comes from job-kit's own constant, never from the module,
    and the message names both schema literals job-kit writes."""
    assert events._EXECUTION_EVENT_BOOTSTRAP == "0.136.0"
    _install_fake_module(monkeypatch, SUPPORTED_SCHEMAS=_DELETE)
    with pytest.raises(events.ExecutionEventSupportError) as excinfo:
        events._execution_event()
    message = str(excinfo.value)
    assert ">= 0.136.0" in message
    assert "plugins-kit.execution-event/v1" in message
    assert "plugins-kit.execution-event/v2" in message
    monkeypatch.setattr(events, "_EXECUTION_EVENT_BOOTSTRAP", "9.8.7")
    with pytest.raises(events.ExecutionEventSupportError) as excinfo:
        events._execution_event()
    assert ">= 9.8.7" in str(excinfo.value)


def test_probe_rejects_module_without_schema_v2(monkeypatch: Any) -> None:
    """A module that supports only v1 cannot record interrupts, so it is
    refused up front: a whole run is refused, not its first interrupt."""
    _install_fake_module(monkeypatch, SUPPORTED_SCHEMAS=frozenset({events.SCHEMA_V1}))
    with pytest.raises(events.ExecutionEventSupportError, match="update"):
        events._execution_event()


def test_probe_v1_only_module_gets_the_v2_missing_message(monkeypatch: Any) -> None:
    _install_fake_module(monkeypatch, SUPPORTED_SCHEMAS=frozenset({events.SCHEMA_V1}))
    with pytest.raises(events.ExecutionEventSupportError) as excinfo:
        events._execution_event()
    message = str(excinfo.value)
    assert "supports plugins-kit.execution-event/v1 but not /v2" in message
    assert "to record interrupts" in message
    assert ">= 0.136.0" in message
    assert "claude plugin update bootstrap@plugins-kit" in message
    assert "predates or lacks" not in message
    _install_fake_module(monkeypatch, SUPPORTED_SCHEMAS=frozenset({events.SCHEMA_V2}))
    with pytest.raises(events.ExecutionEventSupportError) as generic:
        events._execution_event()
    assert "but not /v2" not in str(generic.value)


def test_probe_rejects_make_event_without_schema_keyword(monkeypatch: Any) -> None:
    def make_event(*, seq, run_id, event, plugin, at=None, unit_id=None,  # noqa: ANN001
                   attempt_id=None, adapter=None, model=None, payload=None):
        raise AssertionError("never called")

    assert "schema" not in inspect.signature(make_event).parameters
    _install_fake_module(monkeypatch, make_event=make_event)
    with pytest.raises(events.ExecutionEventSupportError, match="update"):
        events._execution_event()


def test_store_verb_refuses_before_writing_when_the_module_is_unusable(
    tmp_path: Path, monkeypatch: Any
) -> None:
    store = JobStore(tmp_path / "ledger.sqlite3")
    _install_fake_module(monkeypatch, SUPPORTED_SCHEMAS=frozenset())
    with pytest.raises(events.ExecutionEventSupportError):
        store.create_run("run", [_job(tmp_path)])
    monkeypatch.undo()
    assert store.get_run("run") is None


# --------------------------------------------------------------------------
# Whole-stream guards
# --------------------------------------------------------------------------


def _every_verb_stream(tmp_path: Path) -> tuple[JobStore, tuple[dict, ...]]:
    store = JobStore(tmp_path / "ledger.sqlite3")
    store.create_run(
        "run",
        [
            _job(tmp_path, "accepted"),
            _job(tmp_path, "retried", max_attempts=2),
            _job(tmp_path, "unroutable"),
            _job(tmp_path, "pre-invoke"),
            _job(tmp_path, "lost", max_attempts=2),
            _job(tmp_path, "halted"),
        ],
    )
    _drive(store, "run", "accepted", terminal=JobState.ACCEPTED)
    _drive(store, "run", "retried", accepted=False)
    _drive(store, "run", "retried", terminal=JobState.REJECTED, accepted=False)
    store.mark_unroutable("run", "unroutable", "no endpoint")
    number = _reserve(store, "run", "pre-invoke")
    store.resolve_reservation_before_invoke("run", "pre-invoke", number, reason="boom")
    number = _reserve(store, "run", "lost")
    store.arm_reservation("run", "lost", number, invoke_armed_at=ARMED_AT)
    store.recover_reservations("run")
    _drive(store, "run", "lost", terminal=JobState.ACCEPTED)
    _drive(store, "run", "halted", accepted=None)
    store.mark_halted("run", "halted", "endpoints exhausted")
    return store, store.list_events("run")


def _interrupt_lifecycle_stream(store: JobStore, tmp_path: Path) -> tuple[dict, ...]:
    """A run whose job waits, is answered, continues and is accepted."""
    from job_kit.interrupts import REQUEST_ENVELOPE_V1
    from job_kit.model import InterruptRequest

    store.create_run("interrupted", [_job(tmp_path)])
    attempt_no = _reserve(store, "interrupted", "job")
    store.arm_reservation("interrupted", "job", attempt_no, invoke_armed_at=ARMED_AT)
    requested = Acceptance(
        command=("true",),
        directory=Path.cwd(),
        exit_code=0,
        stdout="",
        stderr="",
        wall_ms=1,
        accepted=False,
        outcome="interrupt_requested",
    )
    store.append_attempt(
        replace_acceptance(_attempt("interrupted", "job", attempt_no), requested),
        interrupt=InterruptRequest(
            envelope=REQUEST_ENVELOPE_V1,
            kind="approval",
            request_schema={"type": "object"},
            payload={},
        ),
        at=10.0,
    )
    [record] = store.list_interrupts("interrupted")
    store.resolve_interrupt("interrupted", record.id, decision="answer", input={}, now=11.0)
    continuation = store.begin_continuation("interrupted", "job", now=12.0)
    store.finish_continuation(
        "interrupted",
        "job",
        continuation.attempt_no,
        continuation.continuation_no,
        acceptance=_attempt("interrupted", "job", 1).acceptance,  # exit 0, observed
        terminal_state=JobState.ACCEPTED,
        now=13.0,
    )
    return store.list_events("interrupted")


def replace_acceptance(attempt: Attempt, acceptance: Acceptance) -> Attempt:
    from dataclasses import replace

    return replace(attempt, acceptance=acceptance)


def test_no_later_revision_names_emitted(tmp_path: Path) -> None:
    """No ``contract`` event at all; ``interrupt`` only under v2; every other
    event under v1, so a run without interrupts is a pure v1 stream."""
    store, stream = _every_verb_stream(tmp_path)
    names = set(_names(list(stream)))
    assert not names & real_module.LATER_REVISION_NAMES
    assert names <= real_module.CORE_EVENTS | {"job-kit:run-created"}
    assert names >= real_module.CORE_EVENTS
    assert {event["schema"] for event in stream} == {real_module.SCHEMA_V1}

    interrupted = _interrupt_lifecycle_stream(store, tmp_path)
    assert real_module.validate_stream(interrupted) == interrupted
    assert "contract" not in set(_names(list(interrupted)))
    assert "interrupt" in set(_names(list(interrupted)))
    for event in interrupted:
        expected = (
            real_module.SCHEMA_V2 if event["event"] == "interrupt" else real_module.SCHEMA_V1
        )
        assert event["schema"] == expected, event


def test_events_validate_as_stream(tmp_path: Path) -> None:
    store, stream = _every_verb_stream(tmp_path)
    assert real_module.validate_stream(stream) == stream
    assert stream[0]["event"] == "job-kit:run-created"
    assert stream[0]["payload"] == {"max_parallel": 1, "job_count": 6}
    for job in store.list_jobs("run"):
        terminals = _of(stream, unit_id=job.job.id, event="terminal")
        assert [event["payload"]["state"] for event in terminals] == [job.state.value]


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
    return names


def test_events_module_imports_no_other_plugin_store() -> None:
    """events.py reaches only the stdlib and bootstrap_lib; the store reaches
    no other plugin at all."""
    foreign = ("content_pipeline", "llm_scripting_kit", "workflow_kit", "workflow_kit_lib")
    events_imports = _imported_modules(LIB / "events.py")
    store_imports = _imported_modules(LIB / "store.py")
    for name in events_imports | store_imports:
        assert name.split(".")[0] not in foreign, name
    non_stdlib = {
        name
        for name in events_imports
        if name.split(".")[0] not in sys.stdlib_module_names and name != "__future__"
    }
    assert non_stdlib <= {"bootstrap_lib"}, non_stdlib
