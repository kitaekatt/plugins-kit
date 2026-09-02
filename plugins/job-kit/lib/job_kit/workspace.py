"""Git worktree isolation and conservative workspace garbage collection."""

from __future__ import annotations

import hashlib
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence

from .model import Job, JobRecord, JobState, RunRecord, TERMINAL_STATES
from .store import JobStore, StoreError


WORKSPACE_STATUSES = frozenset({"isolated", "none", "removing", "removed"})
WORKSPACE_REASON_NONE = "no git repository detected from the declared directory"
WORKSPACE_REASON_DECLINED = "workspace isolation declined by the job"

# Git documents no concurrency promise for `worktree add` against one
# repository, so creations are serialised per repository root. The lock is
# keyed on the resolved root: two runs over different repositories never wait
# on each other.
_WORKTREE_LOCK_REGISTRY_GUARD = threading.Lock()
_WORKTREE_LOCK_REGISTRY: dict[Path, threading.Lock] = {}


def _worktree_lock(repo_root: Path) -> threading.Lock:
    """Return the process-wide creation lock for one repository root."""
    with _WORKTREE_LOCK_REGISTRY_GUARD:
        return _WORKTREE_LOCK_REGISTRY.setdefault(repo_root, threading.Lock())


class WorkspaceError(RuntimeError):
    """Base class for workspace preparation failures."""


class WorkspaceCreationError(WorkspaceError):
    """A detected Git repository could not yield a worktree."""


class WorkspaceDetectionError(WorkspaceError):
    """Git could not determine whether the declared directory is a repository."""


@dataclass(frozen=True)
class WorkspaceResolution:
    """The workspace selected for one attempt."""

    path: Optional[Path]
    base_ref: Optional[str]
    repo_root: Optional[Path]
    status: str
    reason: str
    relative_directory: Path = Path()

    def __post_init__(self) -> None:
        if self.path is not None:
            object.__setattr__(self, "path", Path(self.path).expanduser().resolve())
        if self.repo_root is not None:
            object.__setattr__(
                self, "repo_root", Path(self.repo_root).expanduser().resolve()
            )
        object.__setattr__(self, "relative_directory", Path(self.relative_directory))
        if self.status not in {"isolated", "none"}:
            raise ValueError("workspace resolution status must be isolated or none")
        if self.status == "isolated" and (self.path is None or self.base_ref is None):
            raise ValueError("an isolated workspace requires a path and base_ref")
        if self.status == "none" and self.path is not None:
            raise ValueError("a non-isolated workspace cannot have a path")

    @property
    def working_directory(self) -> Path:
        """Return the declared repository subdirectory inside the worktree."""
        if self.path is None:
            raise ValueError("a non-isolated workspace has no isolated working directory")
        return (self.path / self.relative_directory).resolve()


@dataclass(frozen=True)
class _WorkspacePlan:
    """Run-start workspace facts for one job."""

    repo_root: Optional[Path]
    base_ref: Optional[str]
    relative_directory: Path
    status: str
    reason: str
    error: Optional[str] = None


def _git_result(
    args: Sequence[str], *, cwd: Path
) -> Optional[subprocess.CompletedProcess[str]]:
    """Run one local Git command without raising for a missing executable."""
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError:
        return None


def _git_detail(result: Optional[subprocess.CompletedProcess[str]]) -> str:
    """Return bounded deterministic detail for a failed Git command."""
    if result is None:
        return "git command could not be started"
    detail = (result.stderr or result.stdout).strip()
    if not detail:
        detail = f"git exited with code {result.returncode}"
    return detail[:2000]


def _git_detection_result(
    directory: Path,
) -> subprocess.CompletedProcess[str]:
    """Run the repository probe while preserving operational Git failures."""
    try:
        return subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(directory),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        raise WorkspaceDetectionError(
            f"could not run Git repository detection in {directory}: {exc}"
        ) from exc


def detect_repo_root(directory: Path) -> Optional[Path]:
    """Detect a repository root from the job's declared directory."""
    declared = Path(directory).expanduser().resolve()
    result = _git_detection_result(declared)
    if result.returncode != 0:
        detail = _git_detail(result)
        if "not a git repository" in detail.lower():
            return None
        raise WorkspaceDetectionError(
            f"Git repository detection failed in {declared}: {detail}"
        )
    if not result.stdout.strip():
        return None
    raw_root = result.stdout.strip()
    return Path(raw_root).expanduser().resolve()


