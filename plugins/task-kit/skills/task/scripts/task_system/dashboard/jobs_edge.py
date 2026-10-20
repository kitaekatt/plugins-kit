"""The optional edge from task-kit to llm-kit (REFUSE).

Autonomous runs are the only dashboard feature that needs llm-kit, an
optional plugin, so task-kit must work without it.
This is the ONLY dashboard module that names ``llm_kit`` (a guard test pins
that), and it imports lazily: importing the dashboard never needs llm-kit, and
every probe distinguishes the three runtime states an ``import`` cannot:

- ``absent``   -- ``llm_kit`` is not importable here;
- ``too-old``  -- present, but a symbol or behaviour this module relies on is
  missing (a stale or older shared copy; the two read the same);
- ``ok``.

The newest things relied on are the ledger's ``cancelled`` job state with
``JobStore.mark_cancelled``, and the ``llm-kit jobs cancel`` verb that records it
(run cancellation and crash recovery close a ledger through that verb), on top of
a job option mapping that carries ``disallowed_tools`` (the Claude deny floor)
through ``Job``. Neither failure
state falls back to anything: the autonomous controls report the message and
stay off, and the rest of the dashboard is unaffected.

Each failure message names the owning plugin, the llm-kit version that first
shipped everything probed (``MIN_LLM_KIT_VERSION``), and the command that fixes
it: install when absent, update when too old.
"""

from __future__ import annotations

import contextlib
import importlib
import io
import sys
import time
from dataclasses import dataclass
from pathlib import Path

OK = "ok"
ABSENT = "absent"
TOO_OLD = "too-old"

MIN_LLM_KIT_VERSION = "0.3.0"  # first release carrying JobState.CANCELLED, JobStore.mark_cancelled and `jobs cancel`

ABSENT_MESSAGE = (
    "Autonomous runs need the llm-kit plugin (version " + MIN_LLM_KIT_VERSION + " or newer), which is not "
    "importable here. Install it with `claude plugin install llm-kit@plugins-kit`; bootstrap then links it. "
    "Viewing and managing tasks still works."
)
TOO_OLD_MESSAGE = (
    "The llm-kit available here is too old or stale for autonomous runs ({why}); the dashboard needs "
    "llm-kit " + MIN_LLM_KIT_VERSION + " or newer. Update it with `claude plugin update llm-kit@plugins-kit`. "
    "Viewing and managing tasks still works."
)

SUPPORTED_HARNESSES = {"claude": "claude-cli", "codex": "codex-cli"}


class EdgeError(RuntimeError):
    """llm-kit is present but a call into it failed (a registry that will not load, say)."""


@dataclass(frozen=True)
class EdgeStatus:
    state: str
    detail: str

    @property
    def available(self) -> bool:
        return self.state == OK


# Job states llm-kit never leaves (its TERMINAL_STATES, with the cancelled state this module needs).
TERMINAL_JOB_STATES = frozenset({
    "accepted", "rejected", "failed", "halted", "unroutable", "operator_rejected", "expired", "cancelled",
})


@dataclass(frozen=True)
class LedgerView:
    """What the dashboard reads of a run's ledger: its jobs and their recorded states."""

    run_id: str
    jobs: tuple[tuple[str, str], ...]  # (job id, state)

    @property
    def states(self) -> tuple[str, ...]:
        return tuple(state for _id, state in self.jobs)

    @property
    def terminal(self) -> bool:
        return bool(self.jobs) and all(state in TERMINAL_JOB_STATES for state in self.states)

    @property
    def phase(self) -> str:
        """One word for the run: the single job's state, or a count when there are several."""
        states = self.states
        if not states:
            return "empty"
        return states[0] if len(set(states)) == 1 else "mixed"


@dataclass(frozen=True)
class EntryInfo:
    id: str
    kind: str  # harness | transport
    harness: str | None  # claude | codex | opencode | None for a transport
    model: str

    @property
    def adapter(self) -> str | None:
        return SUPPORTED_HARNESSES.get(self.harness or "")


def _require_cancel_verb(cli_jobs) -> None:
    """Raise AttributeError unless ``llm-kit jobs cancel <run> --reason R`` parses."""
    parser = cli_jobs._parser()
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            parser.parse_args(["cancel", "probe", "--reason", "probe"])
        except SystemExit as exc:
            raise AttributeError("llm-kit has no `jobs cancel` verb") from exc


