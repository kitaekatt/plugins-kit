"""Behavioral tests for content_pipeline.llm.backends.

Covers the mock seam, the ClaudeCliBackend (subprocess boundary faked via the
``runner`` seam -- no real spawn), the OpenRouterBackend (message shape via an
injected fake client -- no openrouter_kit / openai needed), and process-level
routing. Translates loc's routing cases and firstpass's agent_io retry /
hard-stop cases.
"""

import json

import pytest

from content_pipeline.llm import backends
from content_pipeline.llm.backends import (
    ClaudeCliBackend,
    ClaudeCliError,
    ClaudeCliTimeout,
    MockBackend,
    OpenRouterBackend,
    active_backend_name,
    route,
    routed_model,
    set_active_backend,
)
from content_pipeline.llm.platform import BackendOptions, HALT_RATE_LIMIT


# --- MockBackend -------------------------------------------------------------


def test_mock_serves_in_order():
    backend = MockBackend(responses=["a", "b"])
    assert backend.complete("s", "u", model="m").text == "a"
    assert backend.complete("s", "u", model="m").text == "b"


def test_mock_records_calls():
    backend = MockBackend(responses=["a"])
    backend.complete("sys", "usr", model="m1", options=BackendOptions(temperature=0.7))
    assert backend.calls[-1]["system"] == "sys"
    assert backend.calls[-1]["user"] == "usr"
    assert backend.calls[-1]["model"] == "m1"


def test_mock_raises_on_exhaustion():
    backend = MockBackend(responses=[])
    with pytest.raises(RuntimeError, match="exhausted"):
        backend.complete("s", "u", model="m")


def test_mock_dict_entry_carries_usage():
    backend = MockBackend(responses=[{"text": "x", "input_tokens": 12, "output_tokens": 3}])
    resp = backend.complete("s", "u", model="m")
    assert resp.input_tokens == 12
    assert resp.output_tokens == 3


def test_mock_keyed_responses_content_addressed():
    backend = MockBackend(keyed_responses={"alpha": "A", "beta": "B"})
    assert backend.complete("s", "please do beta now", model="m").text == "B"
    assert backend.complete("s", "and alpha too", model="m").text == "A"


def test_mock_keyed_no_match_raises():
    backend = MockBackend(keyed_responses={"alpha": "A"})
    with pytest.raises(RuntimeError, match="no key matched"):
        backend.complete("s", "nothing here", model="m")


def test_mock_exception_entry_is_raised():
    backend = MockBackend(responses=[ValueError("boom")])
    with pytest.raises(ValueError, match="boom"):
        backend.complete("s", "u", model="m")


def test_mock_default_model_when_blank():
    backend = MockBackend(responses=["x"], default_model="mm")
    assert backend.complete("s", "u", model="").model == "mm"


# --- ClaudeCliBackend (runner seam) ------------------------------------------


def _envelope(result="ok", *, is_error=False, status=None, usage=None):
    data = {"type": "result", "is_error": is_error, "result": result}
    if status is not None:
        data["api_error_status"] = status
    if usage is not None:
        data["usage"] = usage
    return json.dumps(data)


def _runner_returning(*sequence):
    """Build a runner that yields (stdout, stderr, rc) tuples in sequence."""
    state = {"i": 0, "calls": []}

    def runner(cmd, request, cwd, *, timeout_s):
        state["calls"].append((cmd, request, timeout_s))
        idx = state["i"]
        state["i"] += 1
        return sequence[idx]

    runner.state = state  # type: ignore[attr-defined]
    return runner


def test_claude_cli_happy_path():
    runner = _runner_returning((_envelope("hello", usage={"input_tokens": 5, "output_tokens": 2}), "", 0))
    backend = ClaudeCliBackend(runner=runner)
    resp = backend.complete("sys", "usr", model="claude-x")
    assert resp.text == "hello"
    assert resp.model == "claude-x"
    assert resp.input_tokens == 5
    assert resp.attempts == 1


def test_claude_cli_retries_transient_500_then_succeeds(monkeypatch):
    monkeypatch.setattr(backends.time, "sleep", lambda *_: None)
    runner = _runner_returning(
        (_envelope("err", is_error=True, status=500), "s", 1),
        (_envelope("recovered"), "", 0),
    )
    backend = ClaudeCliBackend(runner=runner, retry_max_attempts=3)
    resp = backend.complete("s", "u", model="claude-x")
    assert resp.text == "recovered"
    assert resp.attempts == 2


def test_claude_cli_hard_stop_429_does_not_retry(monkeypatch):
    monkeypatch.setattr(backends.time, "sleep", lambda *_: None)
    runner = _runner_returning((_envelope("You've hit your limit.", is_error=True, status=429), "", 0))
    backend = ClaudeCliBackend(runner=runner, retry_max_attempts=3)
    with pytest.raises(ClaudeCliError, match="hard-stop"):
        backend.complete("s", "u", model="claude-x")
    assert runner.state["i"] == 1  # only one attempt


def test_claude_cli_nonzero_exit_raises():
    runner = _runner_returning(("", "boom", 1))
    with pytest.raises(ClaudeCliError, match="exit 1"):
        ClaudeCliBackend(runner=runner).complete("s", "u", model="claude-x")


def test_claude_cli_is_error_envelope_raises():
    runner = _runner_returning((_envelope("validator rejected", is_error=True), "", 0))
    with pytest.raises(ClaudeCliError, match="returned error"):
        ClaudeCliBackend(runner=runner).complete("s", "u", model="claude-x")


