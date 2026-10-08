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
    TEXT_ONLY_MODE,
    TEXT_ONLY_PARAMETER,
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
    SkillContextCapability,
    SystemPromptCapability,
    PARSED_RESULT,
    TEXT_RESULT,
    WINDOWS,
)
from .contract_types import (
    DELIVERY_NATIVE,
    DELIVERY_PROMPT,
    POLICY_NATIVE_REQUIRED,
    POLICY_TEXT_ONLY,
    POLICY_VALIDATED_RESULT,
    SCHEMA_CLASS_OPENAI_STRICT,
    SCHEMA_CLASS_SUBSET,
)
from .skill_context_types import DELIVERY_SYSTEM_MESSAGE
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


def _output_contract_param(emits: "str | None" = None) -> ParamCapability:
    """``output_contract`` as every adapter handles it: READ, then refused
    before dispatch unless the record lists the contract's policy.

    A refused param is still a read one, so it belongs in ``params`` and not in
    ``dropped_params`` -- "dropped" means "not read", and reporting a contract
    as dropped would tell a caller it went nowhere when in fact it stopped the
    call. Which policies an adapter satisfies is its
    ``structured_output.policies``. An adapter that DELIVERS a schema contract
    passes ``emits``, the same string as its ``structured_output.contract_emits``.
    """
    if emits is None:
        return ParamCapability(
            type="output-contract",
            note=(
                "read and refused before dispatch unless structured_output.policies "
                "lists the contract's policy"
            ),
        )
    return ParamCapability(
        type="output-contract",
        emits=emits,
        note=(
            "a listed schema policy is delivered as emitted and the answer is "
            "validated at the seam; text-only emits nothing; a policy "
            "structured_output.policies does not list is refused before dispatch"
        ),
    )


#: ``params.skill_context.emits`` and ``skill_context.emits`` of the one
#: adapter that delivers skill context: the block leads the system message,
#: before the caller's system text and any schema instruction. Asserted
#: against the captured request by
#: tests/llm-scripting-kit/test_completion_skill_context_adapters.py.
_OPENROUTER_SKILL_CONTEXT_EMITS = "messages[system] leading skill context block"


def _skill_context_param(emits: "str | None" = None) -> ParamCapability:
    """``skill_context`` as every adapter handles it: READ, and either
    delivered as ``emits`` or refused before dispatch.

    Refused is still read, for the reason :func:`_output_contract_param`
    gives: reporting the param as dropped would say it went nowhere when it
    stopped the call.
    """
    if emits is None:
        return ParamCapability(
            type="skill-context",
            note="read and refused before dispatch: the harness loads skills itself",
        )
    return ParamCapability(
        type="skill-context",
        emits=emits,
        note=(
            "the materialized block leads the system message, followed by the "
            "caller's system text and then any output-contract instruction"
        ),
    )


#: ``structured_output.contract_emits`` (and ``params.output_contract.emits``)
#: of the two adapters that deliver a contract in this module. Each names the
#: concrete element the delivery produces, asserted byte for byte by
#: tests/llm-scripting-kit/test_completion_contract_conformance.py.
_OPENROUTER_CONTRACT_EMITS = "messages[system] schema instruction"
_CODEX_CONTRACT_EMITS = "--output-schema <temp schema file>"
_CLAUDE_CONTRACT_EMITS = "--system-prompt schema instruction"
_OPENCODE_CONTRACT_EMITS = "stdin schema instruction"


