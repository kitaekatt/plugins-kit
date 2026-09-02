"""Tests for Git worktree isolation and conservative workspace GC."""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Optional

import pytest

from llm_scripting_kit.completion import BackendSelection, Capabilities, LLMResponse

from job_kit import cli
from job_kit.model import Contract, Job, JobState, Prompt, WorkspaceSpec
from job_kit.run import default_workspace_root, resume_run, run_job, run_jobs
from job_kit.store import JobStore
import job_kit.run as run_module
import job_kit.workspace as workspace_module
from job_kit.workspace import (
    WORKSPACE_REASON_DECLINED,
    WORKSPACE_REASON_NONE,
    WorkspaceCreationError,
    WorkspaceDetectionError,
    gc_workspaces,
)


def _git(
    repository: Path, *arguments: str, input_text: Optional[str] = None
) -> str:
    """Run a local Git command in a test repository."""
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        input=input_text,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _git_repository(path: Path) -> tuple[Path, str]:
    """Create a committed temporary repository without using the user's tree."""
    path.mkdir()
    _git(path, "init", "--quiet")
    tracked = path / "tracked.txt"
    tracked.write_text("tracked\n", encoding="utf-8")
    component = path / "component"
    component.mkdir()
    (component / "component.txt").write_text("component\n", encoding="utf-8")
    blob = _git(path, "hash-object", "-w", str(tracked))
    component_blob = _git(path, "hash-object", "-w", str(component / "component.txt"))
    component_tree = _git(
        path,
        "mktree",
        input_text=f"100644 blob {component_blob}\tcomponent.txt\n",
    )
    tree = _git(
        path,
        "mktree",
        input_text=(
            f"040000 tree {component_tree}\tcomponent\n"
            f"100644 blob {blob}\ttracked.txt\n"
        ),
    )
    commit = _git(
        path,
        "-c",
        "user.name=job-kit-test",
        "-c",
        "user.email=job-kit-test@example.invalid",
        "commit-tree",
        tree,
        "-m",
        "initial",
    )
    head_ref = _git(path, "symbolic-ref", "HEAD")
    _git(path, "update-ref", head_ref, commit)
    return path, commit


def _advance_head(repository: Path, parent: str) -> str:
    """Create a new same-tree commit and move the repository HEAD to it."""
    tree = _git(repository, "rev-parse", f"{parent}^{{tree}}")
    commit = _git(
        repository,
        "-c",
        "user.name=job-kit-test",
        "-c",
        "user.email=job-kit-test@example.invalid",
        "commit-tree",
        tree,
        "-p",
        parent,
        "-m",
        "advanced",
    )
    head_ref = _git(repository, "symbolic-ref", "HEAD")
    _git(repository, "update-ref", head_ref, commit)
    return commit


def _advance_detached_head(worktree: Path, parent: str) -> str:
    """Create a clean commit in a detached test worktree."""
    tree = _git(worktree, "rev-parse", f"{parent}^{{tree}}")
    commit = _git(
        worktree,
        "-c",
        "user.name=job-kit-test",
        "-c",
        "user.email=job-kit-test@example.invalid",
        "commit-tree",
        tree,
        "-p",
        parent,
        "-m",
        "worktree-result",
    )
    _git(worktree, "update-ref", "HEAD", commit)
    return commit


class FakeBackend:
    """A hermetic completion backend for workspace tests."""

    name = "fake"

    def __init__(self) -> None:
        self.calls: list[object] = []

    def complete(
        self,
        system: str,
        user: str,
        *,
        model: str,
        options: object = None,
    ) -> LLMResponse:
        """Record the effective completion options and return success."""
        self.calls.append(options)
        return LLMResponse(
            text=f"answer for {user}",
            model=model,
            input_tokens=1,
            output_tokens=1,
            dropped_params=(),
            execution_controls_applied=(),
            started_at="2026-09-01T00:00:00Z",
            ended_at="2026-09-01T00:00:01Z",
        )


def _advertisement() -> dict[str, Capabilities]:
    """Return the single advertised fake adapter."""
    return {"fake": Capabilities(adapter="fake")}


def _factory_for(backend: FakeBackend) -> Callable[..., BackendSelection]:
    """Build a backend factory accepted by the selection seam."""
    def factory(endpoint: str, **_: object) -> BackendSelection:
        return BackendSelection(endpoint, "fake", backend, "fake-model")

    return factory


