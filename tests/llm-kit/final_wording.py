"""Named deliberate wording differences between the legacy plugins and llm-kit.

The final package speaks of llm-kit wherever the legacy text named a legacy
plugin as the thing to install, update, run or link, and names its two bare CLI
verbs inside their groups. Each difference is declared once here, by name, with
the legacy and final text and the two source files that carry them. A parity
test that compares legacy text with final text passes the legacy side through
:func:`to_final`; ``test_final_messages.py`` proves every rename against the
live sources, so a rename cannot stand in for an unrelated difference.

Not covered here: event names, envelope literals, the ``llm-scripting-kit``
config and data namespace and the frozen schema-subset name. Those are frozen
by the architecture and compared byte for byte.
"""

from __future__ import annotations

import dataclasses
import re
from typing import NamedTuple

_LSK = "plugins/llm-scripting-kit/lib/llm_scripting_kit/"
_JOB = "plugins/job-kit/lib/job_kit/"
_CPK = "plugins/content-pipeline-kit/lib/content_pipeline/"
_KIT = "packages/katsoft-llm-kit/src/llm_kit/"


class Rename(NamedTuple):
    """One named rename: runtime text on each side and where its source lives.

    ``legacy`` and ``final`` are the runtime texts. When the source spells a
    text differently (an assignment), ``legacy_src`` and ``final_src`` give the
    fragment to look for in the folded source instead.
    """

    name: str
    legacy_file: str
    legacy: str
    final_file: str
    final: str
    legacy_src: str = ""
    final_src: str = ""


_TEXT_ONLY_EMITS_LEGACY = (
    '--tools "" --strict-mcp-config --mcp-config {"mcpServers":{}} '
    "--disable-slash-commands --permission-mode dontAsk"
)
_TEXT_ONLY_EMITS_FINAL = (
    '--tools "" --strict-mcp-config --mcp-config {"mcpServers":{}} '
    '--disable-slash-commands --setting-sources "" --permission-mode dontAsk'
)
_TEXT_ONLY_ARGV_LEGACY_SRC = '"--disable-slash-commands",\n    "--permission-mode",'
_TEXT_ONLY_ARGV_FINAL_SRC = (
    '"--disable-slash-commands",\n    "--setting-sources",\n    "",\n    "--permission-mode",'
)

#: llm-kit's claude-cli delivers an output contract natively (--json-schema, read
#: from the envelope's structured_output); the frozen legacy record delivers it by
#: prompt. The advertised fields differ in three places; see claude_contract_to_final.
CLAUDE_CONTRACT_EMITS_LEGACY = "--system-prompt schema instruction"
CLAUDE_CONTRACT_EMITS_FINAL = "--json-schema <canonical schema json>"
CLAUDE_CONTRACT_POLICIES_FINAL = ("native-required", "validated-result", "text-only")
CLAUDE_CONTRACT_DELIVERY_FINAL = "native"


def claude_contract_to_final(value):
    """A claude-cli ``structured_output`` record, as a dataclass or a dict, in the final form.

    Recognised by its ``contract_emits`` (legacy or final) and idempotent, so a
    harness may pass both sides through it.
    """
    if isinstance(value, dict):
        if value.get("contract_emits") in (CLAUDE_CONTRACT_EMITS_LEGACY, CLAUDE_CONTRACT_EMITS_FINAL):
            return {
                **value,
                "policies": list(CLAUDE_CONTRACT_POLICIES_FINAL),
                "contract_delivery": CLAUDE_CONTRACT_DELIVERY_FINAL,
                "contract_emits": CLAUDE_CONTRACT_EMITS_FINAL,
            }
        return value
    if (
        dataclasses.is_dataclass(value)
        and not isinstance(value, type)
        and getattr(value, "contract_emits", None)
        in (CLAUDE_CONTRACT_EMITS_LEGACY, CLAUDE_CONTRACT_EMITS_FINAL)
    ):
        return dataclasses.replace(
            value,
            policies=CLAUDE_CONTRACT_POLICIES_FINAL,
            contract_delivery=CLAUDE_CONTRACT_DELIVERY_FINAL,
            contract_emits=CLAUDE_CONTRACT_EMITS_FINAL,
        )
    return value


def claude_contract_deep(value):
    """``value`` with every claude-cli ``structured_output`` dict in the final form."""
    if isinstance(value, dict):
        return claude_contract_to_final({k: claude_contract_deep(v) for k, v in value.items()})
    if isinstance(value, list):
        return [claude_contract_deep(v) for v in value]
    if isinstance(value, tuple):
        return tuple(claude_contract_deep(v) for v in value)
    return value