def resolve_base_ref(repo_root: Path, requested: Optional[str] = None) -> str:
    """Resolve a requested ref or observe the repository HEAD as a commit."""
    root = Path(repo_root).expanduser().resolve()
    if requested is None:
        args: tuple[str, ...] = ("rev-parse", "HEAD")
    else:
        candidate = str(requested).strip()
        if not candidate:
            raise WorkspaceCreationError("workspace base_ref must not be empty")
        args = ("rev-parse", "--verify", f"{candidate}^{{commit}}")
    result = _git_result(args, cwd=root)
    if result is None or result.returncode != 0:
        detail = _git_detail(result)
        raise WorkspaceCreationError(
            f"could not resolve workspace base ref in {root}: {detail}"
        )
    resolved = result.stdout.strip()
    if not resolved:
        raise WorkspaceCreationError(f"Git returned no workspace base ref in {root}")
    return resolved


def _job_path_component(job_id: str) -> str:
    """Make a stable, bounded path component from a caller job identifier."""
    safe = "".join(
        character
        if character.isascii() and (character.isalnum() or character in "._-")
        else "_"
        for character in job_id
    ).strip("._")
    if not safe:
        safe = "job"
    digest = hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:12]
    return f"{safe[:80]}-{digest}"


def create_worktree(repo_root: Path, workspace_path: Path, base_ref: str) -> Path:
    """Create one detached worktree or raise a typed creation failure."""
    root = Path(repo_root).expanduser().resolve()
    path = Path(workspace_path).expanduser().resolve()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise WorkspaceCreationError(
            f"could not create workspace parent {path.parent}: {exc}"
        ) from exc
    with _worktree_lock(root):
        if path.exists():
            raise WorkspaceCreationError(f"workspace path already exists: {path}")
        result = _git_result(
            ("-C", str(root), "worktree", "add", "--detach", str(path), base_ref),
            cwd=root,
        )
    if result is None or result.returncode != 0:
        raise WorkspaceCreationError(
            f"git worktree creation failed for {path}: {_git_detail(result)}"
        )
    if not path.is_dir():
        raise WorkspaceCreationError(
            f"git worktree creation returned success without a directory: {path}"
        )
    return path


