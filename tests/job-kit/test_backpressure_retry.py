"""Tests for waiting out transient endpoint backpressure (halt kind "backpressure").

Fakes only: no model, and no real sleeping (the policy's sleeper is injected).
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import job_kit.run as run_module
from job_kit.model import JobState
from job_kit.run import BackpressurePolicy, run_jobs
from job_kit.store import JobStore
from llm_scripting_kit.completion import HALT_RATE_LIMIT, HaltError

from test_runner import (
    SequenceBackend,
    _advertisement,
    _factory_for,
    _job,
)

BACKPRESSURE = "backpressure"


def _pressure(retry_after_s: float | None = None) -> HaltError:
    return HaltError(BACKPRESSURE, "endpoint overloaded", retry_after_s=retry_after_s)


class _Waits(list):
    state: dict


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Install a no-sleep policy; the list records every requested wait."""
    waits = _Waits()
    state: dict[str, object] = {"cap_s": 600.0}

    def policy() -> BackpressurePolicy:
        return BackpressurePolicy(
            cap_s=float(state["cap_s"]),  # type: ignore[arg-type]
            sleeper=waits.append,
            rng=lambda: 0.5,  # zero jitter
        )

    monkeypatch.setattr(BackpressurePolicy, "from_environment", staticmethod(policy))
    waits.state = state
    return waits


def _run(tmp_path: Path, backend: SequenceBackend, name: str = "bp"):
    return run_jobs(
        [_job(tmp_path)],
        tmp_path / f"{name}.sqlite3",
        capabilities_provider=_advertisement,
        backend_factory=_factory_for(backend),
    )


def _events(tmp_path: Path, snapshot, name: str = "bp"):
    store = JobStore(tmp_path / f"{name}.sqlite3")
    return [e for e in store.list_events(snapshot.run.id) if e["event"] == "job-kit:backpressure-wait"]


def test_retry_after_is_honoured_and_job_succeeds_on_same_attempt(
    tmp_path: Path, sleeps: _Waits
) -> None:
    backend = SequenceBackend([_pressure(7.0), _pressure(3.5), None])
    snapshot = _run(tmp_path, backend)
    assert snapshot.jobs[0].state is JobState.ACCEPTED
    assert sleeps == [7.0, 3.5]
    assert len(backend.calls) == 3
    # Every seam call is its own attempt row; waited-out halts are recorded as such.
    assert len(snapshot.attempts) == 3
    assert [a.attempt_no for a in snapshot.attempts] == [1, 2, 3]
    assert [a.halt_kind for a in snapshot.attempts] == [HALT_RATE_LIMIT, HALT_RATE_LIMIT, None]
    assert {c[2] for c in backend.calls} == {"fake-model"}  # same model


def test_without_retry_after_backoff_is_exponential_and_bounded(
    tmp_path: Path, sleeps: _Waits
) -> None:
    backend = SequenceBackend([_pressure()] * 7 + [None])
    snapshot = _run(tmp_path, backend)
    assert snapshot.jobs[0].state is JobState.ACCEPTED
    assert sleeps == [2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]


def test_jitter_spreads_the_wait() -> None:
    low = BackpressurePolicy(rng=lambda: 0.0).wait_for(1, None)
    high = BackpressurePolicy(rng=lambda: 1.0).wait_for(1, None)
    assert (low, high) == (1.5, 2.5)


def test_waits_are_ledger_events(tmp_path: Path, sleeps: _Waits) -> None:
    backend = SequenceBackend([_pressure(5.0), _pressure(), None])
    snapshot = _run(tmp_path, backend)
    events = _events(tmp_path, snapshot)
    assert [e["payload"]["retry_no"] for e in events] == [1, 2]
    assert events[0]["payload"]["retry_after_s"] == 5.0
    assert "retry_after_s" not in events[1]["payload"]
    assert events[1]["payload"]["waited_s"] == 5.0
    assert all(e["identity"]["unit_id"] == "job" for e in events)


def test_cap_exhaustion_falls_back_to_the_rate_limit_halt(
    tmp_path: Path, sleeps: _Waits
) -> None:
    sleeps.state["cap_s"] = 10.0
    backend = SequenceBackend([_pressure(6.0), _pressure(6.0), None])
    snapshot = _run(tmp_path, backend)
    assert sleeps == [6.0]
    assert len(backend.calls) == 2
    assert snapshot.jobs[0].state is JobState.HALTED
    assert [a.halt_kind for a in snapshot.attempts] == [HALT_RATE_LIMIT, HALT_RATE_LIMIT]


def test_waited_out_attempts_do_not_spend_max_attempts(
    tmp_path: Path, sleeps: _Waits
) -> None:
    job = replace(_job(tmp_path), max_attempts=1)
    backend = SequenceBackend([_pressure(1.0)] * 4 + [None])
    snapshot = run_jobs(
        [job],
        tmp_path / "budget.sqlite3",
        capabilities_provider=_advertisement,
        backend_factory=_factory_for(backend),
    )
    assert snapshot.jobs[0].state is JobState.ACCEPTED
    assert len(snapshot.attempts) == 5
    assert sleeps == [1.0] * 4