def _job(
    directory: Path,
    job_id: str,
    command: tuple[str, ...],
    workspace: Optional[WorkspaceSpec] = None,
) -> Job:
    """Build one job rooted at the supplied temporary repository."""
    return Job(
        id=job_id,
        prompt=Prompt(user=job_id),
        endpoint_preference=("fake-endpoint",),
        directory=directory,
        workspace=workspace,
        contract=Contract(command=command, directory=directory),
    )


def _print_cwd_command() -> tuple[str, ...]:
    """Return a contract that reports its actual working directory."""
    return (
        sys.executable,
        "-c",
        "from pathlib import Path; print(Path.cwd())",
    )


def test_default_workspace_root_rejects_path_escape() -> None:
    """A run identifier cannot redirect the default root outside its parent."""
    with pytest.raises(ValueError):
        default_workspace_root("../outside")


def test_git_attempts_use_separate_worktrees_and_run_start_head(
    tmp_path: Path,
) -> None:
    """Each Git job gets a detached worktree based on the observed run head."""
    repository, head = _git_repository(tmp_path / "repository")
    workspace_root = tmp_path / "workspaces"
    backend = FakeBackend()
    snapshot = run_jobs(
        [
            _job(
                repository,
                "first",
                _print_cwd_command(),
                workspace=WorkspaceSpec(base_ref=head),
            ),
            _job(repository, "second", _print_cwd_command()),
        ],
        tmp_path / "run.sqlite3",
        run_id="isolation-run",
        workspace_root=workspace_root,
        capabilities_provider=_advertisement,
        backend_factory=_factory_for(backend),
    )

    assert [record.state for record in snapshot.jobs] == [
        JobState.ACCEPTED,
        JobState.ACCEPTED,
    ]
    attempts = snapshot.attempts
    assert len(attempts) == 2
    paths = [attempt.workspace for attempt in attempts]
    assert all(path is not None for path in paths)
    assert paths[0] != paths[1]
    assert all(path is not None and path.is_dir() for path in paths)
    assert [attempt.base_ref for attempt in attempts] == [head, head]
    assert all(attempt.workspace_status == "isolated" for attempt in attempts)
    assert all(
        attempt.acceptance is not None
        and attempt.acceptance.directory == attempt.workspace
        for attempt in attempts
    )
    assert all(
        getattr(options, "cwd", None) == attempt.workspace
        for options, attempt in zip(backend.calls, attempts)
    )
    assert snapshot.run.workspace_root == workspace_root.resolve()
    assert _git(repository, "rev-parse", "HEAD") == head


def test_non_git_job_records_explicit_no_isolation(tmp_path: Path) -> None:
    """A non-Git directory keeps its declared cwd and records the reason."""
    directory = tmp_path / "plain"
    directory.mkdir()
    workspace_root = tmp_path / "workspaces"
    backend = FakeBackend()
    snapshot = run_jobs(
        [_job(directory, "plain-job", _print_cwd_command())],
        tmp_path / "run.sqlite3",
        run_id="plain-run",
        workspace_root=workspace_root,
        capabilities_provider=_advertisement,
        backend_factory=_factory_for(backend),
    )

    attempt = snapshot.attempts[0]
    assert snapshot.jobs[0].state is JobState.ACCEPTED
    assert attempt.workspace is None
    assert attempt.base_ref is None
    assert attempt.workspace_status == "none"
    assert attempt.workspace_reason is not None
    assert "no git repository" in attempt.workspace_reason
    assert attempt.acceptance is not None
    assert attempt.acceptance.directory == directory.resolve()
    assert not workspace_root.exists()
    assert json.loads(json.dumps(snapshot.to_mapping()))["attempts"][0][
        "workspace_status"
    ] == "none"


