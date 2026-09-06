"""Tests for the per-call truthfulness contract.

The advertisement says what an adapter can honor in general; these tests pin
what one CALL reports about itself -- which requested params were dropped,
which advertised controls the request actually emitted, whether schema-backed
output was parsed, and when the call ran.

Hermetic throughout: every adapter goes through the same injected runner /
client seams its own test module uses, so nothing here spawns a CLI or reaches
a network.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from llm_scripting_kit.completion import (
    COMPLETED,
    ERROR,
    TIMEOUT,
    BackendOptions,
    ClaudeCliBackend,
    CodexCliBackend,
    LLMResponse,
    OpencodeCliBackend,
    OpenRouterBackend,
    ResponseError,
    adapter_capabilities,
    caller_set_params,
    check_applied_controls,
    derive_dropped_params,
    derive_extras_report,
    derive_forwarded_params,
    fixed_control_ids,
)
from llm_scripting_kit.completion.adapter_capabilities import (
    CLAUDE_CAPABILITIES,
    CODEX_CAPABILITIES,
    OPENCODE_CAPABILITIES,
    OPENROUTER_CAPABILITIES,
)
from llm_scripting_kit.harness_adapters import HarnessInvocation


ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


# ---------------------------------------------------------------------------
# The derivations themselves
# ---------------------------------------------------------------------------


def test_caller_set_params_reports_only_what_differs_from_the_default():
    assert caller_set_params(BackendOptions()) == ()
    assert caller_set_params(BackendOptions(effort="high")) == ("effort",)
    # a value equal to the default is indistinguishable from an unset one, and
    # that collision is deliberately harmless: nothing relied on it
    assert caller_set_params(BackendOptions(max_tokens=4096)) == ()
    assert set(caller_set_params(BackendOptions(cache_salt=2, allowed_tools="Read"))) == {
        "cache_salt",
        "allowed_tools",
    }


def test_dropped_params_is_the_intersection_of_advertised_and_requested():
    # openrouter reads neither, so a caller that set both is told about both
    opts = BackendOptions(effort="high", allowed_tools="Read")
    assert derive_dropped_params(OPENROUTER_CAPABILITIES, opts) == (
        "effort",
        "allowed_tools",
    )
    # claude READS effort, so only the genuinely dropped param is reported
    assert "effort" not in derive_dropped_params(CLAUDE_CAPABILITIES, opts)


def test_dropped_params_stays_silent_about_params_the_caller_never_set():
    assert derive_dropped_params(OPENROUTER_CAPABILITIES, BackendOptions()) == ()


def test_dropped_params_follows_the_advertisement_order():
    """Two calls against one adapter report the same sequence, not a set order."""
    opts = BackendOptions(effort="high", allowed_tools="Read", cache_salt=1)
    reported = derive_dropped_params(OPENROUTER_CAPABILITIES, opts)
    advertised = [p for p in OPENROUTER_CAPABILITIES.dropped_params if p in reported]
    assert list(reported) == advertised


def test_every_adapters_dropped_params_are_real_option_fields():
    """A dropped param that is not an option field could never be reported."""
    known = {f for f in BackendOptions.__dataclass_fields__}
    for name, caps in adapter_capabilities().items():
        unknown = set(caps.dropped_params) - known
        assert not unknown, f"{name} advertises non-fields: {sorted(unknown)}"


def test_check_applied_controls_refuses_an_unadvertised_id():
    with pytest.raises(ValueError, match="does not|not advertise"):
        check_applied_controls(CLAUDE_CAPABILITIES, ("invented-control",))


def test_check_applied_controls_passes_advertised_ids_through():
    assert check_applied_controls(
        CLAUDE_CAPABILITIES, ("permission-bypass",)
    ) == ("permission-bypass",)


def test_fixed_control_ids_selects_only_unconditional_controls():
    ids = fixed_control_ids(CODEX_CAPABILITIES)
    assert "skip-git-repo-check" in ids
    # network-enable is conditional on a caller value, so it is not fixed
    assert "network-enable" not in ids


# ---------------------------------------------------------------------------
# extras: three outcomes per key, one of which is NOT "dropped"
# ---------------------------------------------------------------------------


def test_extras_key_the_adapter_reads_is_reported_nowhere():
    """codex advertises extras.output_schema, so honoring it is silent."""
    opts = BackendOptions(extras={"output_schema": "/tmp/s.json"})
    dropped, forwarded = derive_extras_report(CODEX_CAPABILITIES, opts)
    assert dropped == () and forwarded == ()


def test_codex_reports_only_the_extras_keys_it_does_not_read():
    opts = BackendOptions(extras={"sandbox": "read-only", "invented": 1})
    dropped, forwarded = derive_extras_report(CODEX_CAPABILITIES, opts)
    assert dropped == ("extras.invented",)
    assert forwarded == ()


def test_openrouter_extras_are_forwarded_not_dropped():
    """The one adapter that sends every key: calling these dropped would lie."""
    opts = BackendOptions(extras={"response_format": {}, "top_k": 40})
    dropped, forwarded = derive_extras_report(OPENROUTER_CAPABILITIES, opts)
    assert dropped == ()
    assert forwarded == ("extras.response_format", "extras.top_k")
    assert derive_dropped_params(OPENROUTER_CAPABILITIES, opts) == ()


@pytest.mark.parametrize(
    "caps", [CLAUDE_CAPABILITIES, OPENCODE_CAPABILITIES], ids=["claude", "opencode"]
)
def test_an_adapter_that_reads_no_extras_drops_every_key(caps):
    opts = BackendOptions(extras={"alpha": 1, "beta": 2})
    dropped, forwarded = derive_extras_report(caps, opts)
    assert dropped == ("extras.alpha", "extras.beta")
    assert forwarded == ()


def test_the_bare_word_extras_never_reaches_the_report():
    """The field name has no single answer on an adapter reading some keys."""
    opts = BackendOptions(extras={"alpha": 1})
    for caps in adapter_capabilities().values():
        assert "extras" not in derive_dropped_params(caps, opts)
        assert "extras" not in derive_forwarded_params(caps, opts)


def test_extras_verdicts_are_stable_across_calls_with_the_same_keys():
    """An extras key has no advertisement order to borrow, so it is sorted."""
    a = derive_dropped_params(
        CLAUDE_CAPABILITIES, BackendOptions(extras={"z": 1, "a": 2})
    )
    b = derive_dropped_params(
        CLAUDE_CAPABILITIES, BackendOptions(extras={"a": 2, "z": 1})
    )
    assert a == b == ("extras.a", "extras.z")


def test_empty_extras_reports_nothing():
    for caps in adapter_capabilities().values():
        assert derive_dropped_params(caps, BackendOptions(extras={})) == ()
        assert derive_forwarded_params(caps, BackendOptions(extras={})) == ()


def test_field_drops_and_extras_drops_are_both_reported():
    """The two derivations are separate rules feeding one list."""
    opts = BackendOptions(allowed_tools="Read", extras={"alpha": 1})
    reported = derive_dropped_params(OPENCODE_CAPABILITIES, opts)
    assert "allowed_tools" in reported
    assert "extras.alpha" in reported


# ---------------------------------------------------------------------------
# openrouter
# ---------------------------------------------------------------------------


class _FakeClient:
    def __init__(self):
        message = SimpleNamespace(content="hello", reasoning_content=None)
        choice = SimpleNamespace(message=message, finish_reason="stop")
        usage = SimpleNamespace(
            prompt_tokens=3, completion_tokens=2, prompt_tokens_details=None
        )
        self._response = SimpleNamespace(choices=[choice], usage=usage)
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create)
        )

    def _create(self, **kwargs):
        return self._response


def _openrouter() -> OpenRouterBackend:
    return OpenRouterBackend(client=_FakeClient())


def test_openrouter_reports_dropped_params_and_no_controls():
    resp = _openrouter().complete(
        "s", "u", model="m", options=BackendOptions(allowed_tools="Read")
    )
    assert "allowed_tools" in resp.dropped_params
    # an HTTP request has no sandbox, tool or permission surface to constrain
    assert resp.execution_controls_applied == ()


def test_openrouter_forwards_an_unrecognised_extra_unvalidated():
    resp = _openrouter().complete(
        "s", "u", model="m", options=BackendOptions(extras={"top_k": 40})
    )
    assert resp.forwarded_params == ("extras.top_k",)
    assert "extras.top_k" not in resp.dropped_params


def test_openrouter_brackets_the_call_with_iso_timestamps():
    resp = _openrouter().complete("s", "u", model="m")
    assert ISO_Z.match(resp.started_at or "")
    assert ISO_Z.match(resp.ended_at or "")
    assert resp.started_at <= resp.ended_at


# ---------------------------------------------------------------------------
# claude-cli
# ---------------------------------------------------------------------------


class _ClaudeRunner:
    def __init__(self, envelope: str):
        self.envelope = envelope
        self.calls = []

    def __call__(self, cmd, request, cwd, *, log_prefix, timeout_s):
        self.calls.append(list(cmd))
        return self.envelope, "", 0


def _claude(runner) -> ClaudeCliBackend:
    return ClaudeCliBackend(runner=runner, executable="claude")


def test_claude_reports_its_three_unconditional_controls():
    runner = _ClaudeRunner(json.dumps({"result": "hi", "is_error": False}))
    resp = _claude(runner).complete("s", "u", model="opus")
    assert resp.execution_controls_applied == (
        "allowed-tools",
        "permission-bypass",
        "no-session-persistence",
    )


def test_claude_reports_the_deny_control_only_when_it_emitted_one():
    """The conditional counterpart to allowed-tools below.

    An unset deny-list emits no flag, so reporting the control would assert
    something the argv does not contain.
    """
    runner = _ClaudeRunner(json.dumps({"result": "hi", "is_error": False}))
    plain = _claude(runner).complete("s", "u", model="opus")
    assert "disallowed-tools" not in plain.execution_controls_applied

    runner = _ClaudeRunner(json.dumps({"result": "hi", "is_error": False}))
    denied = _claude(runner).complete(
        "s", "u", model="opus", options=BackendOptions(disallowed_tools="Bash")
    )
    assert "disallowed-tools" in denied.execution_controls_applied
    assert "--disallowedTools" in runner.calls[0]


def test_claude_reports_the_system_prompt_mode_it_actually_emitted():
    """A mode is reported through the argv, never through the caller's word."""
    runner = _ClaudeRunner(json.dumps({"result": "hi", "is_error": False}))
    _claude(runner).complete(
        "s", "u", model="opus", options=BackendOptions(system_prompt_mode="append")
    )
    assert "--append-system-prompt" in runner.calls[0]
    assert "--system-prompt" not in runner.calls[0]
    # and the mode the caller chose is not reported as dropped
    assert "system_prompt_mode" not in _claude(
        _ClaudeRunner(json.dumps({"result": "hi", "is_error": False}))
    ).complete(
        "s", "u", model="opus", options=BackendOptions(system_prompt_mode="append")
    ).dropped_params