# -- openrouter (OpenAI-compatible HTTP) -----------------------------------
#
# OpenRouterBackend.complete builds chat-completions kwargs directly. It reads
# temperature, max_tokens, timeout_s, user_cache_prefix, client_id and extras
# unconditionally, and effort only for an endpoint whose effort style delivers
# it (the conditional params below) -- never cwd or allowed_tools, which is
# why cwd is not a core param of this seam.

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
    "max_retries": ParamCapability(
        type="integer",
        default=None,
        emits="client.with_options(max_retries=...)",
        note="SDK retries per call; unset keeps the client default of 2",
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
    "client_id": ParamCapability(
        type="string",
        default=None,
        emits="user",
        note="uses the caller id, a process identity when None, nothing when empty",
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
    "output_contract": _output_contract_param(_OPENROUTER_CONTRACT_EMITS),
    "skill_context": _skill_context_param(_OPENROUTER_SKILL_CONTEXT_EMITS),
}

#: Params openrouter emits only for an endpoint whose profile enables them.
#: ``effort`` stays in the family record's dropped_params -- the truth for an
#: endpoint nothing is known about -- and
#: ``endpoint_profile.endpoint_capabilities`` moves it into ``params`` with the
#: concrete emission once an endpoint resolves a delivering effort style.
_OPENROUTER_CONDITIONAL_PARAMS = {
    "effort": ParamCapability(
        type="string",
        emits="reasoning_effort | chat_template_kwargs.reasoning_effort",
        note=(
            "emitted only for a transport entry that resolves a delivering "
            "effort_style (entry effort_style, frontdoor: true, or a declared "
            "routing.effort_style); an effort already in extras (top-level or "
            "chat_template_kwargs) wins verbatim and an explicit null there "
            "sends none; the ninfer style maps high to xhigh"
        ),
    ),
}

OPENROUTER_CAPABILITIES = Capabilities(
    adapter="openrouter",
    params=_OPENROUTER_PARAMS,
    dropped_params=_dropped(_OPENROUTER_PARAMS),
    conditional_params=_OPENROUTER_CONDITIONAL_PARAMS,
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
        # The output-contract path is separate from the legacy passthrough
        # above: the schema is appended to the system message as the exact
        # render_schema_instruction text and the answer is validated at the
        # seam. No response_format is sent, so native-required is not listed.
        policies=(POLICY_VALIDATED_RESULT, POLICY_TEXT_ONLY),
        contract_delivery=DELIVERY_PROMPT,
        contract_emits=_OPENROUTER_CONTRACT_EMITS,
        # The instruction carries any schema in the supported subset.
        contract_schema_class=SCHEMA_CLASS_SUBSET,
    ),
    system_prompt=SystemPromptCapability(
        mode=NATIVE_ROLE,
        emits="messages[system]",
        note=(
            "a distinct system-role message, not an append: the adapter supplies "
            "the system prompt rather than adding to an existing one"
        ),
    ),
    # The one adapter that DELIVERS skill context. The three harness records
    # carry no block, so each refuses it before dispatch.
    skill_context=SkillContextCapability(
        delivery=DELIVERY_SYSTEM_MESSAGE,
        emits=_OPENROUTER_SKILL_CONTEXT_EMITS,
    ),
)

# -- claude-cli ------------------------------------------------------------
#
# ClaudeCliBackend.complete builds argv directly. --effort and --disallowedTools
# are conditional, and text-only mode swaps --permission-mode bypassPermissions
# --allowedTools for _CLAUDE_TEXT_ONLY_ARGS. No caller-schema flag is emitted: the
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

#: The argv ClaudeCliBackend emits IN PLACE OF ``--permission-mode
#: bypassPermissions --allowedTools <x>`` when its ``text_only`` field is set.
#: One tuple, two jobs, like the map above: the backend extends argv with it
#: and the record below advertises it, so the two cannot drift.
#:
#: ``--tools ""`` removes every built-in tool (the Agent tool included);
#: ``--strict-mcp-config`` with an EMPTY ``--mcp-config`` loads no MCP server
#: from any settings source; ``--disable-slash-commands`` disables skills; and
#: ``--permission-mode dontAsk`` denies anything not pre-approved instead of
#: bypassing the check. Measured 2026-10-07 on Claude Code 2.1.293 (haiku):
#: asked to list every tool it could call and to run ``echo`` if it had a
#: shell, the model answered ``NO_TOOLS`` in one turn with no permission
#: denial.
_CLAUDE_TEXT_ONLY_ARGS = (
    "--tools",
    "",
    "--strict-mcp-config",
    "--mcp-config",
    '{"mcpServers":{}}',
    "--disable-slash-commands",
    "--permission-mode",
    "dontAsk",
)


def _render_args(args: tuple) -> str:
    """An argv fragment as one ``emits`` string; an empty value shows as ``""``."""
    return " ".join(arg if arg else '""' for arg in args)


