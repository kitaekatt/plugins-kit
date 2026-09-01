"""The advertisement must match what the adapters actually emit.

These are the tests that make the capability record a promise rather than a
claim. Each adapter is driven through its existing fake seam and the resulting
argv / request kwargs / environment is compared against its advertisement.

The load-bearing assertions are the negative ones -- that codex emits NO network
flag when network is false, that claude emits no --append-system-prompt, that an
unadvertised param changes nothing -- because those are the shapes a record can
overclaim without any positive test noticing.
"""
from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path

import pytest

from llm_scripting_kit.completion.adapter_capabilities import (
    ADAPTER_CAPABILITIES,
    CLAUDE_CAPABILITIES,
    CODEX_CAPABILITIES,
    OPENCODE_CAPABILITIES,
    OPENROUTER_CAPABILITIES,
)
from llm_scripting_kit.completion.backends import ClaudeCliBackend, OpenRouterBackend
from llm_scripting_kit.completion.capabilities import (
    APPEND,
    NATIVE,
    NONE,
    PASSTHROUGH,
    PROMPT_FOLD,
    REPLACE,
    PARSED_RESULT,
    TEXT_RESULT,
)
from llm_scripting_kit.completion.codex_backend import CodexCliBackend
from llm_scripting_kit.completion.opencode_backend import OpencodeCliBackend
from llm_scripting_kit.completion.types import BackendOptions

# Read from the dataclass, never restated here. A hardcoded copy would agree with
# a hardcoded copy in adapter_capabilities.py and the pair would pass while both
# drifted away from BackendOptions -- a guard that cannot fail is not a guard.
ALL_OPTION_FIELDS = {f.name for f in fields(BackendOptions)}


# -- record invariants -----------------------------------------------------


@pytest.mark.parametrize("cap", list(ADAPTER_CAPABILITIES.values()), ids=lambda c: c.adapter)
def test_every_option_field_is_either_honored_or_dropped(cap):
    """No BackendOptions field may go unadvertised.

    This is what stops a new option from being added and silently honored by
    nobody while the advertisement stays quiet about it.
    """
    honored = {name.split(".")[0] for name in cap.params}
    covered = honored | set(cap.dropped_params)
    assert ALL_OPTION_FIELDS <= covered, ALL_OPTION_FIELDS - covered


@pytest.mark.parametrize("cap", list(ADAPTER_CAPABILITIES.values()), ids=lambda c: c.adapter)
def test_honored_and_dropped_are_disjoint(cap):
    honored = {name.split(".")[0] for name in cap.params}
    assert not (honored & set(cap.dropped_params))


@pytest.mark.parametrize("cap", list(ADAPTER_CAPABILITIES.values()), ids=lambda c: c.adapter)
def test_adapter_name_matches_the_backend_that_produces_it(cap):
    """The advertisement uses the backend's own name, never an invented family."""
    backends = {
        "openrouter": OpenRouterBackend,
        "claude-cli": ClaudeCliBackend,
        "codex-cli": CodexCliBackend,
        "opencode-cli": OpencodeCliBackend,
    }
    assert cap.adapter in backends
    assert backends[cap.adapter].capabilities is cap


@pytest.mark.parametrize("cap", list(ADAPTER_CAPABILITIES.values()), ids=lambda c: c.adapter)
def test_separator_only_on_prompt_fold(cap):
    if cap.system_prompt.mode != PROMPT_FOLD:
        assert cap.system_prompt.separator is None


@pytest.mark.parametrize("cap", list(ADAPTER_CAPABILITIES.values()), ids=lambda c: c.adapter)
def test_request_sourced_controls_name_their_parameter(cap):
    for control in cap.execution_controls:
        if control.source == "request":
            assert control.parameter, control.id
            assert control.parameter in cap.params, control.id


@pytest.mark.parametrize("cap", list(ADAPTER_CAPABILITIES.values()), ids=lambda c: c.adapter)
def test_record_is_json_serializable(cap):
    json.dumps(cap.to_json())


