"""Regression tests for durable seam reservations and process-loss recovery."""

from __future__ import annotations

import os
import re
import sqlite3
import signal
import subprocess
import sys
import textwrap
from dataclasses import replace
from pathlib import Path

from llm_scripting_kit.completion import BackendSelection, Capabilities, LLMResponse

from job_kit.model import Acceptance, Attempt, Contract, Job, JobState, Prompt
from job_kit.store import JobStore
from job_kit.workspace import WorkspaceManager, gc_workspaces


def _job(directory: Path, *, max_attempts: int = 2) -> Job:
    """Build a job suitable for direct reservation tests."""
    return Job(
        id="job",
        prompt=Prompt(user="run"),
        endpoint_preference=("fake",),
        directory=directory,
        max_attempts=max_attempts,
        contract=Contract(command=("true",), directory=directory),
    )


def _attempt(
    run_id: str,
    job_id: str,
    attempt_no: int,
    started_at: str = "2026-09-08T00:00:02Z",
) -> Attempt:
    """Build one observed seam result for a reservation."""
    return Attempt(
        run_id=run_id,
        job_id=job_id,
        attempt_no=attempt_no,
        endpoint="fake",
        backend="fake",
        model="model",
        status="completed",
        started_at=started_at,
        ended_at="2026-09-08T00:00:03Z",
        response_text="done",
        acceptance=Acceptance(
            command=("true",),
            directory=Path.cwd(),
            exit_code=0,
            stdout="",
            stderr="",
            wall_ms=1,
            accepted=True,
        ),
    )


def _store(tmp_path: Path, *, max_attempts: int = 2) -> tuple[JobStore, Job]:
    """Create one run and return its store and job."""
    store = JobStore(tmp_path / "run.sqlite3")
    job = _job(tmp_path, max_attempts=max_attempts)
    store.create_run("run", [job], workspace_root=tmp_path / "workspaces")
    return store, job


def _git_repository(path: Path) -> Path:
    """Create a minimal committed repository for workspace GC tests."""
    path.mkdir()
    subprocess.run(["git", "-C", str(path), "init", "--quiet"], check=True)
    (path / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "tracked.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=job-kit-test",
            "-c",
            "user.email=job-kit-test@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "initial",
        ],
        check=True,
    )
    return path


def test_reserve_and_mark_running_are_atomic(tmp_path: Path) -> None:
    """Reservation and RUNNING state are one durable write transaction."""
    store, job = _store(tmp_path)

    reservation = store.reserve_attempt(
        "run",
        job.id,
        endpoint="fake",
        backend="fake",
        model="model",
        reserved_at="2026-09-08T00:00:00Z",
    )

    assert reservation.attempt_no == 1
    assert store.get_job("run", job.id).state is JobState.RUNNING
    assert store.list_reservations("run", job.id)[0].disposition is None


def test_attempt_number_counts_armed_losses(tmp_path: Path) -> None:
    """An armed process loss consumes a number before the next reservation."""
    store, job = _store(tmp_path)
    first = store.reserve_attempt(
        "run", job.id, endpoint="fake", backend="fake", model="model", reserved_at="t0"
    )
    store.arm_reservation("run", job.id, first.attempt_no, invoke_armed_at="t1")
    store.recover_reservations("run", at="t1")

    second = store.reserve_attempt(
        "run", job.id, endpoint="fake", backend="fake", model="model", reserved_at="t2"
    )

    assert second.attempt_no == 2
    assert second.budget_no == 2


def test_unarmed_reservation_does_not_consume_budget(tmp_path: Path) -> None:
    """Recovery before arming leaves the job eligible for its original retry."""
    store, job = _store(tmp_path, max_attempts=1)
    store.reserve_attempt(
        "run", job.id, endpoint="fake", backend="fake", model="model", reserved_at="t0"
    )
    store.recover_reservations("run")

    reservation = store.reserve_attempt(
        "run", job.id, endpoint="fake", backend="fake", model="model", reserved_at="t1"
    )

    assert reservation.budget_no == 1
    assert store.get_job("run", job.id).state is JobState.RUNNING


def test_armed_reservation_consumes_budget_without_attempt(tmp_path: Path) -> None:
    """Armed recovery records uncertainty without fabricating an attempt row."""
    store, job = _store(tmp_path)
    reservation = store.reserve_attempt(
        "run", job.id, endpoint="fake", backend="fake", model="model", reserved_at="t0"
    )
    store.arm_reservation("run", job.id, reservation.attempt_no, invoke_armed_at="t1")
    store.recover_reservations("run")

    assert store.list_attempts("run", job.id) == []
    loss = store.list_reservations("run", job.id)[0]
    assert loss.disposition == "process_lost"
    assert store.get_job("run", job.id).state is JobState.PENDING


