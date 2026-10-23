"""The answer engine: one pass over a task repository (summary-on-commit-plan.md D4 steps 1-4, D7; the M5 re-cut
of databench-integration-plan.md).

``run_pass`` is called by ``answers_cli`` (``task-kit answers refresh``) and later by databench; nothing here is
a service. One pass, per repository:

1. **Skip** the repository (reported, nothing touched) when HEAD is detached or has no commit, or a git operation
   is in progress (``MERGE_HEAD``, ``CHERRY_PICK_HEAD``, ``REVERT_HEAD``, ``REBASE_HEAD``, ``rebase-merge/``,
   ``rebase-apply/``, ``sequencer/``, located with ``git rev-parse --git-path``).
2. **Prepare** (repository lock held, phase ``prepare``). Commit this machine's dirty or untracked own answer
   files first (ownership checked by the record's ``machine:`` line, ``git commit --only -- <paths>``), so an
   answer written but never committed is not skipped as current. Compute the pending work: eligible task folders
   (committed, no uncommitted change outside ``answers/``) with an applicable model question whose script does not
   answer and whose chosen record is missing, stale or unusable. When work is pending, or own commits are
   unpublished, publish (``session_publish.publish_holding_lock``) and compute again. Order the pending tasks
   most recently modified first (``answer_store.modification_order``), keep the first ``batch_size`` tasks, and
   snapshot each pending question's inputs fingerprint. Release the lock.
3. **Answer** (no lock, phase ``answer``). Up to ``parallel`` tasks at a time: read the inputs, drop the question
   as superseded when its fingerprint differs from the snapshot, then ask the model chain
   (``answer_chain.ask``). The lock is never held across a model call.
4. **Commit** (repository lock held, phase ``commit``). Revalidate each answer (task folder still free of
   uncommitted changes, fingerprint unchanged; otherwise it is dropped as superseded), write this machine's
   record files, commit them with ``git commit --only``, publish. A busy lock is retried ``commit_attempts``
   times, then the finished answers are dropped as unwritten. A failed commit leaves the written files for the
   next pass's prepare step.

The batch counts TASKS: ``batch_size`` tasks are kept, each with every pending question it has, so one pass makes
at most ``batch_size`` times the number of model questions in calls.

A stop request (``should_stop``) is honoured before each phase starts and, while answering, before each question;
it ends the pass between phases. Answers already finished are still committed: a stop never discards paid work.
``budget.deadline`` (absolute ``time.monotonic()`` seconds) works the same way and is also handed to the chain.

Model answering REFUSES when llm_kit is not importable (``answer_chain.ModelAnsweringUnavailable``): the report
carries the refusal with its remedy, the pass answers nothing, and nothing substitutes. Prepare and commit steps
that need no model still run.

``survey`` is the read-only counterpart for ``answers status``: it takes no lock and writes nothing. ``prune``
deletes this machine's answer files of questions the catalog no longer holds.
"""

from __future__ import annotations

import concurrent.futures
import datetime
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import answer_chain, answer_scripts, answer_store, session_publish
from .answers_config import AnswersConfig
from .answers_types import Catalog, Question, RepoLog
from .discovery import read_task_block
from .oslock import LockTimeout
from .question_inputs import QuestionInputError, inputs_fingerprint, read_inputs

DEFAULT_BATCH_SIZE = 4
DEFAULT_PARALLEL = 4
COMMIT_ATTEMPTS = 3
GIT_TIMEOUT_S = 60.0

PHASE_PREPARE = "prepare"
PHASE_ANSWER = "answer"
PHASE_COMMIT = "commit"

OUTCOME_COMPLETED = "completed"
OUTCOME_SKIPPED = "skipped"
OUTCOME_STOPPED = "stopped"
OUTCOME_REFUSED = "refused"

STOP_REQUESTED = "stop"
STOP_DEADLINE = "deadline"

# Paths git keeps while an operation is in progress (the operations a pass must not interleave with).
GIT_OPERATION_PATHS = (
    "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "REBASE_HEAD", "rebase-merge", "rebase-apply", "sequencer",
)
_REPO_ENV_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR")


class EngineError(RuntimeError):
    """A git command the engine needs failed; the message names the command and the repository."""


# --- data -------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskTarget:
    """One task folder and its classification (``active``, ``blocked``, ... as ``validate`` reports it)."""

    folder: Path
    status: str


