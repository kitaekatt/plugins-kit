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
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import traceback
import uuid
from pathlib import Path
from typing import Callable

import yaml

from ..validate import validate_ref
from . import findings as findings_mod
from . import floor, proctree, redact
from .blocker import BlockOutcome, concise, record_block
from .config import DashboardConfig, DashboardConfigError, load_config
from .inventory import project_id_for
from .jobs_edge import EdgeError
from .mutation import (
    LOG,
    TASK_YAML,
    MutationError,
    StaleRevision,
    TaskMutator,
    TaskTexts,
    append_log,
    folder_revision,
    raw_items,
    set_task_status,
    stored_status,
)
from .runtime import (
    ACCEPTED,
    ACTIVE_STATES,
    BLOCKED,
    CRASHED,
    FAILED,
    QUEUED,
    RETRYABLE_STATES,
    RUNNING,
    SKIPPED,
    STARTING,
    TIMEOUT,
    Run,
    RuntimeDB,
)
from .translate import JobSpec, Reason, RunOptions, translate

ACCEPT_SCRIPT = Path(__file__).with_name("accept_cli.py")
RUNNER_SCRIPT = Path(__file__).with_name("runner_cli.py")
RUNNER_SLACK_S = 120.0
RUNNER_GRACE_S = 5.0
LOG_TAIL_BYTES = 64 * 1024
# Reasons that mean "someone changed the task on purpose": drop the entry, change nothing.
SOURCE_CHANGED = {"item-missing", "item-duplicate", "item-not-available", "task-not-active", "no-items-block"}