def test_exhausted_loss_terminalizes_job(tmp_path: Path) -> None:
    """Repeated armed losses cannot create an unbounded retry loop."""
    store, job = _store(tmp_path, max_attempts=2)
    for index in range(2):
        reservation = store.reserve_attempt(
            "run", job.id, endpoint="fake", backend="fake", model="model", reserved_at=f"t{index}"
        )
        store.arm_reservation(
            "run", job.id, reservation.attempt_no, invoke_armed_at=f"a{index}"
        )
        store.recover_reservations("run")

    record = store.get_job("run", job.id)
    assert record.state is JobState.FAILED
    assert record.error is not None


def test_append_attempt_consumes_reservation_atomically(tmp_path: Path) -> None:
    """The observed attempt and reservation resolution share one commit."""
    store, job = _store(tmp_path)
    reservation = store.reserve_attempt(
        "run", job.id, endpoint="fake", backend="fake", model="model", reserved_at="t0"
    )
    store.arm_reservation("run", job.id, reservation.attempt_no, invoke_armed_at="armed")

    attempt = store.append_attempt(
        _attempt("run", job.id, reservation.attempt_no, started_at="wrong"),
        terminal_state=JobState.ACCEPTED,
    )

    assert attempt.started_at == "armed"
    assert len(store.list_attempts("run", job.id)) == 1
    assert store.list_reservations("run", job.id)[0].disposition == "completed"
    assert not [row for row in store.list_reservations("run", job.id) if row.disposition is None]


def test_workspace_path_is_durable_before_creation(tmp_path: Path) -> None:
    """A path recorded before Git creation remains owned after a loss."""
    store, job = _store(tmp_path)
    reservation = store.reserve_attempt(
        "run", job.id, endpoint="fake", backend="fake", model="model", reserved_at="t0"
    )
    path = tmp_path / "workspaces" / "job" / "attempt-1"
    store.record_reservation_workspace(
        "run", job.id, reservation.attempt_no, workspace_path=path
    )

    assert store.list_reservations("run", job.id)[0].workspace_path == path.resolve()