@dataclass(frozen=True)
class Budget:
    """What one pass may spend. ``deadline`` is an absolute ``time.monotonic()`` value, or None."""

    batch_size: int = DEFAULT_BATCH_SIZE
    deadline: float | None = None
    parallel: int = DEFAULT_PARALLEL
    lock_wait_s: float = session_publish.LOCK_WAIT_S
    commit_attempts: int = COMMIT_ATTEMPTS
    publish_window_s: float = session_publish.WINDOW_S
    network_timeout_s: float = session_publish.NETWORK_TIMEOUT_S


@dataclass(frozen=True)
class Answered:
    task: str  # folder, posix, relative to the repository top
    question: str
    entry: str | None
    tier: str | None


@dataclass(frozen=True)
class Dropped:
    task: str
    question: str
    reason: str


@dataclass(frozen=True)
class Failed:
    task: str
    question: str
    reason: str
    attempts: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class PassReport:
    """What one pass did in one repository.

    ``outcome``: ``completed``, ``skipped`` (``reason`` says why; nothing was touched), ``stopped`` (a stop request
    or the deadline ended it; ``stop`` says which, ``stopped_before`` the phase it did not start) or ``refused``
    (model answering is unavailable; ``refusal`` carries the remedy). ``pending`` counts the tasks that had work
    when the batch was cut, ``kept`` names the ones in the batch. ``unattempted`` lists kept (task, question) pairs
    that no model call reached. ``commit`` is the sha of the answers commit, ``commit_error`` the reason a commit
    failed (its files stay written for the next pass). ``unpublished`` counts this machine's committed files not
    yet on the upstream, judged against the last fetch.
    """

    repo: str
    outcome: str
    reason: str | None = None
    stop: str | None = None
    stopped_before: str | None = None
    phase: str | None = None
    committed_dirty: tuple[str, ...] = ()
    pending: int = 0
    kept: tuple[str, ...] = ()
    answered: tuple[Answered, ...] = ()
    dropped: tuple[Dropped, ...] = ()
    failed: tuple[Failed, ...] = ()
    unattempted: tuple[tuple[str, str], ...] = ()
    skipped_tasks: tuple[tuple[str, str], ...] = ()
    commit: str | None = None
    commit_error: str | None = None
    publish: tuple[Mapping[str, Any], ...] = ()
    unpublished: int = 0
    refusal: Mapping[str, str] | None = None
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "outcome": self.outcome,
            "reason": self.reason,
            "stop": self.stop,
            "stopped_before": self.stopped_before,
            "phase": self.phase,
            "committed_dirty": list(self.committed_dirty),
            "pending": self.pending,
            "kept": list(self.kept),
            "answered": [
                {"task": a.task, "question": a.question, "entry": a.entry, "tier": a.tier} for a in self.answered
            ],
            "dropped": [{"task": d.task, "question": d.question, "reason": d.reason} for d in self.dropped],
            "failed": [
                {
                    "task": f.task, "question": f.question, "reason": f.reason,
                    "attempts": [{"entry": entry, "outcome": outcome} for entry, outcome in f.attempts],
                }
                for f in self.failed
            ],
            "unattempted": [{"task": task, "question": question} for task, question in self.unattempted],
            "skipped_tasks": [{"task": task, "reason": reason} for task, reason in self.skipped_tasks],
            "commit": self.commit,
            "commit_error": self.commit_error,
            "publish": [dict(item) for item in self.publish],
            "unpublished": self.unpublished,
            "refusal": None if self.refusal is None else dict(self.refusal),
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class TaskSurvey:
    """One task as the engine sees it: its question states, coverage, and what a model would answer."""

    target: TaskTarget
    rel: str
    skip: str | None  # why the task is not eligible for a pass
    states: Mapping[str, str]  # question id -> state
    coverage: answer_store.Coverage | None
    pending: tuple[str, ...]  # question ids a model should answer
    fingerprints: Mapping[str, str]  # question id -> current inputs fingerprint (pending questions)
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "task": self.rel,
            "status": self.target.status,
            "skip": self.skip,
            "states": dict(self.states),
            "coverage": None if self.coverage is None else self.coverage.as_dict(),
            "pending": list(self.pending),
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class RepoSurvey:
    top: Path
    log: RepoLog | None
    tasks: tuple[TaskSurvey, ...]
    notes: tuple[str, ...] = ()

    @property
    def pending_tasks(self) -> tuple[TaskSurvey, ...]:
        return tuple(task for task in self.tasks if task.pending)


