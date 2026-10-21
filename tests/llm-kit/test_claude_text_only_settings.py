"""claude-cli text-only mode loads no settings and no CLAUDE.md memory.

``--setting-sources ""`` keeps the user, project and local CLAUDE.md out of a
text-only call. Without it a call from inside a repo wrote ~30.6k cache tokens
(measured 2026-10-21) instead of 571, so prompt caching never paid off. The
frozen llm-scripting-kit argv lacks the flag, so this is asserted natively
rather than through the legacy parity suites (which normalise it away).
No real model runs: the backend is driven through its runner seam.
"""
from __future__ import annotations

import json

from llm_kit.adapters.backends import ClaudeCliBackend
from llm_kit.adapters.capabilities import (
    _CLAUDE_TEXT_ONLY_ARGS,
    CLAUDE_CAPABILITIES,
)
from llm_kit.completion.capabilities import TEXT_ONLY_MODE


class _Runner:
    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, cmd, request, cwd, **kwargs):
        self.calls.append(list(cmd))
        return json.dumps({"result": "ok", "is_error": False, "usage": {}}), "", 0


def _argv(text_only: bool) -> list:
    runner = _Runner()
    ClaudeCliBackend(runner=runner, executable="claude", text_only=text_only).complete(
        "sys", "usr", model="m"
    )
    return runner.calls[0]


def test_text_only_argv_passes_an_empty_setting_sources():
    argv = _argv(text_only=True)
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert argv.count("--setting-sources") == 1
    assert argv[-len(_CLAUDE_TEXT_ONLY_ARGS):] == list(_CLAUDE_TEXT_ONLY_ARGS)


def test_default_argv_keeps_its_settings():
    assert "--setting-sources" not in _argv(text_only=False)


def test_the_advertised_text_only_emits_names_the_flag():
    control = next(
        c for c in CLAUDE_CAPABILITIES.execution_controls if c.id == TEXT_ONLY_MODE
    )
    assert '--setting-sources ""' in control.emits


# -- native output contract (--json-schema) --------------------------------

import pytest  # noqa: E402

from llm_kit.completion.contract import OutputContractViolation  # noqa: E402
from llm_kit.completion.contract_types import (  # noqa: E402
    DELIVERY_NATIVE,
    POLICY_NATIVE_REQUIRED,
    POLICY_TEXT_ONLY,
    POLICY_VALIDATED_RESULT,
    OutputContract,
    canonical_schema_json,
)
from llm_kit.completion.types import BackendOptions  # noqa: E402

_SCHEMA = {
    "type": "object",
    "properties": {"a": {"type": "integer"}},
    "required": ["a"],
    "additionalProperties": False,
}


def _contract(policy: str = POLICY_VALIDATED_RESULT) -> OutputContract:
    schema = None if policy == POLICY_TEXT_ONLY else _SCHEMA
    return OutputContract(id="demo.contract", policy=policy, schema=schema)


class _EnvelopeRunner(_Runner):
    def __init__(self, **envelope) -> None:
        super().__init__()
        self.envelope = {"is_error": False, "usage": {}, **envelope}

    def __call__(self, cmd, request, cwd, **kwargs):
        self.calls.append(list(cmd))
        return json.dumps(self.envelope), "", 0


def _complete(runner, contract, *, text_only: bool = False):
    backend = ClaudeCliBackend(runner=runner, executable="claude", text_only=text_only)
    return backend.complete(
        "SYS", "usr", model="m", options=BackendOptions(output_contract=contract)
    )


def _system_text(argv: list) -> str:
    return argv[argv.index("--system-prompt") + 1]


@pytest.mark.parametrize("policy", [POLICY_VALIDATED_RESULT, POLICY_NATIVE_REQUIRED])
@pytest.mark.parametrize("text_only", [False, True])
def test_native_contract_passes_the_canonical_schema_and_reads_structured_output(
    policy, text_only
):
    contract = _contract(policy)
    # ``result`` is deliberately not the answer: structured_output is the channel.
    runner = _EnvelopeRunner(result="done", structured_output={"a": 1})
    response = _complete(runner, contract, text_only=text_only)
    argv = runner.calls[0]
    assert argv[argv.index("--json-schema") + 1] == canonical_schema_json(contract)
    assert _system_text(argv) == "SYS"  # no schema instruction is appended
    assert response.structured == {"a": 1}
    assert json.loads(response.text) == {"a": 1}
    assert response.output_contract.delivery == DELIVERY_NATIVE


def test_native_contract_still_validates_the_answer():
    runner = _EnvelopeRunner(result="x", structured_output={"a": "not an integer"})
    with pytest.raises(OutputContractViolation):
        _complete(runner, _contract())


def test_native_contract_without_structured_output_is_judged_from_result():
    ok = _EnvelopeRunner(result='{"a": 2}')
    assert _complete(ok, _contract()).structured == {"a": 2}
    bad = _EnvelopeRunner(result="no json here")
    with pytest.raises(OutputContractViolation):
        _complete(bad, _contract())


def test_text_only_policy_and_no_contract_pass_no_json_schema():
    runner = _EnvelopeRunner(result="txt")
    _complete(runner, _contract(POLICY_TEXT_ONLY))
    assert "--json-schema" not in runner.calls[0]
    plain = _EnvelopeRunner(result="txt", structured_output={"a": 1})
    ClaudeCliBackend(runner=plain, executable="claude").complete("SYS", "usr", model="m")
    assert "--json-schema" not in plain.calls[0]


def test_the_record_advertises_native_delivery():
    structured = CLAUDE_CAPABILITIES.structured_output
    assert structured.contract_delivery == DELIVERY_NATIVE
    assert POLICY_NATIVE_REQUIRED in structured.policies
    assert structured.contract_emits.startswith("--json-schema")
