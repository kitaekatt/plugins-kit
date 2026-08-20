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
5. :class:`WorkerCommand`, :func:`envelope_path_for`, :func:`worker_envelopes_for`,
   :func:`enumerate_worker_invocations`, :func:`build_launch_prompt` -- the
   enumerated invocation set (P5, six entries: three ``protocol @<path>``
   invocations plus three Write-tool targets) and the launch prompt built
   from it. Every worker verb goes through ``cli.run.build_commands``'s
   ``protocol`` command with a ``@<path>`` JSON envelope -- never the flag
   form an earlier revision of this module emitted, which ``build_commands``
   never registered as a command at all.

   The worker's verbs are ``read``/``submit``/``fail``. ``claim`` is NOT one
   of them: the DISPATCHER claims each unit itself, before the launch, and
   the resulting fencing token rides the launch prompt (see
   :func:`dispatch_unit` and :func:`build_launch_prompt`). A worker session
   therefore has no way to claim anything, which is what stops a session
   left alive by an earlier dispatch from re-claiming a unit that has since
   been reclaimed and re-dispatched under a fresh ``worker_id``.

   :func:`format_fenced_answer` / :func:`parse_fenced_answer` belong to the
   same set: the answer ARTIFACT's own fence. The answer file's first line
   declares the fencing token its text was produced under; ``cli.run``'s
   ``--text-file=`` splice matches that declaration against the submit
   envelope's own ``fencing_token`` and refuses on any mismatch. The path
   stays generation-neutral (see :func:`answer_path_for`), so the token
   lives in runtime FILE CONTENT and never in an enumerated command string
   (P5).
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
import textwrap
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from content_pipeline.execution.adapter import RunAdapter
from content_pipeline.execution.controller import record_halt
from content_pipeline.execution.model import (
    AlreadyClaimedError,
    AttemptKind,
    ExecutionError,
    RunHaltedError,
    RunRecord,
    StaleDispatcherLeaseError,
    TerminalStateError,
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

    ``envelope_dir`` is the directory (a native path) a worker's JSON
    protocol envelopes live in -- see :func:`envelope_path_for`. Additive
    and optional: ``None`` (the default) means "same directory as
    ``answer_dir``", via :attr:`resolved_envelope_dir`, so an existing
    caller that never sets this field keeps writing everything to one
    directory exactly as before this field existed.
    """

    argv: Tuple[str, ...]
    answer_dir: str
    envelope_dir: Optional[str] = None

    @property
    def resolved_envelope_dir(self) -> str:
        """``envelope_dir`` when set, else ``answer_dir`` -- the directory a
        caller should actually use for envelope paths. Kept as a property
        (never resolved into a stored field) so a caller that mutates
        ``answer_dir`` after construction -- there is none today, but the
        class is otherwise immutable-by-convention -- never leaves this
        derived value stale."""
        return self.envelope_dir if self.envelope_dir is not None else self.answer_dir


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
    reason for existing).

    Deliberately carries NO ``worker_id`` and no generation counter, so two
    successive dispatches of the same unit write the same file. The
    generation is fenced in the file's CONTENT instead, by
    :func:`format_fenced_answer` -- putting it in the path would make the
    path un-computable before the run and destroy exactly the pre-run
    enumerability this function exists for."""
    filename = (
        f"{_sanitize_path_component(run_id)}__{_sanitize_path_component(unit_id)}.answer.txt"
    )
    return os.path.join(worker_command.answer_dir, filename)


# ---------------------------------------------------------------------------
# The answer artifact's own fence -- content, never path
# ---------------------------------------------------------------------------

ANSWER_FENCE_PREFIX = "content-pipeline-fence:"


class AnswerFenceError(ExecutionError):
    """Base class for a refusal to read an answer artifact whose declared
    fencing token cannot be trusted for the submission presenting it."""


class MissingAnswerFenceError(AnswerFenceError):
    """The answer artifact's first line is not a fence declaration.

    Refused rather than treated as unfenced-and-fine: an artifact with no
    declared generation is exactly the artifact a previous dispatch's
    still-live session may have written, and accepting it would splice text
    of unknown provenance into a currently-valid submit envelope."""

    def __init__(self, first_line: str) -> None:
        self.first_line = first_line
        super().__init__(
            "answer artifact does not begin with a fence line "
            f"({ANSWER_FENCE_PREFIX!r} followed by the fencing token); its "
            f"first line was {first_line!r}"
        )


class AnswerFenceMismatchError(AnswerFenceError):
    """The answer artifact declares a DIFFERENT fencing token than the
    submit envelope presenting it -- either a stale artifact under a current
    envelope, or a current artifact under a stale envelope. Both are the
    same defect seen from opposite ends: the text and the standing to submit
    it came from different generations of the same unit."""

    def __init__(self, declared: int, expected: int) -> None:
        self.declared = declared
        self.expected = expected
        super().__init__(
            f"answer artifact declares fencing token {declared!r} but the "
            f"submission presents {expected!r}; refusing to submit text "
            "produced under a different claim"
        )


def format_fenced_answer(fencing_token: int, text: str) -> str:
    """The exact bytes a worker writes to :func:`answer_path_for`'s path.

    One fence line, then the answer text verbatim::

        content-pipeline-fence: 7
        <the answer text, exactly as produced>

    Only the FIRST line is ever interpreted, so the body may contain
    anything at all -- including further lines that look like fence lines,
    which :func:`parse_fenced_answer` returns untouched as part of the
    answer."""
    return f"{ANSWER_FENCE_PREFIX} {fencing_token}\n{text}"


def parse_fenced_answer(raw: str, expected_token: int) -> str:
    """The answer text out of ``raw``, or a typed refusal.

    Splits on the FIRST newline only: the first line must be
    :data:`ANSWER_FENCE_PREFIX` followed by an integer equal to
    ``expected_token``, and everything after that newline is the answer,
    returned byte-for-byte. Because only the first line is inspected, a body
    that itself contains ``content-pipeline-fence:`` is ordinary text and is
    neither re-parsed nor stripped.

    Raises :class:`MissingAnswerFenceError` when the first line is not a
    fence declaration at all, and :class:`AnswerFenceMismatchError` when it
    declares a different token."""
    first_line, separator, body = raw.partition("\n")
    declaration = first_line.rstrip("\r").strip()
    if not declaration.startswith(ANSWER_FENCE_PREFIX):
        raise MissingAnswerFenceError(first_line)
    token_text = declaration[len(ANSWER_FENCE_PREFIX):].strip()
    try:
        declared = int(token_text)
    except ValueError as exc:
        raise MissingAnswerFenceError(first_line) from exc
    if declared != expected_token:
        raise AnswerFenceMismatchError(declared, expected_token)
    return body if separator else ""


def envelope_path_for(
    worker_command: WorkerCommand, run_id: str, unit_id: str, verb: str
) -> str:
    """The deterministic per-unit, per-verb JSON protocol-envelope path --
    the file a ``read``/``submit``/``fail`` invocation's ``@<path>``
    argument names (see ``cli.run.build_commands``'s ``protocol`` command).
    Deterministic in ``(run_id, unit_id, verb)`` alone, mirroring
    :func:`answer_path_for`'s determinism in ``(run_id, unit_id)`` -- the
    same property that makes an enumerated invocation pre-allowlistable."""
    filename = (
        f"{_sanitize_path_component(run_id)}__{_sanitize_path_component(unit_id)}.{verb}.json"
    )
    return os.path.join(worker_command.resolved_envelope_dir, filename)


_ENVELOPE_VERBS: Tuple[str, ...] = ("read", "submit", "fail")


def _envelope_payload_text(verb: str, run_id: str, unit_id: str, worker_id: str) -> str:
    """The literal JSON (``read``) or JSON-shaped TEMPLATE
    (``submit``/``fail``) text for one verb's envelope.

    ``read`` needs no fencing token (its payload does not consume one -- see
    ``execution/protocol.py``'s ``_read``), so its text is ordinary, valid,
    ready-to-use JSON. ``submit``/``fail`` DO need a fencing token, but that
    value is not knowable when this function runs (P5's determinism
    constraint: an enumerated invocation string must be computable from
    ``(run_id, unit_id, worker_id)`` alone, before any unit is ever
    claimed). So their text carries the literal, unquoted placeholder token
    ``<FENCING_TOKEN>`` in place of a real value -- NOT valid JSON as
    written, and not meant to be parsed until a worker substitutes the real
    token, which its LAUNCH PROMPT names (the dispatcher claims the unit
    before launching; see :func:`dispatch_unit`). See
    :func:`worker_envelopes_for`'s docstring for who writes which of these
    to disk and when.
    """
    if verb == "read":
        envelope = {
            "protocol_version": "1",
            "verb": verb,
            "payload": {"run_id": run_id, "unit_id": unit_id, "worker_id": worker_id},
        }
        return json.dumps(envelope, indent=2) + "\n"
    # submit / fail: JSON-shaped template text, fencing_token a literal
    # placeholder a worker fills in at runtime -- see docstring above.
    return (
        "{\n"
        '  "protocol_version": "1",\n'
        f"  \"verb\": {json.dumps(verb)},\n"
        '  "payload": {\n'
        f"    \"run_id\": {json.dumps(run_id)},\n"
        f"    \"unit_id\": {json.dumps(unit_id)},\n"
        f"    \"worker_id\": {json.dumps(worker_id)},\n"
        '    "fencing_token": <FENCING_TOKEN>\n'
        "  }\n"
        "}\n"
    )


def worker_envelopes_for(
    worker_command: WorkerCommand, run_id: str, unit_id: str, worker_id: str
) -> Dict[str, Tuple[str, str]]:
    """``{verb: (path, text)}`` for ``read``/``submit``/``fail`` -- the JSON
    protocol-envelope path and content for each verb this unit's worker ever
    needs. Pure function: no filesystem I/O here, deterministic in
    ``(run_id, unit_id, worker_id)`` alone (P5), same as every other
    function in this section.

    There is deliberately no ``claim`` entry. The dispatcher claims the unit
    itself before launching (:func:`dispatch_unit`), so a worker session has
    no claim envelope to run and no way to take a claim -- which is what
    stops a session left alive by an earlier dispatch from re-claiming a
    unit that has since been reclaimed and re-dispatched.

    A caller writes these to disk at two different TIMES, for two different
    reasons, per the D5/P5 design this module ships against:

    - ``read`` is written by the DISPATCHER, before the worker's session
      ever launches (:func:`build_launch_prompt` does this) -- its text
      needs no runtime information, so pre-writing it is what lets the
      dispatcher pre-authorize the ``read`` invocation (P5).
    - ``submit``/``fail`` are written by the WORKER itself, at runtime, via
      the Write tool -- their text needs the fencing token, which is not
      knowable when this function runs. The text this function returns for
      them is a TEMPLATE (see :func:`_envelope_payload_text`): the worker's
      only permitted edit is substituting the literal ``<FENCING_TOKEN>``
      token for the real value its launch prompt names; nothing else in the
      template may change.
    """
    return {
        verb: (envelope_path_for(worker_command, run_id, unit_id, verb),
               _envelope_payload_text(verb, run_id, unit_id, worker_id))
        for verb in _ENVELOPE_VERBS
    }


def enumerate_worker_invocations(
    worker_command: WorkerCommand, run_id: str, unit_id: str, worker_id: str
) -> Tuple[str, str, str, str, str, str]:
    """The EXACT command/Write-tool-target strings a worker for this unit
    may run or write, in order: ``read``, ``submit --text-file=<answer
    path>``, ``fail``, the Write-tool target for the answer file, the
    Write-tool target for the ``submit`` envelope, and the Write-tool target
    for the ``fail`` envelope.

    There is no ``claim`` entry: the dispatcher claims each unit before
    launching its worker (:func:`dispatch_unit`), so ``read`` is a worker's
    first invocation.

    Every returned string is deterministic given ``(run_id, unit_id,
    worker_id)`` -- no unit content, no timestamp, no random component, and
    (P5-critical) NO FENCING TOKEN, even though the dispatcher now knows the
    token before the launch. Keeping it out of these strings is what makes a
    pre-authorized allowlist entry possible: the same six strings can be
    computed, and allowlisted, before the worker ever runs. The token
    reaches the worker through the launch PROMPT and rides in file CONTENT
    (the submit/fail envelopes it authors, and the answer artifact's fence
    line), never in an invocation string.

    Each of ``read``/``submit``/``fail`` is ``<argv> protocol @<envelope
    path>`` (see ``cli.run.build_commands``'s ``protocol`` command and its
    ``@<path>`` envelope-sourcing form) -- never the old flag form
    (``claim --run-id ... --unit-id ...``), which
    ``cli.run.build_commands`` never registered as a command at all (only
    ``protocol`` is), so the flag form always failed as an unknown command
    for every verb except ``claim`` (whose flags accidentally parsed as
    positional argv and silently held the unit's lease forever without
    ever reaching ``read``/``submit``).
    """
    subs = {"run_id": run_id, "unit_id": unit_id, "worker_id": worker_id}
    base = _format_argv(worker_command.argv, **subs)
    envelopes = worker_envelopes_for(worker_command, run_id, unit_id, worker_id)
    answer_path = answer_path_for(worker_command, run_id, unit_id)

    read_path, _ = envelopes["read"]
    submit_path, _ = envelopes["submit"]
    fail_path, _ = envelopes["fail"]

    read_cmd = shlex.join(base + ("protocol", f"@{read_path}"))
    submit_cmd = shlex.join(
        base + ("protocol", f"@{submit_path}", f"--text-file={answer_path}")
    )
    fail_cmd = shlex.join(base + ("protocol", f"@{fail_path}"))
    write_answer_cmd = f"Write tool -> {answer_path}"
    write_submit_cmd = f"Write tool -> {submit_path}"
    write_fail_cmd = f"Write tool -> {fail_path}"
    return (
        read_cmd,
        submit_cmd,
        fail_cmd,
        write_answer_cmd,
        write_submit_cmd,
        write_fail_cmd,
    )


def build_launch_prompt(
    worker_command: WorkerCommand,
    run_id: str,
    unit_id: str,
    worker_id: str,
    fencing_token: int,
) -> str:
    """The ``claude --bg`` launch prompt for one unit.

    Names the run id, unit id, worker id, answer path and FENCING TOKEN;
    states the procedure as INVOCATIONS to run, verbatim, never as an
    outcome to achieve -- the 2026-08-17 probe stalled on a shell redirect
    the worker composed itself to satisfy an instruction phrased as an
    outcome (``echo ... > file``), and no allowlist author would have
    enumerated it (P5). Unit content never appears here: the worker fetches
    its own prepared request via the ``read`` invocation at runtime.

    ``fencing_token`` is the token the DISPATCHER's own claim returned
    (:func:`dispatch_unit` claims before launching). It reaches the worker
    here, in the prompt, and nowhere else -- never in an enumerated
    invocation string, which must stay pre-computable for P5 allowlisting.
    The worker substitutes it into the ``submit``/``fail`` envelope
    templates and writes it as the fence line of its answer artifact.

    This function has a side effect, deliberately: it pre-writes the
    ``read`` JSON envelope file to ``worker_command.resolved_envelope_dir``
    (creating the directory if needed) BEFORE the worker session ever
    launches -- see :func:`worker_envelopes_for`'s docstring for why that
    verb, and only that verb, is safe to pre-write. ``submit``/``fail`` are
    never written here; the prompt instead instructs the worker to author
    them itself, from the verbatim template text, substituting only the
    fencing token named above.
    """
    (
        read_cmd,
        submit_cmd,
        fail_cmd,
        write_answer_cmd,
        write_submit_cmd,
        write_fail_cmd,
    ) = enumerate_worker_invocations(worker_command, run_id, unit_id, worker_id)
    answer_path = answer_path_for(worker_command, run_id, unit_id)
    envelopes = worker_envelopes_for(worker_command, run_id, unit_id, worker_id)

    envelope_dir = worker_command.resolved_envelope_dir
    os.makedirs(envelope_dir, exist_ok=True)
    read_path, read_text = envelopes["read"]
    Path(read_path).write_text(read_text, encoding="utf-8")

    submit_template = textwrap.indent(envelopes["submit"][1], "     ")
    fail_template = textwrap.indent(envelopes["fail"][1], "     ")

    return (
        f"Run id: {run_id}\n"
        f"Unit id: {unit_id}\n"
        f"Worker id: {worker_id}\n"
        f"Answer path: {answer_path}\n"
        f"Fencing token: {fencing_token}\n"
        "\n"
        "This unit is already reserved for you by the dispatcher. The "
        "fencing token above is your authority to submit it, and it is the "
        "only place that value comes from -- there is no invocation below "
        "that returns one.\n"
        "\n"
        "Perform exactly these invocations, in this order, and no others. "
        "Do not compose a redirect, pipe, or any other shell construct to "
        "satisfy any step below -- run the invocation exactly as written.\n"
        "\n"
        f"1. Read the prepared request:\n   {read_cmd}\n"
        "2. Produce your answer text and write it, verbatim, with the Write "
        f"tool, to exactly this path (no other path):\n   {write_answer_cmd}\n"
        f"   The FIRST line of that file must be exactly:\n"
        f"     {ANSWER_FENCE_PREFIX} {fencing_token}\n"
        "   Your answer text follows on the next line, verbatim and "
        "unaltered. That first line is how the submission proves the text "
        "was produced under the token above; a file without it is refused.\n"
        "3. Author your submission envelope: with the Write tool, write "
        "EXACTLY the template below, substituting ONLY the literal token "
        f"<FENCING_TOKEN> with {fencing_token}, to exactly this path (no "
        f"other path):\n   {write_submit_cmd}\n"
        f"   Template:\n{submit_template}\n"
        f"4. Submit your answer:\n   {submit_cmd}\n"
        "   If the submission is rejected with feedback, revise the answer "
        "file (step 2, fence line included) and repeat step 4 -- the "
        "submission envelope from step 3 does not change and must not be "
        "rewritten.\n"
        "5. If you cannot complete the unit, author your failure envelope "
        "the same way as step 3 -- write EXACTLY the template below, "
        "substituting ONLY <FENCING_TOKEN>, to exactly this path (no other "
        f"path):\n   {write_fail_cmd}\n"
        f"   Template:\n{fail_template}\n"
        f"   Then report failure:\n   {fail_cmd}\n"
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

    ``fencing_token`` / ``claimed_by`` are the DISPATCHER's own claim values,
    not a post-launch read of the store: :func:`dispatch_unit` mints
    ``worker_id`` and claims the unit itself BEFORE the launch, so these are
    exact by construction. Every later renewal precondition
    (:func:`supervise_tick`) compares the unit's CURRENT store state against
    them, dropping the slot on any drift rather than renewing a claim this
    dispatcher does not own. (They used to be read back from the store after
    the launch-confirmation poll, which raced the worker's own claim: a
    worker that claimed after confirmation left this holding a PRE-claim
    fence, so the drift guard dropped the slot on the very first tick, the
    dispatch row was never settled, and the unit became permanently
    unreclaimable.)

    ``terminal_since`` is the ONLY mutable field: the time at which
    :func:`supervise_tick` first observed this dispatch's unit in a terminal
    state while its session was still alive. It is the clock for the
    terminal-exit grace (see that function's ``working``/``running``
    branch), and ``None`` until such an observation happens.
    """

    unit_id: str
    worker_id: str
    session_id: str
    id: str  # the short id (agents --json "id"; also the claude jobs/<id> directory name)
    fencing_token: int
    claimed_by: Optional[str]
    terminal_since: Optional[float] = None


def _release_claim_and_settle(
    store: ExecutionStore,
    run_id: str,
    unit_id: str,
    fencing_token: int,
    *,
    session_id: Optional[str] = None,
    at: Optional[float],
) -> None:
    """Undo a dispatcher-held claim whose launch never produced a worker:
    release the claim (a NON-terminal ``fail_unit``, returning the unit to
    PENDING) and settle the open dispatch row (``outcome="launch_failed"``).

    BOTH halves are required and BOTH are best-effort. Required, because a
    unit left CLAIMED holds a live lease nobody will ever renew, and a unit
    left with an OPEN dispatch row is excluded by :func:`reclaimable_units`
    and is therefore unreclaimable FOREVER -- not merely until a lease
    expires. Best-effort, because this only ever runs while an exception is
    already in flight, and a cleanup failure must never replace the failure
    that caused it.

    ``outcome="launch_failed"`` is deliberately the SAME value the observed
    ``state: "failed"`` / never-appeared branch uses, not a new synonym: to
    every reader of a settled dispatch row the two cases are identical --
    this dispatch never reached a confirmed background state and no worker
    ever ran the unit. Nothing downstream branches on the distinction, and
    the exception itself (which is re-raised, never swallowed) is where the
    detail of what went wrong lives.
    """
    try:
        store.fail_unit(
            run_id, unit_id, fencing_token, error="launch_failed", terminal=False, at=at
        )
    except Exception:  # noqa: BLE001 -- best-effort: never mask the in-flight error
        pass
    try:
        store.settle_dispatch(
            run_id, unit_id, outcome="launch_failed", session_id=session_id, at=at
        )
    except Exception:  # noqa: BLE001 -- best-effort: never mask the in-flight error
        pass


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

    THE DISPATCHER CLAIMS. ``store.claim_unit`` is called here, before the
    launch, and the token it returns is passed into
    :func:`build_launch_prompt`. The worker never claims and has no claim
    envelope to run, so a session left alive by an earlier dispatch of the
    same unit cannot re-claim it after a reclaim has re-dispatched it under
    a fresh ``worker_id``; that zombie's token is stale, so its ``submit``
    and ``fail`` fail closed with ``StaleFenceError`` -- the duplicated
    spend invariant 4 already accepts, not lost work.

    On :class:`LaunchMisconfigurationError` the claim taken here is
    RELEASED (a non-terminal ``store.fail_unit``, returning the unit to
    PENDING), mirroring ``_terminally_fail_exhausted_unit``'s claim-then-fail
    shape. A unit must never be left CLAIMED, holding a live lease, on
    behalf of a worker that never started.

    THAT RELEASE COVERS EVERY POST-CLAIM PATH, not just the confirmation
    branch: no exception raised anywhere after the claim -- from
    :func:`build_launch_prompt`'s filesystem writes, from
    ``cli.launch_bg``'s executable resolution or spawn, from the
    confirmation poll -- may leave the unit both CLAIMED and holding an
    unsettled dispatch row, because that combination is unrecoverable
    (:func:`reclaimable_units` skips a unit with an open dispatch, and no
    other code path settles one). The claim is released and the dispatch
    settled (``outcome="launch_failed"``, see
    :func:`_release_claim_and_settle`), best-effort, and the ORIGINAL
    exception is re-raised unchanged.

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

    The returned :class:`OpenDispatch` carries the claim's OWN
    ``fencing_token`` and ``worker_id`` -- what :func:`supervise_tick` later
    checks before EVER renewing this unit's lease.
    """
    if worker_id is None:
        worker_id = _mint_worker_id()

    store.record_dispatch(run_id, unit.unit_id, worker_id, at=at)

    try:
        claim = store.claim_unit(run_id, unit.unit_id, worker_id, at=at)
    except Exception:
        # The dispatch row is already open, and an open dispatch makes its
        # unit permanently unreclaimable (see `reclaimable_units`). Close it
        # before letting the claim failure out, so a unit this dispatcher
        # could not claim is not also stranded for every later wave.
        try:
            store.settle_dispatch(run_id, unit.unit_id, outcome="claim_failed", at=at)
        except Exception:  # noqa: BLE001 -- never mask the claim failure below
            pass
        raise

    # EVERYTHING after the claim runs guarded. `build_launch_prompt` does
    # real filesystem I/O (makedirs + write of the read envelope) and
    # `cli.launch_bg` resolves an executable and spawns a process, so either
    # can raise for reasons that have nothing to do with this unit. An
    # unguarded raise here would leave the unit CLAIMED with a live lease
    # AND holding an open dispatch row -- which `reclaimable_units` excludes,
    # so nothing could ever recover it: `dispatch_wave`'s exit cleanup only
    # settles dispatches it is TRACKING, and this one never got that far.
    try:
        prompt = build_launch_prompt(
            worker_command, run_id, unit.unit_id, worker_id, claim.fencing_token
        )
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
            # Release the claim taken above before settling: no worker ever
            # started, so leaving the unit CLAIMED with a live lease would
            # make it unclaimable until that lease expired, for nothing.
            # Best-effort so a store failure here never masks the
            # misconfiguration.
            _release_claim_and_settle(
                store,
                run_id,
                unit.unit_id,
                claim.fencing_token,
                session_id=matched.session_id if matched is not None else None,
                at=at,
            )
            if matched is not None:
                try:
                    cli.rm(matched.id, env=env)
                except Exception:  # noqa: BLE001 -- best-effort, never masks the error below
                    pass
            raise LaunchMisconfigurationError(run_id, unit.unit_id, worker_id, short_id)
    except LaunchMisconfigurationError:
        raise  # already released and settled by the branch above
    except BaseException:
        _release_claim_and_settle(store, run_id, unit.unit_id, claim.fencing_token, at=at)
        raise

    return OpenDispatch(
        unit_id=unit.unit_id,
        worker_id=worker_id,
        session_id=matched.session_id,
        id=matched.id,
        fencing_token=claim.fencing_token,
        claimed_by=worker_id,
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

# How long a dispatch whose unit is already TERMINAL may keep its slot while
# its background session has not exited yet. Sized to swallow the ordinary
# submit-then-exit window (seconds, at a 15s poll) many times over, so the
# ordinary case still settles through the `done` branch; beyond it the
# session is stopped and the dispatch settled, because nothing else in the
# system can ever close it (see supervise_tick's working/running branch).
DEFAULT_TERMINAL_EXIT_GRACE_SECONDS = 300.0


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
    terminal_exit_grace_seconds: float = DEFAULT_TERMINAL_EXIT_GRACE_SECONDS,
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
    - ``working``/``running`` with the unit still CLAIMED -- renew, via
      ``store.renew_lease(..., lease_seconds=lease_for(adapter.resolve_expected_unit_seconds(unit)))``
      -- byte-identical to what ``protocol.build_handlers``'s
      ``_lease_ceiling`` derives for the worker's own claim.
    - ``working``/``running`` with the unit NO LONGER CLAIMED (it submitted
      through the protocol and its session has not exited yet) -- renew
      NOTHING, and give the session ``terminal_exit_grace_seconds`` to exit
      on its own so the ordinary case still settles through the ``done``
      branch. Past the grace, ``stop`` + ``rm`` the session and settle
      (``outcome="session_lingering"``): nothing else in the system can ever
      close this dispatch, so an unbounded wait here is an unbounded
      :func:`dispatch_wave`.
    - ``blocked`` (any reason) -- STOP renewing, with NO grace (ruling 1: a
      background session has been observed blocked for 19 days with nothing
      timing it out, P12; renewing on ``blocked`` renews forever). Best-effort
      ``stop`` + ``rm`` first, then the dispatch is settled
      (``outcome="blocked"``) so the unit becomes reclaimable once its lease
      naturally expires (D5) -- this dispatcher never calls ``fail_unit``
      for it. The ``stop``/``rm`` are hygiene only; their return codes are
      not inspected, so they do not establish that the session ended.
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
            if current_unit.state is not UnitState.CLAIMED:
                # The worker submitted through the protocol and its session
                # has NOT exited yet -- the normal submit-then-exit window at
                # a 15s poll, not a rare race. The unit is no longer CLAIMED,
                # so store.renew_lease would raise NotClaimedError, and
                # nothing here catches it: it would escape supervise_tick AND
                # dispatch_wave, tearing down the wave and abandoning every
                # other in-flight dispatch.
                #
                # So: renew nothing, and hold the dispatch open for a BOUNDED
                # grace. Holding it is what keeps the slot honest while the
                # session is alive -- max_agents caps LIVE sessions, and the
                # wave's exit cleanup stops/rms only dispatches still open --
                # and the ``done`` branch below settles it on the first tick
                # after it exits, exactly as it does for a worker that
                # submits and exits between two ticks.
                #
                # Bounding it is what keeps the WAVE alive. Nothing else can
                # ever close this dispatch: the unit is terminal (ACCEPTED is
                # the only state reachable here -- every other non-CLAIMED
                # transition either clears ``claimed_by`` via ``fail_unit``
                # or bumps the fence via ``claim_unit``, and is DROPPED by
                # the guard above before reaching this branch), so it is
                # never a dispatch candidate again, and ``accept_unit``
                # leaves ``claimed_by`` and the fence INTACT, so the drift
                # guard never drops it either. An unconditional wait here is
                # therefore an unbounded dispatch_wave -- a silent hang, not
                # a stall that anything reports.
                if open_dispatch.terminal_since is None:
                    open_dispatch.terminal_since = now
                if now - open_dispatch.terminal_since < terminal_exit_grace_seconds:
                    continue
                # Grace spent. END the session here rather than leaving it:
                # settling removes this dispatch from the caller's
                # ``open_dispatches``, so the wave's own exit cleanup will no
                # longer stop/rm it, and a still-live session would be
                # leaked. Both calls are best-effort -- an unreachable
                # daemon must not stop the settle below, which is what frees
                # the slot and closes the dispatch row.
                for _verb in (cli.stop, cli.rm):
                    try:
                        _verb(open_dispatch.id, env=env)
                    except Exception:  # noqa: BLE001 -- best-effort cleanup
                        pass
                store.settle_dispatch(run_id, unit_id, outcome="session_lingering", at=now)
                settled[unit_id] = "session_lingering"
                # No classify_settled_failure: the unit is ACCEPTED. This is
                # a session that overstayed, not a failure to explain.
                continue
            work_unit = adapter.unit_for(unit_id)
            seconds = lease_for(adapter.resolve_expected_unit_seconds(work_unit))
            store.renew_lease(
                run_id, unit_id, open_dispatch.fencing_token, lease_seconds=seconds, at=now
            )
            renewed.append(unit_id)
        elif state == "blocked":
            # End the session before settling, mirroring the
            # session_lingering branch above: settling removes this dispatch
            # from the caller's ``open_dispatches``, so the wave's own exit
            # cleanup will no longer stop/rm it and a still-live session
            # would be leaked. Both calls are best-effort and must never
            # stop the settle below, which is what frees the slot and makes
            # the unit reclaimable once its lease expires.
            #
            # This is HYGIENE, not a fix for anything: ``stop``/``rm``
            # return ``(stdout, stderr, rc)`` and this loop ignores a
            # NONZERO rc as well as an exception, so a session that refuses
            # to die is still left running. Handling the return codes is a
            # separate piece of work; do not read these two calls as a
            # guarantee that the session is gone.
            for _verb in (cli.stop, cli.rm):
                try:
                    _verb(open_dispatch.id, env=env)
                except Exception:  # noqa: BLE001 -- best-effort cleanup
                    pass
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

# The wave-level liveness bound: how long dispatch_wave may observe NO
# progress at all -- nothing dispatched, renewed, settled, or dropped --
# before it aborts instead of polling forever. Deliberately generic: it is
# not aimed at any one cause of a stuck open dispatch (the terminal-exit
# grace above handles the one that is understood), it exists so that a cause
# nobody has thought of yet ends as a reported abort rather than as a silent
# hang. A live unit renews on every tick, so genuinely long work re-arms it
# continuously and is never cut off; only a wave in which literally nothing
# is happening can reach it.
DEFAULT_WAVE_STALL_SECONDS = 900.0


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
    extra_launch_args: Sequence[str] = (),
    terminal_exit_grace_seconds: float = DEFAULT_TERMINAL_EXIT_GRACE_SECONDS,
    stall_timeout_seconds: float = DEFAULT_WAVE_STALL_SECONDS,
    at: Optional[float] = None,
    env: Optional[Mapping[str, str]] = None,
    cwd: Optional[str] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    clock_fn: Callable[[], float] = time.time,
) -> DispatchReport:
    """Bounded dispatch loop over a prepared wave (B1). Mirrors
    ``drivers.inline.run_wave``'s shape: ``store``/``run_id``/``wave``/``adapter``
    positional, policy keyword-only, an injectable ``at``.

    ``max_agents`` and ``batch_size`` are the two configurable dispatch
    settings (both pass the plugin-opinion razor per the plan: protecting
    interactive quota versus maximizing throughput is a genuine power-user
    preference).

    ``extra_launch_args`` is the LAUNCH-ARGS SEAM: a sequence of ``claude``
    flags forwarded verbatim, in order, to :func:`dispatch_unit` and thence
    to :meth:`ClaudeCli.launch_bg`, which places them between ``--bg`` and
    the positional prompt. It is how a consumer selects a worker agent --
    e.g. ``extra_launch_args=("--agent", "pipeline-worker")`` for the agent
    definition this plugin ships, or its own agent name -- and how any other
    launch flag (a permission mode, a system-prompt file) reaches the
    worker.

    The default is empty and the driver NEVER adds a flag of its own, which
    is deliberate rather than conservative: whether agent-selection flags
    compose with ``--bg`` or are silently dropped is NOT established (the
    flags are known to exist and background sessions are known to load
    plugin skills; composition with ``--bg`` has never been observed, and
    per P11 it could only be judged by a worker's behavior, never by the
    launcher's exit code, which is 0 either way). Selecting an agent by
    default would therefore ship a possible silent no-op. With the default
    the launch argv is exactly ``[exe, "--bg", prompt]``, and the launch
    prompt built by :func:`build_launch_prompt` is self-contained: it names
    the run, unit, worker and answer path, enumerates the exact invocations
    the worker may run, and carries the no-shell-construct rule. A worker
    launched with no agent is governed by that prompt.

    Loop: :func:`preflight` -> acquire the run-level DISPATCHER lease (a
    second concurrent dispatcher EXITS WITHOUT LAUNCHING, returning a report
    saying so, never raising) -> while slots are free, candidates remain, and
    the run is not halted: dispatch into free slots; each tick:
    :func:`supervise_tick`, renew the dispatcher lease, fold in settlements,
    refill. A batch-boundary status digest
    (:func:`~content_pipeline.execution.status.compute_status`) is emitted
    every ``batch_size`` dispatches. On exit (including via an early abort):
    release the dispatcher lease; ``stop`` + ``rm`` every dispatch THIS CALL
    opened and did not settle, and SETTLE it (``outcome="wave_exit"``) --
    an open dispatch row makes its unit permanently unreclaimable
    (:func:`reclaimable_units` skips a unit that has one), so a dispatch
    abandoned open on an abort path would strand its unit in every later
    wave.

    CLAIM REFUSALS ARE ROUTINE, NOT WAVE-FATAL. The dispatcher claims
    (:func:`dispatch_unit`), so the store's typed refusals now surface here
    instead of inside a worker process, and exactly three are handled --
    everything else, including any unexpected exception type, still
    propagates:

    - :class:`~content_pipeline.execution.model.TerminalStateError` and
      :class:`~content_pipeline.execution.model.AlreadyClaimedError` -- the
      unit stopped being dispatchable between candidate selection and the
      claim (the accepted invariant-4 race: a still-live prior worker
      settling its unit under a still-current token, since neither
      ``accept_unit`` nor ``fail_unit`` checks lease expiry). SKIP that unit
      and continue with the rest of the wave; it appears in ``settled`` as
      ``claim_failed``.
    - :class:`~content_pipeline.execution.model.RunHaltedError` -- the RUN
      is halted, so dispatching more is wrong. The wave ends GRACEFULLY,
      identically to an observed halt: ``halted`` is set, no further unit is
      dispatched, already-open dispatches wind down, and a
      :class:`DispatchReport` is returned.

    LIVENESS. The loop is bounded three ways, and the third exists because
    the first two are per-cause: ``aborted_reason`` (launch
    misconfiguration, lost dispatcher lease), the per-launch
    ``launch_confirm_seconds``, and -- covering every cause of a dispatch
    that nothing can close -- ``stall_timeout_seconds``, after which a wave
    that has observed NO progress (nothing dispatched, renewed, settled, or
    dropped) aborts with ``aborted_reason="wave_stalled"``. Renewal counts
    as progress, so a genuinely long-running unit re-arms the bound on every
    tick and is never cut off. Stall time is measured on ``clock_fn``, not
    on ``at``, so pinning ``at`` for reproducible writes does not disarm it.
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
    claim_refused: Set[str] = set()
    failed_exhausted: List[str] = []
    halted: Optional[str] = None
    status_digests: List[Dict[str, Any]] = []
    aborted_reason: Optional[str] = None

    def _now() -> float:
        return clock_fn() if at is None else at

    last_progress_at = clock_fn()

    try:
        while True:
            tick_now = _now()
            dispatched_before = len(dispatched)
            exhausted_before = len(failed_exhausted)
            candidates = [
                u
                for u in _select_dispatch_candidates(store, run_id, wave_unit_ids, at=tick_now)
                # A unit whose claim this wave already refused is not
                # dispatchable BY THIS WAVE. Excluding it is what makes the
                # skip terminate: an `AlreadyClaimedError` unit can still be
                # re-selected next tick, and re-attempting it every tick
                # would spin (a refusal is not progress, so only the stall
                # bound would ever end the wave).
                if u.unit_id not in claim_refused
            ]
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
                            extra_launch_args=extra_launch_args,
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
                    except RunHaltedError as exc:
                        # The RUN is halted, so no further claim can succeed
                        # and dispatching more is simply wrong. End the wave
                        # the SAME way an observed halt ends it (the
                        # `halted` field, the `halted is None` dispatch
                        # guard, the wind-down of already-open dispatches) --
                        # never by tearing it down: `halted` is a reportable
                        # outcome here, not an abort. `claim_failed` is the
                        # dispatch row `dispatch_unit` already wrote before
                        # re-raising.
                        halted = halted or exc.kind
                        settled_all.setdefault(unit.unit_id, "claim_failed")
                        break
                    except (TerminalStateError, AlreadyClaimedError):
                        # A ROUTINE race, not an error: between candidate
                        # selection and this claim, the unit went terminal
                        # or was claimed by someone else -- exactly the
                        # duplicate-spend case invariant 4 accepts. Before
                        # the dispatcher claimed, this refusal happened
                        # inside the worker and never reached the wave.
                        # The unit is no longer dispatchable BY THIS WAVE;
                        # skip it and keep going. `dispatch_unit` settled
                        # its dispatch row as `claim_failed` before
                        # re-raising, which is what makes it visible in the
                        # report's `settled` map.
                        claim_refused.add(unit.unit_id)
                        settled_all.setdefault(unit.unit_id, "claim_failed")
                        continue
                    open_dispatches[opened.unit_id] = opened
                    dispatched.append(opened.unit_id)
                    if len(dispatched) % batch_size == 0:
                        status_digests.append(compute_status(store, run_id).to_dict())

            if aborted_reason is not None:
                break

            if not open_dispatches and not candidates:
                break

            tick = supervise_tick(
                store,
                run_id,
                cli,
                adapter,
                open_dispatches,
                env=env,
                terminal_exit_grace_seconds=terminal_exit_grace_seconds,
                at=_now(),
            )
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

            # The wave-level liveness bound (see the docstring's LIVENESS
            # paragraph). Progress is anything that moved: a launch, a
            # terminal failure, a renewal, a settlement, a drop.
            if (
                len(dispatched) != dispatched_before
                or len(failed_exhausted) != exhausted_before
                or tick.renewed
                or tick.settled
                or tick.dropped
            ):
                last_progress_at = clock_fn()
            elif clock_fn() - last_progress_at >= stall_timeout_seconds:
                aborted_reason = "wave_stalled"
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
            # Settle what was just stopped. An open dispatch row makes its
            # unit permanently unreclaimable, so abandoning one here (only
            # reachable on an abort path -- a normal exit leaves nothing
            # open) would strand the unit in every later wave.
            try:
                store.settle_dispatch(run_id, unit_id, outcome="wave_exit", at=_now())
            except Exception:  # noqa: BLE001 -- a finally block must never raise
                pass
            else:
                settled_all.setdefault(unit_id, "wave_exit")
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
    "ANSWER_FENCE_PREFIX",
    "BILLING_DIVERTING_VARS",
    "AgentsJsonParseError",
    "AnswerFenceError",
    "AnswerFenceMismatchError",
    "MissingAnswerFenceError",
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
    "format_fenced_answer",
    "parse_agents_json",
    "parse_fenced_answer",
    "preflight",
    "reclaim_attempt_count",
    "reclaimable_units",
    "supervise_tick",
]