@dataclass(frozen=True)
class StatusReport:
    """``answers status`` for one repository: read-only coverage and pending work."""

    repo: str
    skip_reason: str | None  # the reason a pass would skip the repository
    tasks: tuple[TaskSurvey, ...]
    own_uncommitted: tuple[str, ...]  # this machine's answer files with changes not yet committed
    unpublished: tuple[str, ...]  # this machine's committed files not on the upstream (last fetch)
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        pending = [task for task in self.tasks if task.pending]
        return {
            "repo": self.repo,
            "skip_reason": self.skip_reason,
            "pending_tasks": len(pending),
            "pending_questions": sum(len(task.pending) for task in pending),
            "own_uncommitted": list(self.own_uncommitted),
            "unpublished": list(self.unpublished),
            "tasks": [task.as_dict() for task in self.tasks],
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class PruneReport:
    repo: str
    outcome: str  # completed | skipped
    reason: str | None = None
    pruned: tuple[str, ...] = ()
    refused: tuple[tuple[str, str], ...] = ()
    commit: str | None = None
    commit_error: str | None = None
    publish: tuple[Mapping[str, Any], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "outcome": self.outcome,
            "reason": self.reason,
            "pruned": list(self.pruned),
            "refused": [{"path": path, "reason": reason} for path, reason in self.refused],
            "commit": self.commit,
            "commit_error": self.commit_error,
            "publish": [dict(item) for item in self.publish],
        }


class LazyAllow:
    """The caller's cost classes, resolved once, the first time a pass needs to answer.

    ``allow`` is what ``answer_chain.resolve_allow`` takes (an explicit set, or None for llm-kit's consent).
    Calling the object resolves and remembers; ``resolved`` is None until then. One object serves every
    repository of a refresh, so consent is read once and its note printed once.
    """

    def __init__(self, allow: "answer_chain.Allowed | Iterable[str] | None" = None) -> None:
        self._allow = allow
        self._value: answer_chain.Allowed | None = None
        self._lock = threading.Lock()

    def __call__(self) -> "answer_chain.Allowed":
        with self._lock:
            if self._value is None:
                self._value = answer_chain.resolve_allow(self._allow)
            return self._value

    @property
    def resolved(self) -> "answer_chain.Allowed | None":
        return self._value


# --- git ---------------------------------------------------------------------------------------------------


def _git(top: Path, *args: str, optional_locks: bool = True) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in _REPO_ENV_VARS}
    env.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never", LC_ALL="C", LANGUAGE="C")
    prefix = ["git"] if optional_locks else ["git", "--no-optional-locks"]
    try:
        return subprocess.run(
            [*prefix, "-C", str(top), *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=GIT_TIMEOUT_S, env=env, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EngineError(f"git {' '.join(args[:2])} could not run in {top}: {exc}") from exc


def _git_ok(top: Path, *args: str, optional_locks: bool = True) -> str:
    proc = _git(top, *args, optional_locks=optional_locks)
    if proc.returncode != 0:
        raise EngineError(f"git {' '.join(args[:2])} failed in {top}: {(proc.stderr or proc.stdout).strip()}")
    return proc.stdout


def skip_reason(top: Path) -> str | None:
    """Why a pass must leave the repository alone, or None: detached or unborn HEAD, or a git operation in progress."""
    if _git(top, "symbolic-ref", "-q", "HEAD", optional_locks=False).returncode != 0:
        return "HEAD is detached"
    if _git(top, "rev-parse", "--verify", "-q", "HEAD", optional_locks=False).returncode != 0:
        return "the repository has no commit yet"
    for name in GIT_OPERATION_PATHS:
        located = _git(top, "rev-parse", "--git-path", name, optional_locks=False)
        if located.returncode != 0 or not located.stdout.strip():
            raise EngineError(f"git rev-parse --git-path {name} failed in {top}: {located.stderr.strip()}")
        path = Path(located.stdout.strip())
        if not path.is_absolute():
            path = top / path
        if path.exists():
            return f"a git operation is in progress ({name})"
    return None


def _status(top: Path) -> list[tuple[str, str]]:
    """``(XY code, path)`` for every changed or untracked path; paths are posix, relative to ``top``."""
    out = _git_ok(top, "status", "--porcelain=v1", "-z", "--untracked-files=all", optional_locks=False)
    tokens = out.split("\0")
    found: list[tuple[str, str]] = []
    index = 0
    while index < len(tokens):
        entry = tokens[index]
        index += 1
        if len(entry) < 4:
            continue
        code, path = entry[:2], entry[3:]
        if "R" in code or "C" in code:
            index += 1  # the original path follows
        found.append((code, path))
    return found


def _show(top: Path, rev: str, path: str) -> str | None:
    proc = _git(top, "show", f"{rev}:{path}", optional_locks=False)
    return proc.stdout if proc.returncode == 0 else None


def _head(top: Path) -> str:
    return _git_ok(top, "rev-parse", "HEAD", optional_locks=False).strip()


def _last_line(proc: subprocess.CompletedProcess, what: str) -> str:
    lines = [ln.strip() for ln in (proc.stderr + "\n" + proc.stdout).splitlines() if ln.strip()]
    lines = [ln for ln in lines if not ln.startswith("hint:")] or lines
    return lines[-1] if lines else f"{what} exited {proc.returncode}"


def _commit_paths(top: Path, paths: Sequence[str], untracked: Sequence[str], message: str) -> tuple[str | None, str | None]:
    """Commit exactly ``paths`` (other staged content is left alone); ``(sha, error)``."""
    if untracked:
        added = _git(top, "add", "--", *untracked)
        if added.returncode != 0:
            return None, _last_line(added, "git add")
    done = _git(top, "commit", "-q", "-m", message, "--only", "--", *paths)
    if done.returncode != 0:
        return None, _last_line(done, "git commit")
    return _head(top), None


def _publish(top: Path, machine, budget: Budget) -> Mapping[str, Any]:
    result = session_publish.publish_holding_lock(
        top, machine, window_s=budget.publish_window_s, network_timeout_s=budget.network_timeout_s
    )
    return {
        "status": result.status, "message": result.message, "committed": result.committed,
        "published": result.published, "attempts": result.attempts,
    }


# --- own answer files --------------------------------------------------------------------------------------


def _carries(text: str | None, fingerprint: str) -> bool:
    return text is not None and session_publish.answer_record_machine(text) == fingerprint


def own_dirty(top: Path, machine, status: Sequence[tuple[str, str]]) -> tuple[list[tuple[str, str]], list[str]]:
    """This machine's answer files with uncommitted changes, and the notes for the ones it will not commit.

    ``(code, path)`` pairs. A file is this machine's when it is named for this host AND the record's ``machine:``
    line carries this machine's fingerprint, in the working copy (additions, modifications) and, when HEAD holds
    it, in the committed preimage too (a modification or deletion). Anything else is left alone and noted.
    """
    mine: list[tuple[str, str]] = []
    notes: list[str] = []
    for code, path in status:
        if not session_publish.is_answer_path(path, machine):
            continue
        preimage = _show(top, "HEAD", path)
        ok = preimage is None or _carries(preimage, machine.fingerprint)
        if "D" not in code:
            try:
                working = (top / path).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                working = None
            ok = ok and _carries(working, machine.fingerprint)
        if ok:
            mine.append((code, path))
        else:
            notes.append(f"{path}: named for this host but not this machine's by content; not committed")
    return mine, notes


def commit_own_dirty(top: Path, machine, status: Sequence[tuple[str, str]] | None = None) -> tuple[list[str], str | None, list[str]]:
    """Commit this machine's dirty own answer files; ``(paths, error, notes)``. Requires the repository lock."""
    changes = _status(top) if status is None else list(status)
    mine, notes = own_dirty(top, machine, changes)
    if not mine:
        return [], None, notes
    paths = sorted({path for _code, path in mine})
    untracked = sorted({path for code, path in mine if code == "??"})
    _sha, error = _commit_paths(top, paths, untracked, f"task-kit answers: {machine.host}")
    return ([] if error else paths), error, notes


# --- survey ------------------------------------------------------------------------------------------------


def _relative(top: Path, folder: Path) -> str | None:
    try:
        return Path(folder).resolve().relative_to(top.resolve()).as_posix()
    except ValueError:
        return None


def _folder_dirty(rel: str, dirty_paths: Sequence[str]) -> bool:
    """Whether a path inside the folder, outside its own ``answers/``, has an uncommitted change."""
    prefix, answers = rel + "/", rel + "/answers/"
    return any(path.startswith(prefix) and not path.startswith(answers) for path in dirty_paths)


def survey_repo(
    top: Path, machine, targets: Sequence[TaskTarget], catalog: Catalog, now: datetime.datetime
) -> RepoSurvey:
    """Every target's question states and pending model work. Reads files and git; writes nothing."""
    top = Path(top)
    notes: list[str] = []
    try:
        log = answer_scripts.read_repo_log(top)
    except answer_scripts.AnswerScriptError as exc:
        raise EngineError(f"the repository log cannot be read: {exc}") from exc
    dirty_paths = [path for _code, path in _status(top)]
    surveyed: list[TaskSurvey] = []
    for target in targets:
        folder = Path(target.folder)
        rel = _relative(top, folder)
        if rel is None:
            raise EngineError(f"{folder} is not inside the repository {top}")
        block = read_task_block(folder)
        states: dict[str, answer_store.AnswerState] = {}
        pending: list[str] = []
        fingerprints: dict[str, str] = {}
        task_notes: list[str] = []
        for question in catalog.questions:
            state = answer_store.answer_state(
                question, folder, block, target.status, repo_top=top, log=log, now=now
            )
            states[question.id] = state
            task_notes.extend(state.notes)
            if not question.prompt:
                continue
            scripted = state.answerer is not None and state.answerer.kind == "script"
            if scripted or state.state not in (
                answer_store.STATE_MISSING, answer_store.STATE_STALE, answer_store.STATE_INVALID
            ):
                continue
            if state.current_fingerprint is None:
                continue  # the inputs cannot be read; the state's notes say why
            own = answer_store.record_path(folder, question.id, machine.host)
            if state.state == answer_store.STATE_INVALID and own.exists():
                try:
                    writable = _carries(own.read_text(encoding="utf-8"), machine.fingerprint)
                except (OSError, UnicodeDecodeError):
                    writable = False
                if not writable:
                    task_notes.append(f"{own}: this host's file is not this machine's or cannot be read; not rewritten")
                    continue
            pending.append(question.id)
            fingerprints[question.id] = state.current_fingerprint
        skip: str | None = None
        if log.rank(rel) is None:
            skip = "the task folder has no commit in the repository"
        elif _folder_dirty(rel, dirty_paths):
            skip = "the task folder has uncommitted changes outside answers/"
        applicable = any(target.status in question.applies_to for question in catalog.questions)
        surveyed.append(TaskSurvey(
            target=target, rel=rel, skip=skip,
            states={qid: state.state for qid, state in states.items()},
            coverage=answer_store.coverage(catalog, states) if applicable else None,
            pending=() if skip else tuple(pending),
            fingerprints=fingerprints if not skip else {},
            notes=tuple(dict.fromkeys(task_notes)),
        ))
    return RepoSurvey(top=top, log=log, tasks=tuple(surveyed), notes=tuple(notes))


def status(
    repo_top: Path, machine, *, targets: Sequence[TaskTarget], catalog: Catalog,
    now: Callable[[], datetime.datetime] = datetime.datetime.now,
) -> StatusReport:
    """Coverage and pending work of one repository, without a lock and without writing anything."""
    top = Path(repo_top).resolve()
    reason = skip_reason(top)
    surveyed = survey_repo(top, machine, targets, catalog, now())
    own, notes = own_dirty(top, machine, _status(top))
    return StatusReport(
        repo=str(top), skip_reason=reason, tasks=surveyed.tasks,
        own_uncommitted=tuple(sorted(path for _code, path in own)),
        unpublished=tuple(session_publish.pending(top, machine)),
        notes=tuple(notes),
    )


# --- the pass ----------------------------------------------------------------------------------------------


@dataclass
class _Held:
    """One answer waiting for the commit step."""

    task: str
    folder: Path
    question: Question
    fingerprint: str
    asked: answer_chain.Asked


class _Pass:
    """Mutable record of one pass; ``report`` freezes it."""

    def __init__(self, repo: str) -> None:
        self.repo = repo
        self.outcome = OUTCOME_COMPLETED
        self.reason: str | None = None
        self.stop: str | None = None
        self.stopped_before: str | None = None
        self.phase: str | None = None
        self.committed_dirty: list[str] = []
        self.pending = 0
        self.kept: list[str] = []
        self.answered: list[Answered] = []
        self.dropped: list[Dropped] = []
        self.failed: list[Failed] = []
        self.unattempted: list[tuple[str, str]] = []
        self.skipped_tasks: list[tuple[str, str]] = []
        self.commit: str | None = None
        self.commit_error: str | None = None
        self.publish: list[Mapping[str, Any]] = []
        self.unpublished = 0
        self.refusal: Mapping[str, str] | None = None
        self.notes: list[str] = []

    def report(self) -> PassReport:
        outcome = self.outcome
        if self.refusal is not None:
            outcome = OUTCOME_REFUSED
        elif self.stop is not None and outcome == OUTCOME_COMPLETED:
            outcome = OUTCOME_STOPPED
        return PassReport(
            repo=self.repo, outcome=outcome, reason=self.reason, stop=self.stop, stopped_before=self.stopped_before,
            phase=self.phase, committed_dirty=tuple(self.committed_dirty), pending=self.pending,
            kept=tuple(self.kept), answered=tuple(self.answered), dropped=tuple(self.dropped),
            failed=tuple(self.failed), unattempted=tuple(self.unattempted),
            skipped_tasks=tuple(self.skipped_tasks), commit=self.commit, commit_error=self.commit_error,
            publish=tuple(self.publish), unpublished=self.unpublished, refusal=self.refusal,
            notes=tuple(dict.fromkeys(self.notes)),
        )


def run_pass(
    repo_top: Path,
    machine,
    config: AnswersConfig,
    *,
    budget: Budget = Budget(),
    should_stop: Callable[[], bool] = lambda: False,
    targets: Sequence[TaskTarget],
    catalog: Catalog,
    allow: "answer_chain.Allowed | Iterable[str] | Callable[[], answer_chain.Allowed] | None" = None,
    run=None,
    data_dir: Path | None = None,
    on_phase: Callable[[str], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime.datetime] = datetime.datetime.now,
    publish: bool = True,
) -> PassReport:
    """One pass over the task repository at ``repo_top`` (see the module docstring).

    ``targets`` are the task folders of the repository with their classification, ``catalog`` the questions.
    ``allow`` is the caller's cost classes: an ``Allowed``, an iterable of class names, a callable returning an
    ``Allowed`` (``LazyAllow``, resolved only when a model must answer) or None for llm-kit's consent.
    ``run`` replaces ``declaration.run`` (tests). ``data_dir`` is the repository lock's directory (default: the
    plugin's data directory). ``publish=False`` commits but never touches the network.
    """
    top = Path(repo_top).resolve()
    state = _Pass(str(top))

    def phase(name: str) -> None:
        state.phase = name
        if on_phase is not None:
            on_phase(name)

    def stop_reason() -> str | None:
        if should_stop():
            return STOP_REQUESTED
        if budget.deadline is not None and clock() >= budget.deadline:
            return STOP_DEADLINE
        return None

    def stopped(before: str, why: str) -> None:
        state.stop, state.stopped_before = why, before

    why = stop_reason()
    if why is not None:
        stopped(PHASE_PREPARE, why)
        return state.report()
    reason = skip_reason(top)
    if reason is not None:
        state.outcome, state.reason = OUTCOME_SKIPPED, reason
        return state.report()

    # --- prepare ---
    phase(PHASE_PREPARE)
    try:
        with session_publish.repo_lock(top, data_dir, budget.lock_wait_s):
            kept = _prepare(top, machine, catalog, targets, budget, state, now, publish)
    except LockTimeout:
        state.outcome, state.reason = OUTCOME_SKIPPED, "the repository lock is busy; the pass was skipped"
        return state.report()
    if not kept:
        state.unpublished = len(session_publish.pending(top, machine))
        return state.report()

    # --- answer ---
    why = stop_reason()
    if why is not None:
        stopped(PHASE_ANSWER, why)
        state.unattempted = [(item.rel, qid) for item in kept for qid in item.pending]
        state.unpublished = len(session_publish.pending(top, machine))
        return state.report()
    phase(PHASE_ANSWER)
    held, unexpected = _answer(config, catalog, kept, budget, should_stop, allow, run, clock, state)

    # --- commit ---
    why = stop_reason()
    if why is not None and state.stop is None:
        state.stop, state.stopped_before = why, PHASE_COMMIT
    if held:
        phase(PHASE_COMMIT)
        _commit(top, machine, held, budget, data_dir, state, publish, clock)
    state.unpublished = len(session_publish.pending(top, machine))
    if unexpected is not None:
        raise unexpected
    return state.report()


def _prepare(
    top: Path, machine, catalog: Catalog, targets: Sequence[TaskTarget], budget: Budget, state: _Pass,
    now: Callable[[], datetime.datetime], publish: bool,
) -> list[TaskSurvey]:
    """Prepare, repository lock held: commit own dirty files, compute and (when needed) publish, cut the batch."""
    paths, error, notes = commit_own_dirty(top, machine)
    state.committed_dirty.extend(paths)
    state.notes.extend(notes)
    if error is not None:
        state.commit_error = error
        state.notes.append(f"own answer files could not be committed: {error}")
    surveyed = survey_repo(top, machine, targets, catalog, now())
    if publish and (surveyed.pending_tasks or session_publish.pending(top, machine)):
        state.publish.append(_publish(top, machine, budget))
        surveyed = survey_repo(top, machine, targets, catalog, now())  # an integrate may have moved HEAD
    state.notes.extend(surveyed.notes)
    for task in surveyed.tasks:
        if task.skip:
            state.skipped_tasks.append((task.rel, task.skip))
        state.notes.extend(task.notes)
    pending = surveyed.pending_tasks
    state.pending = len(pending)
    assert surveyed.log is not None
    order = answer_store.modification_order(top, [task.target.folder for task in pending], surveyed.log)
    by_folder = {Path(task.target.folder): task for task in pending}
    kept = [by_folder[Path(folder)] for folder in order][: max(0, budget.batch_size)]
    state.kept = [task.rel for task in kept]
    return kept


def _answer(
    config: AnswersConfig, catalog: Catalog, kept: Sequence[TaskSurvey], budget: Budget,
    should_stop: Callable[[], bool], allow, run, clock: Callable[[], float], state: _Pass,
) -> tuple[list[_Held], BaseException | None]:
    """The answer phase, no lock held. Returns the finished answers and the first unexpected exception."""
    questions = {question.id: question for question in catalog.questions}
    order = {item.rel: index for index, item in enumerate(kept)}
    rank = {question.id: index for index, question in enumerate(catalog.questions)}
    try:
        allowed = allow() if callable(allow) else answer_chain.resolve_allow(allow)
    except answer_chain.ModelAnsweringUnavailable as exc:
        state.refusal = {"state": exc.state, "message": str(exc)}
        state.unattempted = [(item.rel, qid) for item in kept for qid in item.pending]
        return [], None
    cache: dict = {}
    refusal_seen = threading.Event()
    guard = threading.Lock()
    held: list[_Held] = []

    def stop_now() -> str | None:
        if should_stop():
            return STOP_REQUESTED
        if budget.deadline is not None and clock() >= budget.deadline:
            return STOP_DEADLINE
        return None

    def work(item: TaskSurvey) -> None:
        folder = Path(item.target.folder)
        for qid in item.pending:
            question = questions[qid]
            why = stop_now()
            if refusal_seen.is_set() or why is not None:
                with guard:
                    state.unattempted.append((item.rel, qid))
                    if why is not None and state.stop is None and not refusal_seen.is_set():
                        state.stop, state.stopped_before = why, PHASE_ANSWER
                continue
            try:
                bundle = read_inputs(folder, read_task_block(folder), question)
                fingerprint = inputs_fingerprint(question, bundle)
            except QuestionInputError as exc:
                with guard:
                    state.failed.append(Failed(item.rel, qid, f"the inputs cannot be read: {exc}"))
                continue
            if fingerprint != item.fingerprints[qid]:
                with guard:
                    state.dropped.append(Dropped(item.rel, qid, "superseded: the task changed since the batch was cut"))
                continue
            try:
                asked = answer_chain.ask(
                    question, bundle, config, allow=allowed, deadline=budget.deadline, reachability_cache=cache,
                    run=run, clock=clock,
                )
            except answer_chain.ModelAnsweringUnavailable as exc:
                refusal_seen.set()
                with guard:
                    state.refusal = {"state": exc.state, "message": str(exc)}
                    state.unattempted.append((item.rel, qid))
                continue
            except answer_chain.NoAnswer as exc:
                with guard:
                    state.failed.append(Failed(item.rel, qid, str(exc), tuple(exc.attempts)))
                continue
            with guard:
                held.append(_Held(item.rel, folder, question, fingerprint, asked))

    unexpected: BaseException | None = None
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, budget.parallel)) as pool:
        for future in [pool.submit(work, item) for item in kept]:
            try:
                future.result()
            except Exception as exc:  # noqa: BLE001 -- finished answers are still committed; the first error is re-raised after
                if unexpected is None:
                    unexpected = exc
    held.sort(key=lambda h: (order[h.task], rank[h.question.id]))
    return held, unexpected


