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
    FILESYSTEM_WRITE,
    SHELL_EXEC,
    SUBAGENT_SPAWN,
    ALLOW,
    APPEND,
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
    PARSED_RESULT,
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
        type="number",
        default=None,
        emits="temperature",
        note="server/model default when unset; omitted from the request",
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
    # A transport adapter exposes no tools, so there is no filesystem write to
    # deny and nothing that could turn one back on. The strongest form of the
    # guarantee and the cheapest: no flag, no sandbox, no checkout.
    guarantees=(FILESYSTEM_WRITE, SHELL_EXEC, SUBAGENT_SPAWN),
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

#: The claude-cli flag each ``system_prompt_mode`` emits, consumed by
#: ClaudeCliBackend to BUILD the argv and by the record below to ADVERTISE the
#: menu. One map, both jobs: the advertised ``values`` are its keys and the
#: emitted flag is its value, so a mode cannot be added to the adapter without
#: appearing in the advertisement. It lives on this side of the pair because
#: backends.py already imports this module -- the reverse edge would be a cycle.
#:
#: The two flags are not interchangeable spellings: ``--system-prompt`` makes
#: the caller's text the whole system prompt, while ``--append-system-prompt``
#: adds it to the CLI's own default one.
_CLAUDE_SYSTEM_PROMPT_FLAGS = {
    "replace": "--system-prompt",
    "append": "--append-system-prompt",
}

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
    "disallowed_tools": ParamCapability(
        type="string",
        emits="--disallowedTools",
        note=(
            "emitted ONLY when not None; unlike --allowedTools no empty value "
            "is sent, because an empty deny-list restricts nothing"
        ),
    ),
    "system_prompt_mode": ParamCapability(
        type="string",
        default="replace",
        values=tuple(sorted(_CLAUDE_SYSTEM_PROMPT_FLAGS)),
        emits="--system-prompt | --append-system-prompt",
        note=(
            "the menu is advertised because ClaudeCliBackend REJECTS an unknown "
            "mode before dispatch; the two flags differ in meaning, not just "
            "spelling (see system_prompt.emits_by_mode)"
        ),
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
            id="disallowed-tools",
            emits="--disallowedTools",
            effect=DENY,
            subjects=(FILESYSTEM_WRITE, SHELL_EXEC, SUBAGENT_SPAWN),
            source=REQUEST,
            parameter="disallowed_tools",
            note=(
                "the only real tool DENY channel across the four adapters. "
                "Emitted only when the caller sets the param, so an unset "
                "deny-list reports no control -- suppressing a flag is not a "
                "control. The record claims the EMISSION only: nothing here "
                "establishes that the CLI honors the deny, and the subjects are "
                "caller-supplied rather than a native identifier list, so none "
                "are enumerated. FILESYSTEM_WRITE is carried as a CANONICAL "
                "subject rather than a native one: it names the outcome a "
                "caller can require, and the caller arms it by passing the "
                "deny list"
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
        modes=(REPLACE, APPEND),
        parameter="system_prompt_mode",
        emits_by_mode={
            REPLACE: "--system-prompt",
            APPEND: "--append-system-prompt",
        },
        note=(
            "REPLACE is the default and makes the caller's text the WHOLE "
            "system prompt; APPEND emits --append-system-prompt, which adds it "
            "to the CLI's own default prompt. That difference is why append is "
            "a separate mode rather than a spelling of the same thing. Both "
            "claims are about the argv this adapter builds; neither asserts "
            "what the CLI then does with the text"
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
            subjects=(FILESYSTEM_WRITE,),
            source=REQUEST,
            parameter="extras.sandbox",
            note=(
                "always emitted, defaulting to workspace-write. The value is "
                "forwarded as given -- the adapter validates no mode menu, so "
                "modes beyond workspace-write and read-only reach the CLI. "
                "It carries FILESYSTEM_WRITE because read-only confines it -- "
                "but the DEFAULT does not, so a caller requiring that subject "
                "must pass extras.sandbox=read-only to arm it"
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
        result=PARSED_RESULT,
        note=(
            "--output-schema is a first-class CLI schema control, and the adapter "
            "parses the -o result file into the normalized structured field when "
            "-- and only when -- a caller schema was sent. Unparseable output "
            "leaves structured None; text still carries the result verbatim"
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
# subjects below are the exact native key paths written, except the canonical
# FILESYSTEM_WRITE carried by the caller-armed deny control: opencode has no
# deny list, so a neutral disallowed_tools value is TRANSLATED into the
# permission scalars that express it.

_OPENCODE_PARAMS = {
    "timeout_s": ParamCapability(
        type="number", default=120.0, emits="runner timeout_s"
    ),
    "cwd": ParamCapability(
        type="absolute-path",
        emits="--dir",
        note="also the process cwd; --dir is NOT a filesystem-confinement boundary",
    ),
    "disallowed_tools": ParamCapability(
        type="string",
        emits="OPENCODE_CONFIG_CONTENT permission.{edit,bash,task}=deny",
        note=(
            "read as a NEUTRAL tool-deny vocabulary and translated into "
            "opencode's permission scalars, which have no deny-list form. The "
            "edit scalar gates write, edit and patch together; unrecognized "
            "names are ignored rather than guessed at"
        ),
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
            id="permission-bash-deny",
            emits="OPENCODE_CONFIG_CONTENT permission.bash=deny",
            effect=DENY,
            subjects=(SHELL_EXEC,),
            source=REQUEST,
            parameter="disallowed_tools",
            note="armed by a deny list naming a shell tool",
        ),
        ExecutionControl(
            id="permission-task-request-deny",
            emits="OPENCODE_CONFIG_CONTENT permission.task=deny",
            effect=DENY,
            subjects=(SUBAGENT_SPAWN,),
            source=REQUEST,
            parameter="disallowed_tools",
            note=(
                "the fixed permission-task-deny below already denies task on "
                "every call; this records the same scalar as ALSO reachable "
                "through a caller's deny list"
            ),
        ),
        ExecutionControl(
            id="permission-edit-deny",
            emits="OPENCODE_CONFIG_CONTENT permission.edit=deny",
            effect=DENY,
            subjects=(FILESYSTEM_WRITE,),
            source=REQUEST,
            parameter="disallowed_tools",
            note=(
                "armed by a caller-supplied deny list naming any write tool. "
                "Verified 2026-09-05 on opencode 1.18.25 with a two-arm check: "
                "denied, the agent reports read-only tools and writes nothing; "
                "undenied, the same prompt and model create the file. So the "
                "deny survives --auto, which approves only what is not already "
                "denied"
            ),
        ),
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
    "_CLAUDE_SYSTEM_PROMPT_FLAGS",
]