def test_git_job_can_decline_isolation_with_a_distinct_reason(
    tmp_path: Path,
) -> None:
    """A job may keep a Git cwd when it explicitly declines isolation."""
    repository, _ = _git_repository(tmp_path / "repository")
    declined = run_jobs(
        [
            _job(
                repository,
                "declined",
                _print_cwd_command(),
                workspace=WorkspaceSpec(isolate=False),
            )
        ],
        tmp_path / "declined.sqlite3",
        run_id="declined-run",
        workspace_root=tmp_path / "declined-workspaces",
        capabilities_provider=_advertisement,
        backend_factory=_factory_for(FakeBackend()),
    )

    plain = tmp_path / "plain"
    plain.mkdir()
    fallback = run_jobs(
        [_job(plain, "fallback", _print_cwd_command())],
        tmp_path / "fallback.sqlite3",
        run_id="fallback-run",
        workspace_root=tmp_path / "fallback-workspaces",
        capabilities_provider=_advertisement,
        backend_factory=_factory_for(FakeBackend()),
    )

    declined_attempt = declined.attempts[0]
    fallback_attempt = fallback.attempts[0]
    assert declined.jobs[0].state is JobState.ACCEPTED
    assert declined_attempt.workspace is None
    assert declined_attempt.base_ref is None
    assert declined_attempt.workspace_status == "none"
    assert declined_attempt.workspace_reason == WORKSPACE_REASON_DECLINED
    assert declined_attempt.workspace_reason != fallback_attempt.workspace_reason
    assert declined_attempt.workspace_reason != WORKSPACE_REASON_NONE
    assert declined_attempt.acceptance is not None
    assert declined_attempt.acceptance.directory == repository.resolve()


def test_declared_repository_subdirectory_is_preserved_in_worktree(
    tmp_path: Path,
) -> None:
    """The effective cwd keeps the declared path relative to the repo root."""
    repository, _ = _git_repository(tmp_path / "repository")
    declared = repository / "component"
    backend = FakeBackend()
    snapshot = run_jobs(
        [_job(declared, "subdirectory", _print_cwd_command())],
        tmp_path / "run.sqlite3",
        run_id="subdirectory-run",
        workspace_root=tmp_path / "workspaces",
        capabilities_provider=_advertisement,
        backend_factory=_factory_for(backend),
    )

    attempt = snapshot.attempts[0]
    assert attempt.workspace is not None
    assert attempt.acceptance is not None
    expected_directory = (attempt.workspace / "component").resolve()
    assert attempt.acceptance.directory == expected_directory
    assert getattr(backend.calls[0], "cwd", None) == expected_directory
    assert attempt.acceptance.stdout.strip() == str(expected_directory)


def test_git_workspace_creation_failure_fails_without_an_attempt_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A detected repository failure is durable without faking a seam call."""
    repository, _ = _git_repository(tmp_path / "repository")

    def fail_creation(repo_root: Path, path: Path, base_ref: str) -> Path:
        raise WorkspaceCreationError("test worktree creation failure")

    monkeypatch.setattr(workspace_module, "create_worktree", fail_creation)
    snapshot = run_jobs(
        [_job(repository, "failed", _print_cwd_command())],
        tmp_path / "run.sqlite3",
        run_id="creation-failure-run",
        workspace_root=tmp_path / "workspaces",
        capabilities_provider=_advertisement,
        backend_factory=_factory_for(FakeBackend()),
    )

    assert snapshot.jobs[0].state is JobState.FAILED
    assert snapshot.jobs[0].error == "test worktree creation failure"
    assert snapshot.attempts == ()


def test_git_detection_failure_fails_closed_without_an_attempt_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Git operational error is not treated as a non-Git fallback."""
    repository, _ = _git_repository(tmp_path / "repository")

    def fail_detection(directory: Path) -> Optional[Path]:
        raise WorkspaceDetectionError("test Git detection failure")

    monkeypatch.setattr(workspace_module, "detect_repo_root", fail_detection)
    snapshot = run_jobs(
        [_job(repository, "detection-failure", _print_cwd_command())],
        tmp_path / "run.sqlite3",
        run_id="detection-failure-run",
        workspace_root=tmp_path / "workspaces",
        capabilities_provider=_advertisement,
        backend_factory=_factory_for(FakeBackend()),
    )

    assert snapshot.jobs[0].state is JobState.FAILED
    assert snapshot.jobs[0].error == "test Git detection failure"
    assert snapshot.attempts == ()


