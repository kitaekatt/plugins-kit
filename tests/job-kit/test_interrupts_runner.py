"""Tests for the runner half of durable interrupts: request ingestion from the
request file, the waiting job, the continuation after an answer, lazy expiry
before dispatch, and the validator probe at the runner's entry points."""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable, Optional

import pytest

import job_kit.run as run_module

from llm_scripting_kit.completion import BackendSelection, Capabilities, LLMResponse

from job_kit.interrupts import (
    REQUEST_ENVELOPE_V1,
    JsonSchemaSupportError,
    resolution_document,
)
from job_kit.model import Acceptance, Contract, Job, JobState, Prompt
from job_kit.run import (
    INTERRUPT_IO_DIRNAME,
    InterruptIO,
    resume_run,
    run_job_file,
    run_jobs,
)
from job_kit.store import JobStore, _MIGRATIONS


RESPONSE_TEXT = "the recorded model answer"

# One contract script drives every scenario. It logs what it received
# (working directory, stdin, the JOB_KIT_* variables, the resolution file's
# bytes) as one JSON line, then acts on its mode. A continuation is told apart
# from a first run by JOB_KIT_INTERRUPT_RESOLUTION, exactly as the README
# tells contract authors to do.
CONTRACT_SCRIPT = r'''
import json, os, sys

mode, log_path = sys.argv[1], sys.argv[2]
names = (
    "JOB_KIT_RUN_ID", "JOB_KIT_JOB_ID", "JOB_KIT_ATTEMPT_NO", "JOB_KIT_ENDPOINT",
    "JOB_KIT_BACKEND", "JOB_KIT_MODEL", "JOB_KIT_INTERRUPT_REQUEST",
    "JOB_KIT_INTERRUPT_RESOLUTION", "JOB_KIT_INTERRUPT_ID",
    "JOB_KIT_CONTINUATION_NO",
)
env = {name: os.environ.get(name) for name in names}
resolution_path = env["JOB_KIT_INTERRUPT_RESOLUTION"]
resolution_text = None
if resolution_path is not None:
    with open(resolution_path, "rb") as handle:
        resolution_text = handle.read().decode("latin-1")
request_path = env["JOB_KIT_INTERRUPT_REQUEST"]
entry = {
    "cwd": os.getcwd(),
    "stdin": sys.stdin.read(),
    "env": env,
    "resolution": resolution_text,
    "request_exists": bool(request_path) and os.path.exists(request_path),
    "request_dir_exists": bool(request_path) and os.path.isdir(os.path.dirname(request_path)),
}
with open(log_path, "a", encoding="utf-8") as handle:
    handle.write(json.dumps(entry) + "\n")

APPROVAL = {"type": "object", "required": ["approved"],
            "properties": {"approved": {"const": True}}}


def ask(step, **extra):
    request = {"schema": "job-kit.interrupt-request/v1", "kind": "approval",
               "request_schema": APPROVAL, "payload": {"step": step}}
    request.update(extra)
    with open(request_path, "w", encoding="utf-8") as handle:
        json.dump(request, handle)


first = resolution_path is None
step = json.loads(resolution_text)["payload"]["step"] if resolution_text else 0
if mode == "accept":
    sys.exit(0)
if mode == "ask":
    if first:
        ask(1)
    sys.exit(0)
if mode == "ask-expiring":
    if first:
        ask(1, expires_in_s=1)
    sys.exit(0)
if mode == "ask-then-reject":
    if first:
        ask(1)
        sys.exit(0)
    sys.exit(3)
if mode == "ask-twice":
    if first:
        ask(1)
    elif step == 1:
        ask(2)
    sys.exit(0)
if mode == "malformed":
    with open(request_path, "w", encoding="utf-8") as handle:
        json.dump({"schema": "job-kit.interrupt-request/v1", "kind": "approval",
                   "request_schema": APPROVAL, "payload": {}, "surprise": 1}, handle)
    sys.exit(0)
if mode == "ask-and-fail":
    ask(1)
    sys.exit(5)
if mode == "stdout-envelope":
    print(json.dumps({"schema": "job-kit.interrupt-request/v1", "kind": "approval",
                      "request_schema": APPROVAL, "payload": {"step": 1}}))
    sys.exit(0)
raise SystemExit("unknown mode " + mode)
'''


