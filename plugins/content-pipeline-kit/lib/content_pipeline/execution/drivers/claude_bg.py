"""The Claude-background-session driver: foundation only (B1 steps 1-4).

**Scope boundary, read before extending this module.** This module currently
ships exactly four things, per the plan's B1 sequencing
(``docs/planning/content-pipeline-kit/session-recipients-plan.md``, "Phase B
-- Claude background sessions"):

1. :class:`ClaudeCli` -- the ``claude`` process seam. Every argv this module
   ever builds funnels through ``ClaudeCli.runner``, its SOLE process
   boundary; nothing here calls :mod:`subprocess` directly.
2. :func:`preflight` -- capability/auth/platform-shape checks run once before
   a dispatcher does anything, per :class:`PreflightReport`.
3. The store migration and dispatcher-lease / dispatch-tracking methods (see
   ``execution/store.py``) this module's later parts will use.
4. :func:`compose_worker_environment` -- builds a worker's process
   environment from the adapter's :class:`~content_pipeline.execution.adapter.WorkerEnvironment`
   declaration, fixed BEFORE the worker process starts (see that function's
   docstring for why this matters).

**Not shipped here, deliberately.** Dispatch (bounded-N launch loop), the
launch prompt, session-ID reconciliation from ``agents --json``, lease
renewal, reclaim, halt classification, pause/resume, and the worker agent /
skill assets are a LATER unit of work. Nothing below composes a launch
command against a real unit, and there is no dispatch loop in this module.

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
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from content_pipeline.execution.adapter import RunAdapter
from content_pipeline.execution.model import ExecutionError, RunRecord

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


__all__ = [
    "BILLING_DIVERTING_VARS",
    "ClaudeCli",
    "ClaudeExecutableNotFoundError",
    "PreflightError",
    "PreflightReport",
    "WorkerEnvironmentBillingLeakError",
    "compose_worker_environment",
    "preflight",
]