def test_workspace_creation_interruption_records_a_pre_seam_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hard interruption during preparation cannot leave the job running."""
    repository, _ = _git_repository(tmp_path / "repository")
    db_path = tmp_path / "run.sqlite3"

    def interrupt_creation(repo_root: Path, path: Path, base_ref: str) -> Path:
        raise KeyboardInterrupt

    monkeypatch.setattr(workspace_module, "create_worktree", interrupt_creation)
    with pytest.raises(KeyboardInterrupt):
        run_jobs(
            [_job(repository, "creation-interrupted", _print_cwd_command())],
            db_path,
            run_id="creation-interruption-run",
            workspace_root=tmp_path / "workspaces",
            capabilities_provider=_advertisement,
            backend_factory=_factory_for(FakeBackend()),
        )

    snapshot = JobStore(db_path).snapshot("creation-interruption-run")
    assert snapshot.jobs[0].state is JobState.FAILED
    assert snapshot.jobs[0].error == "KeyboardInterrupt"
    assert snapshot.attempts == ()


def test_resume_uses_the_run_workspace_root_for_a_pending_git_job(
    tmp_path: Path,
) -> None:
    """Resume keeps the run root and creates a fresh pending-attempt worktree."""
    repository, _ = _git_repository(tmp_path / "repository")
    first = _job(repository, "first", _print_cwd_command())
    second = replace(
        _job(repository, "second", _print_cwd_command()),
        max_attempts=2,
    )
    db_path = tmp_path / "run.sqlite3"
    workspace_root = tmp_path / "workspaces"
    backend = FakeBackend()
    completion_calls = 0
    original_complete = backend.complete

    def interrupting_complete(
        system: str,
        user: str,
        *,
        model: str,
        options: object = None,
    ) -> LLMResponse:
        nonlocal completion_calls
        completion_calls += 1
        if completion_calls == 2:
            raise KeyboardInterrupt
        return original_complete(system, user, model=model, options=options)

    backend.complete = interrupting_complete

    with pytest.raises(KeyboardInterrupt):
        run_jobs(
            [first, second],
            db_path,
            run_id="resume-run",
            workspace_root=workspace_root,
            capabilities_provider=_advertisement,
            backend_factory=_factory_for(backend),
        )

    interrupted = JobStore(db_path).snapshot("resume-run")
    assert interrupted.jobs[0].state is JobState.ACCEPTED
    assert interrupted.jobs[1].state is JobState.PENDING
    assert len(interrupted.attempts) == 2
    assert interrupted.attempts[0].workspace is not None
    initial_head = interrupted.attempts[0].base_ref
    assert initial_head is not None
    advanced_head = _advance_head(repository, initial_head)
    assert advanced_head != initial_head

    resumed = resume_run(
        "resume-run",
        db_path,
        capabilities_provider=_advertisement,
        backend_factory=_factory_for(backend),
    )
    assert [record.state for record in resumed.jobs] == [
        JobState.ACCEPTED,
        JobState.ACCEPTED,
    ]
    assert len(resumed.attempts) == 3
    assert resumed.attempts[2].workspace is not None
    assert resumed.attempts[2].workspace != resumed.attempts[0].workspace
    assert resumed.attempts[2].workspace != resumed.attempts[1].workspace
    assert resumed.attempts[2].base_ref == initial_head
    assert _git(resumed.attempts[2].workspace, "rev-parse", "HEAD") == initial_head
    assert _git(repository, "rev-parse", "HEAD") == advanced_head
    assert resumed.attempts[2].acceptance is not None
    assert resumed.attempts[2].acceptance.directory == resumed.attempts[2].workspace


def test_direct_run_job_inherits_the_recorded_custom_workspace_root(
    tmp_path: Path,
) -> None:
    """The public one-job API uses the run's persisted root when no override is given."""
    repository, head = _git_repository(tmp_path / "repository")
    db_path = tmp_path / "run.sqlite3"
    workspace_root = tmp_path / "custom-workspaces"
    job = _job(repository, "direct", _print_cwd_command())
    store = JobStore(db_path)
    store.create_run(
        "direct-run",
        [job],
        workspace_root=workspace_root,
    )
    advanced_head = _advance_head(repository, head)

    attempt = run_job(
        store,
        "direct-run",
        job,
        capabilities_provider=_advertisement,
        backend_factory=_factory_for(FakeBackend()),
    )

    assert attempt.workspace is not None
    assert attempt.workspace.is_relative_to(workspace_root.resolve())
    assert attempt.base_ref == head
    assert _git(attempt.workspace, "rev-parse", "HEAD") == head
    assert _git(repository, "rev-parse", "HEAD") == advanced_head
    report = gc_workspaces(db_path, "direct-run")
    assert len(report.removed) == 1
    assert not attempt.workspace.exists()


