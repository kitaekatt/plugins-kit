"""Tests for openrouter_kit.completion.halt -- the shared halt taxonomy."""
from __future__ import annotations

import sys
import types

import pytest

from openrouter_kit.completion import halt
from openrouter_kit.completion.claude_runner import AgentTimeoutError


class TestClassifyHaltText:
    """Substring matcher must accept BOTH historical marker shapes."""

    def test_loc_shape_429(self):
        assert halt.classify_halt_text("blah api_error_status:429 blah") == halt.HALT_RATE_LIMIT

    def test_firstpass_shape_429(self):
        assert halt.classify_halt_text('x "api_error_status":429 y') == halt.HALT_RATE_LIMIT

    def test_hit_your_limit(self):
        assert halt.classify_halt_text("You have HIT YOUR LIMIT for today") == halt.HALT_RATE_LIMIT

    def test_loc_shape_401(self):
        assert halt.classify_halt_text("api_error_status:401") == halt.HALT_AUTH

    def test_firstpass_shape_401(self):
        assert halt.classify_halt_text('"api_error_status":401') == halt.HALT_AUTH

    def test_authentication_error_string(self):
        assert halt.classify_halt_text("...authentication_error...") == halt.HALT_AUTH

    def test_invalid_credentials_string(self):
        assert halt.classify_halt_text("Invalid Authentication Credentials") == halt.HALT_AUTH

    def test_rate_limit_wins_over_auth(self):
        text = 'hit your limit and also "api_error_status":401'
        assert halt.classify_halt_text(text) == halt.HALT_RATE_LIMIT

    def test_empty_and_unrelated(self):
        assert halt.classify_halt_text("") is None
        assert halt.classify_halt_text("all fine here") is None


class TestHaltError:
    def test_carries_kind_and_detail(self):
        exc = halt.HaltError(halt.HALT_RATE_LIMIT, "429 detail")
        assert exc.kind == halt.HALT_RATE_LIMIT
        assert exc.detail == "429 detail"
        assert "rate_limit" in str(exc) and "429 detail" in str(exc)

    def test_kind_only_message(self):
        exc = halt.HaltError(halt.HALT_AUTH)
        assert str(exc) == "auth"


class TestClassifyClaudeException:
    def test_agent_timeout_is_rate_limit(self):
        exc = AgentTimeoutError(
            "timed out", cmd=["claude"], elapsed_s=901, stdout="", stderr="",
        )
        assert halt.classify_claude_exception(exc) == halt.HALT_RATE_LIMIT

    def test_runtime_error_with_marker(self):
        exc = RuntimeError('claude -p hard-stop error (api_error_status":429): hit your limit')
        assert halt.classify_claude_exception(exc) == halt.HALT_RATE_LIMIT

    def test_exceeded_timeout_fallback(self):
        exc = RuntimeError("claude -p exceeded 900s timeout (elapsed 901s)")
        assert halt.classify_claude_exception(exc) == halt.HALT_RATE_LIMIT

    def test_unknown_is_none(self):
        assert halt.classify_claude_exception(ValueError("boom")) is None


def _stub_openai_module() -> types.ModuleType:
    """A stand-in openai module with the exception classes the classifier
    isinstance-checks. classify_openai_exception does ``import openai`` inside
    the function body, so a sys.modules entry is enough to steer it."""
    mod = types.ModuleType("openai")

    class AuthenticationError(Exception):
        pass

    class RateLimitError(Exception):
        pass

    class APIStatusError(Exception):
        def __init__(self, msg: str, status_code: int) -> None:
            super().__init__(msg)
            self.status_code = status_code

    mod.AuthenticationError = AuthenticationError
    mod.RateLimitError = RateLimitError
    mod.APIStatusError = APIStatusError
    return mod


class TestClassifyOpenAIException:
    @pytest.fixture()
    def stub_openai(self, monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
        mod = _stub_openai_module()
        monkeypatch.setitem(sys.modules, "openai", mod)
        return mod

    def test_auth(self, stub_openai):
        assert halt.classify_openai_exception(stub_openai.AuthenticationError("401")) == halt.HALT_AUTH

    def test_rate_limit(self, stub_openai):
        assert halt.classify_openai_exception(stub_openai.RateLimitError("429")) == halt.HALT_RATE_LIMIT

    def test_insufficient_credit_402(self, stub_openai):
        exc = stub_openai.APIStatusError("payment required", status_code=402)
        assert halt.classify_openai_exception(exc) == halt.HALT_INSUFFICIENT_CREDIT

    def test_suspended_account_403(self, stub_openai):
        exc = stub_openai.APIStatusError("forbidden", status_code=403)
        assert halt.classify_openai_exception(exc) == halt.HALT_INSUFFICIENT_CREDIT

    def test_other_status_is_none(self, stub_openai):
        exc = stub_openai.APIStatusError("server error", status_code=500)
        assert halt.classify_openai_exception(exc) is None

    def test_cause_chain_unwrap(self, stub_openai):
        inner = stub_openai.RateLimitError("429")
        outer = RuntimeError("OpenRouter call failed")
        outer.__cause__ = inner
        assert halt.classify_openai_exception(outer) == halt.HALT_RATE_LIMIT

    def test_unknown_is_none(self, stub_openai):
        assert halt.classify_openai_exception(ValueError("boom")) is None