def test_gc_reclaims_lost_reservation_workspace(tmp_path: Path) -> None:
    """GC accepts a lost reservation workspace in normal and forced modes."""
    repository = _git_repository(tmp_path / "repository")
    store = JobStore(tmp_path / "run.sqlite3")
    job = replace(_job(repository), contract=Contract(command=("true",), directory=repository))
    store.create_run("run", [job], workspace_root=tmp_path / "workspaces")
    reservation = store.reserve_attempt(
        "run", job.id, endpoint="fake", backend="fake", model="model", reserved_at="t0"
    )
    manager = WorkspaceManager(tmp_path / "workspaces", (job,))
    path = manager.allocate_path(job, reservation.attempt_no)
    assert path is not None
    store.record_reservation_workspace(
        "run", job.id, reservation.attempt_no, workspace_path=path
    )
    manager.create(job, workspace_path=path)
    store.arm_reservation("run", job.id, reservation.attempt_no, invoke_armed_at="t1")
    store.recover_reservations("run")

    report = gc_workspaces(store, "run")
    assert not report.refused
    assert report.removed

    second = store.reserve_attempt(
        "run", job.id, endpoint="fake", backend="fake", model="model", reserved_at="t2"
    )
    second_path = manager.allocate_path(job, second.attempt_no)
    assert second_path is not None
    store.record_reservation_workspace(
        "run", job.id, second.attempt_no, workspace_path=second_path
    )
    manager.create(job, workspace_path=second_path)
    (second_path / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    store.arm_reservation("run", job.id, second.attempt_no, invoke_armed_at="t3")
    store.recover_reservations("run", at="t3")
    refused = gc_workspaces(store, "run")
    assert refused.refused
    forced = gc_workspaces(store, "run", force=True)
    assert not forced.refused


def test_losses_do_not_count_as_unreachable_attempts(tmp_path: Path) -> None:
    """A loss has no attempt status or started/ended attempt evidence."""
    store, job = _store(tmp_path)
    reservation = store.reserve_attempt(
        "run", job.id, endpoint="fake", backend="fake", model="model", reserved_at="t0"
    )
    store.arm_reservation("run", job.id, reservation.attempt_no, invoke_armed_at="t1")
    store.recover_reservations("run", at="t1")

    with sqlite3.connect(str(store.db_path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
        assert connection.execute(
            "SELECT disposition, invoke_armed_at, lost_at FROM reservations"
        ).fetchone() == ("process_lost", "t1", "t1")


def test_sigkill_at_each_boundary(tmp_path: Path) -> None:
    """SIGKILL after each durable boundary leaves a resumable ledger."""
    repository = _git_repository(tmp_path / "repository")
    db_path = tmp_path / "crash.sqlite3"
    workspace_root = tmp_path / "workspaces"
    marker = tmp_path / "side-effects.txt"
    boundaries = ("reserve", "worktree", "arm", "seam", "attempt")
    script = textwrap.dedent(
        """
        import os
        import signal
        from contextlib import contextmanager
        from pathlib import Path

        from llm_scripting_kit.completion import BackendSelection, Capabilities, LLMResponse
        from job_kit.model import Contract, Job, Prompt
        from job_kit.run import run_jobs
        from job_kit.store import JobStore
        import job_kit.workspace as workspace_module

        boundary = os.environ["JOB_KIT_CRASH_BOUNDARY"]
        db_path = Path(os.environ["JOB_KIT_CRASH_DB"])
        repository = Path(os.environ["JOB_KIT_CRASH_REPOSITORY"])
        workspace_root = Path(os.environ["JOB_KIT_CRASH_WORKSPACE"])
        marker = Path(os.environ["JOB_KIT_CRASH_MARKER"])

        def crash() -> None:
            os.kill(os.getpid(), signal.SIGKILL)

        store = JobStore(db_path)
        if boundary == "reserve":
            original = JobStore.reserve_attempt
            def reserve(self, *args, **kwargs):
                result = original(self, *args, **kwargs)
                crash()
                return result
            JobStore.reserve_attempt = reserve
        elif boundary == "worktree":
            original = workspace_module.WorkspaceManager.create
            def create(self, *args, **kwargs):
                result = original(self, *args, **kwargs)
                crash()
                return result
            workspace_module.WorkspaceManager.create = create
        elif boundary == "arm":
            original = JobStore.arm_reservation
            def arm(self, *args, **kwargs):
                result = original(self, *args, **kwargs)
                crash()
                return result
            JobStore.arm_reservation = arm
        elif boundary == "attempt":
            original = JobStore._connect
            @contextmanager
            def connect(self):
                with original(self) as connection:
                    def trace(sql):
                        if "INSERT INTO ATTEMPTS" in sql.upper():
                            crash()
                    connection.set_trace_callback(trace)
                    yield connection
            JobStore._connect = connect

        class Backend:
            name = "fake"
            def complete(self, system, user, *, model, options=None):
                marker.write_text(marker.read_text() + "side effect\\n" if marker.exists() else "side effect\\n")
                if boundary == "seam":
                    crash()
                return LLMResponse(
                    text="answer", model=model, input_tokens=1, output_tokens=1,
                    dropped_params=(), execution_controls_applied=(),
                    started_at="response-start", ended_at="response-end",
                )

        def advertise():
            return {"fake": Capabilities(adapter="fake")}

        def factory(endpoint, **kwargs):
            return BackendSelection(endpoint, "fake", Backend(), "model")

        job = Job(
            id="job", prompt=Prompt(user="run"), endpoint_preference=("fake",),
            directory=repository, max_attempts=2,
            contract=Contract(command=("true",), directory=repository),
        )
        run_jobs(
            [job], store, run_id="run", workspace_root=workspace_root,
            capabilities_provider=advertise, backend_factory=factory,
        )
        """
    )
    repo_root = Path(__file__).resolve().parents[2]
    for boundary in boundaries:
        environment = os.environ.copy()
        environment.update(
            {
                "JOB_KIT_CRASH_BOUNDARY": boundary,
                "JOB_KIT_CRASH_DB": str(db_path),
                "JOB_KIT_CRASH_REPOSITORY": str(repository),
                "JOB_KIT_CRASH_WORKSPACE": str(workspace_root),
                "JOB_KIT_CRASH_MARKER": str(marker),
                "PYTHONPATH": os.pathsep.join(
                    (
                        str(repo_root / "plugins" / "job-kit" / "lib"),
                        str(repo_root / "plugins" / "llm-scripting-kit" / "lib"),
                    )
                ),
            }
        )
        if db_path.exists():
            db_path.unlink()
        result = subprocess.run(
            [sys.executable, "-c", script],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == -signal.SIGKILL, f"{boundary}: {result.stderr}"
        resumed = __import__("job_kit.run", fromlist=["resume_run"]).resume_run(
            "run",
            db_path,
            capabilities_provider=lambda: {"fake": Capabilities(adapter="fake")},
            backend_factory=lambda endpoint, **kwargs: BackendSelection(
                endpoint,
                "fake",
                type(
                    "ResumeBackend",
                    (),
                    {
                        "name": "fake",
                        "complete": lambda self, system, user, *, model, options=None: LLMResponse(
                            text="answer", model=model, input_tokens=1, output_tokens=1,
                            dropped_params=(), execution_controls_applied=(),
                            started_at="response-start", ended_at="response-end",
                        ),
                    },
                )(),
                "model",
            ),
        )
        assert resumed.jobs[0].state is JobState.ACCEPTED
        assert resumed.attempts
        gc_workspaces(db_path, "run", force=True)


def test_contract_exception_spends_the_budget_not_the_attempt_number(
    tmp_path: Path,
) -> None:
    """A contract failure must terminalize on the budget, not the attempt number.

    An unarmed process loss occupies an attempt number without consuming a
    retry, so the two diverge. Terminalizing on the attempt number fails a job
    while retries remain.
    """
    import inspect

    from job_kit import run as run_module

    source = inspect.getsource(run_module)
    call_args = re.findall(
        r"_terminal_state_after_attempt\(\s*\n?\s*job,\s*([A-Za-z_.]+),", source
    )
    assert call_args, "no _terminal_state_after_attempt call sites found"
    assert set(call_args) == {"reservation.budget_no", "budget_no"}, (
        "every terminal-state decision must be made on the budget number, "
        f"but these arguments were passed: {sorted(set(call_args))}"
    )


def test_migration_makes_a_pre_reservation_running_job_resumable(
    tmp_path: Path,
) -> None:
    """A ledger interrupted before reservations existed must still resume.

    Such a job sits in RUNNING with no reservation row, so recovery has
    nothing to reset and reserve_attempt would refuse it forever.
    """
    db_path = tmp_path / "run.sqlite3"
    store = JobStore(db_path)
    job = _job(tmp_path)
    store.create_run("run", [job], workspace_root=tmp_path / "workspaces")

    # Reproduce the pre-reservation interrupted shape: RUNNING, no reservation.
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE jobs SET state = 'running' WHERE run_id = ? AND id = ?",
            ("run", job.id),
        )
        conn.execute("DELETE FROM reservations WHERE run_id = ?", ("run",))

    reopened = JobStore(db_path)
    reopened.recover_reservations("run")
    record = reopened.get_job("run", job.id)
    assert record is not None
    assert record.state is JobState.PENDING, (
        "a RUNNING job with no reservation predates reservations and must be "
        f"returned to PENDING so the run can resume, got {record.state}"
    )


def test_tree_operations_are_not_bounded_by_the_probe_timeout(
    tmp_path: Path, monkeypatch
) -> None:
    """A worktree checkout must not share the fast probe's hang guard.

    ``git worktree add`` writes a whole working tree and ``git status
    --untracked-files=all`` walks one, so on a large repository either can run
    far longer than a ``rev-parse`` probe ever should.
    """
    from job_kit import workspace as workspace_module

    assert (
        workspace_module.GIT_TREE_TIMEOUT_S > workspace_module.GIT_PROBE_TIMEOUT_S
    ), "a tree operation needs a longer bound than a probe"

    recorded: list[tuple[tuple[str, ...], float | None]] = []
    real_run = workspace_module.subprocess.run

    def capture(cmd, **kwargs):
        recorded.append((tuple(cmd), kwargs.get("timeout")))
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(workspace_module.subprocess, "run", capture)

    repository = _git_repository(tmp_path / "repository")
    manager = WorkspaceManager(tmp_path / "workspaces", (_job(repository),))
    manager.prepare(_job(repository), 1)

    tree_calls = [
        (cmd, timeout)
        for cmd, timeout in recorded
        if "worktree" in cmd and "add" in cmd
    ]
    assert tree_calls, f"no worktree add was recorded: {recorded}"
    for cmd, timeout in tree_calls:
        assert timeout == workspace_module.GIT_TREE_TIMEOUT_S, (
            f"{' '.join(cmd)} ran under a {timeout}s bound; a checkout needs "
            f"{workspace_module.GIT_TREE_TIMEOUT_S}s"
        )