# -- openrouter ------------------------------------------------------------


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


class _RecordingClient:
    """Records the kwargs the adapter hands the OpenAI SDK."""

    def __init__(self):
        self.kwargs = None
        outer = self

        class _Completions:
            def create(self, **kwargs):
                outer.kwargs = kwargs
                return _FakeResponse("ok")

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()


def _openrouter_call(**opts):
    client = _RecordingClient()
    backend = OpenRouterBackend(client=client)
    backend.complete("sys", "usr", model="m", options=BackendOptions(**opts))
    return client.kwargs


def test_openrouter_emits_every_advertised_param():
    kwargs = _openrouter_call(max_tokens=99, temperature=0.7, timeout_s=12.0)
    assert kwargs["max_tokens"] == 99
    assert kwargs["temperature"] == 0.7
    assert kwargs["timeout"] == 12.0


def test_openrouter_system_is_a_role_not_an_append():
    kwargs = _openrouter_call()
    roles = [m["role"] for m in kwargs["messages"]]
    assert roles == ["system", "user"]
    assert OPENROUTER_CAPABILITIES.system_prompt.mode != APPEND


def test_openrouter_extras_ride_as_passthrough_extra_body():
    kwargs = _openrouter_call(extras={"response_format": {"type": "json_object"}})
    assert kwargs["extra_body"] == {"response_format": {"type": "json_object"}}
    assert OPENROUTER_CAPABILITIES.params["extras"].handling == PASSTHROUGH
    assert OPENROUTER_CAPABILITIES.structured_output.mode == PASSTHROUGH
    assert OPENROUTER_CAPABILITIES.structured_output.result == TEXT_RESULT


def test_openrouter_omits_timeout_when_unset():
    assert "timeout" not in _openrouter_call()


@pytest.mark.parametrize("dropped", sorted(OPENROUTER_CAPABILITIES.dropped_params))
def test_openrouter_dropped_params_change_nothing(dropped):
    """A dropped param must not alter the request -- that is what dropped means."""
    baseline = _openrouter_call()
    values = {
        "cache_salt": 7,
        "effort": "high",
        "allowed_tools": "Read",
        "cwd": Path.cwd(),
        "log_prefix": "[x]",
    }
    assert _openrouter_call(**{dropped: values[dropped]}) == baseline


def test_openrouter_emits_no_execution_controls():
    assert OPENROUTER_CAPABILITIES.execution_controls == ()


# -- claude ----------------------------------------------------------------