def test_cap_exhausted_attempt_spends_the_budget(tmp_path: Path, sleeps: _Waits) -> None:
    sleeps.state["cap_s"] = 2.0
    job = replace(_job(tmp_path), max_attempts=1)
    backend = SequenceBackend([_pressure(1.0), _pressure(1.0), _pressure(1.0), None])
    snapshot = run_jobs(
        [job],
        tmp_path / "budget2.sqlite3",
        capabilities_provider=_advertisement,
        backend_factory=_factory_for(backend),
    )
    assert snapshot.jobs[0].state is JobState.HALTED
    assert len(snapshot.attempts) == 3


def test_each_wait_event_names_the_attempt_it_follows(
    tmp_path: Path, sleeps: _Waits
) -> None:
    snapshot = _run(tmp_path, SequenceBackend([_pressure(1.0), _pressure(1.0), None]))
    assert len(_events(tmp_path, snapshot)) == 2
    store = JobStore(tmp_path / "bp.sqlite3")
    assert store.waited_out_attempt_numbers(snapshot.run.id, "job") == {1, 2}


def test_zero_cap_disables_waiting(tmp_path: Path, sleeps: _Waits) -> None:
    sleeps.state["cap_s"] = 0.0
    snapshot = _run(tmp_path, SequenceBackend([_pressure(1.0)]))
    assert sleeps == []
    assert len(snapshot.attempts) == 1
    assert snapshot.attempts[0].halt_kind == HALT_RATE_LIMIT


def test_rate_limit_is_transient_and_waited_out(
    tmp_path: Path, sleeps: _Waits
) -> None:
    err = HaltError(HALT_RATE_LIMIT, "429", retry_after_s=9.0)
    backend = SequenceBackend([err, HaltError(HALT_RATE_LIMIT, "429"), None])
    snapshot = _run(tmp_path, backend)
    assert snapshot.jobs[0].state is JobState.ACCEPTED
    assert sleeps == [9.0, 4.0]


def test_credit_and_quota_halts_are_not_waited_out(tmp_path: Path, sleeps: _Waits) -> None:
    backend = SequenceBackend([HaltError("insufficient_credit", "402")])
    snapshot = _run(tmp_path, backend)
    assert sleeps == []
    assert len(backend.calls) == 1
    assert snapshot.jobs[0].state is JobState.HALTED


def test_classifier_reported_backpressure_is_waited_out(
    tmp_path: Path, sleeps: _Waits
) -> None:
    class Classifying(SequenceBackend):
        def classify_halt(self, exc: BaseException) -> str | None:
            return BACKPRESSURE if isinstance(exc, ConnectionError) else None

    err = ConnectionError("busy")
    err.retry_after_s = 1.25  # type: ignore[attr-defined]
    snapshot = _run(tmp_path, Classifying([err, None]))
    assert snapshot.jobs[0].state is JobState.ACCEPTED
    assert sleeps == [1.25]


def test_default_cap_and_environment_knob(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()  # drop the conftest zero-cap stub
    monkeypatch.delenv(run_module.BACKPRESSURE_CAP_ENV, raising=False)
    assert BackpressurePolicy.from_environment().cap_s == 600.0
    monkeypatch.setenv(run_module.BACKPRESSURE_CAP_ENV, "42")
    assert BackpressurePolicy.from_environment().cap_s == 42.0
    monkeypatch.setenv(run_module.BACKPRESSURE_CAP_ENV, "soon")
    with pytest.raises(ValueError):
        BackpressurePolicy.from_environment()


# --- text-only arming of a deny floor (kit's arm_requirements) -------------

import json  # noqa: E402

from llm_scripting_kit.completion import BackendSelection  # noqa: E402
from llm_scripting_kit.completion.backends import ClaudeCliBackend  # noqa: E402


def _claude_run(tmp_path: Path, floor: str | None):
    argvs: list[list[str]] = []

    def runner(cmd, user, cwd, **_):
        argvs.append(list(cmd))
        return json.dumps({"result": "ok", "usage": {}}), "", 0

    backend = ClaudeCliBackend(runner=runner, executable="claude")

    def factory(endpoint: str, **_: object) -> BackendSelection:
        return BackendSelection(endpoint, "harness", backend, "claude-model")

    snapshot = run_jobs(
        [replace(_job(tmp_path), models=("claude-ep",))],
        tmp_path / "arm.sqlite3",
        capabilities_provider=None,
        backend_factory=factory,
        disallowed_tools=floor,
    )
    return snapshot, argvs


def test_deny_floor_runs_claude_text_only(tmp_path: Path) -> None:
    snapshot, argvs = _claude_run(tmp_path, "Write,Edit,MultiEdit,NotebookEdit,Bash,Task")
    assert snapshot.jobs[0].state is JobState.ACCEPTED
    argv = argvs[0]
    assert "bypassPermissions" not in argv
    assert "--strict-mcp-config" in argv
    assert "--tools" in argv


def test_no_floor_leaves_claude_unchanged(tmp_path: Path) -> None:
    snapshot, argvs = _claude_run(tmp_path, None)
    assert snapshot.jobs[0].state is JobState.ACCEPTED
    assert "bypassPermissions" in argvs[0]
    assert "--strict-mcp-config" not in argvs[0]
