"""The filled capability record for each adapter family.

Every value below is derived from the code that builds the request, and every
``emits`` string is asserted by a seam test in
``tests/llm-scripting-kit/test_completion_capabilities.py``. When an adapter
changes what it emits, this file changes in the same commit -- that pairing is
the SSOT rule, and the seam tests are what enforce it rather than trust.

The records live beside the adapters rather than in a config file precisely
because a capability is a fact about code. A YAML copy would be a second source
of truth that can disagree with the adapter, which is the drift this replaces.
"""
from __future__ import annotations

from dataclasses import fields

from .capabilities import (
    ALLOW,
    BYPASS,
    Capabilities,
    CONFINE,
    DENY,
    DISABLE,
    ExecutionControl,
    FIXED,
    NATIVE,
    NATIVE_ROLE,
    NONE,
    PASSTHROUGH,
    PROMPT_FOLD,
    ParamCapability,
    REPLACE,
    REQUEST,
    StructuredOutputCapability,
    SystemPromptCapability,
    TEXT_RESULT,
    WINDOWS,
)
from .types import BackendOptions

# Every field on BackendOptions, READ FROM THE DATACLASS rather than restated.
# An adapter's dropped_params is this set minus the params it reads, so a field
# added to BackendOptions is immediately dropped-by-default everywhere instead of
# going unadvertised. A hand-copied list here would be a second source of truth
# free to fall behind the dataclass -- exactly the drift this module exists to
# remove -- and it would fail silently, because a forgotten field simply never
# appears in any record.
_ALL_OPTION_FIELDS = tuple(f.name for f in fields(BackendOptions))


def _dropped(honored: object) -> tuple:
    """BackendOptions fields this adapter does not read, in declaration order."""
    return tuple(name for name in _ALL_OPTION_FIELDS if name not in honored)


# -- openrouter (OpenAI-compatible HTTP) -----------------------------------
#
# OpenRouterBackend.complete builds chat-completions kwargs directly. It reads
# temperature, max_tokens, timeout_s, user_cache_prefix and extras, and nothing
# else -- notably NOT cwd, effort or allowed_tools, which is why cwd is not a
# core param of this seam.

_OPENROUTER_PARAMS = {
    "max_tokens": ParamCapability(
        type="integer", default=4096, emits="max_tokens"
    ),
    "temperature": ParamCapability(
        type="number", default=0.3, emits="temperature"
    ),
    "timeout_s": ParamCapability(
        type="number",
        emits="timeout",
        note="omitted from the request entirely when None",
    ),
    "user_cache_prefix": ParamCapability(
        type="string",
        default="",
        emits="messages[user].content[0].cache_control",
        note=(
            "when set, the user message becomes a two-part content list with an "
            "ephemeral cache breakpoint on the static prefix"
        ),
    ),
    "extras": ParamCapability(
        type="json-object",
        handling=PASSTHROUGH,
        emits="extra_body",
        note=(
            "every key rides as a TOP-LEVEL request parameter, unvalidated and "
            "unfiltered; the adapter makes no claim the provider accepts any of "
            "them"
        ),
    ),
}

OPENROUTER_CAPABILITIES = Capabilities(
    adapter="openrouter",
    params=_OPENROUTER_PARAMS,
    dropped_params=_dropped(_OPENROUTER_PARAMS),
    execution_controls=(),
    structured_output=StructuredOutputCapability(
        mode=PASSTHROUGH,
        request_param="extras.response_format",
        result=TEXT_RESULT,
        note=(
            "the adapter neither defines nor validates a schema; it forwards a "
            "caller-supplied response_format through extras and always reads the "
            "result as message.content"
        ),
    ),
    system_prompt=SystemPromptCapability(
        mode=NATIVE_ROLE,
        emits="messages[system]",
        note=(
            "a distinct system-role message, not an append: the adapter supplies "
            "the system prompt rather than adding to an existing one"
        ),
    ),
)

# -- claude-cli ------------------------------------------------------------
#
# ClaudeCliBackend.complete builds argv directly. --effort is conditional; the
# other flags are unconditional. No caller-schema flag is emitted: the
# --output-format json flag selects claude's TRANSPORT envelope, which the
# adapter parses to reach data["result"], and is not a structured-output channel.

_CLAUDE_PARAMS = {
    "timeout_s": ParamCapability(
        type="number", default=900.0, emits="runner timeout_s"
    ),
    "cwd": ParamCapability(
        type="path", emits="subprocess cwd", note="defaults to the process cwd"
    ),
    "effort": ParamCapability(
        type="string",
        emits="--effort",
        note="emitted only when not None; the adapter validates no value menu",
    ),
    "allowed_tools": ParamCapability(
        type="string",
        default="",
        emits="--allowedTools",
        note="None becomes the empty string, i.e. a pure completion with no tools",
    ),
    "log_prefix": ParamCapability(
        type="string", default="[llm]", emits="runner log_prefix"
    ),
}

