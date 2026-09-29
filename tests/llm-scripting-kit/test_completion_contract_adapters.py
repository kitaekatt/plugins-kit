"""Every adapter refuses an output contract it does not advertise, before dispatch.

The adapters dispatch directly -- there is no central pre-dispatch point -- so
each one calls ``prepare_contract`` itself, first thing after resolving its
options. These tests drive each adapter through its fake seam with a contract
of each policy and require ZERO runner or client invocations. Removing the
call from any one adapter turns exactly that adapter's test red.

No adapter lists a policy yet, so all three policies are refused everywhere.
When an adapter starts to deliver a policy, its case narrows to the policies
it still does not list.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from llm_scripting_kit.completion import codex_backend as codex_mod
from llm_scripting_kit.completion.backends import ClaudeCliBackend, OpenRouterBackend
from llm_scripting_kit.completion.codex_backend import CodexCliBackend
from llm_scripting_kit.completion.contract import (
    POLICY_NATIVE_REQUIRED,
    POLICY_TEXT_ONLY,
    POLICY_VALIDATED_RESULT,
    OutputContract,
    OutputContractUnsatisfiable,
)
from llm_scripting_kit.completion.opencode_backend import OpencodeCliBackend
from llm_scripting_kit.completion.types import BackendOptions

_SCHEMA = {"type": "object", "required": ["answer"]}

POLICIES = (POLICY_NATIVE_REQUIRED, POLICY_VALIDATED_RESULT, POLICY_TEXT_ONLY)


def _options(policy, **kw):
    if policy == POLICY_TEXT_ONLY:
        contract = OutputContract("t.adapter", policy)
    else:
        contract = OutputContract("t.adapter", policy, _SCHEMA)
    return BackendOptions(output_contract=contract, **kw)


class _RecordingRunner:
    """The CLI runner seam: records every invocation, answers "ok"."""

    def __init__(self, stdout="ok"):
        self.calls = []
        self.stdout = stdout

    def __call__(self, cmd, request, cwd, **kwargs):
        self.calls.append(list(cmd))
        return self.stdout, "", 0


class _RecordingClient:
    """The OpenAI SDK seam: records every chat.completions.create call."""

    def __init__(self):
        self.calls = []
        outer = self

        class _Completions:
            def create(self, **kwargs):
                outer.calls.append(kwargs)
                raise AssertionError("the client must not be called")

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()


@pytest.mark.parametrize("policy", POLICIES)
def test_openrouter_refuses_any_contract_before_client_call(policy, monkeypatch):
    client = _RecordingClient()
    backend = OpenRouterBackend(client=client)
    with pytest.raises(OutputContractUnsatisfiable, match=policy):
        backend.complete("sys", "usr", model="test/slug", options=_options(policy))
    assert client.calls == []

    # Refused before the client is even BUILT or the model resolved.
    def _forbidden(*args, **kwargs):
        raise AssertionError("nothing may run before the contract is refused")

    unbuilt = OpenRouterBackend()
    monkeypatch.setattr(OpenRouterBackend, "_ensure_client", _forbidden)
    monkeypatch.setattr(OpenRouterBackend, "_resolve_model", _forbidden)
    with pytest.raises(OutputContractUnsatisfiable):
        unbuilt.complete("sys", "usr", model="test/slug", options=_options(policy))


@pytest.mark.parametrize("policy", POLICIES)
def test_claude_refuses_any_contract_before_runner_call(policy):
    runner = _RecordingRunner(stdout=json.dumps({"result": "ok", "usage": {}}))
    backend = ClaudeCliBackend(runner=runner, executable="claude")
    with pytest.raises(OutputContractUnsatisfiable, match=policy):
        backend.complete("sys", "usr", model="m", options=_options(policy))
    assert runner.calls == []


@pytest.mark.parametrize("policy", POLICIES)
def test_codex_refuses_any_contract_before_runner_call(policy, tmp_path, monkeypatch):
    runner = _RecordingRunner()
    temp_files = []
    real_mkstemp = codex_mod.tempfile.mkstemp

    def _recording_mkstemp(*args, **kwargs):
        handle, path = real_mkstemp(*args, **kwargs)
        temp_files.append(path)
        return handle, path

    monkeypatch.setattr(codex_mod.tempfile, "mkstemp", _recording_mkstemp)
    work = tmp_path / "work"
    work.mkdir()
    backend = CodexCliBackend(runner=runner, argv_prefix=("codex",))
    with pytest.raises(OutputContractUnsatisfiable, match=policy):
        backend.complete("sys", "usr", model="m", options=_options(policy, cwd=work))
    assert runner.calls == []
    # No temp file of any kind -- neither the -o result file nor a schema file.
    assert temp_files == []
    assert list(work.iterdir()) == []


@pytest.mark.parametrize("policy", POLICIES)
def test_opencode_refuses_any_contract_before_runner_call(policy, tmp_path):
    runner = _RecordingRunner()
    backend = OpencodeCliBackend(runner=runner, argv_prefix=("opencode-test",))
    with pytest.raises(OutputContractUnsatisfiable, match=policy):
        backend.complete("sys", "usr", model="m", options=_options(policy, cwd=tmp_path))
    assert runner.calls == []


def test_an_uncontracted_call_still_dispatches_on_every_adapter(tmp_path):
    """The refusal is keyed on the contract alone: without one, each adapter
    dispatches exactly once, as before."""
    claude_runner = _RecordingRunner(stdout=json.dumps({"result": "ok", "usage": {}}))
    ClaudeCliBackend(runner=claude_runner, executable="claude").complete(
        "sys", "usr", model="m", options=BackendOptions()
    )
    assert len(claude_runner.calls) == 1

    def codex_runner(cmd, request, cwd, **kwargs):
        codex_runner.calls += 1
        Path(cmd[cmd.index("-o") + 1]).write_text("ok", encoding="utf-8")
        return "", "", 0

    codex_runner.calls = 0
    CodexCliBackend(runner=codex_runner, argv_prefix=("codex",)).complete(
        "sys", "usr", model="m", options=BackendOptions(cwd=tmp_path)
    )
    assert codex_runner.calls == 1

    opencode_runner = _RecordingRunner()
    OpencodeCliBackend(runner=opencode_runner, argv_prefix=("opencode-test",)).complete(
        "sys", "usr", model="m", options=BackendOptions(cwd=tmp_path)
    )
    assert len(opencode_runner.calls) == 1