class CountingBackend:
    """A hermetic backend that counts seam calls and returns one answer."""

    name = "fake"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def complete(
        self, system: str, user: str, *, model: str, options: object = None
    ) -> LLMResponse:
        self.calls.append(user)
        return LLMResponse(
            text=RESPONSE_TEXT,
            model="fake-model",
            input_tokens=3,
            output_tokens=5,
            dropped_params=(),
            execution_controls_applied=(),
            started_at="2026-09-01T00:00:00Z",
            ended_at="2026-09-01T00:00:01Z",
        )

    def classify_halt(self, exc: BaseException) -> Optional[str]:
        return None


def _advertisement() -> dict[str, Capabilities]:
    return {"fake": Capabilities(adapter="fake")}


def _factory(backend: CountingBackend) -> Callable[..., BackendSelection]:
    def factory(endpoint: str, **_: object) -> BackendSelection:
        return BackendSelection(endpoint, "fake", backend, "fake-model")

    return factory


class Harness:
    """One ledger, one contract script, one call log, per test."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.work = tmp_path / "work"
        self.work.mkdir()
        self.elsewhere = tmp_path / "elsewhere"
        self.elsewhere.mkdir()
        self.script = tmp_path / "contract.py"
        self.script.write_text(CONTRACT_SCRIPT, encoding="utf-8")
        self.log = tmp_path / "contract-log.jsonl"
        self.db = tmp_path / "ledger" / "runs.sqlite3"
        self.backend = CountingBackend()

    def job(self, job_id: str, mode: str, *, max_attempts: int = 1) -> Job:
        # The job directory (where the contract runs) differs from the
        # contract's own declared directory, so the continuation's working
        # directory is provably the one the attempt recorded.
        return Job(
            id=job_id,
            prompt=Prompt(system="instructions", user=job_id),
            models=("fake-endpoint",),
            directory=self.work,
            max_attempts=max_attempts,
            contract=Contract(
                command=(sys.executable, str(self.script), mode, str(self.log)),
                directory=self.elsewhere,
            ),
        )

    def run(self, *jobs: Job, max_parallel: int = 1, **kwargs: object):
        return run_jobs(
            list(jobs),
            self.db,
            run_id="run",
            max_parallel=max_parallel,
            workspace_root=self.tmp_path / "workspaces",
            capabilities_provider=_advertisement,
            backend_factory=_factory(self.backend),
            **kwargs,
        )

    def resume(self, **kwargs: object):
        self.backend = CountingBackend()
        return resume_run(
            "run",
            self.db,
            capabilities_provider=_advertisement,
            backend_factory=_factory(self.backend),
            **kwargs,
        )

    @property
    def store(self) -> JobStore:
        return JobStore(self.db)

    def entries(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [
            json.loads(line)
            for line in self.log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def state(self, job_id: str = "job") -> JobState:
        record = self.store.get_job("run", job_id)
        assert record is not None
        return record.state

    def answer(self, job_id: str = "job", value: object = None):
        record = self.store.open_interrupt("run", job_id)
        assert record is not None
        self.store.resolve_interrupt(
            "run",
            record.id,
            decision="answer",
            input={"approved": True} if value is None else value,
        )
        return self.store.list_interrupts("run", job_id)[-1]

    def interrupt_io_children(self) -> list[Path]:
        root = self.db.parent / INTERRUPT_IO_DIRNAME
        return sorted(root.iterdir()) if root.exists() else []


def _count(db: Path, table: str) -> int:
    connection = sqlite3.connect(str(db))
    try:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        connection.close()


# --------------------------------------------------------------------------
# Request ingestion: the outcome table
# --------------------------------------------------------------------------


def test_contract_receives_interrupt_request_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A first run gets a request path in a live scratch directory, the file
    absent, and no inherited continuation variables."""
    monkeypatch.setenv("JOB_KIT_INTERRUPT_RESOLUTION", "inherited")
    monkeypatch.setenv("JOB_KIT_INTERRUPT_ID", "inherited")
    h = Harness(tmp_path)
    h.run(h.job("job", "accept"))
    [entry] = h.entries()
    request = entry["env"]["JOB_KIT_INTERRUPT_REQUEST"]
    assert request is not None and Path(request).name == "request.json"
    assert entry["request_dir_exists"] is True
    assert entry["request_exists"] is False
    assert entry["env"]["JOB_KIT_INTERRUPT_RESOLUTION"] is None
    assert entry["env"]["JOB_KIT_INTERRUPT_ID"] is None
    assert entry["env"]["JOB_KIT_CONTINUATION_NO"] is None