CLAUDE_CAPABILITIES = Capabilities(
    adapter="claude-cli",
    params=_CLAUDE_PARAMS,
    dropped_params=_dropped(_CLAUDE_PARAMS),
    execution_controls=(
        ExecutionControl(
            id="allowed-tools",
            emits="--allowedTools",
            effect=ALLOW,
            source=REQUEST,
            parameter="allowed_tools",
            note=(
                "an ALLOW-list of caller-supplied tool names, not a deny-list: it "
                "cannot express denial of an arbitrary tool without a complete "
                "tool universe. The record makes no claim about how it composes "
                "with the permission bypass below"
            ),
        ),
        ExecutionControl(
            id="permission-bypass",
            emits="--permission-mode bypassPermissions",
            effect=BYPASS,
            source=FIXED,
        ),
        ExecutionControl(
            id="no-session-persistence",
            emits="--no-session-persistence",
            effect=DISABLE,
            subjects=("session-persistence",),
            source=FIXED,
        ),
    ),
    structured_output=StructuredOutputCapability(
        mode=NONE,
        result=TEXT_RESULT,
        note=(
            "--output-format json is claude's transport envelope, which the "
            "adapter parses to reach data['result']; it is not a caller schema"
        ),
    ),
    system_prompt=SystemPromptCapability(
        mode=REPLACE,
        emits="--system-prompt",
        note=(
            "REPLACES the system prompt. The installed CLI exposes "
            "--append-system-prompt, but this adapter emits it nowhere and no "
            "test backs an append, so append is not advertised"
        ),
    ),
)

# -- codex-cli -------------------------------------------------------------
#
# CodexCliBackend delegates argv construction to bootstrap_lib.codex, in a
# DIFFERENT plugin. Two consequences the records must respect: the effort menu
# validated on CodexAdapter is bypassed on this path, and network=False emits
# nothing at all rather than a deny.

_CODEX_PARAMS = {
    "timeout_s": ParamCapability(
        type="number", default=900.0, emits="runner timeout_s"
    ),
    "cwd": ParamCapability(
        type="absolute-path",
        emits="-C",
        note="resolved absolute when None; a relative path is rejected before dispatch",
    ),
    "effort": ParamCapability(
        type="string",
        emits="-c model_reasoning_effort=<value>",
        note=(
            "any truthy string is emitted. The [low, medium, high, xhigh, max] "
            "menu is validated on CodexAdapter, which this backend BYPASSES by "
            "calling the shared argv builder directly, so no menu is advertised"
        ),
    ),
    "log_prefix": ParamCapability(
        type="string", default="[llm]", emits="runner log_prefix"
    ),
    "extras.scratch_dir": ParamCapability(
        type="absolute-path", emits="--add-dir"
    ),
    "extras.add_dirs": ParamCapability(
        # a tuple, not a list: these records are frozen and shared process-wide,
        # so a mutable default would be a shared object a caller could edit
        type="absolute-path-list", default=(), emits="--add-dir (repeated)"
    ),
    "extras.sandbox": ParamCapability(
        type="string", default="workspace-write", emits="-s"
    ),
    "extras.network": ParamCapability(
        type="boolean",
        default=True,
        emits="-c sandbox_workspace_write.network_access=true",
        note="emitted ONLY when true; false emits nothing",
    ),
    "extras.output_schema": ParamCapability(
        type="absolute-path", emits="--output-schema"
    ),
}

CODEX_CAPABILITIES = Capabilities(
    adapter="codex-cli",
    # extras IS read, but only for the keys above; every other extras key is
    # dropped, which the note records rather than the coarse field name.
    params=_CODEX_PARAMS,
    dropped_params=_dropped(set(_CODEX_PARAMS) | {"extras"}),
    execution_controls=(
        ExecutionControl(
            id="sandbox-mode",
            emits="-s <value>",
            effect=CONFINE,
            source=REQUEST,
            parameter="extras.sandbox",
            note=(
                "always emitted, defaulting to workspace-write. The value is "
                "forwarded as given -- the adapter validates no mode menu, so "
                "modes beyond workspace-write and read-only reach the CLI"
            ),
        ),
        ExecutionControl(
            id="network-enable",
            emits="-c sandbox_workspace_write.network_access=true",
            effect=ALLOW,
            subjects=("network-egress",),
            source=REQUEST,
            parameter="extras.network",
            when_value="true",
            note=(
                "there is deliberately no network-disable control: false emits "
                "NOTHING, and the absence of a flag is not a control"
            ),
        ),
        ExecutionControl(
            id="windows-sandbox-mode",
            emits='-c windows.sandbox="unelevated"',
            effect=CONFINE,
            source=FIXED,
            platform=WINDOWS,
            note=(
                "a compatibility selector without which workspace-write silently "
                "degrades to read-only on Windows; not itself a filesystem "
                "confinement control"
            ),
        ),
        ExecutionControl(
            id="skip-git-repo-check",
            emits="--skip-git-repo-check",
            effect=DISABLE,
            subjects=("git-repo-check",),
            source=FIXED,
        ),
    ),
    structured_output=StructuredOutputCapability(
        mode=NATIVE,
        request_param="extras.output_schema",
        result=TEXT_RESULT,
        note=(
            "--output-schema is a first-class CLI schema control, but the adapter "
            "reads the -o result file as raw text; nothing parses it, so the "
            "normalized response carries no structured field yet"
        ),
    ),
    system_prompt=SystemPromptCapability(
        mode=PROMPT_FOLD,
        separator="\n\n---\n\n",
        emits="stdin",
        note="system text is concatenated ahead of the user text into one prompt",
    ),
)

