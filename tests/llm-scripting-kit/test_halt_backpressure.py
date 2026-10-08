"""Backpressure halt kind: transient overload, distinct from quota halts."""
import pytest

from llm_scripting_kit.completion import halt


class _Resp:
    def __init__(self, status, headers=None):
        self.status_code = status
        self.headers = headers or {}


class _Err(Exception):
    def __init__(self, msg, status=None, headers=None):
        super().__init__(msg)
        if status is not None:
            self.status_code = status
            self.response = _Resp(status, headers)


def test_kind_constant():
    assert halt.HALT_BACKPRESSURE == "backpressure"
    assert halt.HALT_BACKPRESSURE not in (
        halt.HALT_RATE_LIMIT, halt.HALT_QUOTA, halt.HALT_INSUFFICIENT_CREDIT, halt.HALT_AUTH
    )


def test_503_request_queue_timeout_from_sdk_text():
    exc = RuntimeError("Error code: 503 - {'error': {'code': 'request_queue_timeout'}}")
    assert halt.classify_openai_exception(exc) == halt.HALT_BACKPRESSURE
    assert halt.classify_backpressure(exc).retry_after_s is None


def test_bare_503_is_not_backpressure():
    assert halt.classify_backpressure(_Err("service unavailable", 503)) is None
    assert halt.classify_openai_exception(_Err("service unavailable", 503)) is None


def test_503_overload_wording():
    assert halt.classify_backpressure(_Err("server overloaded, try later", 503)).status == 503


def test_429_with_retry_after_header():
    exc = _Err("Too Many Requests", 429, {"retry-after": "7"})
    signal = halt.classify_backpressure(exc)
    assert signal.status == 429 and signal.retry_after_s == 7.0
    assert halt.classify_openai_exception(exc) == halt.HALT_BACKPRESSURE


def test_429_body_hint_seconds_and_ms():
    assert halt.classify_backpressure(_Err("Error code: 429 - try again in 2.5s")).retry_after_s == 2.5
    assert halt.classify_backpressure(_Err("Error code: 429 - try again in 500ms")).retry_after_s == 0.5


@pytest.mark.parametrize(
    "msg",
    [
        "Error code: 429 - {'error': {'code': 'insufficient_quota'}}",
        "Error code: 429 - You exceeded your current quota, check billing",
        "Error code: 429 - credit exhausted, retry-after 5",
    ],
)
def test_quota_429_is_not_backpressure(msg):
    assert halt.classify_backpressure(RuntimeError(msg)) is None
    assert halt.classify_openai_exception(RuntimeError(msg)) != halt.HALT_BACKPRESSURE


def test_bare_429_keeps_rate_limit_via_text_marker():
    exc = RuntimeError('claude failed "api_error_status":429')
    assert halt.classify_backpressure(exc) is None
    assert halt.classify_openai_exception(exc) == halt.HALT_RATE_LIMIT


def test_cause_chain_is_classified():
    outer = RuntimeError("call failed")
    outer.__cause__ = _Err("queue full", 503)
    assert halt.classify_openai_exception(outer) == halt.HALT_BACKPRESSURE


def test_unrelated_status_and_unparseable_retry_after():
    assert halt.classify_backpressure(_Err("boom", 500)) is None
    exc = _Err("429 {'code': 'rate_limit_exceeded'}", 429, {"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"})
    assert halt.classify_backpressure(exc).retry_after_s is None


def test_halt_payload_shape():
    exc = _Err("Too Many Requests", 429, {"retry-after": "3"})
    assert halt.halt_payload(halt.HALT_BACKPRESSURE, exc) == {
        "kind": "backpressure", "retry_after_s": 3.0,
    }
    assert halt.halt_payload(halt.HALT_RATE_LIMIT, exc) == {"kind": "rate_limit"}


def test_halt_error_carries_retry_after():
    err = halt.HaltError(halt.HALT_BACKPRESSURE, "busy", retry_after_s=4.0)
    assert err.kind == "backpressure" and err.retry_after_s == 4.0
    assert halt.HaltError(halt.HALT_AUTH).retry_after_s is None


def test_halt_payload_prefers_the_halt_errors_own_retry_after():
    exc = halt.HaltError(halt.HALT_BACKPRESSURE, "busy", retry_after_s=5.0)
    assert halt.halt_payload(halt.HALT_BACKPRESSURE, exc) == {
        "kind": "backpressure", "retry_after_s": 5.0,
    }
    bare = halt.HaltError(halt.HALT_BACKPRESSURE, "busy")
    assert halt.halt_payload(halt.HALT_BACKPRESSURE, bare)["retry_after_s"] is None
