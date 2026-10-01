"""PR2: the per-call ``on_attempt`` observer on call_llm / submit_validated."""

import dataclasses

import pytest

from content_pipeline.llm.backends import MockBackend
from content_pipeline.llm.platform import (
    BackendOptions,
    CallAttempt,
    CostBudget,
    LLMResponse,
    StructuralOutputError,
    call_llm,
    submit_validated,
)

PRICING = {"m": {"input": 1.0, "output": 1.0}}


def _parse_ok(text):
    if not text.startswith("ok"):
        raise ValueError("not ok")
    return text


def test_on_attempt_sees_retry_prompt_and_salt():
    seen = []
    backend = MockBackend(responses=["bad", "ok"])
    result = submit_validated(
        backend=backend, system="S", user="U", model="m", parse_fn=_parse_ok,
        on_attempt=seen.append,
    )
    assert result.accepted
    assert [(a.validation_attempt, a.transport_attempt) for a in seen] == [(1, 1), (2, 1)]
    assert seen[0].user == "U" and seen[0].cache_salt == 0
    assert seen[1].user != "U" and seen[1].user.startswith("U")
    assert seen[1].cache_salt == 1
    assert seen[1].system == "S" and seen[1].model == "m"
    assert seen[1].max_tokens == 4096


def test_on_attempt_sees_cache_hit_as_transport_attempt_1(tmp_path):
    backend = MockBackend(responses=["hello"])
    call_llm(backend, "s", "u", model="m", cache_dir=tmp_path)
    seen = []
    resp = call_llm(backend, "s", "u", model="m", cache_dir=tmp_path, on_attempt=seen.append)
    assert resp.from_cache
    assert len(seen) == 1
    assert seen[0].transport_attempt == 1
    assert seen[0].response.from_cache is True


def test_on_attempt_sees_transport_exception_then_retry():
    seen = []
    backend = MockBackend(responses=[ValueError("boom"), "fine"])
    call_llm(backend, "s", "u", model="m", retries=1, on_attempt=seen.append)
    assert [a.transport_attempt for a in seen] == [1, 2]
    assert seen[0].response is None
    assert seen[0].error == "ValueError: boom"
    assert seen[1].response.text == "fine" and seen[1].error is None


def test_observer_raise_keeps_original_exception():
    class ObserverBoom(Exception):
        pass

    def observer(_):
        raise ObserverBoom("observer")

    backend = MockBackend(responses=[ValueError("orig")])
    with pytest.raises(ValueError, match="orig") as info:
        call_llm(backend, "s", "u", model="m", on_attempt=observer)
    assert isinstance(info.value.__cause__, ObserverBoom)
    assert len(backend.calls) == 1


def test_observer_error_not_charged_or_retried():
    def observer(_):
        raise RuntimeError("observer")

    budget = CostBudget(limit=100.0)
    backend = MockBackend(responses=[LLMResponse(text="x", model="m"), "second"])
    with pytest.raises(RuntimeError, match="observer"):
        call_llm(backend, "s", "u", model="m", retries=2, on_attempt=observer,
                 pricing=PRICING, cost_budget=budget)
    assert len(backend.calls) == 1  # observer failure was not retried as transport


def test_structural_error_attempt_carries_response():
    failed = LLMResponse(text="{bad", model="m")
    seen = []
    backend = MockBackend(responses=[StructuralOutputError(failed)])
    with pytest.raises(StructuralOutputError):
        call_llm(backend, "s", "u", model="m", on_attempt=seen.append)
    assert len(seen) == 1
    assert seen[0].response is failed
    assert seen[0].error.startswith("StructuralOutputError")


def test_final_attempt_reported_with_rejections():
    seen = []
    backend = MockBackend(responses=["bad", "ok"])
    submit_validated(
        backend=backend, system="s", user="u", model="m", parse_fn=_parse_ok,
        on_attempt=seen.append,
    )
    assert seen[0].rejections == ("parse_error",)
    assert seen[1].rejections == ()


def test_final_transport_attempt_only_gets_rejections():
    seen = []
    backend = MockBackend(responses=[ValueError("t"), "bad"])
    submit_validated(
        backend=backend, system="s", user="u", model="m", parse_fn=_parse_ok,
        max_attempts=1, retries=1, on_attempt=seen.append,
    )
    assert [a.transport_attempt for a in seen] == [1, 2]
    assert seen[0].rejections == ()
    assert seen[1].rejections == ("parse_error",)
    assert all(a.validation_attempt == 1 for a in seen)


def test_no_observer_results_identical():
    def run(observer):
        backend = MockBackend(responses=["bad", "ok"])
        r = submit_validated(
            backend=backend, system="s", user="u", model="m", parse_fn=_parse_ok,
            on_attempt=observer,
        )
        return r.payload, [x.detail for x in r.rejections], r.attempts, backend.calls

    assert run(None) == run(lambda a: None)


def test_call_attempt_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        a = CallAttempt("i", 1, 1, "s", "u", "m", None, 1, 0, None, None, None)
        a.user = "x"  # type: ignore[misc]