def _claude_argv(**opts):
    captured = {}

    def runner(cmd, request, cwd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["cwd"] = cwd
        captured["stdin"] = request
        return json.dumps({"result": "ok", "usage": {}}), "", 0

    backend = ClaudeCliBackend(runner=runner, executable="claude")
    backend.complete("sys", "usr", model="m", options=BackendOptions(**opts))
    return captured


def test_claude_emits_each_advertised_control():
    argv = _claude_argv()["cmd"]
    for control in CLAUDE_CAPABILITIES.execution_controls:
        head = control.emits.split()[0]
        assert head in argv, control.id


def test_claude_permission_bypass_and_allowlist_coexist():
    argv = _claude_argv(allowed_tools="Read")["cmd"]
    assert argv[argv.index("--allowedTools") + 1] == "Read"
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"


def test_claude_system_replaces_and_never_appends():
    """The advertisement says replace; the argv must back exactly that."""
    argv = _claude_argv()["cmd"]
    assert "--system-prompt" in argv
    assert "--append-system-prompt" not in argv
    assert CLAUDE_CAPABILITIES.system_prompt.mode == REPLACE
    assert CLAUDE_CAPABILITIES.system_prompt.mode != APPEND


def test_claude_advertises_no_structured_output():
    """--output-format json is a transport envelope, not a caller schema."""
    argv = _claude_argv()["cmd"]
    assert argv[argv.index("--output-format") + 1] == "json"
    assert CLAUDE_CAPABILITIES.structured_output.mode == NONE


def test_claude_effort_is_conditional_and_unvalidated():
    assert "--effort" not in _claude_argv()["cmd"]
    argv = _claude_argv(effort="anything-at-all")["cmd"]
    assert argv[argv.index("--effort") + 1] == "anything-at-all"
    assert CLAUDE_CAPABILITIES.params["effort"].values is None


@pytest.mark.parametrize("dropped", ["max_tokens", "temperature", "cache_salt", "user_cache_prefix"])
def test_claude_dropped_params_change_no_argv(dropped):
    baseline = _claude_argv()["cmd"]
    values = {
        "max_tokens": 12345,
        "temperature": 0.99,
        "cache_salt": 7,
        "user_cache_prefix": "prefix",
    }
    assert _claude_argv(**{dropped: values[dropped]})["cmd"] == baseline
    assert dropped in CLAUDE_CAPABILITIES.dropped_params


def test_claude_extras_are_dropped():
    baseline = _claude_argv()["cmd"]
    assert _claude_argv(extras={"anything": "at-all"})["cmd"] == baseline
    assert "extras" in CLAUDE_CAPABILITIES.dropped_params


# -- codex -----------------------------------------------------------------


def _codex_argv(tmp_path, **opts):
    captured = {}

    def runner(cmd, request, cwd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["stdin"] = request
        # the backend reads its answer from the -o file
        out = Path(cmd[cmd.index("-o") + 1])
        out.write_text("ok", encoding="utf-8")
        return "", "tokens used: 10", 0

    opts.setdefault("cwd", tmp_path)
    backend = CodexCliBackend(runner=runner, argv_prefix=("codex",))
    backend.complete("sys", "usr", model="m", options=BackendOptions(**opts))
    return captured


def _stable_argv(captured):
    """argv with the per-call temporary -o path replaced by a fixed token.

    The backend allocates a fresh temp file for every call, so two otherwise
    identical invocations never produce equal argv without this.
    """
    argv = list(captured["cmd"])
    if "-o" in argv:
        argv[argv.index("-o") + 1] = "<output-file>"
    return argv


def test_codex_network_true_emits_the_flag(tmp_path):
    argv = _codex_argv(tmp_path, extras={"network": True})["cmd"]
    assert "sandbox_workspace_write.network_access=true" in argv


def test_codex_network_false_emits_nothing(tmp_path):
    """The load-bearing negative: absence of a flag is NOT a deny control.

    An earlier design advertised a `network-off` control with effect=deny. The
    code emits nothing at all for network=False, so no such control exists.
    """
    argv = _stable_argv(_codex_argv(tmp_path, extras={"network": False}))
    assert "sandbox_workspace_write.network_access=true" not in argv
    assert not any("network_access" in str(part) for part in argv)
    ids = {c.id for c in CODEX_CAPABILITIES.execution_controls}
    assert "network-disable" not in ids
    assert "network-off" not in ids


def test_codex_effort_is_forwarded_unvalidated(tmp_path):
    """No menu is advertised because this path validates none."""
    argv = _codex_argv(tmp_path, effort="not-a-real-effort")["cmd"]
    assert "model_reasoning_effort=not-a-real-effort" in argv
    assert CODEX_CAPABILITIES.params["effort"].values is None


def test_codex_sandbox_defaults_to_workspace_write(tmp_path):
    argv = _codex_argv(tmp_path)["cmd"]
    assert argv[argv.index("-s") + 1] == "workspace-write"
    assert CODEX_CAPABILITIES.params["extras.sandbox"].default == "workspace-write"


def test_codex_output_schema_is_native(tmp_path):
    schema = tmp_path / "schema.json"
    schema.write_text("{}", encoding="utf-8")
    argv = _codex_argv(tmp_path, extras={"output_schema": schema})["cmd"]
    assert argv[argv.index("--output-schema") + 1] == str(schema)
    assert CODEX_CAPABILITIES.structured_output.mode == NATIVE
    # a native schema control whose result the adapter parses into `structured`
    assert CODEX_CAPABILITIES.structured_output.result == PARSED_RESULT


def test_codex_folds_system_into_the_prompt(tmp_path):
    stdin = _codex_argv(tmp_path)["stdin"]
    assert stdin == "sys" + CODEX_CAPABILITIES.system_prompt.separator + "usr"


def test_codex_unrecognized_extras_are_dropped(tmp_path):
    baseline = _stable_argv(_codex_argv(tmp_path))
    argv = _stable_argv(_codex_argv(tmp_path, extras={"not_a_real_key": "x"}))
    assert argv == baseline


@pytest.mark.parametrize("dropped", ["max_tokens", "temperature", "allowed_tools"])
def test_codex_dropped_params_change_no_argv(tmp_path, dropped):
    baseline = _stable_argv(_codex_argv(tmp_path))
    values = {"max_tokens": 999, "temperature": 0.9, "allowed_tools": "Read"}
    assert _stable_argv(_codex_argv(tmp_path, **{dropped: values[dropped]})) == baseline
    assert dropped in CODEX_CAPABILITIES.dropped_params


# -- opencode --------------------------------------------------------------


def _opencode_run(tmp_path, **opts):
    captured = {}

    def runner(cmd, request, cwd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["stdin"] = request
        captured["env"] = kwargs.get("env") or {}
        return "ok", "", 0

    opts.setdefault("cwd", tmp_path)
    backend = OpencodeCliBackend(runner=runner, argv_prefix=("opencode-test",))
    backend.complete("sys", "usr", model="m", options=BackendOptions(**opts))
    return captured


def _opencode_policy(captured):
    raw = captured["env"].get("OPENCODE_CONFIG_CONTENT")
    assert raw, "adapter must inject its policy"
    return json.loads(raw)


def test_opencode_injects_scalar_permission_settings(tmp_path):
    """They are scalar settings under a permission namespace, not deny LISTS."""
    policy = _opencode_policy(_opencode_run(tmp_path))
    assert policy["permission"]["external_directory"] == "deny"
    assert policy["permission"]["task"] == "deny"
    assert policy["agent"]["build"]["permission"]["external_directory"] == "deny"
    assert policy["agent"]["build"]["permission"]["task"] == "deny"


def test_opencode_control_subjects_are_exact_native_key_paths(tmp_path):
    """Each advertised subject must resolve in the policy the adapter writes."""
    policy = _opencode_policy(_opencode_run(tmp_path))
    for control in OPENCODE_CAPABILITIES.execution_controls:
        for subject in control.subjects:
            if not subject.startswith(("permission.", "agent.")):
                continue
            node = policy
            for part in subject.split("."):
                assert part in node, subject
                node = node[part]


def test_opencode_emits_pure_auto_and_fixed_agent(tmp_path):
    argv = _opencode_run(tmp_path)["cmd"]
    assert "--pure" in argv
    assert "--auto" in argv
    assert argv[argv.index("--agent") + 1] == "build"


def test_opencode_advertises_no_structured_output(tmp_path):
    argv = _opencode_run(tmp_path)["cmd"]
    assert "--format" not in argv
    assert OPENCODE_CAPABILITIES.structured_output.mode == NONE


def test_opencode_folds_system_into_the_prompt(tmp_path):
    stdin = _opencode_run(tmp_path)["stdin"]
    assert stdin == "sys" + OPENCODE_CAPABILITIES.system_prompt.separator + "usr"


@pytest.mark.parametrize("dropped", ["max_tokens", "temperature", "allowed_tools"])
def test_opencode_dropped_params_change_no_argv(tmp_path, dropped):
    baseline = _opencode_run(tmp_path)["cmd"]
    values = {"max_tokens": 999, "temperature": 0.9, "allowed_tools": "Read"}
    assert _opencode_run(tmp_path, **{dropped: values[dropped]})["cmd"] == baseline
    assert dropped in OPENCODE_CAPABILITIES.dropped_params