def test_direct_run_job_binds_an_unset_workspace_root_for_gc(
    tmp_path: Path,
) -> None:
    """A direct run binds its chosen root before creating a worktree."""
    repository, _ = _git_repository(tmp_path / "repository")
    db_path = tmp_path / "run.sqlite3"
    workspace_root = tmp_path / "bound-workspaces"
    job = _job(repository, "bound", _print_cwd_command())
    store = JobStore(db_path)
    store.create_run("bound-run", [job])

    attempt = run_job(
        store,
        "bound-run",
        job,
        workspace_root=workspace_root,
        capabilities_provider=_advertisement,
        backend_factory=_factory_for(FakeBackend()),
    )

    assert attempt.workspace is not None
    assert store.snapshot("bound-run").run.workspace_root == workspace_root.resolve()
    report = gc_workspaces(db_path, "bound-run")
    assert len(report.removed) == 1
    assert not attempt.workspace.exists()


def test_gc_removes_a_clean_worktree_with_a_new_detached_commit(
    tmp_path: Path,
) -> None:
    """A clean terminal worktree is eligible even when its HEAD advanced."""
    repository, head = _git_repository(tmp_path / "repository")
    db_path = tmp_path / "run.sqlite3"
    snapshot = run_jobs(
        [_job(repository, "committed", _print_cwd_command())],
        db_path,
        run_id="committed-run",
        workspace_root=tmp_path / "workspaces",
        capabilities_provider=_advertisement,
        backend_factory=_factory_for(FakeBackend()),
    )
    attempt = snapshot.attempts[0]
    assert attempt.workspace is not None
    committed_head = _advance_detached_head(attempt.workspace, head)
    assert committed_head != head

    report = gc_workspaces(db_path, "committed-run")

    assert len(report.removed) == 1
    assert report.refused == ()
    assert not attempt.workspace.exists()


def test_gc_repairs_a_removal_started_before_an_interruption(
    tmp_path: Path,
) -> None:
    """A persisted cleanup intent repairs the ledger after Git already removed it."""
    repository, _ = _git_repository(tmp_path / "repository")
    db_path = tmp_path / "run.sqlite3"
    snapshot = run_jobs(
        [_job(repository, "interrupted-gc", _print_cwd_command())],
        db_path,
        run_id="interrupted-gc-run",
        workspace_root=tmp_path / "workspaces",
        capabilities_provider=_advertisement,
        backend_factory=_factory_for(FakeBackend()),
    )
    attempt = snapshot.attempts[0]
    assert attempt.id is not None
    assert attempt.workspace is not None
    store = JobStore(db_path)
    store.mark_workspace_removing(attempt.id)
    _git(repository, "worktree", "remove", str(attempt.workspace))

    report = gc_workspaces(db_path, "interrupted-gc-run")

    assert len(report.removed) == 1
    assert report.removed[0].reason == "clean worktree removal was already completed"
    assert JobStore(db_path).snapshot("interrupted-gc-run").attempts[0].workspace_status == "removed"


def test_gc_prunes_a_registration_after_directory_removal_interruption(
    tmp_path: Path,
) -> None:
    """A missing directory with a prunable registration converges to removed."""
    repository, _ = _git_repository(tmp_path / "repository")
    db_path = tmp_path / "run.sqlite3"
    snapshot = run_jobs(
        [_job(repository, "prunable-gc", _print_cwd_command())],
        db_path,
        run_id="prunable-gc-run",
        workspace_root=tmp_path / "workspaces",
        capabilities_provider=_advertisement,
        backend_factory=_factory_for(FakeBackend()),
    )
    attempt = snapshot.attempts[0]
    assert attempt.id is not None
    assert attempt.workspace is not None
    store = JobStore(db_path)
    store.mark_workspace_removing(attempt.id)
    shutil.rmtree(attempt.workspace)

    listing = _git(repository, "worktree", "list", "--porcelain")
    assert str(attempt.workspace) in listing
    assert "prunable" in listing

    report = gc_workspaces(db_path, "prunable-gc-run")

    assert len(report.removed) == 1
    assert report.refused == ()
    assert "pruned" in report.removed[0].reason
    stored = JobStore(db_path).snapshot("prunable-gc-run").attempts[0]
    assert stored.workspace_status == "removed"
    assert stored.workspace_removal_forced is False
    assert not attempt.workspace.exists()
    assert str(attempt.workspace) not in _git(
        repository, "worktree", "list", "--porcelain"
    )

    second = gc_workspaces(db_path, "prunable-gc-run")
    assert second.removed == ()
    assert second.refused == ()
    assert len(second.skipped) == 1