class QueueRefused(Exception):
    """An action was refused. ``status`` is the HTTP status the API answers with."""

    def __init__(self, status: int, code: str, message: str, *, reasons: list[dict] | None = None,
                 blocked: bool = False, current_revision: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.reasons = reasons or []
        self.blocked = blocked
        self.current_revision = current_revision


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
    ) -> None:
        self.root = Path(root)
        self.db = db
        self.mutator = mutator
        self.edge = edge
        self._config_path = config_path
        self._python = python or sys.executable
        self._spawn = spawn or self._default_spawn
        self._log = log
        self._tick_lock = threading.Lock()
        self._queue_lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._reported: dict[str, str] = {}

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
                "actions": ["queue", "retry", "recheck"],
                "reason": "Queue, Retry and Re-check are live; Cancel run, Defer, New task, Add item and Pause worker "
                          "arrive in a later increment.",
            },
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

    # -- actions -------------------------------------------------------------

    def queue(self, *, project_root: str, task_ref: str, item_id: str, body: dict,
              expected_revision: str | None, retry_of: str | None = None) -> Run:
        """Translate and enqueue one item. Serialized, so two requests cannot both queue the same item."""
        with self._queue_lock:
            return self._queue(project_root=project_root, task_ref=task_ref, item_id=item_id, body=body,
                               expected_revision=expected_revision, retry_of=retry_of)

    def _queue(self, *, project_root: str, task_ref: str, item_id: str, body: dict,
               expected_revision: str | None, retry_of: str | None) -> Run:
        folder = Path(project_root) / task_ref
        try:
            current = folder_revision(folder)
        except MutationError as exc:
            raise QueueRefused(422, "unreadable", str(exc)) from exc
        if expected_revision is not None and current != expected_revision:
            raise QueueRefused(409, "stale-revision", "the task changed since this page last loaded it",
                               current_revision=current)
        status = self.edge.status()
        if not status.available:
            raise QueueRefused(503, "llm-kit-unavailable", status.detail)
        project_id = project_id_for(Path(project_root))
        if self.db.live_for(project_id, task_ref, item_id):
            raise QueueRefused(409, "already-queued", f"{item_id} already has a queued or running dashboard run")
        try:
            config = self.config()
        except DashboardConfigError as exc:
            raise QueueRefused(422, "config", str(exc)) from exc
        options_in = _options_from(body)
        run_id = uuid.uuid4().hex[:16]
        result = self._translate(Path(project_root), task_ref, item_id, RunOptions(**options_in), config, run_id,
                                 self.run_dir_for(project_id, run_id), current)
        if result.reasons:
            blocking = [r for r in result.reasons if r.blocking]
            hard = [r for r in blocking if r.code not in SOURCE_CHANGED]
            if blocking and not hard:
                raise QueueRefused(422, "not-ready", "the item is not ready to run: " + "; ".join(r.line() for r in blocking),
                                   reasons=_reasons_json(result.reasons), current_revision=current)
            if hard:
                self._block_for(folder, item_id, hard, f"dashboard queue of {item_id}")
                raise QueueRefused(422, "translation-failed", "the item could not be translated into a job, so the task is now blocked: "
                                   + "; ".join(r.line() for r in hard), reasons=_reasons_json(result.reasons), blocked=True)
        title = next((i.get("title", item_id) for i in (raw_items((folder / "plan.md").read_text(encoding="utf-8")) or [])
                      if i.get("id") == item_id), item_id)
        run = self.db.enqueue(run_id=run_id, project_id=project_id, project_root=str(project_root), task_ref=task_ref,
                              item_id=item_id, item_title=str(title), options=options_in, source_revision=current,
                              retry_of=retry_of)
        self.wake()
        return run

    def retry(self, run_id: str, *, expected_revision: str | None) -> Run:
        old = self.db.get(run_id)
        if old is None:
            raise QueueRefused(404, "unknown-run", f"no run {run_id}")
        if old.state not in RETRYABLE_STATES:
            raise QueueRefused(409, "not-retryable", f"run {run_id} is {old.state}; only failed or timed-out runs retry. "
                               "A blocked run is resolved in Claude Code, then Re-checked.")
        return self.queue(project_root=old.project_root, task_ref=old.task_ref, item_id=old.item_id, body=dict(old.options),
                          expected_revision=expected_revision, retry_of=old.id)

    def recheck(self, *, project_root: str, task_ref: str, expected_revision: str | None) -> dict:
        folder = Path(project_root) / task_ref
        released = self.db.clear_holds(str(folder))
        try:
            current = folder_revision(folder)
        except MutationError as exc:
            raise QueueRefused(422, "unreadable", str(exc)) from exc
        if expected_revision is not None and current != expected_revision:
            raise QueueRefused(409, "stale-revision", "the task changed since this page last loaded it", current_revision=current)
        result = validate_ref(task_ref, Path(project_root))
        findings = findings_mod.gating_of(result)
        plan = (folder / "plan.md").read_text(encoding="utf-8") if (folder / "plan.md").is_file() else ""
        blocked_items = [i.get("id") for i in (raw_items(plan) or []) if i.get("state") == "blocked-user"]
        problems = [Reason("validation", f) for f in findings] + [
            Reason("item-blocked", f"item {i} is still blocked-user") for i in blocked_items]
        if problems:
            raise QueueRefused(422, "recheck-failed", "the task is not clear yet: " + "; ".join(p.line() for p in problems[:5]),
                               reasons=_reasons_json(problems), current_revision=current)

        changed = {"value": False}

        def edit(texts: TaskTexts) -> dict[str, str]:
            if stored_status(texts.task_yaml) != "blocked":
                return {}
            changed["value"] = True
            return {TASK_YAML: set_task_status(texts.task_yaml, "active"),
                    LOG: append_log(texts.log, "dashboard: re-check cleared the block; the task is active again")}

        try:
            revision = self.mutator.mutate(folder, edit, expected_revision=current)
        except StaleRevision as exc:
            raise QueueRefused(409, "stale-revision", str(exc), current_revision=exc.current) from exc
        except MutationError as exc:
            raise QueueRefused(500, exc.code, str(exc)) from exc
        return {"changed": changed["value"], "holds_released": released, "revision": revision}

    # -- the loop -------------------------------------------------------------

    def start(self, interval_s: float = 1.0) -> None:
        self.adopt()
        self._thread = threading.Thread(target=self._loop, args=(interval_s,), name="scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=10)

    def wake(self) -> None:
        self._wake.set()

    def _loop(self, interval_s: float) -> None:
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
        """On server start: a run that never spawned its runner goes back to the queue.

        A ``running`` run keeps its recorded runner identity and is observed as
        usual, so a server restart does not disturb it.
        """
        for run in self.db.runs((STARTING,)):
            if run.runner_pid is None:
                self.db.release_to_queue(run.id)

    def tick(self) -> None:
        with self._tick_lock:
            for run in self.db.runs((RUNNING,)):
                self._reconcile(run)
            self._start_queued()

    # -- starting -------------------------------------------------------------

    def _start_queued(self) -> None:
        try:
            config = self.config()
        except DashboardConfigError as exc:
            self._report_once("config", str(exc))
            return
        if not self.edge.status().available:
            return
        for run in self.db.runs((QUEUED,)):
            self._try_start(run, config)

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
            if hard:
                try:
                    self._block_for(folder, run.item_id, hard, f"dashboard run {run.id}")
                    self.db.set_state(run.id, BLOCKED, code=hard[0].code, detail="; ".join(r.line() for r in hard))
                except MutationError as exc:
                    self.db.set_state(run.id, FAILED, code="block-not-recorded",
                                      detail=f"translation failed ({hard[0].line()}) and the block could not be recorded: {exc}")
                return
            self._report_once(run.id, "; ".join(r.line() for r in result.reasons))
            return  # only non-blocking reasons: it stays queued
        if not self.db.try_lease(run.id, run.project_id, max_projects=config.max_parallel_projects):
            return
        try:
            self._dispatch(run, result.spec, run_dir, revision)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001 -- anything here leaves a loud failed run, never a stuck lease
            self._log("dispatch failed:\n" + traceback.format_exc())
            if run_dir.exists():
                shutil.rmtree(run_dir, ignore_errors=True)
            self.db.set_state(run.id, FAILED, code="dispatch-failed", detail=f"{type(exc).__name__}: {exc}")

    def _translate(self, project_root: Path, task_ref: str, item_id: str, options: RunOptions, config: DashboardConfig,
                   run_id: str, run_dir: Path, revision: str):
        return translate(
            project_root=project_root, task_ref=task_ref, item_id=item_id, options=options, config=config, edge=self.edge,
            hold_for=self.hold_for, run_id=run_id, run_dir=run_dir, accept_script=ACCEPT_SCRIPT, python=self._python,
            revision=revision,
        )

    def _block_for(self, folder: Path, item_id: str, reasons: list[Reason], source: str) -> BlockOutcome:
        detail = "; ".join(r.line() for r in reasons)
        return record_block(self.mutator, folder, item_id=item_id, note=f"{reasons[0].code}: {reasons[0].detail}",
                            log_detail=f"{source}: not translated into a job: {concise(detail)}")

    def _dispatch(self, run: Run, spec: JobSpec, run_dir: Path, revision: str) -> None:
        if spec.worker == "claude":
            floor.check_job_document(spec.document)
        run_dir.mkdir(parents=True, exist_ok=False)
        job_file = run_dir / "job.yaml"
        job_file.write_text(yaml.safe_dump(dict(spec.document), sort_keys=False, default_flow_style=False), encoding="utf-8")
        try:
            self.edge.validate_job_file(job_file)
        except EdgeError as exc:
            shutil.rmtree(run_dir, ignore_errors=True)
            self._block_for(Path(spec.folder), spec.item_id, [Reason("job-file-invalid", str(exc))], f"dashboard run {run.id}")
            self.db.set_state(run.id, BLOCKED, code="job-file-invalid", detail=str(exc))
            return
        start = spec.start
        runner_spec = {
            "schema": 1, "run_id": run.id, "project_root": spec.project_root, "folder": spec.folder,
            "task_ref": spec.task_ref, "item_id": spec.item_id, "lock_path": str(self.mutator.lock_path),
            "accept": [list(a) for a in spec.accept],
            "start": {"revision": start.revision, "vcs": start.vcs, "head": start.head, "branch": start.branch,
                      "status": list(start.status), "log_lines": start.log_lines},
            "worker": spec.worker, "entry": spec.entry, "timeout_s": spec.timeout_s, "network": spec.network,
            "runner": {
                "argv": self.edge.runner_argv(job_file, run_dir / "ledger.sqlite3", spec.timeout_s, run.id),
                "deadline_s": spec.timeout_s + RUNNER_SLACK_S, "grace_s": RUNNER_GRACE_S,
                "env": {"TASK_KIT_AGENT_DEPTH": "0"},
            },
        }
        (run_dir / "spec.json").write_text(json.dumps(runner_spec, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self.db.set_state(run.id, STARTING, run_dir=str(run_dir))
        self._spawn([self._python, str(RUNNER_SCRIPT), str(run_dir)], run_dir)
        ident = self._await_runner(run_dir)
        self.db.set_state(run.id, RUNNING, runner=ident, detail=f"{spec.worker}:{spec.entry}")

    @staticmethod
    def _await_runner(run_dir: Path, timeout_s: float = 20.0) -> proctree.ProcIdent:
        import time

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

    # -- settling ---------------------------------------------------------------

    def _reconcile(self, run: Run) -> None:
        run_dir = Path(run.run_dir) if run.run_dir else None
        if run_dir is None:
            return
        result_path = run_dir / "result.json"
        if result_path.is_file():
            self._settle(run, run_dir, json.loads(result_path.read_text(encoding="utf-8")))
            return
        ident = (proctree.ProcIdent(run.runner_pid, run.runner_ctime)
                 if run.runner_pid is not None and run.runner_ctime is not None else None)
        if ident is None or proctree.live(ident) is None:
            if result_path.is_file():  # it finished between the two looks
                return
            self._crashed(run, run_dir)

    def _crashed(self, run: Run, run_dir: Path) -> None:
        detail = "the run's process vanished without a result; the shared tree may hold partial work to inspect in the harness"
        try:
            self._block_for(Path(run.folder), run.item_id, [Reason("runner-crashed", detail)], f"dashboard run {run.id}")
        except MutationError as exc:
            detail += f" (the block could not be recorded: {exc})"
        self.db.set_state(run.id, CRASHED, code="runner-crashed", detail=detail)

    def _settle(self, run: Run, run_dir: Path, result: dict) -> None:
        receipt_path = run_dir / "receipt.json"
        if receipt_path.is_file():
            verdict = json.loads(receipt_path.read_text(encoding="utf-8"))["verdict"]
            if verdict["outcome"] == "accepted":
                self.db.set_state(run.id, ACCEPTED, code="accepted", detail=verdict["detail"])
            else:
                self.db.set_state(run.id, BLOCKED, code=verdict["code"], detail=verdict["detail"])
            return
        outcome = result.get("outcome")
        if outcome == "timeout":
            self.db.set_state(run.id, TIMEOUT, code="timeout",
                              detail="the run reached its time limit and its process tree was stopped; partial work may be in the shared tree")
            return
        if outcome == "runner-refused":
            self.db.set_state(run.id, FAILED, code="runner-refused", detail=result.get("detail", ""))
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
                self._block_for(Path(run.folder), run.item_id, [Reason("acceptance-starved", detail)], f"dashboard run {run.id}")
            except MutationError as exc:
                detail += f" (the block could not be recorded: {exc})"
            self.db.set_state(run.id, BLOCKED, code="acceptance-starved", detail=detail)
            return
        self.db.set_state(run.id, FAILED, code=f"llm-kit-exit-{result.get('exit_code')}",
                          detail=self.log_tail(run, 0)["text"][-400:] or "the job ended without an acceptance verdict")

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