# -- opencode-cli ----------------------------------------------------------
#
# OpencodeCliBackend injects policy as SCALAR settings under opencode's
# permission namespace via OPENCODE_CONFIG_CONTENT -- not deny lists. The
# subjects below are the exact native key paths written.

_OPENCODE_PARAMS = {
    "timeout_s": ParamCapability(
        type="number", default=120.0, emits="runner timeout_s"
    ),
    "cwd": ParamCapability(
        type="absolute-path",
        emits="--dir",
        note="also the process cwd; --dir is NOT a filesystem-confinement boundary",
    ),
    "effort": ParamCapability(
        type="string",
        emits="--variant",
        note="any nonempty provider variant; the adapter validates no menu",
    ),
    "log_prefix": ParamCapability(
        type="string", default="[llm]", emits="runner log_prefix"
    ),
}

OPENCODE_CAPABILITIES = Capabilities(
    adapter="opencode-cli",
    params=_OPENCODE_PARAMS,
    dropped_params=_dropped(_OPENCODE_PARAMS),
    execution_controls=(
        ExecutionControl(
            id="permission-external-directory-deny",
            emits="OPENCODE_CONFIG_CONTENT permission.external_directory=deny",
            effect=DENY,
            subjects=("permission.external_directory",),
            source=FIXED,
        ),
        ExecutionControl(
            id="permission-task-deny",
            emits="OPENCODE_CONFIG_CONTENT permission.task=deny",
            effect=DENY,
            subjects=("permission.task",),
            source=FIXED,
            note=(
                "task lives in opencode's PERMISSION namespace; this is not a "
                "tool allow/deny list"
            ),
        ),
        ExecutionControl(
            id="agent-permission-external-directory-deny",
            emits="OPENCODE_CONFIG_CONTENT agent.build.permission.external_directory=deny",
            effect=DENY,
            subjects=("agent.build.permission.external_directory",),
            source=FIXED,
        ),
        ExecutionControl(
            id="agent-permission-task-deny",
            emits="OPENCODE_CONFIG_CONTENT agent.build.permission.task=deny",
            effect=DENY,
            subjects=("agent.build.permission.task",),
            source=FIXED,
        ),
        ExecutionControl(
            id="pure-mode",
            emits="--pure",
            effect=DISABLE,
            subjects=("external-plugins",),
            source=FIXED,
        ),
        ExecutionControl(
            id="auto-approve",
            emits="--auto",
            effect=BYPASS,
            source=FIXED,
            note=(
                "auto-approves permissions not explicitly denied above; shell "
                "remains available"
            ),
        ),
        ExecutionControl(
            id="agent-selection",
            emits="--agent build",
            effect=CONFINE,
            subjects=("agent.build",),
            source=FIXED,
            note="the agent is fixed, not caller-selectable",
        ),
    ),
    structured_output=StructuredOutputCapability(
        mode=NONE,
        result=TEXT_RESULT,
        note="--format json is deliberately unused; stdout is read as the answer",
    ),
    system_prompt=SystemPromptCapability(
        mode=PROMPT_FOLD,
        separator="\n\n---\n\n",
        emits="stdin",
    ),
)


ADAPTER_CAPABILITIES = {
    OPENROUTER_CAPABILITIES.adapter: OPENROUTER_CAPABILITIES,
    CLAUDE_CAPABILITIES.adapter: CLAUDE_CAPABILITIES,
    CODEX_CAPABILITIES.adapter: CODEX_CAPABILITIES,
    OPENCODE_CAPABILITIES.adapter: OPENCODE_CAPABILITIES,
}


def adapter_capabilities() -> dict:
    """Every adapter family's advertisement, keyed by the backend's own name."""
    return dict(ADAPTER_CAPABILITIES)


__all__ = [
    "ADAPTER_CAPABILITIES",
    "adapter_capabilities",
    "OPENROUTER_CAPABILITIES",
    "CLAUDE_CAPABILITIES",
    "CODEX_CAPABILITIES",
    "OPENCODE_CAPABILITIES",
]