def _commit(
    top: Path, machine, held: Sequence[_Held], budget: Budget, data_dir: Path | None, state: _Pass,
    publish: bool, clock: Callable[[], float],
) -> None:
    """The commit phase: take the lock (retrying a busy one), then revalidate, write, commit and publish."""
    attempts = max(1, budget.commit_attempts)
    for attempt in range(1, attempts + 1):
        try:
            with session_publish.repo_lock(top, data_dir, budget.lock_wait_s):
                _commit_locked(top, machine, held, budget, state, publish)
            return
        except LockTimeout:
            if attempt == attempts or (budget.deadline is not None and clock() >= budget.deadline):
                break
    for item in held:
        state.dropped.append(Dropped(item.task, item.question.id, "unwritten: the repository lock stayed busy at commit"))


def _commit_locked(top: Path, machine, held: Sequence[_Held], budget: Budget, state: _Pass, publish: bool) -> None:
    reason = skip_reason(top)
    if reason is not None:
        for item in held:
            state.dropped.append(Dropped(item.task, item.question.id, f"unwritten: {reason}"))
        return
    dirty_paths = [path for _code, path in _status(top)]
    head = _head(top)
    written: list[tuple[_Held, str]] = []
    for item in held:
        if _folder_dirty(item.task, dirty_paths):
            state.dropped.append(Dropped(item.task, item.question.id, "superseded: the task has uncommitted changes"))
            continue
        try:
            fingerprint = inputs_fingerprint(
                item.question, read_inputs(item.folder, read_task_block(item.folder), item.question)
            )
        except QuestionInputError as exc:
            state.failed.append(Failed(item.task, item.question.id, f"the inputs cannot be read: {exc}"))
            continue
        if fingerprint != item.fingerprint:
            state.dropped.append(
                Dropped(item.task, item.question.id, "superseded: the task changed while it was being answered")
            )
            continue
        new = answer_store.NewAnswer(
            question_version=item.question.version, answer=item.asked.answer, inputs_fingerprint=fingerprint,
            as_of=answer_store.AsOf(task_commit=head, code_commit=None), answerer=item.asked.answerer,
            generated_at=answer_store.timestamp(),
        )
        try:
            path = answer_store.write_answer(item.folder, item.question.id, machine, new)
        except answer_store.AnswerStoreError as exc:
            state.failed.append(Failed(item.task, item.question.id, str(exc), tuple(item.asked.answerer.attempts)))
            continue
        written.append((item, path.resolve().relative_to(top).as_posix()))
    if not written:
        return
    paths = [rel for _item, rel in written]
    untracked = [rel for rel in paths if _git(top, "cat-file", "-e", f"HEAD:{rel}", optional_locks=False).returncode != 0]
    sha, error = _commit_paths(top, paths, untracked, f"task-kit answers: {machine.host}")
    for item, _rel in written:
        answerer = item.asked.answerer
        state.answered.append(Answered(item.task, item.question.id, answerer.entry, answerer.tier))
    if error is not None:
        state.commit_error = error
        state.notes.append(f"the answers were written but not committed ({error}); the next pass commits them")
        return
    state.commit = sha
    if publish:
        state.publish.append(_publish(top, machine, budget))