def test_contract_request_puts_job_waiting(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    snapshot = h.run(h.job("job", "ask"))
    assert h.state() is JobState.WAITING
    assert snapshot.run.status.value == "waiting"
    [attempt] = snapshot.attempts
    assert attempt.acceptance is not None
    assert attempt.acceptance.outcome == "interrupt_requested"
    assert attempt.acceptance.accepted is False
    assert attempt.error is None
    [record] = h.store.list_interrupts("run", "job")
    assert record.attempt_no == 1 and record.continuation_no == 0
    assert record.kind == "approval" and dict(record.payload) == {"step": 1}
    assert record.resolution is None


def test_malformed_request_fails_the_attempt(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    snapshot = h.run(h.job("job", "malformed"))
    assert h.state() is JobState.FAILED
    [attempt] = snapshot.attempts
    assert attempt.error is not None and attempt.error.code == "interrupt_request"
    assert "surprise" in attempt.error.message
    assert h.store.list_interrupts("run", "job") == []
    record = h.store.get_job("run", "job")
    assert record is not None and "surprise" in (record.error or "")


def test_request_with_nonzero_exit_fails_the_attempt(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    snapshot = h.run(h.job("job", "ask-and-fail"))
    assert h.state() is JobState.FAILED
    [attempt] = snapshot.attempts
    assert attempt.error is not None and attempt.error.code == "interrupt_request"
    assert attempt.error.message == "contract exited 5 after writing an interrupt request"
    assert h.store.list_interrupts("run", "job") == []


def _result_acceptance(h: Harness) -> list[object]:
    return [
        event["payload"].get("acceptance")
        for event in h.store.list_events("run")
        if event["event"] == "result"
    ]


@pytest.mark.parametrize("mode", ["malformed", "ask-and-fail"])
def test_refused_request_is_recorded_as_request_refused_never_accepted(
    tmp_path: Path, mode: str
) -> None:
    """A refused request: the attempt row, the job state and the result
    event agree that nothing was accepted."""
    h = Harness(tmp_path)
    snapshot = h.run(h.job("job", mode))
    assert h.state() is JobState.FAILED
    [attempt] = snapshot.attempts
    assert attempt.acceptance is not None
    assert attempt.acceptance.outcome == "request_refused"
    assert attempt.acceptance.accepted is False
    [stored] = h.store.list_attempts("run", "job")
    assert stored.acceptance is not None
    assert stored.acceptance.outcome == "request_refused"
    assert _result_acceptance(h) == ["request_refused"]
    # The shared envelope does not restrict acceptance values: the stream,
    # refused request included, still validates.
    from bootstrap_lib import execution_event

    stream = h.store.list_events("run")
    assert execution_event.validate_stream(stream) == stream


def test_request_refused_outcome_is_never_an_acceptance(tmp_path: Path) -> None:
    refused = Acceptance(
        command=("contract",),
        directory=tmp_path,
        exit_code=0,
        stdout="",
        stderr="",
        wall_ms=1,
        accepted=True,
        outcome="request_refused",
    )
    assert refused.accepted is False
    assert refused.to_mapping()["accepted"] is False


def test_stdout_envelope_text_is_not_a_request(tmp_path: Path) -> None:
    """A valid envelope printed on stdout is output text, never state."""
    h = Harness(tmp_path)
    snapshot = h.run(h.job("job", "stdout-envelope"))
    assert h.state() is JobState.ACCEPTED
    [attempt] = snapshot.attempts
    assert attempt.acceptance is not None
    assert REQUEST_ENVELOPE_V1 in attempt.acceptance.stdout
    assert attempt.acceptance.outcome == "observed"
    assert h.store.list_interrupts("run", "job") == []


def test_timed_out_contract_request_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A contract that wrote a valid request and then timed out is a timeout:
    the file is not read."""
    h = Harness(tmp_path)

    def timed_out(contract, *, directory=None, interrupt_io=None, **_: object):
        assert interrupt_io is not None
        interrupt_io.request_path.write_text(
            json.dumps(
                {
                    "schema": REQUEST_ENVELOPE_V1,
                    "kind": "approval",
                    "request_schema": {"type": "object"},
                    "payload": {},
                }
            ),
            encoding="utf-8",
        )
        return Acceptance(
            command=contract.command,
            directory=directory,
            exit_code=None,
            stdout="",
            stderr="",
            wall_ms=1,
            accepted=False,
            outcome="timed_out",
        )

    monkeypatch.setattr(run_module, "run_contract", timed_out)
    snapshot = h.run(h.job("job", "accept"))
    assert h.state() is JobState.REJECTED
    [attempt] = snapshot.attempts
    assert attempt.acceptance is not None and attempt.acceptance.outcome == "timed_out"
    assert attempt.error is None
    assert h.store.list_interrupts("run", "job") == []


@pytest.mark.parametrize("scenario", ["waiting", "failed", "accepted"])
def test_interrupt_io_directory_is_removed(tmp_path: Path, scenario: str) -> None:
    mode = {"waiting": "ask", "failed": "malformed", "accepted": "accept"}[scenario]
    h = Harness(tmp_path)
    h.run(h.job("job", mode))
    assert h.state().value == scenario
    [entry] = h.entries()
    scratch = Path(entry["env"]["JOB_KIT_INTERRUPT_REQUEST"]).parent
    assert not scratch.exists()
    assert h.interrupt_io_children() == []


def test_interrupt_io_lives_beside_the_ledger(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.run(h.job("job", "ask"))
    h.answer()
    h.resume()
    first, continuation = h.entries()
    root = (h.db.parent / INTERRUPT_IO_DIRNAME).resolve()
    for entry in (first, continuation):
        scratch = Path(entry["env"]["JOB_KIT_INTERRUPT_REQUEST"]).parent
        assert scratch.parent.resolve() == root
    # A continuation gets a FRESH scratch directory, with both files in it.
    assert Path(first["env"]["JOB_KIT_INTERRUPT_REQUEST"]).parent != Path(
        continuation["env"]["JOB_KIT_INTERRUPT_REQUEST"]
    ).parent
    assert (
        Path(continuation["env"]["JOB_KIT_INTERRUPT_RESOLUTION"]).parent
        == Path(continuation["env"]["JOB_KIT_INTERRUPT_REQUEST"]).parent
    )


# --------------------------------------------------------------------------
# The waiting job in the runner loop
# --------------------------------------------------------------------------


def test_waiting_job_ends_the_pass_without_reserving(tmp_path: Path) -> None:
    """The pass that made a job wait returns with it waiting (no StoreError
    from a reservation attempt), and a resume makes no backend call."""
    h = Harness(tmp_path)
    h.run(h.job("job", "ask", max_attempts=3))
    assert h.state() is JobState.WAITING
    assert len(h.backend.calls) == 1
    assert len(h.store.list_reservations("run", "job")) == 1
    h.resume()
    assert h.backend.calls == []
    assert len(h.store.list_reservations("run", "job")) == 1
    assert h.state() is JobState.WAITING


def test_resume_with_unanswered_interrupt_keeps_waiting(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.run(h.job("job", "ask"))
    [record] = h.store.list_interrupts("run", "job")
    snapshot = h.resume()
    assert h.state() is JobState.WAITING
    assert snapshot.run.status.value == "waiting"
    assert h.store.open_interrupt("run", "job") == record
    assert len(h.entries()) == 1  # the contract did not run again
    assert h.store.list_continuations("run", "job") == []


def test_resume_after_answer_reruns_only_the_contract(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.run(h.job("job", "ask", max_attempts=2))
    record = h.answer()
    h.resume()
    assert h.state() is JobState.ACCEPTED
    assert h.backend.calls == []
    assert len(h.store.list_attempts("run", "job")) == 1
    assert len(h.store.list_reservations("run", "job")) == 1
    first, continuation = h.entries()
    assert continuation["stdin"] == RESPONSE_TEXT
    assert continuation["resolution"] == resolution_document(record)
    document = json.loads(continuation["resolution"])
    assert document["interrupt_id"] == record.id
    assert document["input"] == {"approved": True}
    assert document["outcome"] == "answered"
    env = continuation["env"]
    assert env["JOB_KIT_INTERRUPT_ID"] == record.id
    assert env["JOB_KIT_CONTINUATION_NO"] == "1"
    # The owning attempt's identity, unchanged from its first run.
    identity = (
        "JOB_KIT_RUN_ID", "JOB_KIT_JOB_ID", "JOB_KIT_ATTEMPT_NO",
        "JOB_KIT_ENDPOINT", "JOB_KIT_BACKEND", "JOB_KIT_MODEL",
    )
    assert {name: env[name] for name in identity} == {
        name: first["env"][name] for name in identity
    }
    [continued] = h.store.list_continuations("run", "job")
    assert continued.disposition == "completed"
    assert continued.acceptance is not None and continued.acceptance.accepted


def test_continuation_runs_in_the_recorded_directory(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.run(h.job("job", "ask"))
    [attempt] = h.store.list_attempts("run", "job")
    assert attempt.acceptance is not None
    h.answer()
    h.resume()
    first, continuation = h.entries()
    assert Path(continuation["cwd"]).resolve() == attempt.acceptance.directory
    assert Path(continuation["cwd"]).resolve() == h.work.resolve()
    assert Path(first["cwd"]).resolve() == h.work.resolve()


def test_continuation_with_missing_directory_fails(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.run(h.job("job", "ask"))
    h.answer()
    h.work.rmdir()
    h.resume()
    assert h.state() is JobState.FAILED
    record = h.store.get_job("run", "job")
    assert record is not None and record.error is not None
    assert "no longer exists" in record.error and "work" in record.error
    assert len(h.entries()) == 1  # the contract never ran
    [continued] = h.store.list_continuations("run", "job")
    assert continued.acceptance is not None
    assert continued.acceptance.outcome == "not_run"


def test_rejected_continuation_retries_within_budget(tmp_path: Path) -> None:
    """A continuation that exits non-zero with budget left returns the job to
    pending, and the pass makes a fresh attempt (one model call)."""
    h = Harness(tmp_path)
    h.run(h.job("job", "ask-then-reject", max_attempts=2))
    h.answer()
    h.resume()
    assert len(h.backend.calls) == 1
    attempts = h.store.list_attempts("run", "job")
    assert [attempt.attempt_no for attempt in attempts] == [1, 2]
    # The fresh attempt's contract asked again, on attempt 2.
    assert h.state() is JobState.WAITING
    latest = h.store.list_interrupts("run", "job")[-1]
    assert latest.attempt_no == 2 and latest.resolution is None
    [continued] = h.store.list_continuations("run", "job")
    assert continued.acceptance is not None and continued.acceptance.exit_code == 3


def test_rejected_continuation_at_budget_is_rejected(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.run(h.job("job", "ask-then-reject", max_attempts=1))
    h.answer()
    h.resume()
    assert h.state() is JobState.REJECTED
    assert h.backend.calls == []
    assert len(h.store.list_attempts("run", "job")) == 1


def test_continuation_can_request_a_follow_up_interrupt(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.run(h.job("job", "ask-twice"))
    h.answer()
    h.resume()
    assert h.state() is JobState.WAITING
    first, follow_up = h.store.list_interrupts("run", "job")
    assert follow_up.attempt_no == 1 and follow_up.continuation_no == 1
    assert dict(follow_up.payload) == {"step": 2} and follow_up.resolution is None
    h.answer()
    h.resume()
    assert h.state() is JobState.ACCEPTED
    assert h.backend.calls == []
    numbers = [item.continuation_no for item in h.store.list_continuations("run", "job")]
    assert numbers == [1, 2]
    third = h.entries()[-1]
    assert third["env"]["JOB_KIT_INTERRUPT_ID"] == follow_up.id


def test_operator_rejected_job_is_not_continued(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.run(h.job("job", "ask"))
    record = h.store.open_interrupt("run", "job")
    assert record is not None
    h.store.resolve_interrupt("run", record.id, decision="reject", reason="no")
    h.resume()
    assert h.state() is JobState.OPERATOR_REJECTED
    assert len(h.entries()) == 1
    assert h.backend.calls == []
    assert h.store.list_continuations("run", "job") == []


def test_parallel_pass_leaves_waiting_job_and_finishes_others(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    snapshot = h.run(
        h.job("asks", "ask"),
        h.job("first", "accept"),
        h.job("second", "accept"),
        max_parallel=3,
    )
    states = {record.id: record.state for record in snapshot.jobs}
    assert states == {
        "asks": JobState.WAITING,
        "first": JobState.ACCEPTED,
        "second": JobState.ACCEPTED,
    }
    assert len(h.backend.calls) == 3


# --------------------------------------------------------------------------
# Expiry before dispatch
# --------------------------------------------------------------------------


def _later(monkeypatch: pytest.MonkeyPatch, seconds: float) -> None:
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + seconds)


def test_resume_expires_lapsed_interrupts_before_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    h.run(h.job("job", "ask-expiring"))
    assert h.state() is JobState.WAITING
    _later(monkeypatch, 3600)
    h.resume()
    assert h.state() is JobState.EXPIRED
    [record] = h.store.list_interrupts("run", "job")
    assert record.resolution is not None and record.resolution.outcome == "expired"
    assert len(h.entries()) == 1


def test_expired_job_is_never_submitted_to_a_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    h.run(h.job("lapses", "ask-expiring"), h.job("answered", "ask"))
    h.answer("answered")
    submitted: list[str] = []
    real_drive = run_module._drive_job

    def spy(store, run_id, job, **kwargs):
        submitted.append(job.id)
        return real_drive(store, run_id, job, **kwargs)

    monkeypatch.setattr(run_module, "_drive_job", spy)
    _later(monkeypatch, 3600)
    h.resume()
    assert submitted == ["answered"]
    assert h.state("lapses") is JobState.EXPIRED
    assert h.state("answered") is JobState.ACCEPTED


# --------------------------------------------------------------------------
# Loss, Ctrl-C and the stable idempotency key
# --------------------------------------------------------------------------


def _interrupt_continuation(
    monkeypatch: pytest.MonkeyPatch, signal: BaseException, *, after_contract: bool
) -> None:
    """Raise ``signal`` around the next continuation's contract run."""
    real = run_module.run_contract
    fired = []

    def wrapper(*args, interrupt_io: Optional[InterruptIO] = None, **kwargs):
        if interrupt_io is None or interrupt_io.resolution_path is None or fired:
            return real(*args, interrupt_io=interrupt_io, **kwargs)
        fired.append(True)
        if after_contract:
            real(*args, interrupt_io=interrupt_io, **kwargs)
        raise signal

    monkeypatch.setattr(run_module, "run_contract", wrapper)


def test_ctrl_c_during_continuation_returns_job_to_waiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    h.run(h.job("job", "ask"))
    record = h.answer()
    _interrupt_continuation(monkeypatch, KeyboardInterrupt(), after_contract=False)
    with pytest.raises(KeyboardInterrupt):
        h.resume()
    assert h.state() is JobState.WAITING
    [interrupted] = h.store.list_continuations("run", "job")
    assert interrupted.disposition == "interrupted"
    [kept] = h.store.list_interrupts("run", "job")
    assert kept == record and kept.resolution is not None
    monkeypatch.undo()
    h.resume()
    assert h.state() is JobState.ACCEPTED


class _ProcessDeath(BaseException):
    """Stands in for the runner process dying: nothing below records it."""


def test_lost_continuation_is_rerun_on_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A continuation whose process died leaves its job running with a live
    continuation; the next resume's recovery returns it to waiting (never
    pending, which would make a model call) and re-runs the contract."""
    h = Harness(tmp_path)
    h.run(h.job("job", "ask", max_attempts=3))
    h.answer()
    _interrupt_continuation(monkeypatch, _ProcessDeath(), after_contract=True)
    with pytest.raises(_ProcessDeath):
        h.resume()
    assert h.state() is JobState.RUNNING
    [live] = h.store.list_continuations("run", "job")
    assert live.disposition is None
    monkeypatch.undo()
    h.resume()
    assert h.state() is JobState.ACCEPTED
    assert h.backend.calls == []
    assert len(h.store.list_attempts("run", "job")) == 1
    lost, rerun = h.store.list_continuations("run", "job")
    assert lost.disposition == "process_lost"
    assert rerun.disposition == "completed" and rerun.continuation_no == 2
    assert len(h.entries()) == 3  # first run, the lost run, the re-run


def test_continuation_reruns_receive_identical_interrupt_id_and_resolution_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    h.run(h.job("job", "ask"))
    record = h.answer()
    _interrupt_continuation(monkeypatch, KeyboardInterrupt(), after_contract=True)
    with pytest.raises(KeyboardInterrupt):
        h.resume()
    monkeypatch.undo()
    h.resume()
    assert h.state() is JobState.ACCEPTED
    _, interrupted, rerun = h.entries()
    assert interrupted["env"]["JOB_KIT_INTERRUPT_ID"] == record.id
    assert rerun["env"]["JOB_KIT_INTERRUPT_ID"] == record.id
    assert interrupted["resolution"] is not None
    assert rerun["resolution"] == interrupted["resolution"]
    assert interrupted["env"]["JOB_KIT_CONTINUATION_NO"] == "1"
    assert rerun["env"]["JOB_KIT_CONTINUATION_NO"] == "2"


# --------------------------------------------------------------------------
# The validator probe at the runner's entry points
# --------------------------------------------------------------------------


def _without_validator(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "llm_scripting_kit.completion.json_schema", None)


def _schema_11_ledger(tmp_path: Path, *, armed_reservation: bool) -> Path:
    """A schema-11 ledger with one run and one job, built by hand."""
    path = tmp_path / "schema-11.sqlite3"
    job = Job(
        id="job",
        prompt=Prompt(user="job"),
        models=("fake-endpoint",),
        directory=tmp_path,
        contract=Contract(command=(sys.executable, "-c", "pass"), directory=tmp_path),
    )
    connection = sqlite3.connect(str(path))
    try:
        connection.execute("PRAGMA journal_mode = WAL")
        for index in range(11):
            for statement in _MIGRATIONS[index]:
                connection.execute(statement)
            if index:
                connection.execute("UPDATE schema_version SET version = ?", (index + 1,))
        connection.execute(
            "INSERT INTO runs(id, created_at, max_parallel, events_recorded) "
            "VALUES ('old', 1.0, 1, 1)"
        )
        connection.execute(
            "INSERT INTO jobs(run_id, id, ordinal, definition_json, state, created_at, "
            "updated_at) VALUES ('old', 'job', 0, ?, ?, 1.0, 1.0)",
            (
                json.dumps(job.to_mapping(), sort_keys=True),
                "running" if armed_reservation else "pending",
            ),
        )
        if armed_reservation:
            connection.execute(
                "INSERT INTO reservations(run_id, job_id, attempt_no, budget_no, "
                "endpoint, backend, model, reserved_at, invoke_armed_at) VALUES "
                "('old', 'job', 1, 1, 'fake-endpoint', 'fake', 'fake-model', "
                "'2026-09-01T00:00:00Z', '2026-09-01T00:00:01Z')"
            )
        connection.commit()
    finally:
        connection.close()
    return path


def _schema_version(path: Path) -> int:
    connection = sqlite3.connect(str(path))
    try:
        return int(connection.execute("SELECT version FROM schema_version").fetchone()[0])
    finally:
        connection.close()


def test_run_job_file_refuses_before_creating_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs_path = tmp_path / "jobs.yaml"
    jobs_path.write_text(
        "jobs:\n"
        "  - id: job\n"
        "    prompt: run\n"
        "    models: [fake-endpoint]\n"
        "    directory: .\n"
        "    contract:\n"
        "      command: [true]\n",
        encoding="utf-8",
    )
    store_path = tmp_path / "ledger" / "runs.sqlite3"
    _without_validator(monkeypatch)
    with pytest.raises(JsonSchemaSupportError, match="0.56.0"):
        run_job_file(
            jobs_path,
            store_path=store_path,
            capabilities_provider=_advertisement,
            backend_factory=_factory(CountingBackend()),
        )
    assert not store_path.exists()
    assert not store_path.parent.exists()


def test_run_jobs_refuses_before_any_run_or_event_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _schema_11_ledger(tmp_path, armed_reservation=False)
    before = (_count(path, "runs"), _count(path, "events"))
    h = Harness(tmp_path)
    backend = CountingBackend()
    _without_validator(monkeypatch)
    with pytest.raises(JsonSchemaSupportError, match="0.56.0"):
        run_jobs(
            [h.job("job", "accept")],
            path,
            run_id="new",
            capabilities_provider=_advertisement,
            backend_factory=_factory(backend),
        )
    monkeypatch.undo()
    assert _schema_version(path) == 11
    assert (_count(path, "runs"), _count(path, "events")) == before
    assert backend.calls == []


def test_resume_refuses_before_recovery_or_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _schema_11_ledger(tmp_path, armed_reservation=True)
    _without_validator(monkeypatch)
    with pytest.raises(JsonSchemaSupportError, match="0.56.0"):
        resume_run(
            "old",
            path,
            capabilities_provider=_advertisement,
            backend_factory=_factory(CountingBackend()),
        )
    monkeypatch.undo()
    assert _schema_version(path) == 11
    connection = sqlite3.connect(str(path))
    try:
        disposition, = connection.execute(
            "SELECT disposition FROM reservations WHERE run_id = 'old'"
        ).fetchone()
        state, = connection.execute(
            "SELECT state FROM jobs WHERE run_id = 'old'"
        ).fetchone()
    finally:
        connection.close()
    assert disposition is None
    assert state == "running"