_CLAUDE_TEXT_ONLY_EMITS = _render_args(_CLAUDE_TEXT_ONLY_ARGS)

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
    "output_contract": _output_contract_param(_CLAUDE_CONTRACT_EMITS),
    "skill_context": _skill_context_param(),
    TEXT_ONLY_PARAMETER: ParamCapability(
        type="boolean",
        default=False,
        emits=_CLAUDE_TEXT_ONLY_EMITS,
        note=(
            "a ClaudeCliBackend field, not a BackendOptions field: "
            "declaration.run sets it when a guarantees requirement admitted "
            "this entry (requirements.arm_requirements). When true it "
            "replaces --permission-mode bypassPermissions and --allowedTools, "
            "and a non-empty allowed_tools is refused before dispatch"
        ),
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
            source=REQUEST,
            parameter=TEXT_ONLY_PARAMETER,
            when_value="false",
            note="emitted on every call except in text-only mode",
        ),
        ExecutionControl(
            id="no-session-persistence",
            emits="--no-session-persistence",
            effect=DISABLE,
            subjects=("session-persistence",),
            source=FIXED,
        ),
        ExecutionControl(
            id=TEXT_ONLY_MODE,
            emits=_CLAUDE_TEXT_ONLY_EMITS,
            effect=DENY,
            subjects=(FILESYSTEM_WRITE, SHELL_EXEC, SUBAGENT_SPAWN),
            source=REQUEST,
            parameter=TEXT_ONLY_PARAMETER,
            when_value="true",
            note=(
                "no tools, no MCP servers, no skills, and no permission "
                "bypass. A guarantees requirement admits claude-cli through "
                "this control and declaration.run arms it, so the caller "
                "passes no flag. Measured 2026-10-07 on Claude Code 2.1.293: "
                "the model reported no callable tool and ran nothing. Neither "
                "allowed-tools nor permission-bypass is emitted in this mode"
            ),
        ),
    ),
    structured_output=StructuredOutputCapability(
        mode=NONE,
        result=TEXT_RESULT,
        note=(
            "--output-format json is claude's transport envelope, which the "
            "adapter parses to reach data['result']; it is not a caller schema"
        ),
        # The output-contract path is separate from the transport envelope:
        # the exact schema instruction is appended to the system prompt sent
        # through the system-prompt flag and the answer is validated at the
        # seam. No native schema flag exists here, so native-required is not
        # listed.
        policies=(POLICY_VALIDATED_RESULT, POLICY_TEXT_ONLY),
        contract_delivery=DELIVERY_PROMPT,
        contract_emits=_CLAUDE_CONTRACT_EMITS,
        contract_schema_class=SCHEMA_CLASS_SUBSET,
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

#: Codex text-only mode has three parts, because codex's tools come from three
#: places:
#:
#: 1. ``--ignore-user-config``: no ``config.toml``, so no user MCP server
#:    (``-c mcp_servers={}`` MERGES and does not remove them), plugin or app.
#: 2. ``_CODEX_TEXT_ONLY_CONFIG``, the ``-c`` pairs below, with ``-s read-only``
#:    and no network flag: they turn off the feature-gated tool families.
#: 3. A one-model ``model_catalog_json``: the MODEL CATALOG, not the config,
#:    gives a model code mode (``tool_mode: code_mode_only`` -> ``exec`` and
#:    ``wait``), multi-agent v2 (``multi_agent_version`` -> the
#:    ``collaboration.*`` tools, ``spawn_agent`` among them), ``apply_patch``
#:    (``apply_patch_tool_type``), ``tool_search`` and the node REPL. No ``-c``
#:    key or feature flag overrides those, so the backend reads the live
#:    catalog (``codex debug models``), applies ``_CODEX_TEXT_ONLY_CATALOG_SET``
#:    and ``_CODEX_TEXT_ONLY_CATALOG_DROP`` to the requested model's entry, and
#:    passes that entry alone.
#:
#: Measured 2026-10-07 on codex-cli 0.160.0 (gpt-6.1-sol, effort low), asking
#: the model to list every tool and run ``echo`` if it had a shell:
#: - parts 2 only: collaboration.* (spawn_agent ...), functions.exec,
#:   functions.wait, functions.request_user_input;
#: - parts 2+3 without dropping apply_patch: tool_search, three MCP resource
#:   tools, request_user_input, apply_patch;
#: - parts 2+3: the node_repl MCP tools (``mcp__node_repl.js``) from the user
#:   config, the MCP resource tools, request_user_input;
#: - parts 1+2+3 (this mode): ``functions.request_user_input`` only. It asks
#:   the user a question, belongs to no guarantee subject, and codex refuses it
#:   in ``exec`` mode. It is the one tool the mode does not remove.
_CODEX_TEXT_ONLY_CATALOG_SET = {
    "tool_mode": None,
    "multi_agent_version": None,
    "experimental_supported_tools": [],
    "supports_search_tool": False,
    "node_repl_disabled": True,
}
#: Keys removed from the catalog entry; ``null`` is not accepted for them.
_CODEX_TEXT_ONLY_CATALOG_DROP = ("apply_patch_tool_type",)

_CODEX_TEXT_ONLY_CONFIG = (
    "features.shell_tool=false",
    "features.unified_exec=false",
    "features.multi_agent=false",
    "features.multi_agent_v2=false",
    "features.apps=false",
    "features.plugins=false",
    "features.view_image=false",
    "features.image_generation=false",
    "features.browser_use=false",
    "features.browser_use_external=false",
    "features.computer_use=false",
    "features.in_app_browser=false",
    "features.code_mode_host=false",
    "features.sleep_tool=false",
    "features.tool_suggest=false",
    "features.skill_search=false",
    "features.goals=false",
    'web_search="disabled"',
    "mcp_servers={}",
)

_CODEX_TEXT_ONLY_EMITS = (
    "--ignore-user-config -s read-only "
    + " ".join(f"-c {item}" for item in _CODEX_TEXT_ONLY_CONFIG)
    + " -c model_catalog_json='<temp one-model catalog>'"
)

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
    "output_contract": _output_contract_param(_CODEX_CONTRACT_EMITS),
    "skill_context": _skill_context_param(),
    TEXT_ONLY_PARAMETER: ParamCapability(
        type="boolean",
        default=False,
        emits=_CODEX_TEXT_ONLY_EMITS,
        note=(
            "a CodexCliBackend field, not a BackendOptions field: "
            "declaration.run sets it when a guarantees requirement admitted "
            "this entry. Forces -s read-only and network off, ignores the "
            "user config, and runs one extra `codex debug models` call to "
            "build the patched catalog. extras.sandbox other than read-only, "
            "extras.network true, or no model id is refused before dispatch"
        ),
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
        ExecutionControl(
            id=TEXT_ONLY_MODE,
            emits=_CODEX_TEXT_ONLY_EMITS,
            effect=DENY,
            subjects=(FILESYSTEM_WRITE, SHELL_EXEC, SUBAGENT_SPAWN),
            source=REQUEST,
            parameter=TEXT_ONLY_PARAMETER,
            when_value="true",
            note=(
                "user config ignored, feature-gated tools off, and the "
                "model's catalog entry patched to drop code mode, multi-agent, "
                "apply_patch, tool_search and the node REPL. Measured "
                "2026-10-07 on codex-cli 0.160.0 (gpt-6.1-sol): the model "
                "listed only functions.request_user_input, which belongs to "
                "no guarantee subject and which codex refuses in exec mode"
            ),
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
        # The output-contract path: the canonical schema is written to a temp
        # file passed as --output-schema, and the answer is validated at the
        # seam. The CLI applies OpenAI strict-mode rules to that schema (a
        # schema without additionalProperties false and a full required list
        # on every object fails the run with a non-zero exit), so the record
        # accepts only openai-strict schemas: selection skips codex for any
        # other schema, and prepare_contract refuses one before dispatch.
        policies=(POLICY_NATIVE_REQUIRED, POLICY_VALIDATED_RESULT, POLICY_TEXT_ONLY),
        contract_delivery=DELIVERY_NATIVE,
        contract_emits=_CODEX_CONTRACT_EMITS,
        contract_schema_class=SCHEMA_CLASS_OPENAI_STRICT,
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
    "output_contract": _output_contract_param(_OPENCODE_CONTRACT_EMITS),
    "skill_context": _skill_context_param(),
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
        # The output-contract path: the exact schema instruction is appended
        # to the system half that is folded into the stdin prompt, and the
        # answer is validated at the seam. native-required is not listed.
        policies=(POLICY_VALIDATED_RESULT, POLICY_TEXT_ONLY),
        contract_delivery=DELIVERY_PROMPT,
        contract_emits=_OPENCODE_CONTRACT_EMITS,
        contract_schema_class=SCHEMA_CLASS_SUBSET,
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
    "_CLAUDE_TEXT_ONLY_ARGS",
    "_CODEX_TEXT_ONLY_CATALOG_DROP",
    "_CODEX_TEXT_ONLY_CATALOG_SET",
    "_CODEX_TEXT_ONLY_CONFIG",
]
