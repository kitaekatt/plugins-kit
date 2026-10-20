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
(run cancellation and crash recovery close a ledger through that verb) and
``llm_kit.registry.effort.harness_effort_order`` (the efforts a harness accepts, for the run options a
person chooses; present since llm-kit 0.1.2), on top of
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


class _ArgvCaptured(Exception):
    """Raised by the capturing runner of :meth:`JobsEdge.claude_worker_argv`: nothing was launched."""

    def __init__(self, argv: list[str]) -> None:
        super().__init__("argv captured")
        self.argv = argv


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
        self._menus: dict[str, tuple[float, tuple[str, ...]]] = {}

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
            getattr(importlib.import_module("llm_kit.registry.effort"), "harness_effort_order")
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

    def effort_menu(self, entry: str) -> tuple[str, ...]:
        """The efforts the worker harness of registry entry ``entry`` accepts, low to high; cached for the TTL.

        Resolved from the entry's HARNESS (a task worker is always a harness entry), through the same discovery
        ``entry`` uses: ``llm_kit.registry.effort.effort_menu`` reads only the user's model-endpoints registry,
        which does not hold the entries the shipped baseline config declares (``opus``, ``sol``).
        """
        now = time.monotonic()
        cached = self._menus.get(entry)
        if cached is not None and now - cached[0] < self._ttl_s:
            return cached[1]
        found = self.entry(entry)
        if found is None or found.harness is None:
            raise EdgeError(f"model entry {entry!r} is not a harness entry, so it has no effort menu")
        effort = importlib.import_module("llm_kit.registry.effort")
        try:
            menu = tuple(effort.harness_effort_order(found.harness))
        except Exception as exc:  # noqa: BLE001 -- llm-kit raises its own error type for an unknown harness
            raise EdgeError(f"llm-kit could not resolve the effort menu of {entry!r}: {type(exc).__name__}: {exc}") from exc
        self._menus[entry] = (now, menu)
        return menu

    def claude_worker_argv(self, entry: str, *, effort: str | None, disallowed_tools: str, cwd: Path) -> list[str]:
        """The argv llm-kit would launch for a Claude worker whose job options carry exactly ``disallowed_tools``.

        Built by llm-kit's own factory and backend, with the backend's public ``runner`` field replaced by a
        capture that records the argv and aborts, so nothing is launched. llm-kit passes a job's
        ``disallowed_tools`` unchanged when no run floor is set and emits it as ONE argument, so the value
        in the returned argv is byte-identical to what a run emits. The prompt is not in the argv (llm-kit
        sends it on stdin).
        """
        import dataclasses

        try:
            factory = importlib.import_module("llm_kit.dispatch.factory")
            types = importlib.import_module("llm_kit.completion.types")
            selection = factory.create_backend(entry, project_root=str(cwd))
            backend = selection.backend
            if getattr(backend, "name", None) != "claude-cli" or not hasattr(backend, "runner"):
                raise EdgeError(f"model entry {entry!r} does not run on the claude-cli backend")

            def capture(cmd, user, run_cwd, *, log_prefix, timeout_s):  # noqa: ARG001 -- the runner's own signature
                raise _ArgvCaptured(list(cmd))

            options = types.BackendOptions(
                timeout_s=60.0, cwd=Path(cwd), max_tokens=4096,
                effort=effort if effort is not None else selection.effort,
                allowed_tools=None, disallowed_tools=disallowed_tools, system_prompt_mode="append",
                client_id="task-kit:cli-check", log_prefix="[cli-check]",
            )
            dataclasses.replace(backend, runner=capture).complete("probe", "probe", model=selection.model, options=options)
        except _ArgvCaptured as captured:
            return captured.argv
        except EdgeError:
            raise
        except Exception as exc:  # noqa: BLE001 -- llm-kit raises several error types (registry, executable lookup)
            raise EdgeError(f"llm-kit could not build the worker argv for {entry!r}: {type(exc).__name__}: {exc}") from exc
        raise EdgeError("llm-kit's Claude backend did not reach its runner")

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