RENAMES: tuple[Rename, ...] = (
    Rename(
        "make_openai_client names the llm-kit CLI environment",
        _LSK + "client.py", "Use the bootstrap-provisioned llm-scripting-kit CLI environment",
        _KIT + "adapters/client.py", "Use the bootstrap-provisioned llm-kit CLI environment",
    ),
    Rename(
        "claudx refusal names llm-kit",
        _LSK + "completion/backends.py", "llm-scripting-kit cannot start a `claude -p` child under claudx",
        _KIT + "adapters/backends.py", "llm-kit cannot start a `claude -p` child under claudx",
    ),
    Rename(
        "psutil refusal names llm-kit's pyproject",
        _LSK + "_swapper_process.py", "it is declared in llm-scripting-kit's pyproject.toml",
        _KIT + "adapters/_swapper_process.py", "it is declared in llm-kit's pyproject.toml",
    ),
    Rename(
        "skill context no-pyyaml names the llm-kit command",
        _LSK + "completion/skill_context.py",
        "llm-scripting-kit's own environment has it, so run the call through the `llm-scripting-kit` command",
        _KIT + "completion/skill_context.py",
        "llm-kit's own environment has it, so run the call through the `llm-kit` command",
    ),
    Rename(
        "declaration refusal names llm-kit's describe and run (shared_lib_imports)",
        _LSK + "declaration.py", "shared_lib_imports (llm-scripting-kit's describe/run need ",
        _KIT + "registry/declaration_syntax.py", "shared_lib_imports (llm-kit's describe/run need ",
    ),
    Rename(
        "declaration refusal names llm-kit's describe and run (stale bootstrap_lib)",
        _LSK + "declaration.py", "llm-scripting-kit's describe/run need bootstrap >= ",
        _KIT + "registry/declaration_syntax.py", "llm-kit's describe/run need bootstrap >= ",
    ),
    Rename(
        "jobs: stale-ledger hint names the llm-kit jobs verb",
        _JOB + "store.py", "for example `job-kit resume <run-id>`",
        _KIT + "jobs/store.py", "for example `llm-kit jobs resume <run-id>`",
    ),
    Rename(
        "jobs: newer-ledger refusal names llm-kit (supports)",
        _JOB + "store.py", "job-kit supports (max ",
        _KIT + "jobs/store.py", "llm-kit supports (max ",
    ),
    Rename(
        "jobs: newer-ledger refusal names llm-kit (update)",
        _JOB + "store.py", "it -- update job-kit, or point at a database this version ",
        _KIT + "jobs/store.py", "it -- update llm-kit, or point at a database this version ",
    ),
    Rename(
        "jobs: contract refusal links llm-kit's shared libs",
        _JOB + "interrupts.py", "session so it links job-kit's shared libs.",
        _KIT + "jobs/interrupts.py", "session so it links llm-kit's shared libs.",
    ),
    Rename(
        "jobs: event refusal links llm-kit's shared libs",
        _JOB + "events.py", "new session so it links job-kit's shared libs.",
        _KIT + "jobs/events.py", "new session so it links llm-kit's shared libs.",
    ),
    Rename(
        "jobs: declaration refusal links llm-kit's shared libs",
        _JOB + "model.py", "session so it links job-kit's shared libs.",
        _KIT + "jobs/model.py", "session so it links llm-kit's shared libs.",
    ),
    Rename(
        "jobs: declaration refusal names llm-kit as the unprovisioned plugin",
        _JOB + "model.py", "has not provisioned job-kit here",
        _KIT + "jobs/model.py", "has not provisioned llm-kit here",
    ),
    Rename(
        "jobs: absent validator is llm-kit's own json_schema",
        _JOB + "interrupts.py", "job-kit validates interrupt requests with llm-scripting-kit, which is not linked into this environment: install or enable it ",
        _KIT + "jobs/interrupts.py",
        "llm-kit validates interrupt requests with llm_kit.schema.json_schema, which is not linked into this environment: install or enable llm-kit ",
    ),
    Rename(
        "jobs: absent validator install command",
        _JOB + "interrupts.py", "(`claude plugin install llm-scripting-kit@plugins-kit`)",
        _KIT + "jobs/interrupts.py", "(`claude plugin install llm-kit@plugins-kit`)",
    ),
    Rename(
        "jobs: absent validator links llm-kit's shared libs",
        _JOB + "interrupts.py", "new session so bootstrap links job-kit's shared libs.",
        _KIT + "jobs/interrupts.py", "new session so bootstrap links llm-kit's shared libs.",
    ),
    Rename(
        "jobs: stale validator names llm_kit.schema.json_schema",
        _JOB + "interrupts.py",
        "job-kit validates interrupt requests with llm_scripting_kit.completion.json_schema (check_schema",
        _KIT + "jobs/interrupts.py",
        "llm-kit validates interrupt requests with llm_kit.schema.json_schema (check_schema",
    ),
    Rename(
        "jobs: stale validator names the linked llm-kit",
        _JOB + "interrupts.py", "which the linked llm-scripting-kit predates or lacks: update llm-scripting-kit to >= ",
        _KIT + "jobs/interrupts.py", "which the linked llm-kit predates or lacks: update llm-kit to >= ",
    ),
    Rename(
        "jobs: stale validator update command",
        _JOB + "interrupts.py", "(`claude plugin update llm-scripting-kit@plugins-kit`) and restart.",
        _KIT + "jobs/interrupts.py", "(`claude plugin update llm-kit@plugins-kit`) and restart.",
    ),
    Rename(
        "pipeline interrupts: product named in a contract refusal",
        _CPK + "execution/interrupts.py", "content-pipeline-kit interrupts need bootstrap",
        _KIT + "pipeline/execution/interrupts.py", "llm-kit pipeline interrupts need bootstrap",
    ),
    Rename(
        "pipeline interrupts: validator named in an absent refusal",
        _CPK + "execution/interrupts.py",
        "content-pipeline-kit interrupts validate requests and answers with llm_scripting_kit, which this interpreter cannot import. ",
        _KIT + "pipeline/execution/interrupts.py",
        "llm-kit pipeline interrupts validate requests and answers with llm_kit.schema.json_schema, which this interpreter cannot import. ",
    ),
    Rename(
        "pipeline interrupts: validator owner in install and update commands",
        _CPK + "execution/interrupts.py", "llm-scripting-kit@plugins-kit",
        _KIT + "pipeline/execution/interrupts.py", "llm-kit@plugins-kit",
        '_VALIDATOR_OWNER = "llm-scripting-kit@plugins-kit"', '_VALIDATOR_OWNER = "llm-kit@plugins-kit"',
    ),
    Rename(
        "pipeline interrupts: stale validator names llm-kit",
        _CPK + "execution/interrupts.py", "content-pipeline-kit interrupts need llm-scripting-kit ",
        _KIT + "pipeline/execution/interrupts.py", "llm-kit pipeline interrupts need llm-kit ",
    ),
    Rename(
        "pipeline interrupts: broken validator import names llm_kit.schema.json_schema",
        _CPK + "execution/interrupts.py", "llm_scripting_kit failed to import (",
        _KIT + "pipeline/execution/interrupts.py", "llm_kit.schema.json_schema failed to import (",
    ),
    Rename(
        "backpressure refusal (absent) names the llm_kit shared lib",
        _CPK + "llm/platform.py", "the 'llm_scripting_kit' shared lib is absent, so an overload halt's ",
        _KIT + "pipeline/runtime/platform.py", "the 'llm_kit' shared lib is absent, so an overload halt's ",
    ),
    Rename(
        "backpressure refusal (absent) install command",
        _CPK + "llm/platform.py", "`claude plugin install llm-scripting-kit@plugins-kit`.",
        _KIT + "pipeline/runtime/platform.py", "`claude plugin install llm-kit@plugins-kit`.",
    ),
    Rename(
        "backpressure refusal (stale) names the linked llm_kit",
        _CPK + "llm/platform.py", "the linked llm_scripting_kit predates classify_backpressure, so an ",
        _KIT + "pipeline/runtime/platform.py", "the linked llm_kit predates classify_backpressure, so an ",
    ),
    Rename(
        "backpressure refusal (stale) update target",
        _CPK + "llm/platform.py", "llm-scripting-kit >= ",
        _KIT + "pipeline/runtime/platform.py", "llm-kit >= ",
    ),
    Rename(
        "backpressure refusal (stale) update command",
        _CPK + "llm/platform.py", "`claude plugin update llm-scripting-kit@plugins-kit`.",
        _KIT + "pipeline/runtime/platform.py", "`claude plugin update llm-kit@plugins-kit`.",
    ),
    # CLI verbs that now live in groups. The runtime text is spelled after the
    # CLI command token, which differs by test (a path, ``<CLI>``, ``<exe>``),
    # so each is anchored on what follows the token; the source spelling names
    # the token's call.
    Rename(
        "missing-key hint names the account group (default endpoint)",
        _LSK + "client.py", " set-key`.",
        _KIT + "adapters/client.py", " account set-key`.",
        "{cli_command()} set-key", "{cli_command()} account set-key",
    ),
    Rename(
        "missing-key hint names the account group (named endpoint)",
        _LSK + "client.py", " set-key --endpoint ",
        _KIT + "adapters/client.py", " account set-key --endpoint ",
        "{cli_command()} set-key", "{cli_command()} account set-key",
    ),
    Rename(
        "halt write-back names the models group",
        _LSK + "declaration.py", " record-halt <entry>`",
        _KIT + "dispatch/declaration.py", " models record-halt <entry>`",
        "{cli_command()} record-halt <entry>", "{cli_command()} models record-halt <entry>",
    ),
    # llm-kit's claude-cli text-only mode also passes --setting-sources "" (no
    # settings or CLAUDE.md memory, which defeated prompt caching; 0.3.2). The
    # whole emitted string is the legacy text so that the plain form and the
    # JSON-escaped form (a record dumped as JSON) each get their own quoting.
    Rename(
        "claude text-only argv passes an empty --setting-sources",
        _LSK + "completion/adapter_capabilities.py",
        _TEXT_ONLY_EMITS_LEGACY,
        _KIT + "adapters/capabilities.py",
        _TEXT_ONLY_EMITS_FINAL,
        _TEXT_ONLY_ARGV_LEGACY_SRC,
        _TEXT_ONLY_ARGV_FINAL_SRC,
    ),
    Rename(
        "claude text-only argv passes an empty --setting-sources (JSON-escaped)",
        _LSK + "completion/adapter_capabilities.py",
        _TEXT_ONLY_EMITS_LEGACY.replace('"', '\\"'),
        _KIT + "adapters/capabilities.py",
        _TEXT_ONLY_EMITS_FINAL.replace('"', '\\"'),
        _TEXT_ONLY_ARGV_LEGACY_SRC,
        _TEXT_ONLY_ARGV_FINAL_SRC,
    ),
    Rename(
        "claude output contract is delivered by --json-schema",
        _LSK + "completion/adapter_capabilities.py", CLAUDE_CONTRACT_EMITS_LEGACY,
        _KIT + "adapters/capabilities.py", CLAUDE_CONTRACT_EMITS_FINAL,
    ),
    Rename(
        "claude text-only note names the settings and memory it drops",
        _LSK + "completion/adapter_capabilities.py", "no skills, and no permission bypass",
        _KIT + "adapters/capabilities.py", "no skills, no settings or CLAUDE.md memory, and no permission bypass",
    ),
)

