"""The Claude-background-session driver (B1).

**Scope, steps 1-11.** Per the plan's B1 sequencing
(``docs/planning/content-pipeline-kit/session-recipients-plan.md``, "Phase B
-- Claude background sessions"), this module ships:

1. :class:`ClaudeCli` -- the ``claude`` process seam. Every argv this module
   ever builds funnels through ``ClaudeCli.runner``, its SOLE process
   boundary; nothing here calls :mod:`subprocess` directly.
2. :func:`preflight` -- capability/auth/platform-shape checks run once before
   a dispatcher does anything, per :class:`PreflightReport`.
3. The store migration and dispatcher-lease / dispatch-tracking methods (see
   ``execution/store.py``) the driver below uses.
4. :func:`compose_worker_environment` -- builds a worker's process
   environment from the adapter's :class:`~content_pipeline.execution.adapter.WorkerEnvironment`
   declaration, fixed BEFORE the worker process starts (see that function's
   docstring for why this matters).
5. :class:`WorkerCommand`, :func:`enumerate_worker_invocations`,
   :func:`build_launch_prompt` -- the enumerated invocation set (P5) and the
   launch prompt built from it.
6. :func:`dispatch_unit` -- launch one unit, confirmed by an OBSERVED state
   transition (P11), never by the launcher's exit code or banner.
7. :class:`SessionRecord`, :class:`ParseResult`, :func:`parse_agents_json` --
   the schema-tolerant reconciler (P4).
8. :func:`supervise_tick` -- status classification, lease renewal, and stall
   detection (D5, P12, P13) for every currently open dispatch.
9. :func:`reclaimable_units`, :func:`reclaim_attempt_count` -- driver-local
   reclaim selection and the bounded-reclaim rule.
10. :func:`classify_settled_failure` -- halt classification for a SETTLED
    unit, reading a transcript / job state text fields, never ``claude
    logs``.
11. :func:`dispatch_wave` -- the bounded dispatch loop over all of the above.

Command construction (P3) -- read before adding a method
------------------------------------------------------------------------------
The lifecycle verbs (``stop``, ``rm``, ``respawn`` -- and ``logs``,
deliberately never given a method here, see below) are **top-level**:
``claude <verb> <id>``. ``claude agents <verb> <id>`` is silently accepted
(exit 0) and does NOTHING (P3) -- an undocumented, unstable platform shape
this module must never emit as a dispatch command. The token ``"agents"``
therefore appears in exactly ONE argv shape this module builds for real
dispatch: :meth:`ClaudeCli.agents_json`'s ``[exe, "agents", "--json"]`` /
``[exe, "agents", "--json", "--all"]``. :func:`preflight` step 5 separately
and deliberately constructs the FORBIDDEN shape (``claude agents stop
--help``) as a diagnostic probe of the platform's own behavior -- that is not
a dispatch command and is not covered by the invariant above.

Why no ``logs`` method
------------------------
``claude logs <id>`` is a live-daemon-only channel: it fails once the
session's daemon has exited (P13's ``\\\\.\\pipe\\cc-daemon-*-control``
observation). A later halt-classification path for a SETTLED unit must read
the session transcript or per-job state instead, never ``claude logs`` --
and the cheapest way to guarantee that later code never takes the wrong
shortcut is for the method not to exist at all.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from content_pipeline.execution.adapter import RunAdapter
from content_pipeline.execution.controller import record_halt
from content_pipeline.execution.model import (
    AttemptKind,
    ExecutionError,
    RunRecord,
    StaleDispatcherLeaseError,
    UnitRecord,
    UnitState,
)
from content_pipeline.execution.status import compute_status
from content_pipeline.execution.store import ExecutionStore, lease_for
from content_pipeline.llm.platform import HALT_AUTH, HALT_RATE_LIMIT, HaltError, classify_halt_text

# ---------------------------------------------------------------------------
# Billing-diverting environment variables (shared by preflight's auth check
# and compose_worker_environment's subtraction) -- EXACT NAME membership,
# never substring matching. Any of these being set routes billing away from
# the subscription session pool, which defeats the entire premise of B1.
# ---------------------------------------------------------------------------

BILLING_DIVERTING_VARS: Tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "AWS_BEARER_TOKEN_BEDROCK",
    "ANTHROPIC_VERTEX_PROJECT_ID",
)


def _billing_diverting_hits(env: Mapping[str, str]) -> List[str]:
    """Names from :data:`BILLING_DIVERTING_VARS` set to a NON-EMPTY value in
    ``env``. Exact-name dict lookup -- never a substring scan -- so a name
    that merely shares a prefix (``ANTHROPIC_LOG``,
    ``CLAUDE_CODE_ENABLE_TELEMETRY``) never matches, and an explicitly empty
    value (``ANTHROPIC_API_KEY=""``) is treated as unset, mirroring
    :meth:`~content_pipeline.execution.adapter.WorkerEnvironment.check`'s own
    forbidden-var truthiness (``if os.environ.get(name):``)."""
    return [name for name in BILLING_DIVERTING_VARS if env.get(name)]


# ---------------------------------------------------------------------------
# Step 1 -- the claude process seam
# ---------------------------------------------------------------------------


class ClaudeExecutableNotFoundError(ExecutionError):
    """No ``claude`` executable could be resolved (:meth:`ClaudeCli.resolve_executable`)."""


def _default_runner(
    argv: Sequence[str],
    *,
    stdin: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
    cwd: Optional[str] = None,
    timeout: Optional[float] = None,
) -> Tuple[str, str, int]:
    """The production process boundary: an ordinary blocking subprocess call.

    Never exercised by this module's own test suite -- every test supplies
    its own ``runner`` (a fake, scripted callable); see
    ``tests/content-pipeline-kit/test_execution_driver_claude_bg.py``'s
    "no test reaches a real subprocess" guard, which patches THIS name to a
    raising stub and asserts nothing in the suite still reaches it.
    """
    proc = subprocess.run(
        list(argv),
        input=stdin,
        capture_output=True,
        text=True,
        env=dict(env) if env is not None else None,
        cwd=cwd,
        timeout=timeout,
    )
    return proc.stdout, proc.stderr, proc.returncode


@dataclass
class ClaudeCli:
    """Owns ``claude`` executable resolution and argv construction.

    ``runner`` is the SOLE process boundary (the ``runner=`` precedent in
    ``llm_scripting_kit.completion.backends.ClaudeCliBackend``): every method
    below builds an argv list and hands it to ``self.runner(...)``, never to
    :mod:`subprocess` directly. A caller (a test, or a future dispatcher
    wanting a shared timeout/env policy) may also call ``self.runner``
    directly for a one-off invocation this class has no named method for --
    :func:`preflight` does exactly that for its ``--help`` diagnostics.

    ``runner`` is called as
    ``runner(argv, stdin=..., env=..., cwd=..., timeout=...) -> (stdout, stderr, returncode)``.
    """

    executable: Optional[str] = None
    runner: Callable[..., Tuple[str, str, int]] = _default_runner

    def resolve_executable(self) -> str:
        """The configured executable, or the first ``claude`` on ``PATH``.

        Raises :class:`ClaudeExecutableNotFoundError` when neither is
        available -- callers must refuse rather than guess (preflight step
        1)."""
        exe = self.executable or shutil.which("claude")
        if not exe:
            raise ClaudeExecutableNotFoundError(
                "no `claude` executable configured and none found on PATH"
            )
        return exe

    def _invoke(
        self,
        argv: Sequence[str],
        *,
        stdin: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
        cwd: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> Tuple[str, str, int]:
        return self.runner(argv, stdin=stdin, env=env, cwd=cwd, timeout=timeout)

    def launch_bg(
        self,
        prompt: str,
        *,
        extra_args: Sequence[str] = (),
        env: Optional[Mapping[str, str]] = None,
        cwd: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> Tuple[str, str, int]:
        """``claude --bg [extra_args...] <prompt>``. ``prompt`` is positional
        (P3): a background session takes no ``-p``, and mixing the two is a
        hard usage error (see :func:`preflight` step 6)."""
        exe = self.resolve_executable()
        argv = [exe, "--bg", *extra_args, prompt]
        return self._invoke(argv, env=env, cwd=cwd, timeout=timeout)

    def agents_json(
        self,
        *,
        all_sessions: bool = True,
        env: Optional[Mapping[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> Tuple[str, str, int]:
        """``claude agents --json [--all]`` -- the ONE argv shape in this
        module that carries the ``"agents"`` token (see the module
        docstring's "Command construction (P3)" section)."""
        exe = self.resolve_executable()
        argv = [exe, "agents", "--json"]
        if all_sessions:
            argv.append("--all")
        return self._invoke(argv, env=env, timeout=timeout)

    def _lifecycle(
        self,
        verb: str,
        session_id: str,
        *,
        env: Optional[Mapping[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> Tuple[str, str, int]:
        """``claude <verb> <session_id>`` -- TOP-LEVEL, never
        ``claude agents <verb> <session_id>`` (P3)."""
        exe = self.resolve_executable()
        argv = [exe, verb, session_id]
        return self._invoke(argv, env=env, timeout=timeout)

    def stop(
        self,
        session_id: str,
        *,
        env: Optional[Mapping[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> Tuple[str, str, int]:
        return self._lifecycle("stop", session_id, env=env, timeout=timeout)

    def rm(
        self,
        session_id: str,
        *,
        env: Optional[Mapping[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> Tuple[str, str, int]:
        return self._lifecycle("rm", session_id, env=env, timeout=timeout)

    def respawn(
        self,
        session_id: str,
        *,
        env: Optional[Mapping[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> Tuple[str, str, int]:
        return self._lifecycle("respawn", session_id, env=env, timeout=timeout)

    def version(
        self,
        *,
        env: Optional[Mapping[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> Tuple[str, str, int]:
        """``claude --version``. Recorded by preflight for drift visibility;
        never itself a gate (see :func:`preflight` step 2)."""
        exe = self.resolve_executable()
        return self._invoke([exe, "--version"], env=env, timeout=timeout)


# ---------------------------------------------------------------------------
# Step 2 -- preflight
# ---------------------------------------------------------------------------


class PreflightError(ExecutionError):
    """Raised by :func:`preflight` on any of its refusing checks (1, 3, 4, 5,
    6 -- never check 2, the version record, which only ever warns)."""


@dataclass
class PreflightReport:
    """What a passing :func:`preflight` observed. Never a gate by itself --
    a caller inspects ``warnings`` for non-fatal drift."""

    executable: str
    version_output: str
    agents_json_sample: List[Any]
    verb_help_output: Dict[str, str] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)


_LIFECYCLE_VERBS: Tuple[str, ...] = ("stop", "logs", "rm", "respawn")


def preflight(
    cli: ClaudeCli, *, env: Optional[Mapping[str, str]] = None
) -> PreflightReport:
    """Capability/auth/platform-shape checks, run once before a dispatcher
    does anything real. Six checks, in order; every check except 2 (the
    version record) raises :class:`PreflightError` on failure -- see the
    module's B1 assignment for the exact ordering this follows.

    ``env`` defaults to ``os.environ`` -- the dispatcher's OWN process
    environment (the thing that is actually about to launch worker
    sessions), not a copy taken at import time.
    """
    if env is None:
        env = os.environ

    warnings: List[str] = []

    # 1. Executable resolvable. Refuse otherwise.
    try:
        executable = cli.resolve_executable()
    except ClaudeExecutableNotFoundError as exc:
        raise PreflightError(str(exc)) from exc

    # 2. `claude --version` recorded. NOT a gate -- version drift warns, never
    # refuses; the runtime verb assertions in step 5 are the real check.
    try:
        version_stdout, version_stderr, version_rc = cli.version(env=env)
        version_output = version_stdout or version_stderr
        if version_rc != 0:
            warnings.append(
                f"`claude --version` exited {version_rc}; proceeding anyway "
                "(version is recorded, never a gate)"
            )
    except Exception as exc:  # noqa: BLE001 -- version is informational only
        version_output = ""
        warnings.append(f"`claude --version` could not be run: {exc}")

    # 3. Auth fails closed: any BILLING_DIVERTING_VARS name set non-empty
    # refuses. Exact-name membership (see _billing_diverting_hits), never
    # substring matching.
    hits = _billing_diverting_hits(env)
    if hits:
        raise PreflightError(
            "refusing to dispatch: the following environment variable(s) "
            f"divert billing away from the subscription session pool: {hits}"
        )

    # 4. `agents --json` runs, JSON-decodes, and yields a list. Refuse
    # otherwise.
    agents_stdout, agents_stderr, agents_rc = cli.agents_json(all_sessions=True, env=env)
    if agents_rc != 0:
        raise PreflightError(
            f"`claude agents --json --all` exited {agents_rc}: {agents_stderr or agents_stdout}"
        )
    try:
        agents_sample = json.loads(agents_stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        raise PreflightError(
            f"`claude agents --json --all` did not print valid JSON: {exc}"
        ) from exc
    if not isinstance(agents_sample, list):
        raise PreflightError(
            "`claude agents --json --all` printed JSON that is not a list: "
            f"{type(agents_sample).__name__}"
        )

    # 5. Assert each lifecycle verb behaves (P3). For stop|logs|rm|respawn:
    # `claude <verb> --help` must be verb-specific -- not byte-identical to
    # `claude agents --help`, and naming the verb. Then the negative half:
    # `claude agents stop --help` must BE the plain `agents` help (the
    # silent P3 shape). If that stops holding, the platform changed and this
    # says so loudly.
    verb_help: Dict[str, str] = {}
    agents_help_stdout, _agents_help_stderr, _agents_help_rc = cli._invoke(
        [executable, "agents", "--help"], env=env
    )
    for verb in _LIFECYCLE_VERBS:
        stdout, stderr, _rc = cli._invoke([executable, verb, "--help"], env=env)
        text = stdout or stderr
        verb_help[verb] = text
        if text == agents_help_stdout:
            raise PreflightError(
                f"`claude {verb} --help` is byte-identical to `claude agents "
                "--help`; the top-level lifecycle verb no longer appears to "
                "exist as a distinct command (P3 has changed)"
            )
        if verb.lower() not in text.lower():
            raise PreflightError(
                f"`claude {verb} --help` does not mention {verb!r}; refusing "
                "to trust an unverified lifecycle surface"
            )

    negative_stdout, _negative_stderr, _negative_rc = cli._invoke(
        [executable, "agents", "stop", "--help"], env=env
    )
    if negative_stdout != agents_help_stdout:
        raise PreflightError(
            "`claude agents stop --help` no longer matches plain `claude "
            "agents --help` -- the platform's documented silent-no-op shape "
            "(P3) has changed; command construction assumptions must be "
            "re-verified before dispatching"
        )

    # 6. `claude --bg -p x` is rejected: nonzero, conflict text, no spawn.
    bg_stdout, bg_stderr, bg_rc = cli._invoke([executable, "--bg", "-p", "x"], env=env)
    bg_text = (bg_stdout or "") + (bg_stderr or "")
    if bg_rc == 0:
        raise PreflightError(
            "`claude --bg -p x` exited 0; expected a hard usage-error "
            "refusal (P3: --bg takes a positional prompt, never -p)"
        )
    if "backgrounded" in bg_text.lower():
        raise PreflightError(
            "`claude --bg -p x` appears to have spawned a session despite "
            "the conflicting flags; refusing to trust this platform's -p/--bg "
            "handling"
        )

    return PreflightReport(
        executable=executable,
        version_output=version_output,
        agents_json_sample=agents_sample,
        verb_help_output=verb_help,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Step 4 -- worker environment composition
# ---------------------------------------------------------------------------


class WorkerEnvironmentBillingLeakError(ExecutionError):
    """Raised by :func:`compose_worker_environment` when its own ``cwd_vars``
    completion step (4) reintroduces a name that step 3 had just subtracted
    -- an adapter declaring a ``cwd_var`` (or ``required_var``) whose name
    collides with a forbidden var or a :data:`BILLING_DIVERTING_VARS` name.
    The CHILD process is what bills, so this is checked against the fully
    composed child environment, not the adapter's declaration alone."""

    def __init__(self, names: Sequence[str]) -> None:
        self.names = tuple(names)
        super().__init__(
            "compose_worker_environment: the composed child environment "
            f"still carries forbidden/billing-diverting name(s) {list(names)!r} "
            "after subtraction -- a declared cwd_var (or required_var) name "
            "collides with a forbidden or billing-diverting variable name"
        )


def compose_worker_environment(
    run: RunRecord,
    adapter: RunAdapter,
    *,
    base: Optional[Mapping[str, str]] = None,
) -> Tuple[Dict[str, str], Optional[str]]:
    """Build a worker's process environment BEFORE the worker process starts.

    This exists because a Phase B consumer reads its project root from
    ``PWD`` into module-level constants at IMPORT TIME across nine files -- a
    worker that fixed its environment after import is already wrong. Every
    later worker-process concern (the protocol mount, the consumer's own
    entry point) must see a correct environment from its very first line.

    Five steps, in order (mirrors the B1 assignment's numbering):

    1. ``adapter.environment.materialize(run.environment or {})`` -- the
       adapter's own overlay (only its declared ``required_vars``, taken
       from the run's create-time snapshot) and the recorded ``cwd`` when
       ``require_cwd``.
    2. ``child = dict(base)`` (``base`` defaults to ``os.environ``, the
       DISPATCHER's own process environment) updated with the overlay --
       the overlay always wins over whatever the dispatcher process itself
       happened to have set, which is the whole point: a worker must see the
       RUN's recorded value, not the dispatcher's current one.
    3. Subtract every name in ``adapter.environment.forbidden_vars`` and
       every name in :data:`BILLING_DIVERTING_VARS` from the child. Never
       from ``run.environment`` or from ``base`` -- only from the composed
       child, which is the thing about to become a subprocess environment.
    4. ``cwd_vars`` completion: when ``adapter.environment.cwd_vars`` is
       non-empty, resolve the child's cwd (the recorded ``cwd`` from step 1
       when ``require_cwd``, else this DISPATCHER process's own
       ``os.getcwd()``) and set every declared ``cwd_var`` to that NATIVE
       path string in the child env.
    5. Re-run the step-3 scan against the fully composed child env (NOT a
       second subtraction -- a check). Step 4 can only ever ADD names to the
       child (it never removes anything step 3 subtracted), so a hit here
       means a ``cwd_var`` (or ``required_var``) name collides with a
       forbidden/billing-diverting name -- :class:`WorkerEnvironmentBillingLeakError`.

    Returns ``(child_env, cwd)`` -- ``cwd`` is the resolved working directory
    a spawner should launch the worker process IN (``None`` when the adapter
    declares neither ``require_cwd`` nor ``cwd_vars``).
    """
    if base is None:
        base = os.environ

    recorded = run.environment or {}
    overlay, cwd = adapter.environment.materialize(recorded)

    child: Dict[str, str] = dict(base)
    child.update(overlay)

    def _subtract(target: Dict[str, str]) -> None:
        for name in adapter.environment.forbidden_vars:
            target.pop(name, None)
        for name in BILLING_DIVERTING_VARS:
            target.pop(name, None)

    _subtract(child)

    if adapter.environment.cwd_vars:
        if adapter.environment.require_cwd and cwd is not None:
            resolved_cwd = cwd
        else:
            resolved_cwd = os.getcwd()
            if cwd is None:
                cwd = resolved_cwd
        for name in adapter.environment.cwd_vars:
            child[name] = resolved_cwd

    candidates = sorted(set(adapter.environment.forbidden_vars) | set(BILLING_DIVERTING_VARS))
    leaked = [name for name in candidates if child.get(name)]
    if leaked:
        raise WorkerEnvironmentBillingLeakError(leaked)

    return child, cwd


# ---------------------------------------------------------------------------
# Step 5 -- launch prompt and the enumerated invocation set (P5)
# ---------------------------------------------------------------------------


def _sanitize_path_component(value: str) -> str:
    """A filesystem-safe fragment for :func:`answer_path_for` -- every
    non-alnum/``-``/``_`` character becomes ``_``. Deterministic, and never
    empty for a non-empty ``value`` (``run_id``/``unit_id`` are non-empty by
    convention -- see ``pipeline.workunit.WorkUnit``)."""
    return "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in value)


@dataclass(frozen=True)
class WorkerCommand:
    """The consumer's declaration of how its protocol mount is invoked.

    ``argv`` is a TEMPLATE, not a full invocation: a tuple of argv tokens
    (e.g. ``("python", "mytool.py", "run")``) that :func:`enumerate_worker_invocations`
    extends with a verb (``claim``/``read``/``submit``/``fail``) and a fixed
    set of identifying flags. Any token containing the literal substrings
    ``{run_id}``/``{unit_id}``/``{worker_id}`` is substituted first (a
    consumer whose mount needs, say, a per-run ``--db`` path can embed
    ``{run_id}`` in one of its own tokens). B1 ships NO default template --
    the mount is the consumer's.

    ``answer_dir`` is the directory (a native path) a worker writes its
    deterministic per-unit answer file into, via the Write tool -- see
    :func:`answer_path_for`.
    """

    argv: Tuple[str, ...]
    answer_dir: str


def _format_argv(argv: Sequence[str], **subs: str) -> Tuple[str, ...]:
    """Literal-substring substitution over every ``argv`` token -- never
    :meth:`str.format`, which would raise on a token that happens to contain
    an unrelated ``{...}`` (a Windows path, a JSON-shaped flag value)."""
    out: List[str] = []
    for token in argv:
        for key, value in subs.items():
            token = token.replace("{" + key + "}", value)
        out.append(token)
    return tuple(out)


def answer_path_for(worker_command: WorkerCommand, run_id: str, unit_id: str) -> str:
    """The deterministic per-unit answer-file path a worker writes its
    submission text to, and that :func:`enumerate_worker_invocations`'s
    ``submit --from-file`` invocation reads back. Deterministic in ``run_id``
    and ``unit_id`` alone -- computable before the run, which is what makes
    the invocation set enumerable ahead of time (the module docstring's whole
    reason for existing)."""
    filename = (
        f"{_sanitize_path_component(run_id)}__{_sanitize_path_component(unit_id)}.answer.txt"
    )
    return os.path.join(worker_command.answer_dir, filename)


def enumerate_worker_invocations(
    worker_command: WorkerCommand, run_id: str, unit_id: str, worker_id: str
) -> Tuple[str, str, str, str, str]:
    """The EXACT command strings a worker for this unit may run: ``claim``,
    ``read``, ``submit --from-file <answer path>``, ``fail``, plus the
    tool-level entry for writing the answer file (a Write-tool invocation
    name, not a shell command -- writing the file is not a ``claude``
    subprocess call).

    Every returned string is deterministic given ``(run_id, unit_id,
    worker_id)`` -- no unit content, no timestamp, no random component --
    which is the property that makes a pre-authorized allowlist entry
    possible for it (P5): the same five strings can be computed, and
    allowlisted, before the worker ever runs.
    """
    subs = {"run_id": run_id, "unit_id": unit_id, "worker_id": worker_id}
    base = _format_argv(worker_command.argv, **subs)
    common = ("--run-id", run_id, "--unit-id", unit_id, "--worker-id", worker_id)
    answer_path = answer_path_for(worker_command, run_id, unit_id)

    claim_cmd = shlex.join(base + ("claim",) + common)
    read_cmd = shlex.join(base + ("read",) + common)
    submit_cmd = shlex.join(base + ("submit",) + common + ("--from-file", answer_path))
    fail_cmd = shlex.join(base + ("fail",) + common)
    write_cmd = f"Write tool -> {answer_path}"
    return (claim_cmd, read_cmd, submit_cmd, fail_cmd, write_cmd)


def build_launch_prompt(
    worker_command: WorkerCommand, run_id: str, unit_id: str, worker_id: str
) -> str:
    """The ``claude --bg`` launch prompt for one unit.

    Names the run id, unit id, and worker id; states the procedure as
    INVOCATIONS to run, verbatim, never as an outcome to achieve -- the
    2026-08-17 probe stalled on a shell redirect the worker composed itself
    to satisfy an instruction phrased as an outcome (``echo ... > file``), and
    no allowlist author would have enumerated it (P5). Unit content never
    appears here: the worker fetches its own prepared request via the
    ``read`` invocation at runtime.
    """
    claim_cmd, read_cmd, submit_cmd, fail_cmd, write_cmd = enumerate_worker_invocations(
        worker_command, run_id, unit_id, worker_id
    )
    answer_path = answer_path_for(worker_command, run_id, unit_id)
    return (
        f"Run id: {run_id}\n"
        f"Unit id: {unit_id}\n"
        f"Worker id: {worker_id}\n"
        f"Answer path: {answer_path}\n"
        "\n"
        "Perform exactly these invocations, in this order, and no others. "
        "Do not compose a redirect, pipe, or any other shell construct to "
        "satisfy any step below -- run the invocation exactly as written.\n"
        "\n"
        f"1. Claim the unit:\n   {claim_cmd}\n"
        f"2. Read the prepared request:\n   {read_cmd}\n"
        "3. Produce your answer text and write it, verbatim, with the Write "
        f"tool, to exactly this path (no other path):\n   {write_cmd}\n"
        f"4. Submit your answer:\n   {submit_cmd}\n"
        "   If the submission is rejected with feedback, revise the answer "
        "file (step 3) and repeat step 4.\n"
        f"5. If you cannot complete the unit, report failure:\n   {fail_cmd}\n"
    )


# ---------------------------------------------------------------------------
# Step 7 -- the reconciler (defined ahead of step 6, which consumes it)
# ---------------------------------------------------------------------------


class AgentsJsonParseError(ExecutionError):
    """``agents --json`` output could not be parsed under the schema-tolerant
    contract (P4): not JSON, not a list, an element that is not an object, or
    a ``kind == "background"`` element missing a required field."""


_REQUIRED_SESSION_FIELDS: Tuple[str, ...] = ("kind", "id", "sessionId", "state")


@dataclass(frozen=True)
class SessionRecord:
    """One ``kind == "background"`` record from ``agents --json`` (P4).

    ``id`` is the short id the launch banner prints and top-level lifecycle
    verbs (``claude stop|rm|respawn <id>``) take. ``session_id`` is the
    Claude session id (``sessionId`` in the raw JSON) -- D5's "identity is
    the Claude session ID, never the PID" -- and the per-job state file lives
    at ``~/.claude/jobs/<id>/state.json`` (keyed on the SHORT id, not
    ``session_id``; see :func:`classify_settled_failure`).

    ``started_at_ms`` carries the raw epoch-MILLISECONDS value exactly as
    read; :attr:`started_at_seconds` is a distinctly named float-seconds
    computed property, so a call site can never be ambiguous about which
    unit either attribute is in (P4's "startedAt is epoch milliseconds"
    note).

    ``pid``/``status``/``waiting_for`` are OPTIONAL: never required of a
    worker record, and never a background-vs-interactive discriminator --
    ``kind`` is the only one (P4).
    """

    kind: str
    id: str
    session_id: str
    state: str
    pid: Optional[int] = None
    status: Optional[str] = None
    waiting_for: Optional[str] = None
    started_at_ms: Optional[float] = None
    cwd: Optional[str] = None
    name: Optional[str] = None

    @property
    def started_at_seconds(self) -> Optional[float]:
        return self.started_at_ms / 1000.0 if self.started_at_ms is not None else None


@dataclass(frozen=True)
class ParseResult:
    """The result of :func:`parse_agents_json`: the background sessions
    found, and a count of every OTHER record filtered out (interactive
    sessions, including the orchestrator's own -- and any record with no
    ``kind`` at all)."""

    sessions: Tuple[SessionRecord, ...]
    ignored: int


def parse_agents_json(text: str) -> ParseResult:
    """Parse ``claude agents --json --all`` output, schema-tolerant (P4).

    Order is load-bearing (see the module's B1 assignment):

    1. Decode; require a JSON list; require every element to be a JSON
       object -- loud (:class:`AgentsJsonParseError`) otherwise.
    2. Filter ``kind == "background"`` FIRST -- everything else, including a
       record with NO ``kind`` at all (the orchestrator's own interactive
       session carries no ``id``), is counted into ``ParseResult.ignored``
       and never reaches field validation.
    3. THEN require ``kind``/``id``/``sessionId``/``state`` on the survivors.
       Missing -- loud, naming the field. Every other field is optional and
       unknown fields are ignored.

    Validating fields before filtering would raise on the orchestrator's own
    interactive record (no ``id``) instead of simply excluding it -- the
    exact ordering bug this function exists to avoid.
    """
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise AgentsJsonParseError(f"agents --json output is not valid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise AgentsJsonParseError(
            f"agents --json output is not a JSON list: {type(data).__name__}"
        )

    background_raw: List[Mapping[str, Any]] = []
    ignored = 0
    for index, element in enumerate(data):
        if not isinstance(element, Mapping):
            raise AgentsJsonParseError(
                f"agents --json element {index} is not a JSON object: "
                f"{type(element).__name__}"
            )
        if element.get("kind") != "background":
            ignored += 1
            continue
        background_raw.append(element)

    sessions: List[SessionRecord] = []
    for element in background_raw:
        missing = [f for f in _REQUIRED_SESSION_FIELDS if f not in element]
        if missing:
            raise AgentsJsonParseError(
                f"background agents --json record missing required field(s) "
                f"{missing}: {dict(element)!r}"
            )
        sessions.append(
            SessionRecord(
                kind=str(element["kind"]),
                id=str(element["id"]),
                session_id=str(element["sessionId"]),
                state=str(element["state"]),
                pid=element.get("pid"),
                status=element.get("status"),
                waiting_for=element.get("waitingFor"),
                started_at_ms=element.get("startedAt"),
                cwd=element.get("cwd"),
                name=element.get("name"),
            )
        )
    return ParseResult(sessions=tuple(sessions), ignored=ignored)


# ---------------------------------------------------------------------------
# Step 6 -- dispatch one unit, confirmed by an observed transition (P11)
# ---------------------------------------------------------------------------


class LaunchMisconfigurationError(ExecutionError):
    """A single launch never reached a confirmed background state (P11): the
    session either appeared as ``state: "failed"`` inside the confirmation
    window, or never appeared at all. Per the module's B1 assignment, the
    unit was never claimed by this launch, so no ``fail_unit`` is recorded
    for it -- only the dispatch is settled (``outcome="launch_failed"``) and
    the corpse, if one was identified, is ``rm``'d. A misconfiguration is
    identical for every subsequent launch in the same batch, so the caller
    (:func:`dispatch_wave`) aborts the whole dispatch loop rather than
    spending N sessions to learn the same thing N times."""

    def __init__(self, run_id: str, unit_id: str, worker_id: str, short_id: Optional[str]) -> None:
        self.run_id = run_id
        self.unit_id = unit_id
        self.worker_id = worker_id
        self.short_id = short_id
        super().__init__(
            f"run {run_id!r} unit {unit_id!r}: launch for worker {worker_id!r} "
            f"never reached a confirmed background state (short id observed: "
            f"{short_id!r}); classified as a launch misconfiguration"
        )


DEFAULT_LAUNCH_CONFIRM_SECONDS = 60.0
DEFAULT_LAUNCH_POLL_INTERVAL_S = 1.0

_BG_LAUNCH_BANNER_RE = re.compile(r"backgrounded\s*\*\s*([0-9a-fA-F]+)")


def _parse_launch_session_id(stdout: str) -> Optional[str]:
    """Best-effort extraction of the short session id from a ``claude --bg``
    launch banner (``"backgrounded * a47add3f"``, P3). Used only to know
    WHICH ``agents --json`` record to watch -- never as evidence the launch
    succeeded (P11: the banner and exit code are discarded as evidence of
    success; a bad flag surfaces only asynchronously as ``state: "failed"``).
    """
    if not stdout:
        return None
    match = _BG_LAUNCH_BANNER_RE.search(stdout)
    return match.group(1) if match else None


def _mint_worker_id() -> str:
    return f"claude-bg-{uuid.uuid4().hex[:12]}"


@dataclass
class OpenDispatch:
    """A dispatcher's in-memory bookkeeping for one currently open dispatch
    (B1). Distinct from :class:`~content_pipeline.execution.model.DispatchRecord`
    (the durable store row): this is what :func:`dispatch_wave` carries
    between ticks in its own process memory, re-derived from the store on
    every :func:`dispatch_unit` call and never itself the source of truth.

    ``fencing_token`` / ``claimed_by`` are captured once, right after launch
    confirmation, per the author ruling this module ships against: the
    driver mints ``worker_id`` BEFORE launch, and every later renewal
    precondition (:func:`supervise_tick`) compares the unit's CURRENT store
    state against these captured values, dropping the slot on any drift
    rather than renewing a claim this dispatcher does not own.
    """

    unit_id: str
    worker_id: str
    session_id: str
    id: str  # the short id (agents --json "id"; also the claude jobs/<id> directory name)
    fencing_token: int
    claimed_by: Optional[str]


def dispatch_unit(
    store: ExecutionStore,
    run_id: str,
    unit: UnitRecord,
    cli: "ClaudeCli",
    worker_command: WorkerCommand,
    *,
    worker_id: Optional[str] = None,
    extra_launch_args: Sequence[str] = (),
    env: Optional[Mapping[str, str]] = None,
    cwd: Optional[str] = None,
    launch_confirm_seconds: float = DEFAULT_LAUNCH_CONFIRM_SECONDS,
    poll_interval_s: float = DEFAULT_LAUNCH_POLL_INTERVAL_S,
    sleep_fn: Callable[[float], None] = time.sleep,
    clock_fn: Callable[[], float] = time.time,
    at: Optional[float] = None,
) -> OpenDispatch:
    """Dispatch ONE unit and confirm it, per P11.

    ``worker_id`` is minted BEFORE launch (author ruling) when not supplied.
    ``store.record_dispatch`` is called BEFORE ``cli.launch_bg`` -- duplicate
    suppression (the guarded uniqueness index) must bite before any spend.

    The launcher's exit code and banner are discarded as evidence of
    success; this function instead polls ``agents --json --all`` (via
    :func:`parse_agents_json`) until the launched session's short id appears
    in a state outside the initial (absent) state, or
    ``launch_confirm_seconds`` elapses. A ``"failed"`` observation, or no
    observation at all within the window, raises
    :class:`LaunchMisconfigurationError`: the dispatch is settled as
    ``launch_failed`` and, when a short id was identified, the corpse is
    ``rm``'d (best-effort; an ``rm`` failure never masks the
    misconfiguration).

    On confirmation, reads ``store.get_unit`` to capture the claim fence and
    ``claimed_by`` into the returned :class:`OpenDispatch` -- what
    :func:`supervise_tick` later checks before EVER renewing this unit's
    lease.
    """
    if worker_id is None:
        worker_id = _mint_worker_id()

    store.record_dispatch(run_id, unit.unit_id, worker_id, at=at)

    prompt = build_launch_prompt(worker_command, run_id, unit.unit_id, worker_id)
    launch_stdout, _launch_stderr, _launch_rc = cli.launch_bg(
        prompt, extra_args=extra_launch_args, env=env, cwd=cwd
    )
    short_id = _parse_launch_session_id(launch_stdout)

    matched: Optional[SessionRecord] = None
    if short_id is not None:
        deadline = clock_fn() + launch_confirm_seconds
        while True:
            poll_stdout, _poll_stderr, poll_rc = cli.agents_json(all_sessions=True, env=env)
            if poll_rc == 0:
                try:
                    parsed = parse_agents_json(poll_stdout)
                except AgentsJsonParseError:
                    parsed = None
                if parsed is not None:
                    matched = next((s for s in parsed.sessions if s.id == short_id), None)
            if matched is not None:
                break
            if clock_fn() >= deadline:
                break
            sleep_fn(poll_interval_s)

    if matched is None or matched.state == "failed":
        store.settle_dispatch(
            run_id,
            unit.unit_id,
            outcome="launch_failed",
            session_id=matched.session_id if matched is not None else None,
            at=at,
        )
        if matched is not None:
            try:
                cli.rm(matched.id, env=env)
            except Exception:  # noqa: BLE001 -- best-effort cleanup, never masks the error below
                pass
        raise LaunchMisconfigurationError(run_id, unit.unit_id, worker_id, short_id)

    unit_row = store.get_unit(run_id, unit.unit_id)
    return OpenDispatch(
        unit_id=unit.unit_id,
        worker_id=worker_id,
        session_id=matched.session_id,
        id=matched.id,
        fencing_token=unit_row.fencing_token if unit_row is not None else 0,
        claimed_by=unit_row.claimed_by if unit_row is not None else None,
    )


# ---------------------------------------------------------------------------
# Step 10 -- halt classification for a settled unit (never `claude logs`)
# ---------------------------------------------------------------------------

DEFAULT_TRANSCRIPT_TAIL_LINES = 200


def _find_transcript_path(session_id: str, *, projects_root: Optional[Path] = None) -> Optional[Path]:
    root = projects_root if projects_root is not None else (Path.home() / ".claude" / "projects")
    try:
        matches = sorted(root.glob(f"*/{session_id}.jsonl"))
    except OSError:
        return None
    return matches[0] if matches else None


def _tail_scan_transcript(path: Path, *, max_lines: int = DEFAULT_TRANSCRIPT_TAIL_LINES) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    lines = text.splitlines()
    return "\n".join(lines[-max_lines:])


def _read_job_state_text_fields(job_id: str, *, jobs_root: Optional[Path] = None) -> str:
    """``needs``/``detail``/``output.result`` -- TEXT fields only (P13);
    never a field that drives a progress/status decision."""
    root = jobs_root if jobs_root is not None else (Path.home() / ".claude" / "jobs")
    path = root / job_id / "state.json"
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError, ValueError):
        return ""
    if not isinstance(data, Mapping):
        return ""
    parts: List[str] = []
    for key in ("detail", "needs"):
        value = data.get(key)
        if isinstance(value, str):
            parts.append(value)
    output = data.get("output")
    if isinstance(output, Mapping):
        result = output.get("result")
        if isinstance(result, str):
            parts.append(result)
    return "\n".join(parts)


def classify_settled_failure(
    session_id: str,
    *,
    job_id: Optional[str] = None,
    projects_root: Optional[Path] = None,
    jobs_root: Optional[Path] = None,
) -> Optional[str]:
    """Halt classification for a SETTLED (no longer running) background
    session -- never ``claude logs`` (that channel is live-daemon-only, P13,
    and :class:`ClaudeCli` ships no ``logs`` method by construction).

    Two sources, each behind a tolerant parse that can NEVER raise out of
    this function (the whole body is wrapped): the session transcript for
    ``session_id`` (located under ``~/.claude/projects/*/``, tail-scanned),
    then -- when ``job_id`` is supplied -- ``~/.claude/jobs/<job_id>/state.json``'s
    TEXT fields only (``detail``, ``needs``, ``output.result`` -- P13: this
    file's own ``state`` field may disagree with ``agents --json`` and must
    never be read here or anywhere a status decision is made).

    Feeds whatever text either source yields to
    :func:`~content_pipeline.llm.platform.classify_halt_text`. ``None`` means
    an ordinary unit failure, NOT a halt.
    """
    try:
        transcript_path = _find_transcript_path(session_id, projects_root=projects_root)
        transcript_text = _tail_scan_transcript(transcript_path) if transcript_path else ""
        kind = classify_halt_text(transcript_text) if transcript_text else None
        if kind is not None:
            return kind
        if job_id:
            job_text = _read_job_state_text_fields(job_id, jobs_root=jobs_root)
            kind = classify_halt_text(job_text) if job_text else None
            if kind is not None:
                return kind
    except Exception:  # noqa: BLE001 -- must never raise out of a supervise tick
        return None
    return None


def _classify_and_maybe_halt(
    store: ExecutionStore, run_id: str, open_dispatch: OpenDispatch, *, at: Optional[float]
) -> Optional[str]:
    """Classify a settled dispatch's failure and, on ``rate_limit``/``auth``,
    call :func:`~content_pipeline.execution.controller.record_halt` (D4).
    Returns the classified kind, or ``None`` for an ordinary unit failure."""
    kind = classify_settled_failure(open_dispatch.session_id, job_id=open_dispatch.id)
    if kind in (HALT_RATE_LIMIT, HALT_AUTH):
        exc = HaltError(kind, detail=f"classified from settled session {open_dispatch.session_id!r}")
        record_halt(store, run_id, open_dispatch.unit_id, open_dispatch.fencing_token, exc, at=at)
        return kind
    return None


# ---------------------------------------------------------------------------
# Step 8 -- status classification, renewal, stall detection (D5, P12, P13)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TickResult:
    """What one :func:`supervise_tick` call observed and did."""

    renewed: Tuple[str, ...]
    settled: Dict[str, str]  # unit_id -> outcome
    dropped: Tuple[str, ...]  # unit_id -- fence/claimant drift, slot freed with no store write
    halted: Optional[str]


def supervise_tick(
    store: ExecutionStore,
    run_id: str,
    cli: "ClaudeCli",
    adapter: RunAdapter,
    open_dispatches: Mapping[str, OpenDispatch],
    *,
    env: Optional[Mapping[str, str]] = None,
    at: Optional[float] = None,
) -> TickResult:
    """ONE ``agents --json --all`` call serving every tracked open dispatch
    (D5, P12, P13).

    Per open dispatch:

    - Renewal preconditions, ALL THREE required: the dispatch is open (it is
      -- it is in ``open_dispatches``); ``unit.fencing_token`` equals the
      fence captured at confirmation; ``unit.claimed_by`` equals the minted
      ``worker_id``. Any drift means someone else reclaimed the unit --
      the slot is DROPPED (no renew, no store write) rather than renewing a
      claim this dispatcher does not own.
    - ``working``/``running`` -- renew, via
      ``store.renew_lease(..., lease_seconds=lease_for(adapter.resolve_expected_unit_seconds(unit)))``
      -- byte-identical to what ``protocol.build_handlers``'s
      ``_lease_ceiling`` derives for the worker's own claim.
    - ``blocked`` (any reason) -- STOP renewing, with NO grace (ruling 1: a
      background session has been observed blocked for 19 days with nothing
      timing it out, P12; renewing on ``blocked`` renews forever). The
      dispatch is settled (``outcome="blocked"``) so the unit becomes
      reclaimable once its lease naturally expires (D5) -- this dispatcher
      never calls ``fail_unit`` for it.
    - ``failed``/``stopped`` -- stop renewing, settle, classify (step 10).
    - ``done`` with the unit ACCEPTED -- the happy path: settle
      ``outcome="accepted"``, no classification.
    - ``done`` with the unit NOT accepted -- stop renewing, settle, classify.
    - Absent from ``--all`` -- stop renewing, settle (``outcome="missing"``),
      classify (best-effort, using the last-known session/job ids).

    No branch here reads ``~/.claude/jobs/<id>/state.json`` for a status
    decision (P13); :func:`classify_settled_failure` reads it for TEXT
    fields only, and only after a dispatch has already been settled from
    ``agents --json`` state.
    """
    now = time.time() if at is None else at

    stdout, _stderr, rc = cli.agents_json(all_sessions=True, env=env)
    if rc != 0:
        # No usable data this tick -- be conservative: renew nothing, settle
        # nothing, drop nothing. Try again next tick.
        return TickResult(renewed=(), settled={}, dropped=(), halted=None)
    try:
        parsed = parse_agents_json(stdout)
        sessions_by_session_id: Dict[str, SessionRecord] = {
            s.session_id: s for s in parsed.sessions
        }
    except AgentsJsonParseError:
        return TickResult(renewed=(), settled={}, dropped=(), halted=None)

    renewed: List[str] = []
    settled: Dict[str, str] = {}
    dropped: List[str] = []
    halted: Optional[str] = None

    for unit_id, open_dispatch in open_dispatches.items():
        current_unit = store.get_unit(run_id, unit_id)
        if (
            current_unit is None
            or current_unit.fencing_token != open_dispatch.fencing_token
            or current_unit.claimed_by != open_dispatch.worker_id
        ):
            dropped.append(unit_id)
            continue

        session = sessions_by_session_id.get(open_dispatch.session_id)

        if session is None:
            store.settle_dispatch(run_id, unit_id, outcome="missing", at=now)
            settled[unit_id] = "missing"
            kind = _classify_and_maybe_halt(store, run_id, open_dispatch, at=now)
            halted = halted or kind
            continue

        state = session.state
        if state in ("working", "running"):
            work_unit = adapter.unit_for(unit_id)
            seconds = lease_for(adapter.resolve_expected_unit_seconds(work_unit))
            store.renew_lease(
                run_id, unit_id, open_dispatch.fencing_token, lease_seconds=seconds, at=now
            )
            renewed.append(unit_id)
        elif state == "blocked":
            store.settle_dispatch(run_id, unit_id, outcome="blocked", at=now)
            settled[unit_id] = "blocked"
            # No classify_settled_failure here: a stalled worker is not a
            # settled FAILURE, and D5's "no grace" rule is about the RENEWAL
            # stopping, not about diagnosing why -- there is nothing failed
            # to explain yet.
        elif state == "done":
            if current_unit.state is UnitState.ACCEPTED:
                store.settle_dispatch(run_id, unit_id, outcome="accepted", at=now)
                settled[unit_id] = "accepted"
            else:
                store.settle_dispatch(run_id, unit_id, outcome="done_unaccepted", at=now)
                settled[unit_id] = "done_unaccepted"
                kind = _classify_and_maybe_halt(store, run_id, open_dispatch, at=now)
                halted = halted or kind
        elif state in ("failed", "stopped"):
            store.settle_dispatch(run_id, unit_id, outcome=state, at=now)
            settled[unit_id] = state
            kind = _classify_and_maybe_halt(store, run_id, open_dispatch, at=now)
            halted = halted or kind
        else:
            # An unrecognized state (a future platform addition): stop
            # renewing and settle rather than silently renewing forever on
            # an unknown value.
            store.settle_dispatch(run_id, unit_id, outcome=f"unknown:{state}", at=now)
            settled[unit_id] = f"unknown:{state}"

    return TickResult(renewed=tuple(renewed), settled=settled, dropped=tuple(dropped), halted=halted)


# ---------------------------------------------------------------------------
# Step 9 -- reclaim selection and bounded reclaims (driver-local; wave.py is
# untouched -- _flat_ready_wave returns only PENDING, so a unit whose worker
# died sits CLAIMED forever and never re-enters a wave through that module)
# ---------------------------------------------------------------------------


def reclaimable_units(store: ExecutionStore, run_id: str, *, at: Optional[float] = None) -> List[UnitRecord]:
    """Units in ``CLAIMED`` whose lease has expired, with NO open dispatch,
    ordinal order.

    ``no open dispatch`` is the guard that keeps this driver-local: a unit
    whose worker is still tracked (even if this dispatcher stopped renewing
    it, e.g. a ``blocked`` session -- see :func:`supervise_tick`) is not
    reclaimable until its dispatch has been settled, so a second launch is
    never dispatched on top of a still-open one.
    """
    now = time.time() if at is None else at
    open_unit_ids = {d.unit_id for d in store.open_dispatches(run_id)}
    units = sorted(store.list_units(run_id), key=lambda u: u.ordinal)
    return [
        u
        for u in units
        if u.state is UnitState.CLAIMED
        and u.lease_expires_at is not None
        and u.lease_expires_at <= now
        and u.unit_id not in open_unit_ids
    ]


def reclaim_attempt_count(store: ExecutionStore, run_id: str, unit_id: str) -> int:
    """How many :data:`~content_pipeline.execution.model.AttemptKind.EXPIRE`
    rows exist for this unit -- already durable via ``claim_unit``'s reclaim
    path, so this needs no new schema; it just counts."""
    return sum(
        1 for a in store.list_attempts(run_id, unit_id) if a.kind is AttemptKind.EXPIRE
    )


def _select_dispatch_candidates(
    store: ExecutionStore, run_id: str, wave_unit_ids: Set[str], *, at: Optional[float]
) -> List[UnitRecord]:
    """Candidates = (the still-``PENDING`` units of the original wave) +
    ``reclaimable_units(...)``, ordered by ordinal, per the module's B1
    assignment."""
    all_units = {u.unit_id: u for u in store.list_units(run_id)}
    pending_from_wave = [
        all_units[uid]
        for uid in wave_unit_ids
        if uid in all_units and all_units[uid].state is UnitState.PENDING
    ]
    combined: Dict[str, UnitRecord] = {u.unit_id: u for u in pending_from_wave}
    for u in reclaimable_units(store, run_id, at=at):
        combined.setdefault(u.unit_id, u)
    return sorted(combined.values(), key=lambda u: u.ordinal)


def _terminally_fail_exhausted_unit(
    store: ExecutionStore, run_id: str, unit_id: str, *, dispatcher_id: str, at: Optional[float]
) -> None:
    """Beyond ``max_reclaims_per_unit``: reclaim once more (bumping the
    fence, same as any reclaim) and immediately fail terminally, mirroring
    ``execution.controller``'s ``_record_terminal_skip`` claim-then-fail
    shape."""
    claim = store.claim_unit(run_id, unit_id, dispatcher_id, at=at)
    store.fail_unit(
        run_id, unit_id, claim.fencing_token, error="reclaim_exhausted", terminal=True, at=at
    )


# ---------------------------------------------------------------------------
# Step 11 -- the loop
# ---------------------------------------------------------------------------

DEFAULT_MAX_AGENTS = 4
DEFAULT_BATCH_SIZE = 25
DEFAULT_MAX_RECLAIMS_PER_UNIT = 2
DEFAULT_DISPATCH_POLL_INTERVAL_S = 15.0
DEFAULT_DISPATCHER_LEASE_SECONDS = 120.0


@dataclass(frozen=True)
class DispatchReport:
    """What one :func:`dispatch_wave` call did. No unit content anywhere --
    only ids, outcomes, and status digests (:mod:`~content_pipeline.execution.status`
    is itself invariant-6-clean)."""

    run_id: str
    dispatcher_acquired: bool
    dispatched: Tuple[str, ...] = ()
    accepted: Tuple[str, ...] = ()
    settled: Dict[str, str] = field(default_factory=dict)
    failed_exhausted: Tuple[str, ...] = ()
    halted: Optional[str] = None
    status_digests: Tuple[Dict[str, Any], ...] = ()
    aborted_reason: Optional[str] = None


def dispatch_wave(
    store: ExecutionStore,
    run_id: str,
    wave: Sequence[UnitRecord],
    adapter: RunAdapter,
    *,
    cli: Optional["ClaudeCli"] = None,
    worker_command: WorkerCommand,
    max_agents: int = DEFAULT_MAX_AGENTS,
    batch_size: int = DEFAULT_BATCH_SIZE,
    poll_interval_s: float = DEFAULT_DISPATCH_POLL_INTERVAL_S,
    launch_confirm_seconds: float = DEFAULT_LAUNCH_CONFIRM_SECONDS,
    lease_seconds: Optional[float] = None,
    max_reclaims_per_unit: int = DEFAULT_MAX_RECLAIMS_PER_UNIT,
    at: Optional[float] = None,
    env: Optional[Mapping[str, str]] = None,
    cwd: Optional[str] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    clock_fn: Callable[[], float] = time.time,
) -> DispatchReport:
    """Bounded dispatch loop over a prepared wave (B1). Mirrors
    ``drivers.inline.run_wave``'s shape: ``store``/``run_id``/``wave``/``adapter``
    positional, policy keyword-only, an injectable ``at``.

    ``max_agents`` and ``batch_size`` are the only two configurable settings
    (both pass the plugin-opinion razor per the plan: protecting interactive
    quota versus maximizing throughput is a genuine power-user preference).

    Loop: :func:`preflight` -> acquire the run-level DISPATCHER lease (a
    second concurrent dispatcher EXITS WITHOUT LAUNCHING, returning a report
    saying so, never raising) -> while slots are free, candidates remain, and
    the run is not halted: dispatch into free slots; each tick:
    :func:`supervise_tick`, renew the dispatcher lease, fold in settlements,
    refill. A batch-boundary status digest
    (:func:`~content_pipeline.execution.status.compute_status`) is emitted
    every ``batch_size`` dispatches. On exit (including via an early abort):
    release the dispatcher lease; ``stop`` + ``rm`` every dispatch THIS CALL
    opened and did not settle.
    """
    if cli is None:
        cli = ClaudeCli()

    preflight(cli, env=env)

    from content_pipeline.execution.model import UnknownRunError  # local: avoid a top-level cycle risk

    run_record = store.get_run(run_id)
    if run_record is None:
        raise UnknownRunError(run_id)
    # Step 6: compose the WORKER's launch environment/cwd once for the whole
    # call (item 4/5) -- used only for the launch itself; admin calls
    # (agents_json/stop/rm/preflight) keep using the dispatcher's own `env`.
    worker_env, worker_cwd = compose_worker_environment(run_record, adapter, base=env)
    launch_cwd = cwd if cwd is not None else worker_cwd

    now = clock_fn() if at is None else at
    dispatcher_id = _mint_worker_id()
    fence = store.acquire_dispatcher_lease(
        run_id, dispatcher_id, lease_seconds=DEFAULT_DISPATCHER_LEASE_SECONDS, at=now
    )
    if fence is None:
        return DispatchReport(
            run_id=run_id,
            dispatcher_acquired=False,
            aborted_reason="dispatcher_lease_held_by_another_dispatcher",
        )

    wave_unit_ids: Set[str] = {u.unit_id for u in wave}
    open_dispatches: Dict[str, OpenDispatch] = {}
    dispatched: List[str] = []
    accepted: List[str] = []
    settled_all: Dict[str, str] = {}
    failed_exhausted: List[str] = []
    halted: Optional[str] = None
    status_digests: List[Dict[str, Any]] = []
    aborted_reason: Optional[str] = None

    def _now() -> float:
        return clock_fn() if at is None else at

    try:
        while True:
            tick_now = _now()
            candidates = _select_dispatch_candidates(store, run_id, wave_unit_ids, at=tick_now)
            free_slots = max_agents - len(open_dispatches)

            if halted is None and free_slots > 0 and candidates:
                for unit in candidates[:free_slots]:
                    if (
                        unit.state is UnitState.CLAIMED
                        and reclaim_attempt_count(store, run_id, unit.unit_id) >= max_reclaims_per_unit
                    ):
                        _terminally_fail_exhausted_unit(
                            store, run_id, unit.unit_id, dispatcher_id=dispatcher_id, at=tick_now
                        )
                        failed_exhausted.append(unit.unit_id)
                        continue
                    try:
                        opened = dispatch_unit(
                            store,
                            run_id,
                            unit,
                            cli,
                            worker_command,
                            env=worker_env,
                            cwd=launch_cwd,
                            launch_confirm_seconds=launch_confirm_seconds,
                            poll_interval_s=min(poll_interval_s, DEFAULT_LAUNCH_POLL_INTERVAL_S),
                            sleep_fn=sleep_fn,
                            clock_fn=clock_fn,
                            at=tick_now,
                        )
                    except LaunchMisconfigurationError:
                        aborted_reason = "launch_misconfiguration"
                        break
                    open_dispatches[opened.unit_id] = opened
                    dispatched.append(opened.unit_id)
                    if len(dispatched) % batch_size == 0:
                        status_digests.append(compute_status(store, run_id).to_dict())

            if aborted_reason is not None:
                break

            if not open_dispatches and not candidates:
                break

            tick = supervise_tick(store, run_id, cli, adapter, open_dispatches, env=env, at=_now())
            for unit_id, outcome in tick.settled.items():
                open_dispatches.pop(unit_id, None)
                settled_all[unit_id] = outcome
                if outcome == "accepted":
                    accepted.append(unit_id)
            for unit_id in tick.dropped:
                open_dispatches.pop(unit_id, None)
            if tick.halted is not None:
                halted = tick.halted

            try:
                fence = store.acquire_dispatcher_lease(
                    run_id, dispatcher_id, lease_seconds=DEFAULT_DISPATCHER_LEASE_SECONDS, at=_now()
                ) or fence
                store.renew_dispatcher_lease(
                    run_id, dispatcher_id, fence, lease_seconds=DEFAULT_DISPATCHER_LEASE_SECONDS, at=_now()
                )
            except StaleDispatcherLeaseError:
                aborted_reason = "dispatcher_lease_lost"
                break

            if not open_dispatches and (halted is not None or not candidates):
                break

            if open_dispatches:
                sleep_fn(poll_interval_s)
    finally:
        for unit_id, opened in list(open_dispatches.items()):
            try:
                cli.stop(opened.id, env=env)
            except Exception:  # noqa: BLE001 -- best-effort cleanup on exit
                pass
            try:
                cli.rm(opened.id, env=env)
            except Exception:  # noqa: BLE001 -- best-effort cleanup on exit
                pass
        try:
            store.release_dispatcher_lease(run_id, dispatcher_id, fence, at=_now())
        except StaleDispatcherLeaseError:
            pass

    status_digests.append(compute_status(store, run_id).to_dict())

    return DispatchReport(
        run_id=run_id,
        dispatcher_acquired=True,
        dispatched=tuple(dispatched),
        accepted=tuple(accepted),
        settled=settled_all,
        failed_exhausted=tuple(failed_exhausted),
        halted=halted,
        status_digests=tuple(status_digests),
        aborted_reason=aborted_reason,
    )


__all__ = [
    "BILLING_DIVERTING_VARS",
    "AgentsJsonParseError",
    "ClaudeCli",
    "ClaudeExecutableNotFoundError",
    "DispatchReport",
    "LaunchMisconfigurationError",
    "OpenDispatch",
    "ParseResult",
    "PreflightError",
    "PreflightReport",
    "SessionRecord",
    "TickResult",
    "WorkerCommand",
    "WorkerEnvironmentBillingLeakError",
    "answer_path_for",
    "build_launch_prompt",
    "classify_settled_failure",
    "compose_worker_environment",
    "dispatch_unit",
    "dispatch_wave",
    "enumerate_worker_invocations",
    "parse_agents_json",
    "preflight",
    "reclaim_attempt_count",
    "reclaimable_units",
    "supervise_tick",
]
