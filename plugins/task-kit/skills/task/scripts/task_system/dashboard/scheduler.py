"""Queueing, starting and settling autonomous runs.

The scheduler turns the user's Queue action into at most one running item per
project, and observes the run-owning child processes until each settles.

Rules it enforces:

- Queue is the unattended authorization. Nothing is written into the task
  folder to queue; a queue entry is a runtime record naming the task, the item
  and the revision it was queued against.
- A run starts only when its project holds no other lease, a global slot is
  free, and no interactive hold covers the task folder. A hold is one-way: it
  makes the scheduler yield, and the harness never waits for the dashboard.
- Start order is fair across checkouts: the checkout that started a run least recently goes first
  (``start_order``), first-come first-served within one. A tick starts at most ONE run (one
  translation and one runner wait, never the whole queue), then asks for another tick. A queued
  entry that cannot start says why (``wait_reason``), and the UI shows it.
- A Claude run needs its binary observed once (``cli_check``): the observation runs on its own thread,
  never under the tick lock, and the entry waits with reason ``cli-check`` until it ends.
- A Claude run gets native sub-agents only when ``subagents.decide`` says so (a current characterization record for the
  installed binary): the decision is made at each start from the observation the tick already waited for plus one small
  file, never spawns anything, and a missing, stale or failed record only leaves the sub-agent tool denied.
- Every start translates the item again from the current folder. A changed but
  still job-ready item runs with fresh text; an item that disappeared or is no
  longer available is dropped (``skipped``), not blocked, because someone else
  changed it deliberately.
- A translation failure that describes the item or its configuration blocks the
  task (see blocker.py) and creates no run directory, job file or ledger.
- A run that ended without an acceptance verdict is ``failed`` or ``timeout``
  and is never retried automatically; Retry creates a fresh run.
- A runner that vanished without a result, and a run whose acceptance could not
  run for lack of time, block the task so a person inspects the shared tree.
- Authoring (add item, defer, new task) and a Re-check write the task folder only while no
  Claude Code session holds it and no dashboard run works it; Remove and Save run options act on a
  queue entry and are validated against the entry's own state, like Cancel.
- Cancel is process control on an immutable run id: it is validated against the
  run's own state under the scheduler lock and carries no task revision. A
  cancelled run is retryable like a timeout (its partial work is noted, the task
  is not blocked); one that left survivors, or whose ledger could not be closed,
  is a crash-inspection block instead.

Lock order (outermost first; nothing ever takes one while holding a later one)
-----------------------------------------------------------------------------
1. ``_queue_lock``: Queue, Queue all and every authoring write.
2. ``_tick_lock``: a tick (reconcile + start), Cancel, Remove, Save run options and every authoring
   write, so no run starts between an authoring check and its write.
3. the mutator's cross-process file lock (``TaskMutator``; waits up to 10 s).
4. ``_hold_gate``: a plain lock held only for a short check-and-commit: ``precommit`` plus the
   per-file compare + ``os.replace`` of a task-folder write, hold registration, and the start's hold
   re-check + ``try_lease``. Nothing under the gate waits on a lock or runs a process (the DB's own
   short lock aside), so a hold registration, which takes ONLY the gate, fits the 2 s handshake.

Recovery state table
--------------------
Everything the server knows about a run that is not queued is in its run directory
(see ``runfiles.py``); the database only says which run holds the lease. After a
server restart, and on every tick for each run holding a lease, the first row that
matches decides. Receipts, results and ledgers are read BEFORE any process is
trusted, and nothing here ever deletes a directory the current attempt did not
create. ``block`` means: the run ends ``crashed`` with a code, the task is blocked
for a person (crash inspection); it never means success and nothing is retried.

 #  observed                                                 action
 -- -------------------------------------------------------- ---------------------------------------
 0  no run directory recorded in the database, state          requeue (a directory left on disk is never
    starting                                                  reused or deleted: the next dispatch fails
                                                              loudly with ``dispatch-failed``)
 1  result.json present, torn or not a result                 block ``result-unreadable``
 2  result.json lists survivors of the stop                   block ``survivors`` (ledger untouched)
 3  receipt.json present but torn or not a verdict            block ``receipt-unreadable``
 4  receipt.json valid                                        settle by the receipt (accepted, or
                                                              blocked as acceptance already did);
                                                              a receipt always beats a cancel
 5  outcome cancelled/timeout, cancel command failed          block ``cancel-failed``
    (not exit 0/3, timed out, could not start)
 6  outcome cancelled/timeout, ledger already accepted or     block ``ledger-without-receipt``
    rejected but no receipt
 7  outcome cancelled                                         ``cancelled`` (retryable, task not blocked)
 8  outcome timeout                                           ``timeout`` (retryable, task not blocked)
 9  outcome runner-refused                                    ``failed``
10  outcome finished, no receipt                              ledger left open -> close it, block
                                                              ``llm-kit-crashed``; else the model call's
                                                              own timeout -> ``timeout``; acceptance
                                                              starved -> block; else ``failed``
-- (no result.json from here on) ---------------------------------------------------------------
11  runner.json names a live runner, state starting           adopt: ``running`` (``cancelling`` if
                                                              cancel.json exists)
12  runner.json names a live runner, otherwise                nothing: the runner owns the run
13  no runner.json, state starting, younger than the grace    wait (the runner may still be starting)
14  no runner.json, state starting, grace over                write cancel.json, look at runner.json
                                                              once more: found -> row 11; else
                                                              ``failed`` ``dispatch-interrupted`` (also
                                                              for a cancel asked meanwhile: nothing ran),
                                                              directory kept, task not blocked
15  the runner is dead and left no result: stop whatever     (rows 16-22 decide after the stop)
    child.json, tree.json, their descendants and the
    command's leftover session still hold, by PID + ctime
16  ... something would not stop                              block ``survivors`` (ledger untouched)
17  ... ledger cannot be read (llm-kit absent, bad schema)   block ``ledger-unreadable``
18  ... receipt.json present but torn                         block ``receipt-unreadable``
19  ... ledger terminal, receipt valid                        settle by the receipt
20  ... ledger terminal, no valid receipt                     block ``ledger-without-receipt``
21  ... ledger open: run the cancel command; it failed        block ``cancel-failed``
22  ... ledger open and closed, or no ledger at all           block ``runner-crashed``

Only row 0 requeues, and only because no directory exists; every other ambiguity blocks.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import yaml

from .. import init as task_init
from ..task_items import read_task_items, sort_items
from ..types import HAND_OFF
from ..validate import validate_ref
from . import authoring
from . import findings as findings_mod
from . import cli_check, endpoint_helper, floor, proctree, redact, repo_identity, runfiles, subagents, vcs
from .blocker import BlockOutcome, concise, record_block, record_blocks
from .config import DashboardConfig, DashboardConfigError, load_config
from .inventory import project_id_for
from .jobs_edge import EdgeError
from .mutation import (
    LOG,
    PLAN,
    TASK_YAML,
    MutationError,
    StaleRevision,
    TaskMutator,
    TaskTexts,
    append_log,
    folder_revision,
    raw_items,
    set_item_state,
    set_task_fields,
    set_task_status,
    stored_status,
)
from .runtime import (
    ACCEPTED,
    ACTIVE_STATES,
    BLOCKED,
    CANCELLABLE_STATES,
    CANCELLED,
    CANCELLING,
    CRASHED,
    FAILED,
    QUEUED,
    REMOVED,
    RETRYABLE_STATES,
    RUNNING,
    SKIPPED,
    STARTING,
    TERMINAL_STATES,
    TIMEOUT,
    Run,
    RuntimeDB,
    folder_key,
)
from .translate import CODEX_FLOOR_PROVED, OPTION_REASON_CODES, JobSpec, Reason, RunOptions, option_reasons, translate

ACCEPT_SCRIPT = Path(__file__).with_name("accept_cli.py")
RUNNER_SCRIPT = Path(__file__).with_name("runner_cli.py")
RUNNER_SLACK_S = 120.0
RUNNER_GRACE_S = 5.0
# A run that holds a lease but whose runner has not recorded itself yet is given this long before the
# server writes it off (the runner is a fresh Python process; the server may also have just restarted).
ADOPT_GRACE_S = 20.0
USER_CANCEL_REASON = "cancelled from the dashboard"
REPO_KEY_TTL_S = 30.0
LOG_TAIL_BYTES = 64 * 1024
# Reasons that mean "someone changed the task on purpose": drop the entry, change nothing.
SOURCE_CHANGED = {"item-missing", "item-duplicate", "item-not-available", "task-not-active", "no-items-block"}


class QueueRefused(Exception):
    """An action was refused. ``status`` is the HTTP status the API answers with."""

    def __init__(self, status: int, code: str, message: str, *, reasons: list[dict] | None = None,
                 blocked: bool = False, current_revision: str | None = None, extra: dict | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.reasons = reasons or []
        self.blocked = blocked
        self.current_revision = current_revision
        self.extra = extra or {}


HELD_MESSAGE = ("a Claude Code session is working this task; make the change there, or Re-check when it is done")
RUN_ACTIVE_MESSAGE = "a dashboard run is working this task; wait for it to finish, or cancel it"
ITEM_RUN_MESSAGE = "this item has a queued or running dashboard run; remove it from the queue first"


def refusal_from_mutation(exc: MutationError, folder: Path) -> QueueRefused:
    """The one mapping from a failed task-folder write to an API refusal (``written`` is read first).

    A write that replaced some files and then failed is a ``partial-write`` whatever the cause: the
    folder is not atomic, so the response says which files changed and which did not.
    """
    def current() -> str | None:
        try:
            return folder_revision(folder)
        except MutationError:
            return None

    written = list(exc.written)
    if written:
        failed = exc.path.name if exc.path is not None else None
        intended = list(exc.intended)
        not_written = [name for name in intended if name not in written]
        return QueueRefused(
            409, "partial-write",
            f"the change was only partly written ({', '.join(written)} changed; {', '.join(not_written) or 'nothing else'} did not): {exc}",
            current_revision=current(),
            extra={"cause": exc.code, "file": failed, "written": written, "not_written": not_written},
        )
    if isinstance(exc, StaleRevision):
        return QueueRefused(409, "stale-revision", str(exc), current_revision=exc.current or current())
    if exc.code == "replace-failed":
        return QueueRefused(409, "replace-failed", str(exc), extra={"file": exc.path.name if exc.path is not None else None})
    if exc.code == "lock-timeout":
        return QueueRefused(409, "busy", str(exc))
    if exc.code in ("invalid-source", "unreadable"):
        return QueueRefused(422, exc.code, str(exc))
    return QueueRefused(500, exc.code, str(exc))


@dataclass
class _Stage:
    """How far one dispatch attempt got, so a failure undoes only what this attempt did."""

    created: bool = False  # this attempt created the run directory
    spawned: bool = False  # the runner process was started (from here the directory is its home)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _reasons_json(reasons) -> list[dict]:
    return [{"code": r.code, "detail": r.detail, "blocking": r.blocking} for r in reasons]


def _options_from(body: dict) -> dict:
    """Validate the per-run options a client may send; unknown keys are an error."""
    allowed = {"worker", "entry", "effort", "network", "timeout_s"}
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise QueueRefused(422, "bad-options", f"unknown run options {unknown}; allowed: {sorted(allowed)}")
    options: dict = {}
    for key, kind in (("worker", str), ("entry", str), ("effort", str)):
        if key in body:
            if not isinstance(body[key], kind) or not body[key]:
                raise QueueRefused(422, "bad-options", f"{key} must be a non-empty string")
            options[key] = body[key]
    if "network" in body:
        if not isinstance(body["network"], bool):
            raise QueueRefused(422, "bad-options", "network must be true or false")
        options["network"] = body["network"]
    if "timeout_s" in body:
        value = body["timeout_s"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise QueueRefused(422, "bad-options", "timeout_s must be a positive number")
        options["timeout_s"] = float(value)
    return options


# llm-kit's attempt status for a model call that ran into the job's own per-invocation
# timeout (the TIMEOUT constant of llm-kit's jobs model; the dashboard does not import llm-kit, so the
# value is stated here and pinned against the real producer in
# the dashboard's real-job test module).
ATTEMPT_TIMEOUT_STATUS = "timeout"


def read_job_snapshot(run_dir: Path) -> dict | None:
    """The final JSON status line ``llm-kit jobs run`` printed, or None when there is none."""
    try:
        snapshot = json.loads((Path(run_dir) / "llm.out").read_text(encoding="utf-8").strip().splitlines()[-1])
    except (OSError, ValueError, IndexError):
        return None
    return snapshot if isinstance(snapshot, dict) else None


def job_timeout(snapshot: dict | None) -> str | None:
    """The first line of the error of an attempt that hit the job's timeout, or None.

    Identified structurally: an entry of the snapshot's ``attempts`` whose ``status`` is
    llm-kit's timeout status. (The job itself then reads ``failed``, with error code
    ``execution``, which is why it was shown as failed before.)
    """
    for attempt in (snapshot or {}).get("attempts") or []:
        if isinstance(attempt, dict) and attempt.get("status") == ATTEMPT_TIMEOUT_STATUS:
            message = ((attempt.get("error") or {}).get("message") or "").strip()
            return message.splitlines()[0] if message else "the attempt timed out"
    return None


class Scheduler:
    def __init__(
        self,
        *,
        root: Path,
        db: RuntimeDB,
        mutator: TaskMutator,
        edge,
        config_path: Path | None = None,
        python: str | None = None,
        spawn: Callable[[list[str], Path], None] | None = None,
        log: Callable[[str], None] = lambda message: None,
        observer: "cli_check.CliObserver | None" = None,
    ) -> None:
        self.root = Path(root)
        self.db = db
        self.mutator = mutator
        self.edge = edge
        self._config_path = config_path
        self._python = python or sys.executable
        self._spawn = spawn or self._default_spawn
        self._log = log
        self.observer = observer
        self._tick_lock = threading.Lock()
        self._queue_lock = threading.Lock()
        self._hold_gate = threading.Lock()
        # Per task folder: bumped (under the gate) by every hold registration, so a Re-check can tell
        # that a /task work session started after the Re-check began.
        self._hold_epoch: dict[str, int] = {}
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._reported: dict[str, str] = {}
        self._repo_keys: dict[str, tuple[float, str | None]] = {}

    # -- configuration and capabilities --------------------------------------

    def config(self) -> DashboardConfig:
        return load_config(self._config_path)

    def capabilities(self) -> dict:
        status = self.edge.status()
        run = {"available": status.available, "reason": None if status.available else status.detail,
               "state": status.state}
        return {
            "read": True,
            "run": run,
            "mutations": {
                "available": True,
                "actions": ["queue", "retry", "recheck", "cancel", "newtask", "additem", "defer", "undefer",
                            "queueall", "remove", "runoptions"],
                "reason": "New task, Add item, Defer, Queue, Retry, Re-check, Remove and Cancel run are live; "
                          "Pause worker arrives in a later increment.",
            },
            "run_options": self._run_options_capabilities(status),
            "subagents": {"claude": self._subagents_capability()},
        }

    def subagent_decision(self, config: DashboardConfig, worker: str | None = "claude") -> subagents.Decision:
        """Whether a run of ``worker`` gets native sub-agents: the characterization record against the installed Claude Code.

        Never blocks and never spawns: the binary's identity (digest, version) comes from the per-binary observation
        the tick has already waited for, and the record is one small file. A missing, stale or failed record is an
        OFF decision, never a refusal.
        """
        key = cli_check.claude_key() if (self.observer is not None and worker == "claude") else None
        found = self.observer.state_for(key) if key is not None else None
        return subagents.decide(worker, config, found.binary if found is not None else None, subagents.read_record(self.root), found)

    def _subagents_capability(self) -> dict:
        """The ``subagents.claude`` capability: the decision a Claude run would get, plus what the per-binary check knows."""
        try:
            decision = self.subagent_decision(self.config())
            body = decision.to_json()
        except DashboardConfigError as exc:
            body = {"state": "off", "code": "config", "reason": f"the dashboard configuration cannot be read ({exc})",
                    "command": subagents.CHARACTERIZE_COMMAND, "limits": None, "residuals": list(subagents.RESIDUALS),
                    "closed": list(subagents.CLOSED), "deny_tools": list(floor.deny_tools())}
        body["cli_check"] = self.cli_check_summary()
        return body

    def helper_names_by_checkout(self, roots: dict[str, str]) -> dict[str, list[str]]:
        """``{checkout id: names of the endpoint helpers offered there}`` for ``roots`` (checkout id -> working root).

        Empty while no helper is configured. A checkout whose origin cannot be read is offered none (a run there
        is refused with the reason, see ``translate._helper_offers``); the repository key is cached briefly
        because the page asks on every poll.
        """
        try:
            config = self.config()
        except DashboardConfigError:
            return {}
        if not config.endpoint_helpers:
            return {}
        now = time.monotonic()
        out: dict[str, list[str]] = {}
        for checkout, root in roots.items():
            cached = self._repo_keys.get(root)
            if cached is None or now - cached[0] > REPO_KEY_TTL_S:
                try:
                    key: str | None = repo_identity.repo_key_for(Path(root))
                except vcs.GitError as exc:  # git cannot read this checkout's origin: it is offered nothing; say so once
                    key = None
                    self._report_once(f"repo-key:{root}", f"cannot read the origin of {root}: {exc}")
                cached = self._repo_keys[root] = (now, key)
            out[checkout] = [o["name"] for o in endpoint_helper.offers_for(config, cached[1])] if cached[1] else []
        return out

    def cli_check_summary(self) -> dict:
        """What the per-binary denial observation knows right now, as JSON (``state`` is never missing)."""
        if self.observer is None:
            return {"state": "unchecked", "detail": "no observer is attached"}
        key = cli_check.claude_key()
        if key is None:
            return {"state": cli_check.UNOBSERVED, "detail": "no claude executable is on PATH"}
        found = self.observer.state_for(key)
        if found is not None:
            return found.summary()
        if self.observer.in_flight(key):
            return {"state": "checking", "detail": f"checking Claude Code at {key.realpath}"}
        return {"state": "unchecked", "detail": "this Claude Code binary has not been checked yet"}

    def _run_options_capabilities(self, status) -> dict:
        """What the run-options controls offer: defaults, and per worker whether it can run and its effort menu."""
        try:
            config = self.config()
        except DashboardConfigError as exc:
            return {"error": str(exc), "defaults": None, "workers": {}}
        workers: dict[str, dict] = {}
        for worker in ("claude", "codex"):
            entry = config.entry_for(worker)
            info: dict = {"available": False, "entry": entry, "efforts": [], "reason": None}
            if worker == "codex" and not CODEX_FLOOR_PROVED:
                info["reason"] = ("Codex workers are refused until a live Codex-side policy test shows the same "
                                  "git/publication/delete/nested-worker floor Claude gets")
            elif not status.available:
                info["reason"] = status.detail
            else:
                try:
                    found = self.edge.entry(entry)
                    if found is None:
                        info["reason"] = f"model entry {entry!r} is not in the model registry"
                    elif found.adapter != f"{worker}-cli":
                        info["reason"] = f"model entry {entry!r} does not run on the {worker} worker"
                    else:
                        info["efforts"] = list(self.edge.effort_menu(entry))
                        info["available"] = True
                except EdgeError as exc:
                    info["reason"] = str(exc)
            workers[worker] = info
        return {
            "defaults": {"worker": config.default_worker, "effort": config.effort, "timeout_s": config.default_timeout_s},
            "workers": workers,
        }

    def busy_count(self) -> int:
        return len(self.db.runs(ACTIVE_STATES))

    def run_dir_for(self, project_id: str, run_id: str) -> Path:
        return self.root / "projects" / project_id / "runs" / run_id

    # -- holds ---------------------------------------------------------------

    def hold_for(self, folder: str) -> str | None:
        holds = self.db.live_holds(folder)
        if not holds:
            return None
        return f"{len(holds)} Claude Code session(s) are working this task in the harness; the dashboard yields"

    def register_hold(self, folder: str, session: str, ident: proctree.ProcIdent) -> Run | None:
        """Register an interactive hold; the dashboard run active on the folder (to tell the session), or None.

        Takes ONLY the hold gate: never the tick lock (a tick can run for 20 s and the handshake gives
        up at 2 s) and never the process lock. The gate is held by a write only for its short
        check-and-commit, so this waits for at most one such section.
        """
        with self._hold_gate:
            self.db.add_hold(folder, session, ident)
            key = folder_key(folder)
            self._hold_epoch[key] = self._hold_epoch.get(key, 0) + 1
            return self.db.active_for_folder(folder)

    def _refuse_if_held(self, folder: Path) -> None:
        if self.db.live_holds(str(folder)):
            raise QueueRefused(409, "held", HELD_MESSAGE)

    def _refuse_if_live(self, folder: Path, *, except_run: str | None = None) -> None:
        """Refuse (inside the gate) while a Claude Code session holds the folder or a dashboard run works it.

        A block writes the task folder, so it must not land under a live run. ``except_run`` names a run that
        is itself the writer's subject (a run that was just leased and is STARTING: its own run-active state
        is not a reason to refuse the block of its own job-file failure); any OTHER active run still refuses.
        """
        self._refuse_if_held(folder)
        key = folder_key(folder)
        if any(r.id != except_run and folder_key(r.folder) == key for r in self.db.runs(ACTIVE_STATES)):
            raise QueueRefused(409, "run-active", RUN_ACTIVE_MESSAGE)

    def _authoring_precommit(self, folder: Path, *, project_root: Path, task_ref: str, item_id: str | None = None) -> Callable[[], None]:
        """The checks made inside the hold gate immediately before the first replace of an authoring write."""
        def check() -> None:
            self._refuse_if_held(folder)
            if self.db.active_for_folder(str(folder)) is not None:
                raise QueueRefused(409, "run-active", RUN_ACTIVE_MESSAGE)
            if item_id is not None and self.db.live_for(project_id_for(project_root), task_ref, item_id) is not None:
                raise QueueRefused(409, "item-has-live-run", ITEM_RUN_MESSAGE)
        return check

    # -- guarded writes -------------------------------------------------------

    def _write(self, folder: Path, edit, *, expected_revision: str | None, precommit: Callable[[], None] | None) -> str:
        """One task-folder write through the gate; every failure becomes a typed refusal."""
        try:
            return self.mutator.mutate(folder, edit, expected_revision=expected_revision, gate=self._hold_gate,
                                       precommit=precommit)
        except MutationError as exc:
            raise refusal_from_mutation(exc, folder) from exc
        except authoring.AuthoringRefused as exc:
            raise QueueRefused(422, exc.code, str(exc)) from exc

    def _read_revision(self, folder: Path) -> str:
        try:
            return folder_revision(folder)
        except MutationError as exc:
            raise QueueRefused(422, "unreadable", str(exc)) from exc

    # -- run options -----------------------------------------------------------

    def validate_options(self, body: dict, config: DashboardConfig | None = None, project_root: str | Path | None = None) -> dict:
        """The run options a client sent, checked as a REQUEST: any problem is a 422 and nothing is written.

        A bad option describes a run, not the task, so it never blocks the task (translation-time
        option reasons end the run instead). ``project_root`` is the checkout the run will execute in, so the
        endpoint helpers offered there are checked too.
        """
        if not isinstance(body, dict):
            raise QueueRefused(422, "bad-options", "options must be an object")
        options = _options_from(body)
        if config is None:
            try:
                config = self.config()
            except DashboardConfigError as exc:
                raise QueueRefused(422, "config", str(exc)) from exc
        reasons = option_reasons(RunOptions(**options), config, self.edge, project_root)
        if not reasons:
            ignored = self._cli_ignored_reason(RunOptions(**options).resolved(config))
            if ignored is not None:
                reasons = [ignored]
        if reasons:
            raise QueueRefused(422, "bad-options", "the run options are not usable: " + "; ".join(r.line() for r in reasons),
                               reasons=_reasons_json(reasons))
        return options

    def _cli_ignored_reason(self, options: RunOptions) -> Reason | None:
        """The refusal for a Claude run when the installed CLI was observed to ignore a listed denial (else None)."""
        if options.worker != "claude" or self.observer is None:
            return None
        key = cli_check.claude_key()
        found = self.observer.state_for(key) if key is not None else None
        if found is None or found.state != cli_check.IGNORED:
            return None
        return Reason("cli-deny-ignored", f"the installed Claude Code does not honor the run's denials, so a Claude run is refused: {found.detail}")

    def _require_edge(self) -> None:
        status = self.edge.status()
        if not status.available:
            raise QueueRefused(503, "llm-kit-unavailable", status.detail)

    # -- actions -------------------------------------------------------------

    def queue(self, *, project_root: str, task_ref: str, item_id: str, body: dict,
              expected_revision: str | None, retry_of: str | None = None) -> Run:
        """Translate and enqueue one item. Serialized, so two requests cannot both queue the same item."""
        with self._queue_lock:
            return self._queue(project_root=project_root, task_ref=task_ref, item_id=item_id, body=body,
                               expected_revision=expected_revision, retry_of=retry_of)

    def _admit(self, project_root: str, task_ref: str, item_id: str, options_in: dict, config: DashboardConfig,
               revision: str, project_id: str):
        """Translate one item for a new run; ``(run_id, translation)``. Nothing is created."""
        run_id = uuid.uuid4().hex[:16]
        result = self._translate(Path(project_root), task_ref, item_id, RunOptions(**options_in), config, run_id,
                                 self.run_dir_for(project_id, run_id), revision)
        return run_id, result

    def _queue(self, *, project_root: str, task_ref: str, item_id: str, body: dict,
               expected_revision: str | None, retry_of: str | None) -> Run:
        folder = Path(project_root) / task_ref
        current = self._read_revision(folder)
        if expected_revision is not None and current != expected_revision:
            raise QueueRefused(409, "stale-revision", "the task changed since this page last loaded it",
                               current_revision=current)
        self._require_edge()
        project_id = project_id_for(Path(project_root))
        if self.db.live_for(project_id, task_ref, item_id):
            raise QueueRefused(409, "already-queued", f"{item_id} already has a queued or running dashboard run")
        try:
            config = self.config()
        except DashboardConfigError as exc:
            raise QueueRefused(422, "config", str(exc)) from exc
        options_in = self.validate_options(body, config, project_root)
        run_id, result = self._admit(project_root, task_ref, item_id, options_in, config, current, project_id)
        if result.reasons:
            blocking = [r for r in result.reasons if r.blocking]
            hard = [r for r in blocking if r.code not in SOURCE_CHANGED]
            if blocking and not hard:
                raise QueueRefused(422, "not-ready", "the item is not ready to run: " + "; ".join(r.line() for r in blocking),
                                   reasons=_reasons_json(result.reasons), current_revision=current)
            if hard:
                self._block_on_queue(folder, [(item_id, hard)], f"dashboard queue of {item_id}", result.reasons, current)
        title = next((i.get("title", item_id) for i in (raw_items((folder / "plan.md").read_text(encoding="utf-8")) or [])
                      if i.get("id") == item_id), item_id)
        run = self.db.enqueue(run_id=run_id, project_id=project_id, project_root=str(project_root), task_ref=task_ref,
                              item_id=item_id, item_title=str(title), options=options_in, source_revision=current,
                              retry_of=retry_of)
        self.wake()
        return run

    def _block_on_queue(self, folder: Path, hard_by_item: list[tuple[str, list[Reason]]], source: str,
                        reasons: tuple, revision: str) -> None:
        """Queue (single or all) found a translation problem with the item(s): block the task, then refuse. Never returns.

        The block carries the revision the translation read, so a harness fix that landed meanwhile is a
        409 and not a block on stale text. A task a Claude Code session holds, or a dashboard run works, is
        not blocked at all.
        """
        flat = [r for _id, rs in hard_by_item for r in rs]
        summary = "; ".join(r.line() for r in flat)
        try:
            if len(hard_by_item) == 1:
                item_id, rs = hard_by_item[0]
                self._block_for(folder, item_id, rs, source, revision=revision, refuse_live=True)
            else:
                record_blocks(
                    self.mutator, folder,
                    items=[(i, f"{rs[0].code}: {rs[0].detail}") for i, rs in hard_by_item],
                    log_detail=f"{source}: not translated into jobs: " + concise("; ".join(f"{i}: {rs[0].line()}" for i, rs in hard_by_item)),
                    expected_revision=revision, gate=self._hold_gate, precommit=lambda: self._refuse_if_live(folder))
        except QueueRefused as live:  # (a session or a run works the task: the fix happens there, not in a block)
            who = {"held": "a Claude Code session is working it", "run-active": "a dashboard run is working it"}.get(live.code)
            if who is None:
                raise
            raise QueueRefused(422, "translation-failed", f"the item could not be translated into a job; the task was not "
                               f"blocked because {who}: " + summary,
                               reasons=_reasons_json(reasons)) from live
        except MutationError as exc:
            raise refusal_from_mutation(exc, folder) from exc
        raise QueueRefused(422, "translation-failed", "the item could not be translated into a job, so the task is now blocked: "
                           + summary, reasons=_reasons_json(reasons), blocked=True)

    def queue_all(self, *, project_root: str, task_ref: str, expected_revision: str, body: dict) -> list[Run]:
        """Queue every available item without a live run, in ``task items`` order, or none of them."""
        with self._queue_lock:
            folder = Path(project_root) / task_ref
            current = self._read_revision(folder)
            if current != expected_revision:
                raise QueueRefused(409, "stale-revision", "the task changed since this page last loaded it",
                                   current_revision=current)
            self._require_edge()
            project_id = project_id_for(Path(project_root))
            try:
                config = self.config()
            except DashboardConfigError as exc:
                raise QueueRefused(422, "config", str(exc)) from exc
            options_in = self.validate_options(body, config, project_root)
            read = read_task_items(folder, HAND_OFF)
            candidates = [i for i in sort_items(read.items) if i.state == "available"
                          and self.db.live_for(project_id, task_ref, i.id) is None]
            if not candidates:
                raise QueueRefused(422, "nothing-to-queue", "no available item without a queued or running run")
            admitted = []
            hard_by_item: list[tuple[str, list[Reason]]] = []
            source_changed: list[Reason] = []
            every_reason: list[Reason] = []
            for item in candidates:
                run_id, result = self._admit(project_root, task_ref, item.id, options_in, config, current, project_id)
                every_reason.extend(result.reasons)
                blocking = [r for r in result.reasons if r.blocking]
                hard = [r for r in blocking if r.code not in SOURCE_CHANGED]
                if hard:
                    hard_by_item.append((item.id, hard))
                elif blocking:
                    source_changed.extend(blocking)
                admitted.append((run_id, item))
            if hard_by_item:
                self._block_on_queue(folder, hard_by_item, f"dashboard queue of {len(candidates)} item(s)", tuple(every_reason), current)
            if source_changed:
                raise QueueRefused(422, "not-ready", "the items are not ready to run: " + "; ".join(r.line() for r in source_changed),
                                   reasons=_reasons_json(every_reason), current_revision=current)
            runs = self.db.enqueue_many([
                {"run_id": run_id, "project_id": project_id, "project_root": str(project_root), "task_ref": task_ref,
                 "item_id": item.id, "item_title": item.title, "options": options_in, "source_revision": current}
                for run_id, item in admitted])
            self.wake()
            return runs

    def retry(self, run_id: str, *, expected_revision: str | None) -> Run:
        old = self.db.get(run_id)
        if old is None:
            raise QueueRefused(404, "unknown-run", f"no run {run_id}")
        if old.state not in RETRYABLE_STATES:
            raise QueueRefused(409, "not-retryable", f"run {run_id} is {old.state}; only failed, timed-out or cancelled runs retry. "
                               "A blocked run is resolved in Claude Code, then Re-checked.")
        return self.queue(project_root=old.project_root, task_ref=old.task_ref, item_id=old.item_id, body=dict(old.options),
                          expected_revision=expected_revision, retry_of=old.id)

    def cancel(self, run_id: str) -> Run:
        """Ask a run to stop. Process control on an immutable run id: no task revision is involved.

        Validated against the run's own state under the scheduler lock (the same lock a tick holds
        while it starts or settles runs), so a request can never race a state change it did not see:
        a queued run is cancelled outright; a starting or running one gets ``cancel.json`` (the
        runner stops the tree and closes the ledger) and becomes ``cancelling``; asking again is
        accepted and changes nothing; any other state is refused with 409 and named.
        """
        with self._tick_lock:
            run = self.db.get(run_id)
            if run is None:
                raise QueueRefused(404, "unknown-run", f"no run {run_id}")
            if run.state == CANCELLING:
                return run
            if run.state not in CANCELLABLE_STATES:
                raise QueueRefused(409, "not-cancellable", f"run {run_id} is {run.state}; only a queued, starting or "
                                   "running run can be cancelled.")
            if run.state == QUEUED:
                self.db.transition(run.id, (QUEUED,), CANCELLED, code="cancelled",
                                   detail="cancelled before it started; nothing ran and nothing was created")
                return self.db.get(run.id)  # type: ignore[return-value]
            run_dir = Path(run.run_dir) if run.run_dir else None
            if run_dir is None or not run_dir.is_dir():
                raise QueueRefused(409, "not-cancellable", f"run {run_id} is {run.state} but has no run directory to signal")
            if not (run_dir / runfiles.CANCEL_FILE).exists():
                runfiles.write_json(run_dir / runfiles.CANCEL_FILE,
                                    {"requested_at": _utc_now(), "reason": USER_CANCEL_REASON, "run": run.id})
            if run.state == RUNNING:
                self.db.transition(run.id, (RUNNING,), CANCELLING, code="cancelling",
                                   detail="stop requested; the runner is stopping the process tree and closing the ledger")
            self.wake()
            return self.db.get(run.id)  # type: ignore[return-value]

    def remove(self, run_id: str) -> Run:
        """Take a queued entry out of the queue (``queued`` -> ``removed``). A runtime record: validated by its own state."""
        with self._tick_lock:
            run = self.db.get(run_id)
            if run is None:
                raise QueueRefused(404, "unknown-run", f"no run {run_id}")
            if run.state != QUEUED:
                raise QueueRefused(409, "not-queued", f"run {run_id} is {run.state}; only a queued entry can be removed. "
                                   "Cancel a run that has started.")
            self.db.transition(run.id, (QUEUED,), REMOVED, code="removed",
                               detail="removed from the queue before it started; nothing ran and nothing was created")
            return self.db.get(run.id)  # type: ignore[return-value]

    def set_run_options(self, *, project_root: str, task_ref: str, item_id: str, options: dict) -> Run:
        """Replace the options of the item's QUEUED entry. Validated against the entry's own state, like Cancel."""
        self._require_edge()
        with self._tick_lock:
            run = self.db.live_for(project_id_for(Path(project_root)), task_ref, item_id)
            if run is None or run.state != QUEUED:
                raise QueueRefused(409, "not-queued", f"{item_id} has no queued entry (it is "
                                   f"{run.state if run else 'not queued'}); options can be changed only while it waits")
            options_in = self.validate_options(options, None, project_root)
            if not self.db.set_options(run.id, options_in):
                raise QueueRefused(409, "not-queued", f"run {run.id} started while its options were being saved")
            return self.db.get(run.id)  # type: ignore[return-value]

    def recheck(self, *, project_root: str, task_ref: str, expected_revision: str | None) -> dict:
        """Re-validate a task and clear its block; ends the holds that predate this request, never a newer one.

        Reads the folder's hold epoch first. The holds are released INSIDE the hold gate, in the write's
        ``precommit``, and only when no hold registered meanwhile (a new ``/task work`` bumps the epoch):
        in that case nothing is written or released and the answer is 409 ``held``.
        """
        folder = Path(project_root) / task_ref
        key = folder_key(folder)
        epoch = self._hold_epoch.get(key, 0)
        released = {"n": 0}

        def release() -> None:
            if self._hold_epoch.get(key, 0) != epoch:
                raise QueueRefused(409, "held", "a Claude Code session started working this task while Re-check ran; "
                                   "Re-check again when it is done")
            released["n"] = self.db.clear_holds(str(folder))

        current = self._read_revision(folder)
        if expected_revision is not None and current != expected_revision:
            raise QueueRefused(409, "stale-revision", "the task changed since this page last loaded it", current_revision=current)
        result = validate_ref(task_ref, Path(project_root))
        findings = findings_mod.gating_of(result)
        plan = (folder / "plan.md").read_text(encoding="utf-8") if (folder / "plan.md").is_file() else ""
        blocked_items = [i.get("id") for i in (raw_items(plan) or []) if i.get("state") == "blocked-user"]
        problems = [Reason("validation", f) for f in findings] + [
            Reason("item-blocked", f"item {i} is still blocked-user") for i in blocked_items]
        if problems:
            with self._hold_gate:  # the refusal still ends the holds that predate this request
                if self._hold_epoch.get(key, 0) == epoch:
                    self.db.clear_holds(str(folder))
            raise QueueRefused(422, "recheck-failed", "the task is not clear yet: " + "; ".join(p.line() for p in problems[:5]),
                               reasons=_reasons_json(problems), current_revision=current)

        changed = {"value": False}

        def edit(texts: TaskTexts) -> dict[str, str]:
            if stored_status(texts.task_yaml) != "blocked":
                return {}
            changed["value"] = True
            return {TASK_YAML: set_task_status(texts.task_yaml, "active"),
                    LOG: append_log(texts.log, "dashboard: re-check cleared the block; the task is active again")}

        revision = self._write(folder, edit, expected_revision=current, precommit=release)
        return {"changed": changed["value"], "holds_released": released["n"], "revision": revision}

    # -- authoring ------------------------------------------------------------------

    def add_item(self, *, project_root: str, task_ref: str, expected_revision: str, title, priority, details,
                 queue: bool = False, options: dict | None = None) -> dict:
        """Add an available item (and its plan.md section); optionally queue it. See authoring.py for the text edit.

        ``{"item", "revision", "run" | "queue_error"}``. The write is refused (409) while a Claude Code
        session holds the task or a dashboard run works it. When ``queue`` is set and the add
        succeeded but the queue step was refused, the add stands and ``queue_error`` says why.
        """
        try:
            title = authoring.check_title(title)
            priority = authoring.check_priority(priority)
            details = authoring.check_details(details)
        except authoring.AuthoringRefused as exc:
            raise QueueRefused(422, exc.code, str(exc)) from exc
        options_in: dict = {}
        if queue:
            if not details:
                raise QueueRefused(422, "details-required", "an item that is queued needs details: they are the worker's instructions")
            self._require_edge()
            options_in = self.validate_options(options or {}, None, project_root)
        folder = Path(project_root) / task_ref
        added: dict = {}

        def edit(texts: TaskTexts) -> dict[str, str]:
            if stored_status(texts.task_yaml) not in ("active", "blocked"):
                raise authoring.AuthoringRefused("task-not-open", "items can be added only to an active or blocked task")
            plan, item = authoring.add_item_to_plan(texts.plan, title=title, priority=priority, details=details)
            added.update(item)
            return {PLAN: plan, LOG: append_log(texts.log, f"dashboard: added item {item['id']} ({title})")}

        with self._queue_lock, self._tick_lock:
            revision = self._write(folder, edit, expected_revision=expected_revision,
                                   precommit=self._authoring_precommit(folder, project_root=Path(project_root), task_ref=task_ref))
        outcome: dict = {"item": {"id": added["id"], "title": added["title"], "state": added["state"],
                                  "priority": added.get("priority")}, "revision": revision}
        if queue:
            try:
                outcome["run"] = self.queue(project_root=project_root, task_ref=task_ref, item_id=added["id"],
                                            body=options_in, expected_revision=revision)
            except QueueRefused as exc:
                outcome["queue_error"] = {"code": exc.code, "message": str(exc), "reasons": exc.reasons, "blocked": exc.blocked}
        return outcome

    def defer_item(self, *, project_root: str, task_ref: str, item_id: str, expected_revision: str, state: str) -> dict:
        """available -> deferred or deferred -> available; the item's note is untouched."""
        if state not in ("deferred", "available"):
            raise QueueRefused(422, "bad-transition", "state must be deferred or available")
        folder = Path(project_root) / task_ref

        def edit(texts: TaskTexts) -> dict[str, str]:
            current = next((i.get("state") for i in (raw_items(texts.plan) or []) if i.get("id") == item_id), None)
            if (current, state) not in (("available", "deferred"), ("deferred", "available")):
                raise authoring.AuthoringRefused("bad-transition", f"{item_id} is {current!r}; only available <-> deferred is allowed here")
            verb = "deferred" if state == "deferred" else "undeferred"
            return {PLAN: set_item_state(texts.plan, item_id, state), LOG: append_log(texts.log, f"dashboard: {verb} item {item_id}")}

        with self._queue_lock, self._tick_lock:
            revision = self._write(folder, edit, expected_revision=expected_revision,
                                   precommit=self._authoring_precommit(folder, project_root=Path(project_root), task_ref=task_ref,
                                                                       item_id=item_id))
        return {"item_id": item_id, "state": state, "revision": revision}

    def new_task(self, *, project_root: str, title, priority, first_item) -> dict:
        """Create a dev/tasks task with the task system's own ``init``; priority and a first item follow in one write.

        A brand-new folder has no reader yet, so creation needs no gate; the target path is refused when
        a hold already names it. A failed follow-up leaves the (valid) created task and says so.
        """
        try:
            title = authoring.check_title(title)
            priority = authoring.check_priority(priority)
            first = None
            if first_item is not None:
                if not isinstance(first_item, dict):
                    raise authoring.AuthoringRefused("bad-title", "first_item must be an object")
                first = (authoring.check_title(first_item.get("title")), authoring.check_details(first_item.get("details")))
            stub, _ = task_init.derive_stub_and_title(title)
        except authoring.AuthoringRefused as exc:
            raise QueueRefused(422, exc.code, str(exc)) from exc
        except task_init.InitError as exc:
            raise QueueRefused(422, "bad-title", str(exc)) from exc
        root = Path(project_root)
        canonical = f"dev/tasks/{stub}"
        folder = (root / canonical).absolute()
        with self._queue_lock, self._tick_lock:
            if self.db.live_holds(str(folder)):
                raise QueueRefused(409, "held", HELD_MESSAGE)
            if folder.exists():
                raise QueueRefused(409, "task-exists", f"{canonical} already exists", current_revision=self._safe_revision(folder))
            try:
                created = task_init.init_task(title, root, dest="dev/tasks")
            except FileExistsError as exc:
                raise QueueRefused(409, "task-exists", f"{canonical} already exists", current_revision=self._safe_revision(folder)) from exc
            except task_init.InitError as exc:
                if "already exists" in str(exc):
                    raise QueueRefused(409, "task-exists", str(exc), current_revision=self._safe_revision(folder)) from exc
                raise QueueRefused(422, "init-failed", str(exc)) from exc
            revision = self._read_revision(created)
            outcome: dict = {"path": canonical, "task_ref": canonical, "revision": revision}
            if priority or first:
                added: dict = {}

                def edit(texts: TaskTexts) -> dict[str, str]:
                    changes: dict[str, str] = {}
                    parts = []
                    if priority:
                        changes[TASK_YAML] = set_task_fields(texts.task_yaml, priority=priority)
                        parts.append(f"priority {priority}")
                    if first:
                        plan, item = authoring.add_item_to_plan(texts.plan, title=first[0], priority=None, details=first[1])
                        added.update(item)
                        changes[PLAN] = plan
                        parts.append(f"first item {item['id']}")
                    changes[LOG] = append_log(texts.log, "dashboard: created task with " + ", ".join(parts))
                    return changes

                try:
                    outcome["revision"] = self._write(
                        created, edit, expected_revision=revision,
                        precommit=self._authoring_precommit(created, project_root=root, task_ref=canonical))
                except QueueRefused as exc:
                    outcome["followup_error"] = {"code": exc.code, "message": str(exc), "file": exc.extra.get("file"),
                                                 "written": exc.extra.get("written", [])}
                    outcome["revision"] = self._safe_revision(created) or revision
        return outcome

    @staticmethod
    def _safe_revision(folder: Path) -> str | None:
        try:
            return folder_revision(folder)
        except MutationError:
            return None

    # -- the loop -------------------------------------------------------------

    def start(self, interval_s: float = 1.0) -> None:
        self._thread = threading.Thread(target=self._loop, args=(interval_s,), name="scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
        if self.observer is not None:
            self.observer.close()

    def wake(self) -> None:
        self._wake.set()

    def _loop(self, interval_s: float) -> None:
        # First, on this thread (so the server answers requests meanwhile, and a slow stop or cancel
        # command cannot hold the launcher's health check): reconcile every run that holds a lease.
        try:
            self.adopt()
        except Exception:  # noqa: BLE001
            self._log("adopting the runs of the previous server failed:\n" + traceback.format_exc())
        while not self._stop.is_set():
            self._wake.wait(interval_s)
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                self.tick()
            except Exception:  # noqa: BLE001 -- a tick must never kill the loop; the traceback is the loud part
                self._log("scheduler tick failed:\n" + traceback.format_exc())

    def adopt(self) -> None:
        """On server start: reconcile every run that holds a lease before anything new starts.

        A runner outlives the server that spawned it, so each active run is re-read from its run
        directory (rows 0-22 of the table in the module docstring) and either adopted, settled, or
        blocked. Nothing is requeued unless no run directory exists, and no directory is deleted.
        """
        with self._tick_lock:
            self._reconcile_active()

    def tick(self) -> None:
        with self._tick_lock:
            self._reconcile_active()
            self._start_queued()

    def _reconcile_active(self) -> None:
        for run in self.db.runs(ACTIVE_STATES):
            try:
                self._reconcile(run)
            except Exception:  # noqa: BLE001 -- a run the server cannot read is a crash-inspection block, never a loop
                trace = traceback.format_exc()
                self._log(f"reconciling run {run.id} failed:\n{trace}")
                try:
                    self._inspection_block(run, "reconcile-error", f"the server could not reconcile this run: {trace.strip().splitlines()[-1]}")
                except Exception:  # noqa: BLE001
                    self._log(f"run {run.id} could not even be marked crashed:\n" + traceback.format_exc())

    # -- starting -------------------------------------------------------------

    def start_order(self, queued: list[Run]) -> list[Run]:
        """``queued`` in the order the scheduler walks it: least-recently-started checkout first, FIFO within one.

        The key is ``(newest started_at of the run's checkout or "", the checkout's oldest queued seq, seq)``: a
        checkout that never started comes before one that did, and checkouts that tie keep first-come order, so one
        checkout queuing many items cannot starve another that queued one.
        """
        last = self.db.last_started_by_checkout()
        first_seq: dict[str, int] = {}
        for run in queued:
            first_seq[run.project_id] = min(first_seq.get(run.project_id, run.seq), run.seq)
        return sorted(queued, key=lambda r: (last.get(r.project_id, ""), first_seq[r.project_id], r.seq))

    def wait_reason(self, run: Run, *, busy: set[str], ahead: bool, held: bool, config: DashboardConfig | None,
                    config_error: str | None, edge_status, cli_state: str | None) -> dict | None:
        """Why the queued ``run`` cannot start now, as ``{"code", "detail"}``, or None when nothing holds it back.

        Shared by the start loop's pre-check and the UI. Codes: ``config``, ``llm-kit-unavailable``, ``held``,
        ``checkout-busy``, ``queued-behind`` (an earlier entry of the same checkout goes first), ``no-slot`` and
        ``cli-check`` (an observation of a new Claude binary is in flight).
        """
        if config is None:
            return {"code": "config", "detail": config_error or "the dashboard configuration cannot be read"}
        if not edge_status.available:
            return {"code": "llm-kit-unavailable", "detail": edge_status.detail}
        if held:
            return {"code": "held", "detail": "a Claude Code session is working this task; the dashboard yields"}
        if run.project_id in busy:
            return {"code": "checkout-busy", "detail": "another run is working in this checkout"}
        if ahead:
            return {"code": "queued-behind", "detail": "an earlier entry of this checkout starts first"}
        if len(busy) >= config.max_parallel_projects:
            return {"code": "no-slot", "detail": f"{len(busy)} of {config.max_parallel_projects} checkouts are running"}
        if cli_state == "wait":
            return {"code": "cli-check", "detail": "checking the installed Claude Code before the first Claude run"}
        return None

    def queue_view(self) -> dict[str, tuple[int, dict | None]]:
        """``{run_id: (position, waiting)}`` for every queued run, positions in start order (1 = next)."""
        queued = self.db.runs((QUEUED,))
        if not queued:
            return {}
        try:
            config, config_error = self.config(), None
        except DashboardConfigError as exc:
            config, config_error = None, str(exc)
        status = self.edge.status()
        active = self.db.runs(ACTIVE_STATES)
        busy = {r.project_id for r in active}
        held = self.db.held_folders()
        view: dict[str, tuple[int, dict | None]] = {}
        seen_unheld: set[str] = set()
        key = cli_check.claude_key() if self.observer is not None else None
        for position, run in enumerate(self.start_order(queued), start=1):
            is_held = folder_key(run.folder) in held
            cli_state = None
            if config is not None and self._resolved_worker(run, config) == "claude":
                cli_state = self._cli_gate(run, config, request=False, key=key)[0]
            view[run.id] = (position, self.wait_reason(
                run, busy=busy, ahead=run.project_id in seen_unheld, held=is_held, config=config, config_error=config_error,
                edge_status=status, cli_state=cli_state))
            if not is_held:
                seen_unheld.add(run.project_id)
        return view

    @staticmethod
    def _resolved_worker(run: Run, config: DashboardConfig) -> str | None:
        return RunOptions(**run.options).resolved(config).worker

    def _cli_gate(self, run: Run, config: DashboardConfig, *, request: bool = True,
                  key: "cli_check.ClaudeKey | None | str" = "resolve") -> tuple[str, Reason | None]:
        """``("go" | "wait" | "refuse", reason)`` for a run's Claude binary. Never blocks, never spawns.

        ``key`` is the binary's identity when the caller already resolved it once for many runs (``None``: no
        claude on PATH); the default resolves it here (a which, a realpath, a stat).

        ``wait``: the binary is unknown and its observation was asked for (or is running). ``refuse``: it was
        observed to ignore a listed denial. Anything else (observed, vacuous, unobserved, no observer, no binary,
        a Codex run) is ``go``: the documented denials ride along either way.
        """
        if self.observer is None or self._resolved_worker(run, config) != "claude":
            return "go", None
        if key == "resolve":
            key = cli_check.claude_key()
        if key is None:
            return "go", None
        found = self.observer.state_for(key)  # type: ignore[arg-type]
        if found is None:
            if request:
                self.observer.request(key)  # type: ignore[arg-type]
            return "wait", Reason("cli-check", "checking the installed Claude Code before the first Claude run", blocking=False)
        if found.state == cli_check.IGNORED:
            return "refuse", self._cli_ignored_reason(RunOptions(**run.options).resolved(config))
        return "go", None

    def _start_queued(self) -> None:
        """Start AT MOST ONE queued run, then ask for another tick if more can start.

        A tick used to start every startable entry under the tick lock, each translating and waiting up to 20 s
        for its runner, so a Cancel could wait minutes. Now one entry that changes state (starts, fails, is
        skipped or blocked) ends the tick; an entry that is attempted but stays queued does not, so one stuck
        head cannot starve the others.
        """
        try:
            config = self.config()
        except DashboardConfigError as exc:
            self._report_once("config", str(exc))
            return
        if not self.edge.status().available:
            return
        queued = self.db.runs((QUEUED,))
        key = cli_check.claude_key() if self.observer is not None else None
        for run in queued:  # ask for the observation of a new Claude binary early; it runs on its own thread
            self._cli_gate(run, config, key=key)
        progressed = False
        for run in self.start_order(queued):
            # Cheap pre-checks BEFORE translating (translation runs git): a checkout that already holds a
            # lease, no free slot, a held folder or a Claude binary still being observed cannot start this run
            # now, and none of that needs the item translated. The entry stays queued in its place.
            busy = {r.project_id for r in self.db.runs(ACTIVE_STATES)}
            if run.project_id in busy or len(busy) >= config.max_parallel_projects:
                continue
            if self.db.live_holds(run.folder):
                continue
            if self._cli_gate(run, config, key=key)[0] == "wait":
                continue
            try:
                self._try_start(run, config)
            except Exception:  # noqa: BLE001 -- one run that cannot start must not keep the others from starting
                self._log(f"starting run {run.id} failed:\n" + traceback.format_exc())
                self.db.set_state(run.id, FAILED, code="dispatch-failed", detail=f"{traceback.format_exc().strip().splitlines()[-1]}")
            after = self.db.get(run.id)
            if after is None or after.state != QUEUED:
                progressed = True
                break
        if progressed and self._any_startable(config):
            self.wake()

    def _any_startable(self, config: DashboardConfig) -> bool:
        """True when some queued entry passes the cheap pre-checks right now (so another tick is worth asking for)."""
        busy = {r.project_id for r in self.db.runs(ACTIVE_STATES)}
        if len(busy) >= config.max_parallel_projects:
            return False
        return any(
            run.project_id not in busy and not self.db.live_holds(run.folder) and self._cli_gate(run, config)[0] != "wait"
            for run in self.db.runs((QUEUED,)))

    def _report_once(self, key: str, message: str) -> None:
        if self._reported.get(key) != message:
            self._reported[key] = message
            self._log(message)

    def _try_start(self, run: Run, config: DashboardConfig) -> None:
        folder = Path(run.folder)
        try:
            revision = folder_revision(folder)
        except MutationError as exc:
            self.db.set_state(run.id, SKIPPED, code="unreadable", detail=str(exc))
            return
        gate, refusal = self._cli_gate(run, config)
        if gate == "refuse":  # the installed CLI ignores a denial: the run's options are refused, the task is untouched
            self.db.set_state(run.id, FAILED, code="bad-options", detail=refusal.line())  # type: ignore[union-attr]
            return
        run_dir = self.run_dir_for(run.project_id, run.id)
        result = self._translate(Path(run.project_root), run.task_ref, run.item_id, RunOptions(**run.options), config,
                                 run.id, run_dir, revision)
        if result.reasons:
            blocking = [r for r in result.reasons if r.blocking]
            changed = [r for r in blocking if r.code in SOURCE_CHANGED]
            hard = [r for r in blocking if r.code not in SOURCE_CHANGED]
            if changed:
                self.db.set_state(run.id, SKIPPED, code="task-source-changed", detail="; ".join(r.line() for r in changed))
                return
            options_bad = [r for r in hard if r.code in OPTION_REASON_CODES]
            if options_bad:  # the run's options, not the task: the run ends, the task is untouched
                self.db.set_state(run.id, FAILED, code="bad-options", detail="; ".join(r.line() for r in options_bad))
                return
            if hard:
                try:
                    self._block_for(folder, run.item_id, hard, f"dashboard run {run.id}", revision=revision, refuse_live=True)
                    self.db.set_state(run.id, BLOCKED, code=hard[0].code, detail="; ".join(r.line() for r in hard))
                except QueueRefused:  # a hold registered, or a run works the folder: the run yields and stays queued
                    return
                except StaleRevision as exc:  # the task changed during translation: never block on stale text
                    self.db.add_event(run.id, "task-source-changed", f"re-translating next tick: {exc}")
                except MutationError as exc:
                    self.db.set_state(run.id, FAILED, code="block-not-recorded",
                                      detail=f"translation failed ({hard[0].line()}) and the block could not be recorded: {exc}")
                return
            self._report_once(run.id, "; ".join(r.line() for r in result.reasons))
            return  # only non-blocking reasons: it stays queued
        with self._hold_gate:  # the hold re-check and the lease are one step, so a hold cannot slip between them
            if self.db.live_holds(run.folder):
                return
            leased = self.db.try_lease(run.id, run.project_id, max_projects=config.max_parallel_projects)
        if not leased:
            return
        stage = _Stage()
        try:
            self._dispatch(run, result.spec, run_dir, revision, stage)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001 -- anything here leaves a loud failed run or a run the next tick reconciles
            self._log("dispatch failed:\n" + traceback.format_exc())
            if stage.spawned:
                # A runner exists (or is about to): the run directory is its home, so it is never deleted
                # here. The run stays `starting` and the next tick reconciles it from the directory
                # (rows 11-14 and 1-10 of the table in the module docstring).
                return
            if stage.created:
                shutil.rmtree(run_dir, ignore_errors=True)  # this attempt made it, and no runner ever saw it
            self.db.set_state(run.id, FAILED, code="dispatch-failed", detail=f"{type(exc).__name__}: {exc}")

    def _translate(self, project_root: Path, task_ref: str, item_id: str, options: RunOptions, config: DashboardConfig,
                   run_id: str, run_dir: Path, revision: str):
        return translate(
            project_root=project_root, task_ref=task_ref, item_id=item_id, options=options, config=config, edge=self.edge,
            hold_for=self.hold_for, run_id=run_id, run_dir=run_dir, accept_script=ACCEPT_SCRIPT, python=self._python,
            revision=revision, subagents=self.subagent_decision(config, options.resolved(config).worker),
        )

    def _block_for(self, folder: Path, item_id: str, reasons: list[Reason], source: str, *, revision: str | None,
                   refuse_live: bool = False, except_run: str | None = None) -> BlockOutcome:
        """Record a translation block. ``revision`` is the one the translation read (a harness fix that landed
        since is StaleRevision, never a block on stale text); ``refuse_live`` raises QueueRefused ``held`` or
        ``run-active`` from inside the gate when a Claude Code session holds the folder or a dashboard run works
        it. ``except_run`` is the run the block is ABOUT (leased and STARTING): its own activity never refuses."""
        detail = "; ".join(r.line() for r in reasons)
        return record_block(self.mutator, folder, item_id=item_id, note=f"{reasons[0].code}: {reasons[0].detail}",
                            log_detail=f"{source}: not translated into a job: {concise(detail)}", expected_revision=revision,
                            gate=self._hold_gate,
                            precommit=(lambda: self._refuse_if_live(folder, except_run=except_run)) if refuse_live else None)

    def _dispatch(self, run: Run, spec: JobSpec, run_dir: Path, revision: str, stage: "_Stage") -> None:
        if spec.worker == "claude":
            floor.check_job_document(spec.document, deny_tools=spec.deny_tools)
        floor.check_worker_models(spec.document, self._entry_kind)
        run_dir.mkdir(parents=True, exist_ok=False)  # exist_ok=False: a directory that is already there is not ours
        stage.created = True
        job_file = run_dir / "job.yaml"
        job_file.write_text(yaml.safe_dump(dict(spec.document), sort_keys=False, default_flow_style=False), encoding="utf-8")
        try:
            self.edge.validate_job_file(job_file)
        except EdgeError as exc:
            shutil.rmtree(run_dir, ignore_errors=True)  # created above, nothing has seen it
            try:
                # The run was leased above, so it is itself active on this folder: only a hold or ANOTHER run refuses.
                self._block_for(Path(spec.folder), spec.item_id, [Reason("job-file-invalid", str(exc))], f"dashboard run {run.id}",
                                revision=spec.start.revision, refuse_live=True, except_run=run.id)
            except (QueueRefused, StaleRevision) as why:  # a hold, or the task changed: the run fails, the task is left alone
                self.db.set_state(run.id, FAILED, code="job-file-invalid",
                                  detail=f"{exc}. The task was not blocked: {why}")
                return
            self.db.set_state(run.id, BLOCKED, code="job-file-invalid", detail=str(exc))
            return
        start = spec.start
        store = run_dir / runfiles.LEDGER_FILE
        # A run offered endpoint helpers gets ONE run-scoped token: the worker holds it, the database holds its
        # hash and the offers, and the terminal state of the run revokes it (RuntimeDB.set_state).
        run_env = dict(floor.RUN_ENV)
        run_env.update(spec.subagent_env)  # an ON run adds the two documented sub-agent limits
        token = endpoint_helper.new_token() if spec.helper_offers else None
        if token is not None:
            run_env.update(endpoint_helper.grant_env(token, self.root))
            redact.register_secret(token)
        runner_spec = {
            "schema": 1, "run_id": run.id, "project_root": spec.project_root, "folder": spec.folder,
            "task_ref": spec.task_ref, "item_id": spec.item_id, "lock_path": str(self.mutator.lock_path),
            "accept": [list(a) for a in spec.accept],
            "start": {"revision": start.revision, "vcs": start.vcs, "head": start.head, "branch": start.branch,
                      "status": list(start.status), "log_lines": start.log_lines},
            "worker": spec.worker, "entry": spec.entry, "timeout_s": spec.timeout_s, "network": spec.network,
            "runner": {
                "argv": self.edge.runner_argv(job_file, store, spec.timeout_s, run.id),
                "cancel_argv": self.edge.cancel_argv(store, run.id, runfiles.REASON_PLACEHOLDER),
                "deadline_s": spec.timeout_s + RUNNER_SLACK_S, "grace_s": RUNNER_GRACE_S,
                "env": run_env,
            },
            "subagents": dict(spec.subagents),
            "cli_check": self.cli_check_summary() if spec.worker == "claude" else None,
        }
        if token is None:
            runfiles.write_json(run_dir / runfiles.SPEC_FILE, runner_spec)
        else:
            endpoint_helper.write_private_json(run_dir / runfiles.SPEC_FILE, runner_spec)  # it carries the run's token
        self.db.set_state(run.id, STARTING, run_dir=str(run_dir))
        if token is not None:
            self.db.issue_helper_grant(run.id, endpoint_helper.hash_token(token), [dict(o) for o in spec.helper_offers])
        self._spawn([self._python, str(RUNNER_SCRIPT), str(run_dir)], run_dir)
        stage.spawned = True
        ident = self._await_runner(run_dir)
        self.db.set_state(run.id, RUNNING, runner=ident, detail=f"{spec.worker}:{spec.entry}")

    def _entry_kind(self, name: str) -> str | None:
        """``harness``, ``transport`` or None for a registry entry name (through the edge)."""
        found = self.edge.entry(name)
        return None if found is None else found.kind

    @staticmethod
    def _await_runner(run_dir: Path, timeout_s: float = 20.0) -> proctree.ProcIdent:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            path = run_dir / "runner.json"
            result = run_dir / "result.json"
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
                return proctree.ProcIdent(int(data["pid"]), float(data["create_time"]))
            if result.is_file():
                raise RuntimeError(f"the runner refused to dispatch: {result.read_text(encoding='utf-8')}")
            time.sleep(0.05)
        raise RuntimeError(f"the runner wrote no identity in {timeout_s:g} seconds; see {run_dir / 'spawn.log'}")

    @staticmethod
    def _default_spawn(argv: list[str], run_dir: Path) -> None:
        flags: dict = {}
        if sys.platform == "win32":
            flags["creationflags"] = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                                      | getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            flags["start_new_session"] = True
        with open(run_dir / "spawn.log", "ab") as log:
            subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True, **flags)

    # -- reconciling --------------------------------------------------------------
    # The state table is in the module docstring; the row numbers in the comments below are its rows.

    def _reconcile(self, run: Run) -> None:
        if not run.run_dir:
            if run.state == STARTING:
                self.db.release_to_queue(run.id)  # row 0: no directory was ever recorded, so nothing existed
            return
        run_dir = Path(run.run_dir)
        status, result = runfiles.read_json(run_dir / runfiles.RESULT_FILE)
        if status == runfiles.OK and isinstance(result.get("outcome"), str):
            self._settle(run, run_dir, result)
            return
        if status != runfiles.MISSING:  # row 1
            why = result if status == runfiles.UNREADABLE else "it has no outcome"
            self._inspection_block(run, "result-unreadable", f"the runner's result.json cannot be used ({why}); the shared tree "
                                   "may hold partial work to inspect in the harness")
            return
        ident = self._runner_ident(run, run_dir)
        if ident is None:
            if run.state == STARTING:
                self._starting_without_runner(run, run_dir)  # rows 13-14
            else:
                self._runner_vanished(run, run_dir)
            return
        if proctree.live(ident) is not None:  # rows 11-12
            if run.state == STARTING:
                cancelled = (run_dir / runfiles.CANCEL_FILE).exists()
                self.db.transition(run.id, (STARTING,), CANCELLING if cancelled else RUNNING, runner=ident,
                                   code="cancelling" if cancelled else None,
                                   detail="adopted the runner after a server restart")
            return
        status, result = runfiles.read_json(run_dir / runfiles.RESULT_FILE)
        if status != runfiles.MISSING:  # it finished between the two looks
            return  # settled by the next tick, from the top of this method
        self._runner_vanished(run, run_dir)  # rows 15-22

    @staticmethod
    def _runner_ident(run: Run, run_dir: Path) -> proctree.ProcIdent | None:
        """The runner's identity: the database's, else the one on disk (the server may have died before recording it)."""
        if run.runner_pid is not None and run.runner_ctime is not None:
            return proctree.ProcIdent(run.runner_pid, run.runner_ctime)
        status, value = runfiles.read_ident(run_dir / runfiles.RUNNER_FILE)
        return value if status == runfiles.OK else None

    def _starting_without_runner(self, run: Run, run_dir: Path) -> None:
        """Rows 13-14: a lease whose runner has not recorded itself."""
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(run.started_at or "")).total_seconds()
        except ValueError:
            age = ADOPT_GRACE_S
        if age < ADOPT_GRACE_S:
            return  # row 13: the runner may still be starting; look again next tick
        # Row 14. Write the stop marker FIRST, then look at runner.json once more. The runner writes
        # runner.json and then looks for the marker, so whichever of the two comes second sees the other:
        # a runner that wakes up late refuses to dispatch, and one that is already there is adopted.
        if not (run_dir / runfiles.CANCEL_FILE).exists():
            runfiles.write_json(run_dir / runfiles.CANCEL_FILE,
                                {"requested_at": _utc_now(), "reason": "dispatch-interrupted", "run": run.id})
        status, ident = runfiles.read_ident(run_dir / runfiles.RUNNER_FILE)
        if status == runfiles.OK:
            self.db.transition(run.id, (STARTING,), CANCELLING, runner=ident, code="cancelling",
                               detail="a runner appeared at the last moment; it has been asked to stop")
            return
        self.db.set_state(
            run.id, FAILED, code="dispatch-interrupted",
            detail=f"the server stopped while starting this run and no runner ever recorded itself within {ADOPT_GRACE_S:g} seconds. "
                   "The run directory is kept, marked so a runner that starts late will not dispatch. Nothing ran, so the "
                   "task is not blocked; Retry starts a fresh run.")

    def _runner_vanished(self, run: Run, run_dir: Path) -> None:
        """Rows 15-22: the runner is gone and wrote no result. Stop what it left, then decide from receipt and ledger."""
        child, members, problems = runfiles.read_tree(run_dir)
        reachable = proctree.reachable(members, child)
        survivors = proctree.stop_idents(reachable, grace_s=RUNNER_GRACE_S) if reachable else []
        if survivors:  # row 16
            self._inspection_block(run, "survivors", f"the runner vanished and {_pids(survivors)} of its command would not stop; "
                                   "the ledger was left as it is")
            return
        store = run_dir / runfiles.LEDGER_FILE
        try:
            view = self._ledger(store, run.id)
        except EdgeError as exc:  # row 17
            self._inspection_block(run, "ledger-unreadable", f"the runner vanished and its ledger cannot be inspected or closed: {exc}")
            return
        receipt_status, verdict = self._read_receipt(run_dir)
        if receipt_status == runfiles.UNREADABLE:  # row 18
            self._inspection_block(run, "receipt-unreadable", f"the runner vanished and the acceptance receipt cannot be read: {verdict}")
            return
        if view is not None and view.terminal:
            if receipt_status == runfiles.OK:  # row 19
                self._settle_by_verdict(run, verdict)
                return
            self._inspection_block(run, "ledger-without-receipt",  # row 20
                                   f"the runner vanished after the job ended ({', '.join(view.states)}) but acceptance left no receipt")
            return
        note = "no ledger had been created"
        if view is not None:  # row 21-22
            record = self._close_ledger(run, run_dir, "runner vanished")
            if not runfiles.cancel_command_ok(record):
                self._inspection_block(run, "cancel-failed", "the runner vanished and the ledger could not be closed as cancelled: "
                                       + _command_failure(record))
                return
            note = "the ledger was closed as cancelled"
        detail = ("the run's process vanished without a result; the shared tree may hold partial work to inspect in the "
                  f"harness ({note}" + (f"; {'; '.join(problems)}" if problems else "") + ")")
        self._inspection_block(run, "runner-crashed", detail)

    def _ledger(self, store: Path, run_id: str):
        """The run's ledger through the edge (read-only), or None when it has none; EdgeError when it cannot be read."""
        if not self.edge.status().available:
            raise EdgeError("llm-kit is not available here, so the ledger cannot be read")
        return self.edge.ledger_snapshot(store, run_id)

    def _close_ledger(self, run: Run, run_dir: Path, reason: str) -> dict:
        """Run the spec's cancel command from the server (only ever after the run's processes are stopped)."""
        status, spec = runfiles.read_json(run_dir / runfiles.SPEC_FILE)
        argv = spec.get("runner", {}).get("cancel_argv") if status == runfiles.OK else None
        if not argv:
            return {"error": "the run's spec.json names no cancel command"}
        return runfiles.run_cancel_command(argv, reason, cwd=str(spec.get("project_root") or run.project_root))

    @staticmethod
    def _read_receipt(run_dir: Path) -> tuple[str, dict | str | None]:
        """``(OK, verdict)``, ``(MISSING, None)`` or ``(UNREADABLE, why)``: a torn or foreign receipt is never a verdict."""
        status, data = runfiles.read_json(run_dir / runfiles.RECEIPT_FILE)
        if status != runfiles.OK:
            return status, data
        verdict = data.get("verdict")
        if (not isinstance(verdict, dict) or verdict.get("outcome") not in ("accepted", "rejected", "agent-blocked")
                or not isinstance(verdict.get("code"), str) or not isinstance(verdict.get("detail"), str)):
            return runfiles.UNREADABLE, "it has no verdict of the shape acceptance writes"
        return runfiles.OK, verdict

    def _settle_by_verdict(self, run: Run, verdict: dict) -> None:
        if verdict["outcome"] == "accepted":
            self.db.set_state(run.id, ACCEPTED, code="accepted", detail=verdict["detail"])
        else:
            self.db.set_state(run.id, BLOCKED, code=verdict["code"], detail=verdict["detail"])

    def _inspection_block(self, run: Run, code: str, detail: str) -> None:
        """A crash-inspection block: the task is blocked for a person, the run ends ``crashed``. Never a success, never a retry."""
        try:
            record_block(self.mutator, Path(run.folder), item_id=run.item_id, note=f"{code}: {detail}",
                         log_detail=f"dashboard run {run.id}: crash inspection ({code}): {concise(detail)}",
                         gate=self._hold_gate)  # a run boundary: it records what a live run did, so it ignores a hold
        except MutationError as exc:
            detail += f" (the block could not be recorded: {exc})"
        self.db.set_state(run.id, CRASHED, code=code, detail=detail)

    def _settle(self, run: Run, run_dir: Path, result: dict) -> None:
        """Rows 2-10: the runner finished and wrote a result."""
        survivors = result.get("survivors") or []
        if survivors:  # row 2
            idents = [proctree.ProcIdent.from_json(item) for item in survivors]
            self._inspection_block(run, "survivors", f"{_pids(idents)} outlived the stop; the ledger was left as it is")
            return
        receipt_status, verdict = self._read_receipt(run_dir)
        if receipt_status == runfiles.UNREADABLE:  # row 3
            self._inspection_block(run, "receipt-unreadable", f"the acceptance receipt cannot be read: {verdict}")
            return
        if receipt_status == runfiles.OK:  # row 4: a receipt beats a cancel and a timeout
            self._settle_by_verdict(run, verdict)
            return
        outcome = result.get("outcome")
        if outcome in ("cancelled", "timeout"):
            command = (result.get("cancel") or {}).get("command")
            if not runfiles.cancel_command_ok(command):  # row 5
                self._inspection_block(run, "cancel-failed", f"the run was stopped ({outcome}) but its ledger could not be closed "
                                       "as cancelled: " + _command_failure(command))
                return
            ended = _already_terminal_without_acceptance(command)
            if ended:  # row 6
                self._inspection_block(run, "ledger-without-receipt", f"the run was stopped ({outcome}) but its ledger already "
                                       f"shows {ended} with no acceptance receipt")
                return
            if outcome == "cancelled":  # row 7
                self.db.set_state(run.id, CANCELLED, code="cancelled",
                                  detail="cancelled: the process tree was stopped and the ledger closed as cancelled. Partial "
                                         "work may be in the shared tree. Retry starts a fresh run.")
            else:  # row 8
                self.db.set_state(run.id, TIMEOUT, code="timeout",
                                  detail="the run reached its time limit and its process tree was stopped; partial work may be "
                                         "in the shared tree")
            return
        if outcome == "runner-refused":  # row 9
            self.db.set_state(run.id, FAILED, code="runner-refused", detail=result.get("detail", ""))
            return
        if self.edge.status().available:  # row 10: the command ended; was its ledger left open (it crashed or was killed)?
            try:
                view = self._ledger(run_dir / runfiles.LEDGER_FILE, run.id)
            except EdgeError as exc:
                self._inspection_block(run, "ledger-unreadable", f"the llm-kit command ended and its ledger cannot be read: {exc}")
                return
            if view is not None and not view.terminal:
                record = self._close_ledger(run, run_dir, "llm-kit ended without finishing the job")
                note = "the ledger was closed as cancelled" if runfiles.cancel_command_ok(record) else _command_failure(record)
                self._inspection_block(run, "llm-kit-crashed" if runfiles.cancel_command_ok(record) else "cancel-failed",
                                       f"the llm-kit command ended (exit {result.get('exit_code')}) with the job still open; {note}. "
                                       "The shared tree may hold partial work to inspect in the harness")
                return
        limit = job_timeout(read_job_snapshot(run_dir))
        if limit is not None:
            self.db.set_state(run.id, TIMEOUT, code="timeout",
                              detail=f"the model call reached its time limit ({limit}); partial work may be in the shared tree. "
                                     "Retry starts a fresh run.")
            return
        if self._acceptance_starved(run_dir):
            detail = "the model used the whole time budget, leaving none for acceptance; inspect the shared tree"
            try:
                self._block_for(Path(run.folder), run.item_id, [Reason("acceptance-starved", detail)], f"dashboard run {run.id}",
                                revision=None)
            except MutationError as exc:
                detail += f" (the block could not be recorded: {exc})"
            self.db.set_state(run.id, BLOCKED, code="acceptance-starved", detail=detail)
            return
        self.db.set_state(run.id, FAILED, code=f"llm-kit-exit-{result.get('exit_code')}",
                          detail=self.log_tail(run, 0)["text"][-400:] or "the job ended without an acceptance verdict")

    def ledger_events(self, run: Run) -> list[dict]:
        """The run's recorded execution events. Only for a run whose processes have exited (the read may migrate the ledger)."""
        if run.state not in TERMINAL_STATES or not run.run_dir:
            return []
        if run.code in ("survivors", "cancel-failed", "ledger-unreadable"):
            return []  # a process may still hold the ledger, or it was left as it is on purpose: do not open it for writing
        if not self.edge.status().available:
            raise EdgeError("llm-kit is not available here, so the ledger events cannot be read")
        return self.edge.ledger_events(Path(run.run_dir) / runfiles.LEDGER_FILE, run.id)

    def ledger_phase(self, run: Run) -> str | None:
        """For display only: the live ledger's state word for an active run, read through the read-only edge."""
        if run.state not in ACTIVE_STATES or not run.run_dir:
            return None
        try:
            view = self._ledger(Path(run.run_dir) / runfiles.LEDGER_FILE, run.id)
        except EdgeError as exc:
            self._report_once(f"phase-{run.id}", f"run {run.id}: {exc}")
            return "unreadable"
        return view.phase if view is not None else None

    @staticmethod
    def _acceptance_starved(run_dir: Path) -> bool:
        snapshot = read_job_snapshot(run_dir)
        if snapshot is None:
            return False

        def walk(value) -> bool:
            if isinstance(value, dict):
                acceptance = value.get("acceptance")
                if isinstance(acceptance, dict) and acceptance.get("outcome") in ("timed_out", "not_run"):
                    return True
                return any(walk(v) for v in value.values())
            if isinstance(value, list):
                return any(walk(v) for v in value)
            return False

        return walk(snapshot)

    # -- reads ------------------------------------------------------------------

    def log_tail(self, run: Run, after: int) -> dict:
        """A bounded, redacted slice of the runner log starting at byte ``after``."""
        path = Path(run.run_dir) / "runner.log" if run.run_dir else None
        if path is None or not path.is_file():
            return {"text": "", "cursor": after}
        size = path.stat().st_size
        start = max(0, min(after, size))
        if size - start > LOG_TAIL_BYTES:
            start = size - LOG_TAIL_BYTES
        with open(path, "rb") as handle:
            handle.seek(start)
            data = handle.read(LOG_TAIL_BYTES)
        return {"text": redact.redact(data.decode("utf-8", errors="replace")), "cursor": start + len(data)}


def _pids(idents) -> str:
    return "process " + ", ".join(str(i.pid) for i in idents) if len(idents) == 1 else "processes " + ", ".join(str(i.pid) for i in idents)


def _command_failure(record) -> str:
    if not isinstance(record, dict):
        return "no record of the cancel command"
    if record.get("skipped"):
        return f"the cancel command was skipped ({record['skipped']})"
    if record.get("error"):
        return str(record["error"])
    tail = (record.get("stderr") or record.get("stdout") or "").strip().splitlines()
    return f"the cancel command exited {record.get('exit_code')}" + (f": {tail[-1]}" if tail else "")


def _already_terminal_without_acceptance(record) -> str | None:
    """The state, when the cancel command found a job already accepted or rejected (which acceptance must have receipted)."""
    if not isinstance(record, dict) or record.get("exit_code") != 0:
        return None
    try:
        jobs = json.loads(record.get("stdout") or "{}").get("jobs", [])
    except ValueError:
        return None
    for job in jobs:
        if isinstance(job, dict) and job.get("outcome") == "already-terminal" and job.get("state") in ("accepted", "rejected"):
            return str(job["state"])
    return None
