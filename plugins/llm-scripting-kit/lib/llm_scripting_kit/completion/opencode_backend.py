"""OpenCode CLI completion transport.

The fourth backend behind the shared :class:`~.types.LLMBackend` protocol,
alongside the OpenRouter, Claude CLI, and Codex CLI transports. OpenCode owns
provider authentication and any provider billing, so this layer records only
the usage the CLI actually exposes.

The OpenCode shape is deliberately not copied from the Codex transport:

* **The answer arrives on stdout.** The default output is the answer text;
  ``--format json`` is an NDJSON event stream, not a result object, and
  OpenCode has no result-file flag. The adapter therefore builds no output
  file and this backend returns the runner's stdout verbatim.
* **The command grammar belongs to the adapter.** A harness entry is made
  from the requested model and passed to :class:`OpencodeAdapter`, which owns
  ``run``, ``--dir``, ``--variant``, and the required ``--auto`` flag. This
  module owns only completion policy and subprocess execution.
* **Workspace confinement is explicit.** ``--auto`` remains necessary for a
  non-interactive run, so the backend injects a highest-precedence OpenCode
  policy that denies external-directory access and subagent delegation. The
  adapter also disables external plugins while retaining normal shell work.

The failure rule is intentionally narrow: a nonzero exit, empty stdout, or the
runner's bounded wall-clock timeout is a transport failure. A zero exit with
stdout is an answer, even when the answer text says that the provider failed.
Trying to interpret that text here would turn a model judgment into an
automatic re-dispatch.
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, ClassVar, Optional

from ..harness_adapters import OPENCODE_AGENT, OPENCODE_HARNESS, OpencodeAdapter
from ..model_endpoints import HARNESS_KIND, EndpointEntry
from . import halt
from .claude_runner import AgentTimeoutError, run_cli_streaming
from .adapter_capabilities import OPENCODE_CAPABILITIES
from .capabilities import Capabilities
from .prompt_fold import fold_prompt
from .results import (
    check_applied_controls,
    derive_dropped_params,
    derive_forwarded_params,
    fixed_control_ids,
    utc_now_iso,
)
from .types import BackendOptions, LLMResponse


# A refused OpenCode server retries for about 66 seconds before it exits. The
# default must be LONGER than that window, or a refusal is misreported as a
# timeout; it must still be finite because an unreachable server never exits.
DEFAULT_OPENCODE_TIMEOUT_S = 120.0

# OpenCode exposes one stdin prompt rather than a separate system channel.
OPENCODE_PROMPT_SEPARATOR = "\n\n---\n\n"
# Public alias kept for callers that import the sibling helper name from this
# module (a shared-library name in __all__ stays resolvable).
PROMPT_SEPARATOR = OPENCODE_PROMPT_SEPARATOR

# This is an OpenCode permission boundary, not an OS-level filesystem sandbox.
OPENCODE_FILESYSTEM_POSTURE = "workspace-guarded"
_FILESYSTEM_NOTICE = (
    "opencode filesystem posture is workspace-guarded: external-directory "
    "access and subagent delegation are explicitly denied; this is not an OS sandbox"
)
from .capabilities import (
    FILESYSTEM_WRITE,
    SHELL_EXEC,
    SUBAGENT_SPAWN,
    subjects_for_disallowed_tools,
)

_INLINE_CONFIG_ENV = "OPENCODE_CONFIG_CONTENT"


class OpencodeRunError(RuntimeError):
    """An OpenCode run that did not produce a usable final answer.

    The raw channels ride on ATTRIBUTES, never in the message. OpenCode's
    answer is model-authored stdout, and a provider diagnostic can be echoed
    into a channel too. The halt taxonomy matches substrings in an exception
    message, so interpolating either channel would let healthy output that
    merely discusses a rate limit or authentication forge a persistent halt.
    """

    def __init__(
        self,
        message: str,
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: Optional[int] = None,
        cmd: Optional[list[str]] = None,
    ) -> None:
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.cmd = cmd


def compose_prompt(system: str, user: str) -> str:
    """Fold the two protocol prompt halves into OpenCode's stdin brief.

    OpenCode's non-interactive ``run`` command has one prompt channel. Keep
    the system half first, with the same explicit separator used by the Codex
    sibling, and avoid a leading/trailing separator when one half is empty.
    The folding itself is shared with that sibling -- see :mod:`.prompt_fold`.
    """
    return fold_prompt(system, user, OPENCODE_PROMPT_SEPARATOR)


def _entry_for_model(model: str) -> EndpointEntry:
    """Represent a direct model argument as the adapter's harness entry.

    Registry resolution belongs to the caller. The completion protocol already
    supplies the concrete model id, so this small synthetic entry lets the
    adapter apply its normal validation and command grammar without creating a
    second model-resolution path in this transport.
    """
    return EndpointEntry(
        id="opencode-completion",
        base_url=None,
        model=model,
        kind=HARNESS_KIND,
        harness=OPENCODE_HARNESS,
    )


@dataclass
class OpencodeCliBackend:
    """OpenCode ``run`` completion over :func:`.run_cli_streaming`.

    Attributes:
        default_timeout_s: Per-call watchdog when ``options.timeout_s`` is
            absent. The 120-second default exceeds OpenCode's observed
            approximately 66-second connection-refused retry window, while
            placing a hard upper bound on the never-exiting unreachable-host
            case.
        argv_prefix: Explicit launcher argv for tests. ``None`` lets the
            adapter resolve ``opencode`` from PATH at dispatch time.
        runner: The subprocess runner test seam; production uses
            :func:`.claude_runner.run_cli_streaming`.
        adapter: Optional adapter test seam. When omitted, a fresh
            :class:`OpencodeAdapter` is used for each call.

    Options mapping:

    - ``model`` -> the adapter's harness entry model.
    - ``effort`` -> the adapter's provider-specific ``--variant``.
    - ``timeout_s`` -> the runner's explicit per-call watchdog.
    - ``cwd`` -> the process working directory and OpenCode ``--dir``. It is
      an execution directory only, never a confinement boundary.
    - ``log_prefix`` -> the runner's stderr tag.
    - ``max_tokens`` / ``temperature`` / ``cache_salt`` /
      ``user_cache_prefix`` / ``allowed_tools`` /
      ``system_prompt_mode`` / ``extras`` -- OpenCode's
      adapter contract exposes no corresponding completion flag. They are
      accepted for protocol compatibility and dropped, as the other CLI
      backends do for their inapplicable fields -- and REPORTED as dropped
      rather than silently discarded, ``extras`` per key. This backend reads
      no extras key, so every key the caller sent comes back in
      ``dropped_params`` as ``extras.<key>``.

    Usage and cost: the default stdout format supplies answer text but no
    token envelope and no cost. ``input_tokens``, ``output_tokens``,
    ``cache_hit_tokens``, and ``total_tokens`` therefore remain honest zeros;
    this backend does not infer a split or pretend to know a provider's bill.
    The shared response type has no per-call cost field, so caller-side pricing
    must not treat these zeros as provider billing evidence.
    """

    default_timeout_s: float = DEFAULT_OPENCODE_TIMEOUT_S
    argv_prefix: Optional[tuple] = None
    runner: Callable[..., "tuple[str, str, int]"] = run_cli_streaming
    adapter: Optional[OpencodeAdapter] = None
    name: str = field(default="opencode-cli", init=False)
    capabilities: ClassVar[Capabilities] = OPENCODE_CAPABILITIES
    filesystem_posture: str = field(
        default=OPENCODE_FILESYSTEM_POSTURE, init=False
    )

    def complete(
        self,
        system: str,
        user: str,
        *,
        model: str,
        options: Optional[BackendOptions] = None,
    ) -> LLMResponse:
        """Run one OpenCode completion and return stdout as the answer.

        Only the subprocess result determines transport failure. In
        particular, a zero exit with output is returned unchanged even if the
        output text is a provider's own error-shaped response.
        """
        opts = options or BackendOptions()
        timeout_s = (
            opts.timeout_s
            if opts.timeout_s is not None
            else self.default_timeout_s
        )
        root = opts.cwd if opts.cwd is not None else Path.cwd().resolve()
        # An absent cwd is resolved before the adapter sees it. A caller-supplied
        # relative cwd is left alone so the adapter rejects it rather than
        # silently changing the caller's requested working directory.
        prompt = compose_prompt(system, user)
        invocation = self._adapter().build_invocation(
            _entry_for_model(model),
            root,
            prompt=prompt,
            effort=opts.effort,
        )
        env = _confined_opencode_env(disallowed_tools=opts.disallowed_tools)

        # This is deliberately a runtime notice, not just a docstring. A caller
        # must see that this is an OpenCode policy boundary, not an OS sandbox.
        self._announce_filesystem_posture(opts.log_prefix)

        started_at = utc_now_iso()
        start = time.monotonic()
        try:
            stdout, stderr, returncode = self.runner(
                list(invocation.argv),
                invocation.stdin,
                root,
                log_prefix=opts.log_prefix,
                timeout_s=timeout_s,
                hard_stop_markers=(),
                label="opencode run",
                env=env,
            )
        except AgentTimeoutError as exc:
            # run_cli_streaming historically includes channel tails in its
            # timeout message. Preserve its DISTINCT timeout type and all
            # diagnostics on attributes, but strip the tails before re-raising:
            # OpenCode stdout is model-authored and must never reach a message
            # that halt classification substring-matches.
            self._sanitize_timeout_message(exc, timeout_s)
            raise

        wall_ms = int((time.monotonic() - start) * 1000)
        if returncode != 0:
            # Keep this message authored by the transport. The full transcript
            # is available to a caller through OpencodeRunError.stdout/stderr.
            raise OpencodeRunError(
                f"opencode run failed (exit {returncode})",
                stdout=stdout,
                stderr=stderr,
                returncode=returncode,
                cmd=list(invocation.argv),
            )
        if not stdout.strip():
            raise OpencodeRunError(
                "opencode run exited 0 but produced no final answer",
                stdout=stdout,
                stderr=stderr,
                returncode=returncode,
                cmd=list(invocation.argv),
            )

        return LLMResponse(
            text=stdout,
            model=model,
            input_tokens=0,
            output_tokens=0,
            cache_hit_tokens=0,
            total_tokens=0,
            wall_ms=wall_ms,
            attempts=1,
            from_cache=False,
            dropped_params=self._dropped_params(opts),
            forwarded_params=derive_forwarded_params(self.capabilities, opts),
            # Most controls this adapter advertises are source=FIXED -- the
            # four fixed flags plus the unconditional permission scalars go
            # out on every invocation, so the fixed set is read straight from
            # the advertisement. Three are source=REQUEST, though
            # (permission-bash-deny, permission-edit-deny,
            # permission-task-request-deny): they are armed only when a
            # caller's disallowed_tools names a matching subject, via the same
            # translation _confined_opencode_env uses to build the env, so
            # they are reported only when actually emitted.
            execution_controls_applied=self._applied_controls(opts),
            started_at=started_at,
            ended_at=utc_now_iso(),
        )

    def _dropped_params(self, opts: BackendOptions) -> "tuple[str, ...]":
        """The generic derivation, plus disallowed_tools when it went nowhere.

        disallowed_tools is not in OPENCODE_CAPABILITIES.dropped_params -- the
        adapter DOES honour it, when the value names a subject it has a
        permission scalar for. But a value naming no recognised subject at all
        (e.g. "Read WebFetch": no edit/bash/task token in it) emits nothing,
        so the caller's deny request went nowhere -- a partially-honoured-param
        case the generic field-vs-advertisement intersection in
        derive_dropped_params cannot see, because it is per-VALUE rather than
        per-field. Report it as dropped for this call only, exactly when
        subjects_for_disallowed_tools finds nothing to translate; a value that
        maps at least one subject (even alongside unrecognised names) is
        honoured in part and stays unreported, matching the outcome
        _applied_controls reports for it.
        """
        dropped = list(derive_dropped_params(self.capabilities, opts))
        if opts.disallowed_tools is not None and not subjects_for_disallowed_tools(
            opts.disallowed_tools
        ):
            dropped.append("disallowed_tools")
        return tuple(dropped)

    def _applied_controls(self, opts: BackendOptions) -> "tuple[str, ...]":
        """The advertised controls this call's env actually carries.

        The fixed set is unconditional -- every source=FIXED control in the
        advertisement goes out on every invocation. The three source=REQUEST
        deny controls are conditional: they are armed only when
        ``disallowed_tools`` names a subject :func:`denied_permissions`
        translates into a permission scalar, via the same
        ``subjects_for_disallowed_tools`` mapping ``_confined_opencode_env``
        uses to build the env this call sends.
        """
        applied = list(fixed_control_ids(self.capabilities))
        for subject in subjects_for_disallowed_tools(opts.disallowed_tools):
            control_id = _CONTROL_ID_FOR_SUBJECT.get(subject)
            if control_id is not None:
                applied.append(control_id)
        return check_applied_controls(self.capabilities, applied)

    def classify_halt(self, exc: BaseException) -> Optional[str]:
        """Classify a halt without raising :class:`halt.HaltError` itself."""
        return halt.classify_opencode_exception(exc)

    def _adapter(self) -> OpencodeAdapter:
        if self.adapter is not None:
            return self.adapter
        return OpencodeAdapter(argv_prefix=self.argv_prefix)

    @staticmethod
    def _announce_filesystem_posture(log_prefix: str) -> None:
        sys.stderr.write(f"{log_prefix} {_FILESYSTEM_NOTICE}\n")
        sys.stderr.flush()

    @staticmethod
    def _sanitize_timeout_message(
        exc: AgentTimeoutError, timeout_s: float
    ) -> None:
        """Keep timeout diagnostics on attributes, not in ``str(exc)``."""
        exc.args = (
            f"opencode run exceeded {timeout_s}s timeout; subprocess was killed",
        )


_PERMISSION_KEY_FOR_SUBJECT = {
    FILESYSTEM_WRITE: "edit",
    SHELL_EXEC: "bash",
    SUBAGENT_SPAWN: "task",
}

# The execution-control id each subject arms, matching the three
# source=REQUEST entries in OPENCODE_CAPABILITIES.execution_controls. Kept as
# its own map (rather than derived from _PERMISSION_KEY_FOR_SUBJECT) because
# it names ids the advertisement owns, not permission scalars.
_CONTROL_ID_FOR_SUBJECT = {
    FILESYSTEM_WRITE: "permission-edit-deny",
    SHELL_EXEC: "permission-bash-deny",
    SUBAGENT_SPAWN: "permission-task-request-deny",
}


def denied_permissions(disallowed_tools: str | None) -> dict[str, str]:
    """Map a neutral disallowed-tools list onto opencode permission scalars.

    ``disallowed_tools`` is claude-cli's spelling and the only tool-deny
    vocabulary BackendOptions carries, so it is what a harness-neutral caller
    sets. opencode has no deny LIST -- it has permission scalars, and its
    ``edit`` scalar gates write, edit and patch together -- so the list is
    TRANSLATED here rather than forwarded. The name-to-subject half of that
    translation is shared with every other consumer of the deny vocabulary
    (see ``subjects_for_disallowed_tools``); only the subject-to-scalar half is
    opencode's own.

    Measured 2026-09-05 on opencode 1.18.25: with edit and bash denied the agent
    reports its tools as read-only and creates no file, while the same prompt
    and model without them writes one. So an explicit deny survives ``--auto``,
    which auto-approves only what is not already denied.
    """
    return {
        _PERMISSION_KEY_FOR_SUBJECT[subject]: "deny"
        for subject in subjects_for_disallowed_tools(disallowed_tools)
        if subject in _PERMISSION_KEY_FOR_SUBJECT
    }


def _confined_opencode_env(
    base_env: Mapping[str, str] | None = None,
    disallowed_tools: str | None = None,
) -> dict[str, str]:
    """Return an OpenCode environment with a fail-closed workspace policy.

    Inline configuration has higher precedence than user and project config.
    Preserve unrelated inline settings, but replace the two security-sensitive
    permissions so ``--auto`` cannot approve an external path or delegate to an
    agent with a looser policy.
    """
    env = dict(os.environ if base_env is None else base_env)
    raw = env.get(_INLINE_CONFIG_ENV, "").strip()
    if raw:
        try:
            config = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise OpencodeRunError(
                f"{_INLINE_CONFIG_ENV} is not valid JSON; refusing unconfined "
                "OpenCode dispatch"
            ) from exc
        if not isinstance(config, dict):
            raise OpencodeRunError(
                f"{_INLINE_CONFIG_ENV} must contain a JSON object; refusing "
                "unconfined OpenCode dispatch"
            )
    else:
        config = {}

    permission = config.setdefault("permission", {})
    if not isinstance(permission, dict):
        permission = {}
        config["permission"] = permission
    permission["external_directory"] = "deny"
    permission["task"] = "deny"

    agents = config.setdefault("agent", {})
    if not isinstance(agents, dict):
        agents = {}
        config["agent"] = agents
    selected_agent = agents.setdefault(OPENCODE_AGENT, {})
    if not isinstance(selected_agent, dict):
        selected_agent = {}
        agents[OPENCODE_AGENT] = selected_agent
    agent_permission = selected_agent.setdefault("permission", {})
    if not isinstance(agent_permission, dict):
        agent_permission = {}
        selected_agent["permission"] = agent_permission
    agent_permission["external_directory"] = "deny"
    agent_permission["task"] = "deny"

    for key, value in denied_permissions(disallowed_tools).items():
        permission[key] = value
        agent_permission[key] = value

    env[_INLINE_CONFIG_ENV] = json.dumps(config, separators=(",", ":"))
    return env

__all__ = [
    "DEFAULT_OPENCODE_TIMEOUT_S",
    "OPENCODE_FILESYSTEM_POSTURE",
    "OPENCODE_PROMPT_SEPARATOR",
    "PROMPT_SEPARATOR",
    "OpencodeCliBackend",
    "OpencodeRunError",
    "denied_permissions",
    "compose_prompt",
]