@pytest.mark.parametrize("param", ["disallowed_tools", "system_prompt_mode"])
def test_the_other_adapters_report_the_claude_only_params_as_dropped(param):
    """A field added to BackendOptions is dropped-by-default everywhere else."""
    # opencode-cli deliberately READS disallowed_tools: it is the neutral
    # tool-deny vocabulary, and opencode translates it into its permission
    # scalars so a deny floor can be honoured there too. Every other adapter
    # still drops it, which is what this test protects.
    others = [OPENROUTER_CAPABILITIES, CODEX_CAPABILITIES]
    if param != "disallowed_tools":
        others.append(OPENCODE_CAPABILITIES)
    for caps in others:
        assert param in caps.dropped_params, caps.adapter
    assert param not in CLAUDE_CAPABILITIES.dropped_params


def test_claude_reports_allowed_tools_even_when_the_caller_named_none():
    """An empty allow-list is still an emitted allow-list, not a suppression."""
    runner = _ClaudeRunner(json.dumps({"result": "hi", "is_error": False}))
    resp = _claude(runner).complete("s", "u", model="opus")
    assert "--allowedTools" in runner.calls[0]
    assert "allowed-tools" in resp.execution_controls_applied


def test_claude_reported_controls_are_all_advertised():
    runner = _ClaudeRunner(json.dumps({"result": "hi", "is_error": False}))
    resp = _claude(runner).complete("s", "u", model="opus")
    advertised = {c.id for c in CLAUDE_CAPABILITIES.execution_controls}
    assert set(resp.execution_controls_applied) <= advertised


