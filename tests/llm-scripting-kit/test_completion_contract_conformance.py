"""Output-contract conformance, per adapter and per advertised policy.

The cases are GENERATED per adapter in ``ADAPTER_CAPABILITIES`` from
``EXPECTED_POLICIES``, which the records must match exactly: an adapter that
lists a policy gets the valid / mismatch / unparseable cases for it, a
delivery-exact case for its ``contract_delivery``, and every policy it does
NOT list gets a zero-invocation refusal case that also requires the record
not to advertise it. The table is independent of the records on purpose: a
policy wrongly added to a record turns its fixed refusal case red rather than
replacing that case with ones that assume support.

Delivery is asserted EXACTLY, never as "the channel is present" (finding F3):
openrouter always builds a system message, so a presence check would stay
green with the schema instruction removed. The exact forms are:

- prompt delivery (openrouter): the system message text ends with
  ``render_schema_instruction(contract)``, i.e. equals ``system`` plus it;
- native delivery (codex-cli): the ``--output-schema`` file, read by the fake
  runner DURING the call (the adapter deletes it afterwards), holds exactly
  ``canonical_schema_json(contract).encode("ascii")``, and the prompt carries
  no instruction.

Without a contract (and under ``text-only``, which delivers nothing) each
recorded request equals an independently built baseline and contains no
``SCHEMA_INSTRUCTION_PREFIX``.

A prompt-delivering adapter without an exact assertion below FAILS its
``prompt-exact`` case rather than passing generically.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List, Optional

import pytest

from llm_scripting_kit.completion import codex_backend as codex_mod
from llm_scripting_kit.completion.adapter_capabilities import ADAPTER_CAPABILITIES
from llm_scripting_kit.completion.backends import ClaudeCliBackend, OpenRouterBackend
from llm_scripting_kit.completion.codex_backend import (
    CodexCliBackend,
    CodexRunError,
    compose_prompt as codex_compose_prompt,
)
from llm_scripting_kit.completion.contract import (
    DELIVERY_NATIVE,
    DELIVERY_NONE,
    DELIVERY_PROMPT,
    DISPOSITION_SCHEMA_MISMATCH,
    DISPOSITION_TEXT_ONLY,
    DISPOSITION_UNPARSEABLE,
    DISPOSITION_VALID,
    POLICIES,
    POLICY_TEXT_ONLY,
    SCHEMA_INSTRUCTION_PREFIX,
    SCHEMA_POLICIES,
    VIOLATION_ERROR_CODE,
    OutputContract,
    OutputContractUnsatisfiable,
    OutputContractViolation,
    canonical_schema_json,
    render_schema_instruction,
)
from llm_scripting_kit.completion.opencode_backend import (
    OpencodeCliBackend,
    compose_prompt as opencode_compose_prompt,
)
from llm_scripting_kit.completion.types import COMPLETED, ERROR, BackendOptions

SYSTEM = "sys"
USER = "usr"

#: Strict-mode compatible (every object closed, every property required):
#: codex-cli's --output-schema rejects anything else before generation.
SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string", "minLength": 1}},
    "required": ["answer"],
    "additionalProperties": False,
}
VALID_ANSWER = '{"answer":"blue"}'
MISMATCH_ANSWER = '{"answer":""}'
UNPARSEABLE_ANSWER = "The answer is blue."


def _contract(policy: str) -> OutputContract:
    if policy == POLICY_TEXT_ONLY:
        return OutputContract("t.conformance", policy)
    return OutputContract("t.conformance", policy, SCHEMA)


# -- recording drivers ---------------------------------------------------------


@dataclass
class Recording:
    """What one adapter call put on its seam."""

    calls: int = 0
    argv: Optional[List[str]] = None
    stdin: Optional[str] = None
    kwargs: Optional[dict] = None
    schema_bytes: Optional[bytes] = None
    schema_path: Optional[str] = None
    temp_files: List[str] = field(default_factory=list)

    def comparable(self) -> Any:
        """The request with per-call temp paths replaced by fixed tokens."""
        if self.kwargs is not None:
            return self.kwargs
        argv = list(self.argv or [])
        for flag, token in (("-o", "<result-file>"), ("--output-schema", "<schema-file>")):
            if flag in argv:
                argv[argv.index(flag) + 1] = token
        return (argv, self.stdin)

    def text(self) -> str:
        """Every byte of request text, for the no-instruction assertion."""
        if self.kwargs is not None:
            return json.dumps(self.kwargs, default=str)
        return json.dumps([self.argv, self.stdin])


class _FakeMessage:
    def __init__(self, content):
        self.content = content
        self.reasoning_content = None


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)
        self.finish_reason = "stop"


class _FakeResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]
        self.usage = None


def _drive_openrouter(tmp_path: Path, options: BackendOptions, answer: str, rec: Recording):
    class _Completions:
        def create(self, **kwargs):
            rec.calls += 1
            rec.kwargs = kwargs
            return _FakeResponse(answer)

    class _Client:
        class chat:  # noqa: N801 -- mirrors the SDK attribute
            completions = _Completions()

    backend = OpenRouterBackend(client=_Client())
    return backend.complete(SYSTEM, USER, model="test/slug", options=options)


def _drive_codex(tmp_path: Path, options: BackendOptions, answer: str, rec: Recording):
    def runner(cmd, request, cwd, **kwargs):
        rec.calls += 1
        rec.argv = [str(a) for a in cmd]
        rec.stdin = request
        if "--output-schema" in rec.argv:
            # Read NOW: the adapter deletes the file once the call returns.
            rec.schema_path = rec.argv[rec.argv.index("--output-schema") + 1]
            rec.schema_bytes = Path(rec.schema_path).read_bytes()
        Path(rec.argv[rec.argv.index("-o") + 1]).write_text(answer, encoding="utf-8")
        return "", "", 0

    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    opts = BackendOptions(**{**_fields(options), "cwd": work})
    return CodexCliBackend(runner=runner, argv_prefix=("codex",)).complete(
        SYSTEM, USER, model="m", options=opts
    )


def _drive_claude(tmp_path: Path, options: BackendOptions, answer: str, rec: Recording):
    def runner(cmd, request, cwd, **kwargs):
        rec.calls += 1
        rec.argv = [str(a) for a in cmd]
        rec.stdin = request
        return json.dumps({"result": answer, "usage": {}}), "", 0

    return ClaudeCliBackend(runner=runner, executable="claude").complete(
        SYSTEM, USER, model="m", options=options
    )


def _drive_opencode(tmp_path: Path, options: BackendOptions, answer: str, rec: Recording):
    def runner(cmd, request, cwd, **kwargs):
        rec.calls += 1
        rec.argv = [str(a) for a in cmd]
        rec.stdin = request
        return answer, "", 0

    opts = BackendOptions(**{**_fields(options), "cwd": tmp_path})
    return OpencodeCliBackend(runner=runner, argv_prefix=("opencode-test",)).complete(
        SYSTEM, USER, model="m", options=opts
    )


def _fields(options: BackendOptions) -> dict:
    from dataclasses import fields as dc_fields

    return {f.name: getattr(options, f.name) for f in dc_fields(options)}


DRIVERS: "dict[str, Callable[..., Any]]" = {
    "openrouter": _drive_openrouter,
    "codex-cli": _drive_codex,
    "claude-cli": _drive_claude,
    "opencode-cli": _drive_opencode,
}


def _record_temp_files(monkeypatch, rec: Recording) -> None:
    real_mkstemp = codex_mod.tempfile.mkstemp

    def _recording_mkstemp(*args, **kwargs):
        handle, path = real_mkstemp(*args, **kwargs)
        rec.temp_files.append(path)
        return handle, path

    monkeypatch.setattr(codex_mod.tempfile, "mkstemp", _recording_mkstemp)


def _run(adapter, tmp_path, monkeypatch, *, policy=None, answer=VALID_ANSWER, rec=None, **opts):
    rec = rec if rec is not None else Recording()
    _record_temp_files(monkeypatch, rec)
    if policy is not None:
        opts["output_contract"] = _contract(policy)
    result = DRIVERS[adapter](tmp_path, BackendOptions(**opts), answer, rec)
    return result, rec


# -- independent baselines (no contract) --------------------------------------


def _assert_no_contract_baseline(adapter: str, rec: Recording, tmp_path: Path) -> None:
    """The uncontracted request equals one built WITHOUT the adapter's help."""
    assert rec.calls == 1
    assert SCHEMA_INSTRUCTION_PREFIX not in rec.text()
    if adapter == "openrouter":
        assert rec.kwargs["messages"] == [
            {
                "role": "system",
                "content": [
                    {"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}
                ],
            },
            {"role": "user", "content": USER},
        ]
        assert "extra_body" not in rec.kwargs
        assert "response_format" not in rec.kwargs
    elif adapter == "codex-cli":
        from bootstrap_lib.codex import build_codex_exec_argv

        out = rec.argv[rec.argv.index("-o") + 1]
        expected = build_codex_exec_argv(
            root=tmp_path / "work", output_file=Path(out), argv_prefix=("codex",), model="m"
        )
        assert rec.argv == [str(a) for a in expected]
        assert "--output-schema" not in rec.argv
        assert rec.stdin == codex_compose_prompt(SYSTEM, USER)
    elif adapter == "claude-cli":
        assert SYSTEM in rec.argv
    elif adapter == "opencode-cli":
        assert rec.stdin == opencode_compose_prompt(SYSTEM, USER)
    else:  # pragma: no cover -- a new adapter must add its baseline here
        pytest.fail(f"no independent no-contract baseline for {adapter}")


def _assert_prompt_delivery_exact(adapter: str, rec: Recording, contract: OutputContract) -> None:
    instruction = render_schema_instruction(contract)
    if adapter == "openrouter":
        system_message = rec.kwargs["messages"][0]
        assert system_message["role"] == "system"
        text = system_message["content"][0]["text"]
        assert text.endswith(instruction)
        assert text == SYSTEM + instruction
        # The user message is untouched and no response_format is sent.
        assert rec.kwargs["messages"][1] == {"role": "user", "content": USER}
        assert "response_format" not in rec.kwargs
        assert "response_format" not in (rec.kwargs.get("extra_body") or {})
    else:
        pytest.fail(
            f"{adapter} delivers contracts by prompt but has no exact delivery "
            "assertion here; add one (finding F3) rather than a presence check"
        )


# -- case generation ----------------------------------------------------------


@dataclass(frozen=True)
class Case:
    adapter: str
    kind: str
    policy: Optional[str] = None

    @property
    def id(self) -> str:
        middle = f"-{self.policy}" if self.policy else ""
        return f"{self.adapter}{middle}-{self.kind}"


#: The policies each adapter is EXPECTED to list. Cases are generated from this
#: table rather than from the records, so a policy wrongly added to a record
#: turns its fixed ``unsatisfiable`` case red instead of silently replacing it
#: with cases that assume the policy is supported.
EXPECTED_POLICIES = {
    "openrouter": ("validated-result", "text-only"),
    "codex-cli": ("native-required", "validated-result", "text-only"),
    "claude-cli": (),
    "opencode-cli": (),
}


def test_records_list_exactly_the_expected_policies():
    assert set(EXPECTED_POLICIES) == set(ADAPTER_CAPABILITIES)
    for adapter, cap in ADAPTER_CAPABILITIES.items():
        assert set(cap.structured_output.policies) == set(EXPECTED_POLICIES[adapter]), adapter


def _cases() -> List[Case]:
    cases: List[Case] = []
    for adapter, cap in ADAPTER_CAPABILITIES.items():
        structured = cap.structured_output
        listed = EXPECTED_POLICIES[adapter]
        cases.append(Case(adapter, "no-contract-byte-identical"))
        if structured.contract_delivery == DELIVERY_PROMPT:
            cases.append(Case(adapter, "prompt-exact"))
        if structured.contract_delivery == DELIVERY_NATIVE:
            cases.append(Case(adapter, "schema-file-exact"))
        if listed:
            cases.append(Case(adapter, "emits-names-channel"))
        for policy in POLICIES:
            if policy not in listed:
                cases.append(Case(adapter, "unsatisfiable", policy))
            elif policy == POLICY_TEXT_ONLY:
                cases.append(Case(adapter, "report", policy))
            else:
                for kind in ("valid", "mismatch", "unparseable"):
                    cases.append(Case(adapter, kind, policy))
    return cases


CASES = _cases()


def test_every_record_lists_only_known_policies():
    for adapter, cap in ADAPTER_CAPABILITIES.items():
        assert set(cap.structured_output.policies) <= set(POLICIES), adapter
        if cap.structured_output.policies:
            assert cap.structured_output.contract_delivery in (
                DELIVERY_NATIVE,
                DELIVERY_PROMPT,
            ), adapter
            assert cap.structured_output.contract_emits, adapter


def test_every_adapter_has_a_driver():
    assert set(DRIVERS) == set(ADAPTER_CAPABILITIES)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_conformance(case: Case, tmp_path, monkeypatch):
    adapter = case.adapter
    cap = ADAPTER_CAPABILITIES[adapter]
    structured = cap.structured_output

    if case.kind == "no-contract-byte-identical":
        response, rec = _run(adapter, tmp_path, monkeypatch)
        _assert_no_contract_baseline(adapter, rec, tmp_path)
        assert response.output_contract is None
        return

    if case.kind == "prompt-exact":
        for policy in set(structured.policies) & set(SCHEMA_POLICIES):
            _, rec = _run(adapter, tmp_path, monkeypatch, policy=policy)
            assert rec.calls == 1
            _assert_prompt_delivery_exact(adapter, rec, _contract(policy))
        return

    if case.kind == "schema-file-exact":
        schema_policies = sorted(set(structured.policies) & set(SCHEMA_POLICIES))
        assert schema_policies
        for policy in schema_policies:
            contract = _contract(policy)
            _, rec = _run(adapter, tmp_path, monkeypatch, policy=policy)
            assert rec.calls == 1
            assert rec.schema_bytes == canonical_schema_json(contract).encode("ascii")
            # A per-call temp file, created by this call and deleted after it.
            assert rec.schema_path in rec.temp_files
            assert not Path(rec.schema_path).exists()
            # Native delivery: the prompt carries no instruction.
            assert rec.stdin == codex_compose_prompt(SYSTEM, USER)
            assert SCHEMA_INSTRUCTION_PREFIX not in rec.text()
        return

    if case.kind == "emits-names-channel":
        emits = structured.contract_emits
        assert cap.params["output_contract"].emits == emits
        policy = sorted(set(structured.policies) & set(SCHEMA_POLICIES))[0]
        _, with_contract = _run(adapter, tmp_path, monkeypatch, policy=policy)
        _, without = _run(adapter, tmp_path, monkeypatch)
        head = emits.split()[0]
        if structured.contract_delivery == DELIVERY_NATIVE:
            # The named flag is emitted under a contract and only then.
            assert head in with_contract.argv
            assert head not in without.argv
        else:
            # The named channel is the adapter's own system-prompt channel.
            channels = {cap.system_prompt.emits, *cap.system_prompt.emits_by_mode.values()}
            assert head in channels
        return

    if case.kind == "unsatisfiable":
        # Not advertised, and refused. Both halves: an adapter that listed the
        # policy yet refused it would still be an overclaim in selection.
        assert case.policy not in structured.policies
        rec = Recording()
        with pytest.raises(OutputContractUnsatisfiable, match=case.policy):
            _run(adapter, tmp_path, monkeypatch, policy=case.policy, rec=rec)
        # Refused before dispatch: no runner or client call, no temp file.
        assert rec.calls == 0
        assert rec.temp_files == []
        return

    if case.kind == "report":  # text-only: behaves as no contract, plus a report
        response, rec = _run(adapter, tmp_path, monkeypatch, policy=case.policy)
        _, baseline = _run(adapter, tmp_path, monkeypatch)
        assert rec.comparable() == baseline.comparable()
        assert SCHEMA_INSTRUCTION_PREFIX not in rec.text()
        assert response.status == COMPLETED
        assert response.structured is None
        report = response.output_contract
        assert report.disposition == DISPOSITION_TEXT_ONLY
        assert report.delivery == DELIVERY_NONE
        assert report.policy == POLICY_TEXT_ONLY
        return

    contract = _contract(case.policy)
    if case.kind == "valid":
        response, rec = _run(adapter, tmp_path, monkeypatch, policy=case.policy)
        assert rec.calls == 1
        assert response.status == COMPLETED
        assert response.text == VALID_ANSWER
        assert response.structured == {"answer": "blue"}
        report = response.output_contract
        assert report.disposition == DISPOSITION_VALID
        assert report.delivery == structured.contract_delivery
        assert report.policy == case.policy
        assert (report.contract_id, report.schema_digest, report.schema_version) == (
            contract.id,
            contract.schema_digest,
            contract.schema_version,
        )
        assert report.errors == ()
        # A read param is never reported as dropped.
        assert "output_contract" not in response.dropped_params
        return

    answer = MISMATCH_ANSWER if case.kind == "mismatch" else UNPARSEABLE_ANSWER
    expected = (
        DISPOSITION_SCHEMA_MISMATCH if case.kind == "mismatch" else DISPOSITION_UNPARSEABLE
    )
    with pytest.raises(OutputContractViolation) as info:
        _run(adapter, tmp_path, monkeypatch, policy=case.policy, answer=answer)
    failed = info.value.response
    assert failed.status == ERROR
    assert failed.error.code == VIOLATION_ERROR_CODE
    assert failed.text == answer  # the raw text survives on the response
    assert failed.structured is None
    assert failed.output_contract.disposition == expected
    assert failed.output_contract.delivery == structured.contract_delivery
    if case.kind == "mismatch":
        assert failed.output_contract.errors == (("/answer", "minLength"),)
    # The message carries no model-authored text.
    assert answer not in str(info.value)


def test_codex_schema_file_is_removed_when_the_run_fails(tmp_path, monkeypatch):
    """A non-zero exit (how codex reports a schema it rejects) still deletes
    the schema file, and nothing is judged."""
    rec = Recording()
    _record_temp_files(monkeypatch, rec)

    def runner(cmd, request, cwd, **kwargs):
        rec.calls += 1
        argv = [str(a) for a in cmd]
        rec.schema_path = argv[argv.index("--output-schema") + 1]
        assert Path(rec.schema_path).exists()
        return "", "invalid_json_schema", 1

    backend = CodexCliBackend(runner=runner, argv_prefix=("codex",))
    monkeypatch.setattr(backend, "_quota_probe", lambda: None)
    with pytest.raises(CodexRunError):
        backend.complete(
            SYSTEM,
            USER,
            model="m",
            options=BackendOptions(
                cwd=tmp_path, output_contract=_contract("validated-result")
            ),
        )
    assert rec.calls == 1
    assert rec.schema_path in rec.temp_files
    assert not Path(rec.schema_path).exists()
    assert all(not Path(p).exists() for p in rec.temp_files)