def test_claude_cli_timeout_classifies_as_rate_limit():
    def runner(cmd, request, cwd, *, timeout_s):
        raise ClaudeCliTimeout("claude -p exceeded 1s timeout")

    backend = ClaudeCliBackend(runner=runner)
    with pytest.raises(ClaudeCliTimeout):
        backend.complete("s", "u", model="claude-x")
    assert backend.classify_halt(ClaudeCliTimeout("x")) == HALT_RATE_LIMIT


def test_claude_cli_passes_options_into_cmd():
    runner = _runner_returning((_envelope("ok"), "", 0))
    backend = ClaudeCliBackend(runner=runner)
    backend.complete("s", "u", model="claude-x", options=BackendOptions(effort="high", timeout_s=5))
    cmd, _request, timeout_s = runner.state["calls"][0]
    assert "--effort" in cmd and "high" in cmd
    assert timeout_s == 5


# --- OpenRouterBackend (injected fake client) --------------------------------


class _FakeUsage:
    def __init__(self):
        self.prompt_tokens = 100
        self.completion_tokens = 20
        self.prompt_tokens_details = {"cached_tokens": 40}


class _FakeMessage:
    content = "router-response"


class _FakeChoice:
    message = _FakeMessage()


class _FakeResponse:
    choices = [_FakeChoice()]
    usage = _FakeUsage()


class _FakeCompletions:
    def __init__(self, sink):
        self._sink = sink

    def create(self, **kwargs):
        self._sink.append(kwargs)
        return _FakeResponse()


class _FakeChat:
    def __init__(self, sink):
        self.completions = _FakeCompletions(sink)


class _FakeClient:
    def __init__(self):
        self.sink = []
        self.chat = _FakeChat(self.sink)


def test_openrouter_backend_parses_response_and_cache_hit_tokens():
    client = _FakeClient()
    backend = OpenRouterBackend(client=client)
    resp = backend.complete("sys", "usr", model="test/slug")
    assert resp.text == "router-response"
    assert resp.input_tokens == 100
    assert resp.output_tokens == 20
    assert resp.cache_hit_tokens == 40
    assert resp.model == "test/slug"  # concrete slug used verbatim


def test_openrouter_backend_marks_system_cache_control():
    client = _FakeClient()
    OpenRouterBackend(client=client).complete("SYSTEM", "USER", model="test/slug")
    messages = client.sink[0]["messages"]
    system_msg = messages[0]
    assert system_msg["role"] == "system"
    assert isinstance(system_msg["content"], list)
    assert system_msg["content"][0]["cache_control"] == {"type": "ephemeral"}
    # Plain user message when no prefix.
    assert messages[1]["content"] == "USER"


def test_openrouter_backend_user_prefix_two_part_content():
    client = _FakeClient()
    OpenRouterBackend(client=client).complete(
        "s", "u", model="test/slug", options=BackendOptions(user_cache_prefix="PREAMBLE")
    )
    user_msg = client.sink[0]["messages"][1]
    assert isinstance(user_msg["content"], list)
    assert user_msg["content"][0]["text"] == "PREAMBLE"
    assert user_msg["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert user_msg["content"][1]["text"] == "u"


# --- routing -----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_backend_env(monkeypatch):
    monkeypatch.delenv(backends.BACKEND_ENV, raising=False)
    monkeypatch.delenv(backends.MODEL_ENV, raising=False)
    yield


def test_routing_default_is_openrouter():
    assert active_backend_name() == "openrouter"
    assert isinstance(route(), OpenRouterBackend)


def test_routing_set_and_clear():
    set_active_backend("claude-cli")
    assert active_backend_name() == "claude-cli"
    set_active_backend(None)
    assert active_backend_name() == "openrouter"


def test_routing_returns_active_backend():
    set_active_backend("claude-cli")
    assert isinstance(route(), ClaudeCliBackend)
    set_active_backend("mock")
    assert isinstance(route(), MockBackend)


def test_routing_injected_instance_wins():
    set_active_backend("mock")
    mine = MockBackend(responses=["x"])
    assert route(mock=mine) is mine


def test_routed_model_substitutes_for_claude(monkeypatch):
    set_active_backend("claude-cli")
    monkeypatch.setenv(backends.MODEL_ENV, "claude-sonnet-4-6")
    # An OpenRouter-style id is substituted.
    assert routed_model("deepseek/deepseek-v4") == "claude-sonnet-4-6"
    # A caller-passed claude id wins (no substitution).
    assert routed_model("claude-opus") == "claude-opus"


def test_routed_model_no_substitution_for_openrouter():
    assert routed_model("deepseek/deepseek-v4") == "deepseek/deepseek-v4"


def test_openrouter_backend_requires_lib_or_client():
    # No client, no openrouter_kit installed in the test env: complete must
    # raise a clear ImportError rather than a cryptic failure.
    backend = OpenRouterBackend()
    try:
        import openrouter_kit  # noqa: F401
        has_lib = True
    except ImportError:
        has_lib = False
    if not has_lib:
        with pytest.raises(ImportError, match="openrouter_kit"):
            backend.complete("s", "u", model="x")
    else:  # pragma: no cover - depends on env
        pytest.skip("openrouter_kit importable; injection path not exercised here")