def test_claude_reports_the_param_it_drops():
    runner = _ClaudeRunner(json.dumps({"result": "hi", "is_error": False}))
    resp = _claude(runner).complete(
        "s", "u", model="opus", options=BackendOptions(cache_salt=4)
    )
    assert "cache_salt" in resp.dropped_params


def test_claude_reports_each_extras_key_it_drops():
    runner = _ClaudeRunner(json.dumps({"result": "hi", "is_error": False}))
    resp = _claude(runner).complete(
        "s", "u", model="opus", options=BackendOptions(extras={"output_schema": "/s"})
    )
    # named per key, not as the bare field: the caller needs to know WHICH key
    assert "extras.output_schema" in resp.dropped_params
    assert resp.forwarded_params == ()


def test_claude_run_once_reports_one_attempt():
    runner = _ClaudeRunner(json.dumps({"result": "hi", "is_error": False}))
    resp = _claude(runner).complete("s", "u", model="opus")
    assert resp.attempts == 1
    assert len(runner.calls) == 1


def test_claude_surviving_transient_envelope_is_not_an_empty_success():
    """Under run-once a transient 5xx must fail, not return empty text.

    The retry loop used to absorb it; with the budget at 1 it would otherwise
    fall through and be reported as a completed call with no content.
    """
    runner = _ClaudeRunner(json.dumps({"result": "", "api_error_status": 503}))
    with pytest.raises(RuntimeError) as excinfo:
        _claude(runner).complete("s", "u", model="opus")
    # the canonical form the halt matchers read
    assert '"api_error_status":503' in str(excinfo.value)