class JobsEdge:
    """The real llm-kit edge. Tests substitute a fake with the same methods.

    ``status`` is cached for ``ttl_s`` seconds because the dashboard asks on
    every poll; a stale shared copy is noticed within that window.
    """

    def __init__(self, ttl_s: float = 5.0) -> None:
        self._ttl_s = ttl_s
        self._cached: tuple[float, EdgeStatus] | None = None

    def status(self) -> EdgeStatus:
        now = time.monotonic()
        if self._cached is not None and now - self._cached[0] < self._ttl_s:
            return self._cached[1]
        result = self._probe()
        self._cached = (now, result)
        return result

    def _probe(self) -> EdgeStatus:
        try:
            importlib.import_module("llm_kit")
        except ModuleNotFoundError as exc:
            if (exc.name or "").split(".")[0] == "llm_kit":
                return EdgeStatus(ABSENT, ABSENT_MESSAGE)
            return EdgeStatus(TOO_OLD, TOO_OLD_MESSAGE.format(why=f"it imports {exc.name}, which is missing"))
        try:
            model = importlib.import_module("llm_kit.jobs.model")
            importlib.import_module("llm_kit.cli.jobs")
            registry = importlib.import_module("llm_kit.registry.models")
            endpoints = importlib.import_module("llm_kit.registry.model_endpoints")
            for owner, name in (
                (model, "Job"), (model, "JobFile"), (model, "load_job_file"),
                (registry, "discover_model_entries"), (endpoints, "HARNESS_KIND"),
            ):
                getattr(owner, name)
            probe = model.Job(
                id="probe", prompt=model.Prompt(user="x"), models=("sonnet",),
                contract=model.Contract(command=("true",)),
                options={"disallowed_tools": "Bash(probe:*)", "system_prompt_mode": "append"},
            )
            if probe.options.get("disallowed_tools") != "Bash(probe:*)":
                raise AttributeError("Job drops options.disallowed_tools")
            store = importlib.import_module("llm_kit.jobs.store")
            cli_jobs = importlib.import_module("llm_kit.cli.jobs")
            getattr(model.JobState, "CANCELLED")
            for owner, name in ((store.JobStore, "mark_cancelled"), (store, "LedgerReader"), (store, "StoreNotFoundError"),
                                (store, "UnknownRunError")):
                getattr(owner, name)
            _require_cancel_verb(cli_jobs)
        except (ImportError, AttributeError, TypeError, ValueError) as exc:
            return EdgeStatus(TOO_OLD, TOO_OLD_MESSAGE.format(why=f"{type(exc).__name__}: {exc}"))
        return EdgeStatus(OK, "llm-kit is available")

    def cancel_argv(self, store: Path, run_id: str, reason: str) -> list[str]:
        """The command that closes ``run_id``'s ledger as cancelled (the run process must already be stopped).

        Exit 0 prints ``{"run", "jobs": [{"id", "state", "outcome"}]}``; exit 3 means the store has no such run.
        """
        return [sys.executable, "-m", "llm_kit.cli", "jobs", "cancel", run_id, "--store", str(store), "--reason", reason]

    def ledger_snapshot(self, store: Path, run_id: str) -> LedgerView | None:
        """A read-only view of the run's ledger, or None when there is no ledger or no such run yet.

        This is the ONLY ledger read while a run is live: ``LedgerReader`` opens the file read-only
        and never migrates it, so it can never disturb the process that owns the ledger.
        """
        ledger = importlib.import_module("llm_kit.jobs.store")
        try:
            snapshot = ledger.LedgerReader(store).snapshot(run_id)
        except (ledger.StoreNotFoundError, ledger.UnknownRunError):
            return None
        except Exception as exc:  # noqa: BLE001 -- llm-kit raises several ledger error types (future schema, locked, corrupt)
            raise EdgeError(f"the ledger {store} could not be read: {type(exc).__name__}: {exc}") from exc
        return LedgerView(run_id=run_id, jobs=tuple((record.id, record.state.value) for record in snapshot.jobs))

    def ledger_events(self, store: Path, run_id: str) -> list[dict]:
        """The run's execution events. Only after the run process has exited: reading them opens the
        ledger through a connection that may migrate it, which would make a second writer."""
        ledger = importlib.import_module("llm_kit.jobs.store")
        try:
            return list(ledger.JobStore(store, create=False).list_events(run_id))
        except (ledger.StoreNotFoundError, ledger.UnknownRunError):
            return []
        except Exception as exc:  # noqa: BLE001
            raise EdgeError(f"the events of {run_id} in {store} could not be read: {type(exc).__name__}: {exc}") from exc

    def entry(self, name: str) -> EntryInfo | None:
        """The registry entry called ``name``, or None when no such entry exists."""
        registry = importlib.import_module("llm_kit.registry.models")
        endpoints = importlib.import_module("llm_kit.registry.model_endpoints")
        try:
            found = registry.discover_model_entries().get(name)
        except Exception as exc:  # noqa: BLE001 -- llm-kit raises several registry error types
            raise EdgeError(f"llm-kit could not load its model registry: {type(exc).__name__}: {exc}") from exc
        if found is None:
            return None
        kind = "harness" if found.kind == endpoints.HARNESS_KIND else "transport"
        return EntryInfo(id=found.id, kind=kind, harness=found.harness, model=found.model)

    def validate_job_file(self, path: Path) -> None:
        """Load ``path`` through llm-kit's own loader; raises on anything it refuses."""
        model = importlib.import_module("llm_kit.jobs.model")
        try:
            model.load_job_file(path)
        except (ValueError, RuntimeError) as exc:
            raise EdgeError(f"llm-kit refused the job file {path}: {exc}") from exc

    def runner_argv(self, job_file: Path, store: Path, timeout_s: float, run_id: str) -> list[str]:
        """The command that runs the job file to its ledger (one process per ledger)."""
        return [
            sys.executable, "-m", "llm_kit.cli", "jobs", "run", str(job_file),
            "--store", str(store), "--timeout", f"{timeout_s:g}", "--run-id", run_id,
        ]
