"""Skill context through each adapter: openrouter delivers, three harnesses refuse.

The openrouter tests fake the transport BELOW ``OpenRouterBackend.complete``
(the OpenAI SDK client) and assert on the captured ``messages``, so they see
exactly what the adapter composed. The harness tests count runner calls and,
for codex, temp files: a refusal must happen before either.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

import bootstrap_lib.skill_material as real_library
from llm_scripting_kit.completion import codex_backend as codex_mod
from llm_scripting_kit.completion.adapter_capabilities import (
    ADAPTER_CAPABILITIES,
    OPENROUTER_CAPABILITIES,
)
from llm_scripting_kit.completion.backends import ClaudeCliBackend, OpenRouterBackend
from llm_scripting_kit.completion.codex_backend import CodexCliBackend
from llm_scripting_kit.completion.contract import (
    POLICY_NATIVE_REQUIRED,
    POLICY_VALIDATED_RESULT,
    OutputContract,
    OutputContractUnsatisfiable,
    OutputContractViolation,
    render_schema_instruction,
)
from llm_scripting_kit.completion.endpoint_profile import (
    EndpointProfile,
    endpoint_capabilities,
)
from llm_scripting_kit.completion.opencode_backend import OpencodeCliBackend
from llm_scripting_kit.completion.requirements import match_capabilities
from llm_scripting_kit.completion.results import derive_dropped_params
from llm_scripting_kit.completion.skill_context import (
    materialize_skill_context,
    skill_context_requirements,
)
from llm_scripting_kit.completion.skill_context_types import (
    SkillContextError,
    SkillContextUnsatisfiable,
)
from llm_scripting_kit.completion.types import BackendOptions
from llm_scripting_kit.effort import EffortDelivery

EMITS = "messages[system] leading skill context block"

#: Format "1" for one full skill, written out literally (the library's frozen
#: bytes): the adapter must deliver these and nothing re-rendered.
GOLDEN_ALPHA = (
    '<skill_context version="1">\n'
    "Skill material selected by the caller for this request. A skill at level "
    '"catalog" is listed by name and description only; its instructions are not '
    "included and cannot be loaded in this request.\n"
    '<skill name="alpha" level="full">\n'
    "<description>The alpha skill.</description>\n"
    "<instructions>\n"
    "Follow the alpha procedure.\n"
    "</instructions>\n"
    "</skill>\n"
    "</skill_context>"
)

_SCHEMA = {"type": "object", "required": ["answer"]}


def _write_skill(root, name="alpha"):
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    text = f"---\nname: {name}\ndescription: The {name} skill.\n---\nFollow the {name} procedure.\n"
    (skill_dir / "SKILL.md").write_bytes(text.encode("utf-8"))
    return skill_dir


@pytest.fixture
def context(tmp_path):
    skill = _write_skill(tmp_path)
    return materialize_skill_context(
        {"skills": [{"path": str(skill)}], "token_budget": 10_000}
    )


class _Message:
    def __init__(self, content):
        self.content = content
        self.reasoning_content = None


class _Choice:
    def __init__(self, content):
        self.message = _Message(content)
        self.finish_reason = "stop"


class _Response:
    def __init__(self, content):
        self.choices = [_Choice(content)]
        self.usage = None


class _RecordingClient:
    """The OpenAI SDK seam: records every chat.completions.create call."""

    def __init__(self, answer="ok"):
        self.calls = []
        outer = self

        class _Completions:
            def create(self, **kwargs):
                outer.calls.append(kwargs)
                return _Response(answer)

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()


class _RecordingRunner:
    def __init__(self, stdout="ok"):
        self.calls = []
        self.stdout = stdout

    def __call__(self, cmd, request, cwd, **kwargs):
        self.calls.append(list(cmd))
        return self.stdout, "", 0


def _openrouter(options, answer="ok", system="caller system"):
    client = _RecordingClient(answer)
    backend = OpenRouterBackend(client=client)
    response = backend.complete(system, "usr", model="test/slug", options=options)
    return client, response


def _system_text(client):
    message = client.calls[0]["messages"][0]
    assert message["role"] == "system"
    return message["content"][0]["text"]


# -- openrouter delivers -------------------------------------------------------


def test_openrouter_system_is_block_then_caller_system(context):
    client, _ = _openrouter(BackendOptions(skill_context=context))
    assert _system_text(client) == context.text + "\n\n" + "caller system"


def test_delivered_block_equals_library_text(context):
    client, _ = _openrouter(BackendOptions(skill_context=context), system="")
    assert context.text == GOLDEN_ALPHA
    assert _system_text(client) == GOLDEN_ALPHA


def test_openrouter_contract_instruction_stays_last_with_skill_context(context):
    contract = OutputContract("t.skill", POLICY_VALIDATED_RESULT, _SCHEMA)
    client, response = _openrouter(
        BackendOptions(skill_context=context, output_contract=contract),
        answer=json.dumps({"answer": "x"}),
    )
    assert _system_text(client) == (
        context.text + "\n\n" + "caller system" + render_schema_instruction(contract)
    )
    assert response.structured == {"answer": "x"}


def test_openrouter_empty_caller_system_is_block_only(context):
    client, _ = _openrouter(BackendOptions(skill_context=context), system="")
    assert _system_text(client) == context.text


def test_openrouter_system_unchanged_without_skill_context():
    client, response = _openrouter(BackendOptions())
    assert _system_text(client) == "caller system"
    assert response.skill_context is None


def test_openrouter_response_carries_skill_context_report(context):
    _, response = _openrouter(BackendOptions(skill_context=context))
    report = response.skill_context
    assert report == dataclasses.replace(
        context.report, adapter="openrouter", delivery="system-message", emits=EMITS
    )
    assert report.provenance["schema"] == "plugins-kit.skill-material-report/v1"


def test_contract_violation_response_carries_skill_context_report(context):
    contract = OutputContract("t.skill", POLICY_VALIDATED_RESULT, _SCHEMA)
    with pytest.raises(OutputContractViolation) as info:
        _openrouter(
            BackendOptions(skill_context=context, output_contract=contract),
            answer="not json",
        )
    report = info.value.response.skill_context
    assert report is not None
    assert (report.adapter, report.delivery, report.digest) == (
        "openrouter", "system-message", context.report.digest,
    )


def test_openrouter_skill_context_emits_matches_request(context):
    record = OPENROUTER_CAPABILITIES
    assert record.params["skill_context"].emits == EMITS
    assert record.skill_context.emits == EMITS
    assert record.skill_context.delivery == "system-message"
    client, _ = _openrouter(BackendOptions(skill_context=context))
    # "messages[system] leading skill context block": the system-role message
    # begins with the block.
    assert client.calls[0]["messages"][0]["role"] == "system"
    assert _system_text(client).startswith(context.text)


# -- the harness adapters refuse -------------------------------------------------


def test_claude_refuses_skill_context_before_runner(context):
    runner = _RecordingRunner(stdout=json.dumps({"result": "ok", "usage": {}}))
    backend = ClaudeCliBackend(runner=runner, executable="claude")
    with pytest.raises(SkillContextUnsatisfiable) as info:
        backend.complete("sys", "usr", model="m", options=BackendOptions(skill_context=context))
    assert runner.calls == []
    message = str(info.value)
    assert "claude-cli" in message and "transport entry" in message and "system" in message
    assert backend.classify_halt(info.value) is None


def _record_codex_temp_files(monkeypatch):
    temp_files = []
    real_mkstemp = codex_mod.tempfile.mkstemp

    def _recording_mkstemp(*args, **kwargs):
        handle, path = real_mkstemp(*args, **kwargs)
        temp_files.append(path)
        return handle, path

    monkeypatch.setattr(codex_mod.tempfile, "mkstemp", _recording_mkstemp)
    return temp_files


def test_codex_refuses_skill_context_before_temp_file(context, tmp_path, monkeypatch):
    runner = _RecordingRunner()
    temp_files = _record_codex_temp_files(monkeypatch)
    work = tmp_path / "work"
    work.mkdir()
    backend = CodexCliBackend(runner=runner, argv_prefix=("codex",))
    with pytest.raises(SkillContextUnsatisfiable, match="codex-cli"):
        backend.complete(
            "sys", "usr", model="m", options=BackendOptions(skill_context=context, cwd=work)
        )
    assert runner.calls == []
    assert temp_files == []
    assert list(work.iterdir()) == []


def test_opencode_refuses_skill_context_before_runner(context, tmp_path):
    runner = _RecordingRunner()
    backend = OpencodeCliBackend(runner=runner, argv_prefix=("opencode-test",))
    with pytest.raises(SkillContextUnsatisfiable, match="opencode-cli"):
        backend.complete(
            "sys", "usr", model="m",
            options=BackendOptions(skill_context=context, cwd=tmp_path),
        )
    assert runner.calls == []


def test_contract_refusal_precedes_skill_context_refusal(context):
    """When both would refuse, the contract refusal surfaces."""
    runner = _RecordingRunner(stdout=json.dumps({"result": "ok", "usage": {}}))
    backend = ClaudeCliBackend(runner=runner, executable="claude")
    contract = OutputContract("t.skill", POLICY_NATIVE_REQUIRED, _SCHEMA)
    with pytest.raises(OutputContractUnsatisfiable):
        backend.complete(
            "sys", "usr", model="m",
            options=BackendOptions(skill_context=context, output_contract=contract),
        )
    assert runner.calls == []


# -- the record is checked before dispatch ----------------------------------------


def test_non_skill_context_value_is_type_error(context, tmp_path):
    skill = _write_skill(tmp_path, "beta")
    mapping = {"skills": [{"path": str(skill)}], "token_budget": 10_000}
    library_result = real_library.materialize(real_library.SkillSelection.from_json(mapping))
    for value in (mapping, library_result):
        client = _RecordingClient()
        with pytest.raises(TypeError, match="materialize_skill_context"):
            OpenRouterBackend(client=client).complete(
                "sys", "usr", model="test/slug", options=BackendOptions(skill_context=value)
            )
        assert client.calls == []


def test_text_and_digest_mismatch_refused_before_dispatch(context, monkeypatch):
    tampered = dataclasses.replace(context, text=context.text + "\nextra")
    client = _RecordingClient()
    backend = OpenRouterBackend(client=client)

    def _forbidden(*args, **kwargs):
        raise AssertionError("nothing may run before the record is refused")

    monkeypatch.setattr(OpenRouterBackend, "_resolve_model", _forbidden)
    with pytest.raises(SkillContextError, match="disagree"):
        backend.complete("sys", "usr", model="test/slug", options=BackendOptions(skill_context=tampered))
    assert client.calls == []


# -- the advertisement ----------------------------------------------------------


@pytest.mark.parametrize("adapter", sorted(ADAPTER_CAPABILITIES))
def test_skill_context_param_read_by_every_adapter_not_dropped(adapter, context):
    record = ADAPTER_CAPABILITIES[adapter]
    assert record.honors("skill_context")
    assert record.params["skill_context"].type == "skill-context"
    assert "skill_context" not in record.dropped_params
    reported = derive_dropped_params(record, BackendOptions(skill_context=context))
    assert "skill_context" not in reported


def test_harness_records_have_no_skill_context_block():
    for adapter, record in ADAPTER_CAPABILITIES.items():
        payload = record.to_json()
        if adapter == "openrouter":
            assert payload["skill_context"] == {"delivery": "system-message", "emits": EMITS}
        else:
            assert record.skill_context is None
            assert "skill_context" not in payload
            assert "emits" not in payload["params"]["skill_context"]


def test_openrouter_params_report_does_not_drop_skill_context(context):
    backend = OpenRouterBackend(client=_RecordingClient())
    options = BackendOptions(skill_context=context)
    dropped, forwarded = backend.params_report(options)
    assert "skill_context" not in dropped
    assert "skill_context" not in forwarded
    _, response = _openrouter(options)
    assert "skill_context" not in response.dropped_params


def test_skill_context_requirement_matches_only_delivering_adapters(context):
    requirement = skill_context_requirements(context)
    assert requirement == {"skill_context": {"delivery": "system-message"}}
    matched = sorted(
        name for name, record in ADAPTER_CAPABILITIES.items()
        if match_capabilities(record, requirement)
    )
    assert matched == ["openrouter"]
    assert skill_context_requirements(None) == {}
    with pytest.raises(TypeError):
        skill_context_requirements({"skills": []})


def test_endpoint_capabilities_keep_skill_context_block():
    profile = EndpointProfile("e", EffortDelivery("top-level", "endpoint"), "medium")
    record = endpoint_capabilities(OPENROUTER_CAPABILITIES, profile)
    assert record.endpoint == "e"  # really specialized, not returned unchanged
    assert record.skill_context == OPENROUTER_CAPABILITIES.skill_context
    assert record.to_json()["skill_context"] == {"delivery": "system-message", "emits": EMITS}
    assert match_capabilities(record, {"skill_context": {"delivery": "system-message"}})