# ---------------------------------------------------------------------------
# codex-cli
# ---------------------------------------------------------------------------


class _CodexRunner:
    def __init__(self, output_text="codex-answer"):
        self.output_text = output_text
        self.calls = []

    def __call__(self, cmd, request, cwd, **kwargs):
        self.calls.append(list(cmd))
        Path(cmd[cmd.index("-o") + 1]).write_text(self.output_text, encoding="utf-8")
        return "", "", 0


def _codex(runner) -> CodexCliBackend:
    return CodexCliBackend(runner=runner, argv_prefix=("codex",))


def test_codex_reports_controls_read_off_the_built_argv(tmp_path: Path):
    runner = _CodexRunner()
    resp = _codex(runner).complete(
        "s", "u", model="m", options=BackendOptions(cwd=tmp_path.resolve())
    )
    argv = runner.calls[0]
    for control_id, marker in (
        ("sandbox-mode", "-s"),
        ("skip-git-repo-check", "--skip-git-repo-check"),
    ):
        assert marker in argv
        assert control_id in resp.execution_controls_applied


def test_codex_network_control_is_reported_only_when_the_flag_is_emitted(
    tmp_path: Path,
):
    runner = _CodexRunner()
    off = _codex(runner).complete(
        "s", "u", model="m",
        options=BackendOptions(cwd=tmp_path.resolve(), extras={"network": False}),
    )
    # network=False emits NOTHING, and the absence of a flag is not a control
    assert "network-enable" not in off.execution_controls_applied

    runner = _CodexRunner()
    on = _codex(runner).complete(
        "s", "u", model="m",
        options=BackendOptions(cwd=tmp_path.resolve(), extras={"network": True}),
    )
    assert "network-enable" in on.execution_controls_applied


def test_codex_parses_structured_output_only_under_a_caller_schema(tmp_path: Path):
    schema = tmp_path / "schema.json"
    schema.write_text("{}", encoding="utf-8")
    payload = '{"answer": 42}'

    unschemed = _codex(_CodexRunner(payload)).complete(
        "s", "u", model="m", options=BackendOptions(cwd=tmp_path.resolve())
    )
    # valid JSON the model produced unbidden is NOT schema-backed output
    assert unschemed.structured is None
    assert unschemed.text == payload

    schemed = _codex(_CodexRunner(payload)).complete(
        "s", "u", model="m",
        options=BackendOptions(
            cwd=tmp_path.resolve(), extras={"output_schema": schema}
        ),
    )
    assert schemed.structured == {"answer": 42}
    assert schemed.text == payload