def prune(
    repo_top: Path, machine, catalog: Catalog, *, targets: Sequence[TaskTarget], data_dir: Path | None = None,
    budget: Budget = Budget(), publish: bool = True,
) -> PruneReport:
    """Delete this machine's answer files of questions the catalog no longer holds (retired or unknown ids).

    Only this machine's own file is ever considered, and ``answer_store.delete_answer`` checks its committed
    preimage and working copy; a file it refuses is reported. The deletions are committed (``--only``) and
    published. Another machine's files are never touched.
    """
    top = Path(repo_top).resolve()
    reason = skip_reason(top)
    if reason is not None:
        return PruneReport(repo=str(top), outcome=OUTCOME_SKIPPED, reason=reason)
    try:
        with session_publish.repo_lock(top, data_dir, budget.lock_wait_s):
            pruned: list[str] = []
            tracked: list[str] = []
            refused: list[tuple[str, str]] = []
            for target in targets:
                folder = Path(target.folder)
                for question_id in answer_store.question_dirs(folder):
                    if question_id in catalog.ids:
                        continue
                    path = answer_store.record_path(folder, question_id, machine.host)
                    if not path.exists():
                        continue
                    rel = path.resolve().relative_to(top).as_posix()
                    was_tracked = _git(top, "cat-file", "-e", f"HEAD:{rel}", optional_locks=False).returncode == 0
                    try:
                        answer_store.delete_answer(folder, question_id, machine, top)
                    except answer_store.AnswerOwnershipError as exc:
                        refused.append((rel, str(exc)))
                        continue
                    pruned.append(rel)
                    if was_tracked:
                        tracked.append(rel)
            sha = error = None
            results: list[Mapping[str, Any]] = []
            if tracked:
                sha, error = _commit_paths(top, tracked, [], f"task-kit answers: prune {machine.host}")
                if error is None and publish:
                    results.append(_publish(top, machine, budget))
    except LockTimeout:
        return PruneReport(repo=str(top), outcome=OUTCOME_SKIPPED, reason="the repository lock is busy")
    return PruneReport(
        repo=str(top), outcome=OUTCOME_COMPLETED, pruned=tuple(pruned), refused=tuple(refused), commit=sha,
        commit_error=error, publish=tuple(results),
    )