def test_gc_ignores_workspaces_recorded_by_another_run_with_a_shared_root(
    tmp_path: Path,
) -> None:
    """An override root may safely contain workspaces from multiple runs."""
    repository, _ = _git_repository(tmp_path / "repository")
    db_path = tmp_path / "run.sqlite3"
    workspace_root = tmp_path / "shared-workspaces"
    first = run_jobs(
        [_job(repository, "first", _print_cwd_command())],
        db_path,
        run_id="first-run",
        workspace_root=workspace_root,
        capabilities_provider=_advertisement,
        backend_factory=_factory_for(FakeBackend()),
    )
    second = run_jobs(
        [_job(repository, "second", _print_cwd_command())],
        db_path,
        run_id="second-run",
        workspace_root=workspace_root,
        capabilities_provider=_advertisement,
        backend_factory=_factory_for(FakeBackend()),
    )
    first_workspace = first.attempts[0].workspace
    second_workspace = second.attempts[0].workspace
    assert first_workspace is not None
    assert second_workspace is not None

    report = gc_workspaces(db_path, "first-run")

    assert [entry.job_id for entry in report.removed] == ["first"]
    assert report.refused == ()
    assert not first_workspace.exists()
    assert second_workspace.is_dir()


def test_seam_interruption_after_workspace_creation_is_recorded(
    tmp_path: Path,
) -> None:
    """A hard interruption after preparation still leaves one attempt row."""
    repository, _ = _git_repository(tmp_path / "repository")
    db_path = tmp_path / "run.sqlite3"

    class InterruptingBackend(FakeBackend):
        """Raise a process-style interruption from the completion seam."""

        def complete(
            self,
            system: str,
            user: str,
            *,
            model: str,
            options: object = None,
        ) -> LLMResponse:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_jobs(
            [_job(repository, "interrupted", _print_cwd_command())],
            db_path,
            run_id="seam-interruption-run",
            workspace_root=tmp_path / "workspaces",
            capabilities_provider=_advertisement,
            backend_factory=_factory_for(InterruptingBackend()),
        )

    snapshot = JobStore(db_path).snapshot("seam-interruption-run")
    assert snapshot.jobs[0].state is JobState.FAILED
    assert len(snapshot.attempts) == 1
    assert snapshot.attempts[0].workspace is not None
    assert snapshot.attempts[0].error_code == "execution"
    assert snapshot.attempts[0].error_message == "KeyboardInterrupt"