#: Version floors named in messages: the first release of the plugin that ships
#: the module. The legacy value is an llm-scripting-kit release, the final value
#: the first llm-kit release carrying the module (git history of that module).
FLOORS: tuple[tuple[str, str, str], ...] = (
    ("json_schema subset selector", "0.56.0", "0.1.2"),
    ("classify_backpressure", "0.61.0", "0.1.3"),
)


def flat(text: str) -> str:
    """Source text with adjacent string literals folded, for fragment search."""
    return re.sub(r'"\s*\n\s*f?"', "", text)


def to_final(text: str) -> str:
    """The legacy wording rewritten to the final wording (every named rename).

    Idempotent: text already in the final wording is left alone, so a test may
    pass both sides through it. A final text can contain its legacy text (a verb
    gaining its group), so each rename protects final occurrences first.
    """
    for index, rename in enumerate(RENAMES):
        token = f"{index}"
        text = text.replace(rename.final, token).replace(rename.legacy, token).replace(token, rename.final)
    return text


def floor_to_final(value: str) -> str:
    for _name, legacy, final in FLOORS:
        if value == legacy:
            return final
    return value


def floors_in_text_to_final(text: str) -> str:
    """A message's version floors (``>= X`` and ``X or newer``) in the final value."""
    for _name, legacy, final in FLOORS:
        text = text.replace(f">= {legacy}", f">= {final}").replace(f" {legacy} or newer", f" {final} or newer")
    return text