def test_codex_unparseable_schema_result_is_none_not_a_failure(tmp_path: Path):
    schema = tmp_path / "schema.json"
    schema.write_text("{}", encoding="utf-8")
    resp = _codex(_CodexRunner("not json at all")).complete(
        "s", "u", model="m",
        options=BackendOptions(
            cwd=tmp_path.resolve(), extras={"output_schema": schema}
        ),
    )
    assert resp.structured is None
    assert resp.text == "not json at all"


def test_codex_reports_an_unrecognised_extra_while_honoring_a_known_one(
    tmp_path: Path,
):
    schema = tmp_path / "s.json"
    schema.write_text("{}", encoding="utf-8")
    resp = _codex(_CodexRunner('{"a": 1}')).complete(
        "s", "u", model="m",
        options=BackendOptions(
            cwd=tmp_path.resolve(),
            extras={"output_schema": str(schema), "invented": True},
        ),
    )
    assert resp.dropped_params == ("extras.invented",)
    assert resp.forwarded_params == ()


def test_codex_advertises_parsed_now_that_the_field_exists():
    assert CODEX_CAPABILITIES.structured_output.result == "parsed"


# ---------------------------------------------------------------------------
# opencode-cli
# ---------------------------------------------------------------------------


class _OpencodeRunner:
    def __call__(self, cmd, request, cwd, **kwargs):
        return "answer", "", 0


def _opencode(monkeypatch) -> OpencodeCliBackend:
    from llm_scripting_kit.completion import opencode_backend

    monkeypatch.setattr(
        opencode_backend.OpencodeAdapter,
        "build_invocation",
        lambda self, *a, **kw: HarnessInvocation(argv=("opencode",), stdin="brief"),
    )
    return OpencodeCliBackend(runner=_OpencodeRunner())


def test_opencode_reports_its_advertised_fixed_controls(monkeypatch, tmp_path: Path):
    resp = _opencode(monkeypatch).complete(
        "s", "u", model="p/m", options=BackendOptions(cwd=tmp_path.resolve())
    )
    assert set(resp.execution_controls_applied) == set(
        fixed_control_ids(OPENCODE_CAPABILITIES)
    )
    assert "permission-task-deny" in resp.execution_controls_applied


def test_opencode_reports_the_param_it_drops(monkeypatch, tmp_path: Path):
    resp = _opencode(monkeypatch).complete(
        "s", "u", model="p/m",
        options=BackendOptions(cwd=tmp_path.resolve(), allowed_tools="Read"),
    )
    assert "allowed_tools" in resp.dropped_params


def test_opencode_reports_each_extras_key_it_drops(monkeypatch, tmp_path: Path):
    resp = _opencode(monkeypatch).complete(
        "s", "u", model="p/m",
        options=BackendOptions(cwd=tmp_path.resolve(), extras={"variant": "x"}),
    )
    assert "extras.variant" in resp.dropped_params
    assert resp.forwarded_params == ()


# ---------------------------------------------------------------------------
# The response type itself
# ---------------------------------------------------------------------------


def test_a_response_is_completed_and_error_free_by_default():
    resp = LLMResponse(text="x", model="m")
    assert resp.status == COMPLETED
    assert resp.error is None
    assert resp.dropped_params == ()
    assert resp.forwarded_params == ()
    assert resp.execution_controls_applied == ()
    assert resp.structured is None
    assert resp.started_at is None and resp.ended_at is None


@pytest.mark.parametrize("status", [TIMEOUT, ERROR])
def test_a_failure_carries_its_detail_as_data(status):
    resp = LLMResponse(
        text="", model="m", status=status,
        error=ResponseError(code="halt_rate_limit", message="hit your limit"),
    )
    assert resp.status == status
    assert resp.error.to_json() == {
        "code": "halt_rate_limit", "message": "hit your limit"
    }


def test_every_adapter_reports_the_same_truthfulness_surface():
    """No adapter may quietly omit a field the contract promises."""
    for field in (
        "status", "error", "dropped_params", "forwarded_params",
        "execution_controls_applied",
        "structured", "started_at", "ended_at",
    ):
        assert field in LLMResponse.__dataclass_fields__