def test_response_recording_interruption_is_recorded_after_seam(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hard interruption while copying a response still leaves one attempt row."""
    repository, _ = _git_repository(tmp_path / "repository")
    db_path = tmp_path / "run.sqlite3"
    backend = FakeBackend()

    def interrupt_response(**_: object) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(run_module, "_response_attempt", interrupt_response)
    with pytest.raises(KeyboardInterrupt):
        run_jobs(
            [_job(repository, "response-interrupted", _print_cwd_command())],
            db_path,
            run_id="response-interruption-run",
            workspace_root=tmp_path / "workspaces",
            capabilities_provider=_advertisement,
            backend_factory=_factory_for(backend),
        )

    snapshot = JobStore(db_path).snapshot("response-interruption-run")
    assert snapshot.jobs[0].state is JobState.FAILED
    assert len(snapshot.attempts) == 1
    assert len(backend.calls) == 1
    assert snapshot.attempts[0].workspace is not None
    assert snapshot.attempts[0].error_code == "response_interrupted"
    assert snapshot.attempts[0].error_message == "KeyboardInterrupt"


def test_contract_interruption_after_seam_is_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A contract interruption does not cause a second seam call on resume."""
    repository, _ = _git_repository(tmp_path / "repository")
    db_path = tmp_path / "run.sqlite3"

    def interrupt_contract(
        contract: Contract,
        *,
        directory: Optional[Path] = None,
        timeout_s: Optional[float] = None,
        response_text: str = "",
        context: object = None,
    ) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(run_module, "run_contract", interrupt_contract)
    with pytest.raises(KeyboardInterrupt):
        run_jobs(
            [_job(repository, "contract-interrupted", _print_cwd_command())],
            db_path,
            run_id="contract-interruption-run",
            workspace_root=tmp_path / "workspaces",
            capabilities_provider=_advertisement,
            backend_factory=_factory_for(FakeBackend()),
        )

    snapshot = JobStore(db_path).snapshot("contract-interruption-run")
    assert snapshot.jobs[0].state is JobState.FAILED
    assert len(snapshot.attempts) == 1
    assert snapshot.attempts[0].error_code == "contract_interrupted"
    assert snapshot.attempts[0].acceptance is None


def test_contract_exception_after_seam_is_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unexpected contract error still records the completed seam attempt."""
    repository, _ = _git_repository(tmp_path / "repository")
    db_path = tmp_path / "run.sqlite3"

    def failing_contract(
        contract: Contract,
        *,
        directory: Optional[Path] = None,
        timeout_s: Optional[float] = None,
        response_text: str = "",
        context: object = None,
    ) -> object:
        raise RuntimeError("contract bookkeeping failure")

    monkeypatch.setattr(run_module, "run_contract", failing_contract)
    snapshot = run_jobs(
        [_job(repository, "contract-failed", _print_cwd_command())],
        db_path,
        run_id="contract-failure-run",
        workspace_root=tmp_path / "workspaces",
        capabilities_provider=_advertisement,
        backend_factory=_factory_for(FakeBackend()),
    )

    assert snapshot.jobs[0].state is JobState.FAILED
    assert len(snapshot.attempts) == 1
    assert snapshot.attempts[0].error_code == "contract"
    assert snapshot.attempts[0].error_message == "contract bookkeeping failure"
    assert snapshot.attempts[0].acceptance is None


def test_gc_refuses_tracked_modifications(tmp_path: Path) -> None:
    """A tracked modification keeps an accepted worktree for diagnosis."""
    repository, _ = _git_repository(tmp_path / "repository")
    dirty_command = (
        sys.executable,
        "-c",
        "from pathlib import Path; Path('tracked.txt').write_text('keep', encoding='utf-8')",
    )
    db_path = tmp_path / "run.sqlite3"
    snapshot = run_jobs(
        [_job(repository, "dirty", dirty_command)],
        db_path,
        run_id="dirty-run",
        workspace_root=tmp_path / "workspaces",
        capabilities_provider=_advertisement,
        backend_factory=_factory_for(FakeBackend()),
    )
    workspace = snapshot.attempts[0].workspace
    assert workspace is not None

    report = gc_workspaces(db_path, "dirty-run")

    assert report.removed == ()
    assert len(report.refused) == 1
    assert report.refused[0].reason == "worktree is dirty"
    assert workspace.is_dir()
    stored = JobStore(db_path).snapshot("dirty-run").attempts[0]
    assert stored.workspace_status == "isolated"
    assert stored.workspace_removed_at is None


def test_gc_refuses_untracked_files(tmp_path: Path) -> None:
    """An untracked file keeps an accepted worktree for diagnosis."""
    repository, _ = _git_repository(tmp_path / "repository")
    dirty_command = (
        sys.executable,
        "-c",
        "from pathlib import Path; Path('untracked.txt').write_text('keep', encoding='utf-8')",
    )
    db_path = tmp_path / "run.sqlite3"
    snapshot = run_jobs(
        [_job(repository, "untracked", dirty_command)],
        db_path,
        run_id="untracked-run",
        workspace_root=tmp_path / "workspaces",
        capabilities_provider=_advertisement,
        backend_factory=_factory_for(FakeBackend()),
    )
    workspace = snapshot.attempts[0].workspace
    assert workspace is not None

    report = gc_workspaces(db_path, "untracked-run")

    assert report.removed == ()
    assert len(report.refused) == 1
    assert report.refused[0].reason == "worktree is dirty"
    assert workspace.is_dir()
    assert (workspace / "untracked.txt").is_file()


def test_gc_ignores_ignored_files_as_dirty_worktree_content(tmp_path: Path) -> None:
    """Ignored generated content alone does not block conservative GC."""
    repository, _ = _git_repository(tmp_path / "repository")
    ignored = tmp_path / "ignored-patterns"
    ignored.write_text("ignored.txt\n", encoding="utf-8")
    _git(repository, "config", "core.excludesFile", str(ignored))
    dirty_command = (
        sys.executable,
        "-c",
        "from pathlib import Path; Path('ignored.txt').write_text('keep', encoding='utf-8')",
    )
    db_path = tmp_path / "run.sqlite3"
    snapshot = run_jobs(
        [_job(repository, "ignored", dirty_command)],
        db_path,
        run_id="ignored-run",
        workspace_root=tmp_path / "workspaces",
        capabilities_provider=_advertisement,
        backend_factory=_factory_for(FakeBackend()),
    )
    workspace = snapshot.attempts[0].workspace
    assert workspace is not None

    report = gc_workspaces(db_path, "ignored-run")

    assert len(report.removed) == 1
    assert report.refused == ()
    assert report.removed[0].reason == "clean worktree removed"
    assert not workspace.exists()
    stored = JobStore(db_path).snapshot("ignored-run").attempts[0]
    assert stored.workspace_status == "removed"
    assert stored.workspace_removal_forced is False


def test_gc_force_removes_dirty_worktree_and_records_forcing(
    tmp_path: Path, capsys: Any
) -> None:
    """Forced GC removes dirty content and records that fact in the attempt row."""
    repository, _ = _git_repository(tmp_path / "repository")
    dirty_command = (
        sys.executable,
        "-c",
        "from pathlib import Path; Path('dirty.txt').write_text('keep', encoding='utf-8')",
    )
    db_path = tmp_path / "run.sqlite3"
    snapshot = run_jobs(
        [_job(repository, "forced", dirty_command)],
        db_path,
        run_id="forced-run",
        workspace_root=tmp_path / "workspaces",
        capabilities_provider=_advertisement,
        backend_factory=_factory_for(FakeBackend()),
    )
    workspace = snapshot.attempts[0].workspace
    assert workspace is not None

    assert (
        cli.main(
            ["gc", "forced-run", "--force", "--store", str(db_path)]
        )
        == cli.EXIT_OK
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["counts"] == {"removed": 1, "refused": 0, "skipped": 0}
    assert payload["removed"][0]["reason"] == "forced worktree removed"
    assert not workspace.exists()
    stored = JobStore(db_path).snapshot("forced-run").attempts[0]
    assert stored.workspace_status == "removed"
    assert stored.workspace_removal_forced is True
    assert stored.workspace_removed_at is not None


def test_gc_refuses_a_non_terminal_attempt(tmp_path: Path) -> None:
    """A workspace attached to a non-terminal job is never reclaimed."""
    repository, _ = _git_repository(tmp_path / "repository")
    db_path = tmp_path / "run.sqlite3"
    snapshot = run_jobs(
        [_job(repository, "running", _print_cwd_command())],
        db_path,
        run_id="running-run",
        workspace_root=tmp_path / "workspaces",
        capabilities_provider=_advertisement,
        backend_factory=_factory_for(FakeBackend()),
    )
    workspace = snapshot.attempts[0].workspace
    assert workspace is not None
    with sqlite3.connect(str(db_path)) as connection:
        connection.execute(
            "UPDATE jobs SET state = ? WHERE run_id = ? AND id = ?",
            (JobState.PENDING.value, "running-run", "running"),
        )

    report = gc_workspaces(db_path, "running-run")

    assert report.removed == ()
    assert len(report.refused) == 1
    assert "non-terminal" in report.refused[0].reason
    assert workspace.is_dir()


def test_gc_accepted_only_and_cli_report(tmp_path: Path, capsys: Any) -> None:
    """Accepted-only GC removes accepted work and reports other terminal jobs as skipped."""
    repository, _ = _git_repository(tmp_path / "repository")
    db_path = tmp_path / "run.sqlite3"
    accepted = _job(repository, "accepted", _print_cwd_command())
    rejected = _job(
        repository,
        "rejected",
        (sys.executable, "-c", "import sys; sys.exit(7)"),
    )
    snapshot = run_jobs(
        [accepted, rejected],
        db_path,
        run_id="gc-run",
        workspace_root=tmp_path / "workspaces",
        capabilities_provider=_advertisement,
        backend_factory=_factory_for(FakeBackend()),
    )
    accepted_workspace = snapshot.attempts[0].workspace
    rejected_workspace = snapshot.attempts[1].workspace
    assert accepted_workspace is not None
    assert rejected_workspace is not None

    assert (
        cli.main(
            [
                "gc",
                "gc-run",
                "--accepted-only",
                "--store",
                str(db_path),
            ]
        )
        == cli.EXIT_OK
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["counts"] == {"removed": 1, "refused": 0, "skipped": 1}
    assert not accepted_workspace.exists()
    assert rejected_workspace.is_dir()
    stored = JobStore(db_path).snapshot("gc-run")
    assert stored.attempts[0].workspace_status == "removed"
    assert stored.attempts[0].workspace_removed_at is not None
    assert stored.attempts[1].workspace_removed_at is None

    report = gc_workspaces(db_path, "gc-run")
    assert len(report.removed) == 1
    assert report.removed[0].job_id == "rejected"
    assert not rejected_workspace.exists()