class WorkspaceManager:
    """Capture run-start Git facts and create attempt worktrees."""

    def __init__(
        self,
        workspace_root: Path,
        jobs: Sequence[Job],
        *,
        base_refs: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        observed_heads: dict[Path, str] = {}
        captured_refs = dict(base_refs or {})
        self._plans: dict[str, _WorkspacePlan] = {}
        for job in jobs:
            self._plans[job.id] = self._plan_job(
                job, observed_heads, captured_refs.get(job.id)
            )

    @staticmethod
    def _plan_job(
        job: Job,
        observed_heads: dict[Path, str],
        captured_base_ref: Optional[str] = None,
    ) -> _WorkspacePlan:
        """Capture one job's repository and run-start base-ref facts."""
        if job.workspace is not None and not job.workspace.isolate:
            return _WorkspacePlan(
                repo_root=None,
                base_ref=None,
                relative_directory=Path(),
                status="none",
                reason=WORKSPACE_REASON_DECLINED,
            )
        try:
            repo_root = detect_repo_root(job.declared_directory)
        except WorkspaceError as exc:
            return _WorkspacePlan(
                repo_root=None,
                base_ref=None,
                relative_directory=Path(),
                status="isolated",
                reason="Git repository detection failed",
                error=str(exc),
            )
        if repo_root is None:
            return _WorkspacePlan(
                repo_root=None,
                base_ref=None,
                relative_directory=Path(),
                status="none",
                reason=WORKSPACE_REASON_NONE,
            )
        try:
            relative_directory = job.declared_directory.relative_to(repo_root)
        except ValueError as exc:
            return _WorkspacePlan(
                repo_root=repo_root,
                base_ref=None,
                relative_directory=Path(),
                status="isolated",
                reason="Git repository detected",
                error=f"declared directory is outside detected repository root: {exc}",
            )
        requested = job.workspace.base_ref if job.workspace is not None else None
        try:
            if captured_base_ref is not None:
                base_ref = captured_base_ref
            elif requested is None:
                base_ref = observed_heads.get(repo_root)
                if base_ref is None:
                    base_ref = resolve_base_ref(repo_root)
                    observed_heads[repo_root] = base_ref
            else:
                base_ref = resolve_base_ref(repo_root, requested)
        except WorkspaceError as exc:
            return _WorkspacePlan(
                repo_root=repo_root,
                base_ref=None,
                relative_directory=relative_directory,
                status="isolated",
                reason="Git repository detected",
                error=str(exc),
            )
        return _WorkspacePlan(
            repo_root=repo_root,
            base_ref=base_ref,
            relative_directory=relative_directory,
            status="isolated",
            reason="Git detached worktree",
        )

    @property
    def base_refs(self) -> dict[str, str]:
        """Return resolved base refs suitable for durable run storage."""
        return {
            job_id: plan.base_ref
            for job_id, plan in self._plans.items()
            if plan.status == "isolated"
            and plan.error is None
            and plan.base_ref is not None
        }

    def _attempt_path(self, job: Job, attempt_no: int) -> Path:
        """Return a unique and root-confined path for one attempt."""
        if attempt_no <= 0:
            raise ValueError("attempt_no must be positive")
        candidate = (
            self.workspace_root
            / _job_path_component(job.id)
            / f"attempt-{attempt_no}-{uuid.uuid4().hex}"
        ).resolve()
        try:
            relative = candidate.relative_to(self.workspace_root)
        except ValueError as exc:
            raise WorkspaceCreationError(
                f"workspace path escapes the configured workspace root: {candidate}"
            ) from exc
        if not relative.parts:
            raise WorkspaceCreationError("workspace path must be below its root")
        return candidate

    def prepare(self, job: Job, attempt_no: int) -> WorkspaceResolution:
        """Create the worktree for an attempt or report explicit no-isolation."""
        plan = self._plans.get(job.id)
        if plan is None:
            plan = self._plan_job(job, {})
        if plan.error is not None:
            raise WorkspaceCreationError(plan.error)
        if plan.status == "none":
            return WorkspaceResolution(
                path=None,
                base_ref=None,
                repo_root=None,
                status="none",
                reason=plan.reason,
                relative_directory=plan.relative_directory,
            )
        if plan.repo_root is None or plan.base_ref is None:
            raise WorkspaceCreationError("Git workspace plan is incomplete")
        path = self._attempt_path(job, attempt_no)
        created = create_worktree(plan.repo_root, path, plan.base_ref)
        return WorkspaceResolution(
            path=created,
            base_ref=plan.base_ref,
            repo_root=plan.repo_root,
            status="isolated",
            reason=plan.reason,
            relative_directory=plan.relative_directory,
        )


def prepare_workspace(
    job: Job, workspace_root: Path, attempt_no: int = 1
) -> WorkspaceResolution:
    """Prepare one workspace using facts observed at this call's run start."""
    return WorkspaceManager(workspace_root, (job,)).prepare(job, attempt_no)


def capture_base_refs(jobs: Sequence[Job]) -> dict[str, str]:
    """Observe job base refs without creating any worktrees."""
    return WorkspaceManager(Path.cwd(), jobs).base_refs


@dataclass(frozen=True)
class WorkspaceGCEntry:
    """One workspace action reported by garbage collection."""

    run_id: str
    job_id: str
    attempt_id: int
    workspace: Path
    reason: str

    def to_mapping(self) -> dict[str, object]:
        """Return a JSON-compatible GC action."""
        return {
            "run_id": self.run_id,
            "job_id": self.job_id,
            "attempt_id": self.attempt_id,
            "workspace": str(self.workspace),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class WorkspaceGCReport:
    """The removed, refused and intentionally skipped workspace actions."""

    runs: tuple[str, ...]
    accepted_only: bool
    removed: tuple[WorkspaceGCEntry, ...] = ()
    refused: tuple[WorkspaceGCEntry, ...] = ()
    skipped: tuple[WorkspaceGCEntry, ...] = ()

    def to_mapping(self) -> dict[str, object]:
        """Return a JSON-compatible GC report."""
        return {
            "runs": list(self.runs),
            "accepted_only": self.accepted_only,
            "removed": [entry.to_mapping() for entry in self.removed],
            "refused": [entry.to_mapping() for entry in self.refused],
            "skipped": [entry.to_mapping() for entry in self.skipped],
            "counts": {
                "removed": len(self.removed),
                "refused": len(self.refused),
                "skipped": len(self.skipped),
            },
        }


def _workspace_repo_root(workspace: Path) -> tuple[Optional[Path], Optional[str]]:
    """Find the Git repository root that owns a recorded worktree."""
    result = _git_result(("rev-parse", "--show-toplevel"), cwd=workspace)
    if result is None or result.returncode != 0:
        return None, f"workspace is not a readable Git worktree: {_git_detail(result)}"
    raw_root = result.stdout.strip()
    if not raw_root:
        return None, "workspace Git root was empty"
    return Path(raw_root).expanduser().resolve(), None


def _registered_worktrees(repo_root: Path) -> tuple[set[Path], Optional[str]]:
    """Read the registered worktree paths from the owning repository."""
    result = _git_result(("worktree", "list", "--porcelain"), cwd=repo_root)
    if result is None or result.returncode != 0:
        return set(), f"could not list Git worktrees: {_git_detail(result)}"
    paths = {
        Path(line[len("worktree ") :]).expanduser().resolve()
        for line in result.stdout.splitlines()
        if line.startswith("worktree ")
    }
    return paths, None


def _reclaim_workspace(
    attempt_workspace: Path, run: RunRecord, *, force: bool = False
) -> Optional[str]:
    """Return a refusal reason or remove one registered worktree."""
    if run.workspace_root is None:
        return "run has no recorded workspace root"
    workspace_root = run.workspace_root.resolve()
    workspace = attempt_workspace.expanduser().resolve()
    try:
        relative = workspace.relative_to(workspace_root)
    except ValueError:
        return "workspace is outside the run workspace root"
    if not relative.parts:
        return "workspace path is the run workspace root"
    if not workspace.is_dir():
        return "workspace directory does not exist"

    repo_root, refusal = _workspace_repo_root(workspace)
    if refusal is not None or repo_root is None:
        return refusal or "workspace repository root could not be detected"
    status = _git_result(
        (
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ),
        cwd=workspace,
    )
    if status is None or status.returncode != 0:
        return f"could not inspect workspace status: {_git_detail(status)}"
    if status.stdout.strip() and not force:
        return "worktree is dirty"
    registered, refusal = _registered_worktrees(repo_root)
    if refusal is not None:
        return refusal
    if workspace not in registered:
        return "workspace is not a registered Git worktree"

    removal_args: tuple[str, ...] = ("worktree", "remove")
    if force:
        removal_args += ("--force",)
    removed = _git_result((*removal_args, str(workspace)), cwd=repo_root)
    if removed is None or removed.returncode != 0:
        return f"git worktree removal failed: {_git_detail(removed)}"
    return None


def _discover_orphan_worktrees(
    run: RunRecord, recorded_workspaces: set[Path]
) -> tuple[Path, ...]:
    """Find registered worktrees below the run root that have no attempt row."""
    if run.workspace_root is None or not run.workspace_root.is_dir():
        return ()
    candidates: set[Path] = set()
    for marker in run.workspace_root.glob("*/*/.git"):
        if not marker.is_file():
            continue
        candidate = marker.parent.resolve()
        if candidate in recorded_workspaces:
            continue
        repo_root, refusal = _workspace_repo_root(candidate)
        if repo_root is None or refusal is not None:
            continue
        registered, refusal = _registered_worktrees(repo_root)
        if refusal is None and candidate in registered:
            candidates.add(candidate)
    return tuple(sorted(candidates, key=str))


def _registered_workspace_repository(
    workspace: Path, jobs: Sequence[JobRecord]
) -> tuple[Optional[Path], bool, bool]:
    """Find the run repository that still registers a workspace."""
    recorded_workspace = workspace.expanduser().resolve()
    inspected = False
    uninspected = False
    for record in jobs:
        try:
            repo_root = detect_repo_root(record.job.declared_directory)
        except WorkspaceError:
            uninspected = True
            continue
        if repo_root is None:
            continue
        registered, refusal = _registered_worktrees(repo_root)
        if refusal is not None:
            uninspected = True
            continue
        inspected = True
        if recorded_workspace in registered:
            return repo_root, True, uninspected
    return None, inspected, uninspected


def _workspace_registration_state(
    workspace: Path, jobs: Sequence[JobRecord]
) -> Optional[bool]:
    """Check whether a missing workspace is still registered by a run repo."""
    repo_root, inspected, uninspected = _registered_workspace_repository(
        workspace, jobs
    )
    if repo_root is not None:
        return True
    return False if inspected and not uninspected else None


def _prune_interrupted_workspace(
    workspace: Path, jobs: Sequence[JobRecord]
) -> Optional[str]:
    """Prune a stale Git registration left by an interrupted worktree removal."""
    repo_root, inspected, uninspected = _registered_workspace_repository(
        workspace, jobs
    )
    if repo_root is None:
        if inspected and not uninspected:
            return None
        return "could not verify completed Git worktree removal"
    pruned = _git_result(
        ("worktree", "prune", "--expire", "now"), cwd=repo_root
    )
    if pruned is None or pruned.returncode != 0:
        return f"git worktree prune failed: {_git_detail(pruned)}"
    registered, refusal = _registered_worktrees(repo_root)
    if refusal is not None:
        return f"could not verify Git worktree pruning: {refusal}"
    if workspace.expanduser().resolve() in registered:
        return "workspace remains a registered Git worktree after pruning"
    return None


def gc_workspaces(
    store: JobStore | str | Path,
    run_id: Optional[str] = None,
    *,
    accepted_only: bool = False,
    force: bool = False,
) -> WorkspaceGCReport:
    """Reclaim eligible worktrees and report every refusal or skip."""
    store_object = store if isinstance(store, JobStore) else JobStore(store, create=False)
    all_run_ids = tuple(store_object.list_run_ids())
    if run_id is not None and run_id not in all_run_ids:
        store_object.snapshot(run_id)
    run_ids = (run_id,) if run_id is not None else all_run_ids
    snapshots = {
        current_run_id: store_object.snapshot(current_run_id)
        for current_run_id in all_run_ids
    }
    all_recorded_workspaces = {
        attempt.workspace.resolve()
        for snapshot in snapshots.values()
        for attempt in snapshot.attempts
        if attempt.workspace is not None
    }
    removed: list[WorkspaceGCEntry] = []
    refused: list[WorkspaceGCEntry] = []
    skipped: list[WorkspaceGCEntry] = []
    scanned_roots: set[Path] = set()

    for current_run_id in run_ids:
        snapshot = snapshots[current_run_id]
        jobs = {record.id: record for record in snapshot.jobs}
        for attempt in snapshot.attempts:
            if attempt.workspace is None:
                continue
            if attempt.id is None:
                refused.append(
                    WorkspaceGCEntry(
                        run_id=current_run_id,
                        job_id=attempt.job_id,
                        attempt_id=-1,
                        workspace=attempt.workspace,
                        reason="attempt row has no database id",
                    )
                )
                continue
            job_record = jobs.get(attempt.job_id)
            if job_record is None:
                refused.append(
                    WorkspaceGCEntry(
                        run_id=current_run_id,
                        job_id=attempt.job_id,
                        attempt_id=attempt.id,
                        workspace=attempt.workspace,
                        reason="attempt has no matching job row",
                    )
                )
                continue
            entry = WorkspaceGCEntry(
                run_id=current_run_id,
                job_id=attempt.job_id,
                attempt_id=attempt.id,
                workspace=attempt.workspace,
                reason="",
            )
            if not job_record.terminal:
                refused.append(
                    WorkspaceGCEntry(
                        run_id=entry.run_id,
                        job_id=entry.job_id,
                        attempt_id=entry.attempt_id,
                        workspace=entry.workspace,
                        reason=f"job is non-terminal ({job_record.state.value})",
                    )
                )
                continue
            if accepted_only and job_record.state is not JobState.ACCEPTED:
                skipped.append(
                    WorkspaceGCEntry(
                        run_id=entry.run_id,
                        job_id=entry.job_id,
                        attempt_id=entry.attempt_id,
                        workspace=entry.workspace,
                        reason=f"job is not accepted ({job_record.state.value})",
                    )
                )
                continue
            if attempt.workspace_removed_at is not None or attempt.workspace_status == "removed":
                skipped.append(
                    WorkspaceGCEntry(
                        run_id=entry.run_id,
                        job_id=entry.job_id,
                        attempt_id=entry.attempt_id,
                        workspace=entry.workspace,
                        reason="workspace was already removed",
                    )
                )
                continue
            if attempt.workspace_status not in {"isolated", "removing"}:
                skipped.append(
                    WorkspaceGCEntry(
                        run_id=entry.run_id,
                        job_id=entry.job_id,
                        attempt_id=entry.attempt_id,
                        workspace=entry.workspace,
                        reason="attempt does not record an isolated worktree",
                    )
                )
                continue

            cleanup_started = attempt.workspace_status == "removing"
            if cleanup_started and not attempt.workspace.is_dir():
                registration = _workspace_registration_state(
                    attempt.workspace, snapshot.jobs
                )
                if registration is True:
                    refusal = _prune_interrupted_workspace(
                        attempt.workspace, snapshot.jobs
                    )
                    if refusal is not None:
                        refused.append(
                            WorkspaceGCEntry(
                                run_id=entry.run_id,
                                job_id=entry.job_id,
                                attempt_id=entry.attempt_id,
                                workspace=entry.workspace,
                                reason=refusal,
                            )
                        )
                        continue
                    store_object.record_workspace_removed(
                        attempt.id,
                        at=time.time(),
                        forced=attempt.workspace_removal_forced,
                    )
                    removed.append(
                        WorkspaceGCEntry(
                            run_id=entry.run_id,
                            job_id=entry.job_id,
                            attempt_id=entry.attempt_id,
                            workspace=entry.workspace,
                            reason=(
                                "forced stale Git worktree registration pruned after interrupted removal"
                                if attempt.workspace_removal_forced
                                else "stale Git worktree registration pruned after interrupted removal"
                            ),
                        )
                    )
                    continue
                if registration is None:
                    refused.append(
                        WorkspaceGCEntry(
                            run_id=entry.run_id,
                            job_id=entry.job_id,
                            attempt_id=entry.attempt_id,
                            workspace=entry.workspace,
                            reason="could not verify completed Git worktree removal",
                        )
                    )
                    continue
                store_object.record_workspace_removed(
                    attempt.id,
                    at=time.time(),
                    forced=attempt.workspace_removal_forced,
                )
                removed.append(
                    WorkspaceGCEntry(
                        run_id=entry.run_id,
                        job_id=entry.job_id,
                        attempt_id=entry.attempt_id,
                        workspace=entry.workspace,
                        reason=(
                            "forced worktree removal was already completed"
                            if attempt.workspace_removal_forced
                            else "clean worktree removal was already completed"
                        ),
                    )
                )
                continue
            try:
                if not cleanup_started:
                    store_object.mark_workspace_removing(
                        attempt.id, forced=force
                    )
            except StoreError as exc:
                refused.append(
                    WorkspaceGCEntry(
                        run_id=entry.run_id,
                        job_id=entry.job_id,
                        attempt_id=entry.attempt_id,
                        workspace=entry.workspace,
                        reason=f"could not record workspace removal intent: {exc}",
                    )
                )
                continue

            refusal = _reclaim_workspace(
                attempt.workspace, snapshot.run, force=force
            )
            if refusal is not None:
                try:
                    store_object.restore_workspace_isolated(attempt.id)
                except StoreError:
                    pass
                refused.append(
                    WorkspaceGCEntry(
                        run_id=entry.run_id,
                        job_id=entry.job_id,
                        attempt_id=entry.attempt_id,
                        workspace=entry.workspace,
                        reason=refusal,
                    )
                )
                continue
            store_object.record_workspace_removed(
                attempt.id,
                at=time.time(),
                forced=force or attempt.workspace_removal_forced,
            )
            removed.append(
                WorkspaceGCEntry(
                    run_id=entry.run_id,
                    job_id=entry.job_id,
                    attempt_id=entry.attempt_id,
                    workspace=entry.workspace,
                    reason="forced worktree removed" if force else "clean worktree removed",
                )
            )

        workspace_root = snapshot.run.workspace_root
        if workspace_root is not None:
            resolved_root = workspace_root.resolve()
            if resolved_root not in scanned_roots:
                scanned_roots.add(resolved_root)
                for orphan in _discover_orphan_worktrees(
                    snapshot.run, all_recorded_workspaces
                ):
                    refused.append(
                        WorkspaceGCEntry(
                            run_id=current_run_id,
                            job_id="<unrecorded>",
                            attempt_id=-1,
                            workspace=orphan,
                            reason="registered worktree has no attempt row",
                        )
                    )

    return WorkspaceGCReport(
        runs=tuple(run_ids),
        accepted_only=accepted_only,
        removed=tuple(removed),
        refused=tuple(refused),
        skipped=tuple(skipped),
    )


__all__ = [
    "WORKSPACE_STATUSES",
    "WORKSPACE_REASON_NONE",
    "WORKSPACE_REASON_DECLINED",
    "WorkspaceError",
    "WorkspaceCreationError",
    "WorkspaceDetectionError",
    "WorkspaceResolution",
    "detect_repo_root",
    "resolve_base_ref",
    "create_worktree",
    "WorkspaceManager",
    "prepare_workspace",
    "WorkspaceGCEntry",
    "WorkspaceGCReport",
    "gc_workspaces",
]
