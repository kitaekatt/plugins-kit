"""Behavioral tests for content_pipeline.llm.platform.

Pins cache, cost (hard-fail on unknown model), budget, and validate-until-valid
loop behavior on the generic surface.
Everything runs on MockBackend -- no network, no subprocess.
"""

import pytest

from content_pipeline.llm import platform
from content_pipeline.llm.backends import MockBackend
from content_pipeline.llm.platform import (
    BackendOptions,
    BudgetExceededError,
    CostBudget,
    HaltError,
    LLMResponse,
    ResponseCache,
    build_cache_key,
    call_llm,
    check_request_fits,
    estimate_cost,
    estimate_request_tokens,
    response_cost,
    submit_validated,
)
from content_pipeline.validate.contract import Rejection, Severity

PRICING = {
    "test/model": {"input": 0.30, "cache_hit": 0.10, "output": 1.20, "alias": "tm"},
    "no-cache/model": {"input": 1.00, "output": 2.00},
}


# --- cache key ---------------------------------------------------------------


def test_cache_key_is_deterministic():
    a = build_cache_key(backend="mock", model="m", system="s", user="u")
    b = build_cache_key(backend="mock", model="m", system="s", user="u")
    assert a == b


def test_cache_key_differs_per_field():
    base = build_cache_key(backend="mock", model="m", system="s", user="u")
    assert base != build_cache_key(backend="mock", model="m2", system="s", user="u")
    assert base != build_cache_key(backend="mock", model="m", system="s2", user="u")
    assert base != build_cache_key(backend="mock", model="m", system="s", user="u2")
    assert base != build_cache_key(backend="other", model="m", system="s", user="u")


def test_cache_key_user_prefix_participates():
    plain = build_cache_key(backend="mock", model="m", system="s", user="u")
    with_prefix = build_cache_key(
        backend="mock",
        model="m",
        system="s",
        user="u",
        options=BackendOptions(user_cache_prefix="PRE"),
    )
    assert plain != with_prefix
    # Empty prefix is byte-identical to no prefix.
    assert plain == build_cache_key(
        backend="mock",
        model="m",
        system="s",
        user="u",
        options=BackendOptions(user_cache_prefix=""),
    )


def test_cache_key_is_whitespace_sensitive():
    a = build_cache_key(backend="mock", model="m", system="s", user="hi")
    b = build_cache_key(backend="mock", model="m", system="s", user="hi\n")
    assert a != b


def test_cache_key_salt_only_participates_when_set():
    plain = build_cache_key(backend="mock", model="m", system="s", user="u")
    assert plain == build_cache_key(
        backend="mock", model="m", system="s", user="u",
        options=BackendOptions(cache_salt=0),
    )
    assert plain != build_cache_key(
        backend="mock", model="m", system="s", user="u",
        options=BackendOptions(cache_salt=1),
    )


# --- ResponseCache -----------------------------------------------------------


def test_cache_roundtrip(tmp_path):
    cache = ResponseCache(tmp_path)
    resp = LLMResponse(text="hello", model="m", input_tokens=3, output_tokens=2, wall_ms=42)
    assert cache.store("k", resp) is True
    got = cache.lookup("k")
    assert got is not None
    assert got.text == "hello"
    assert got.from_cache is True
    assert got.wall_ms == 42  # original wall time preserved


def test_cache_miss_returns_none(tmp_path):
    assert ResponseCache(tmp_path).lookup("absent") is None


def test_cache_does_not_store_empty(tmp_path):
    cache = ResponseCache(tmp_path)
    assert cache.store("k", LLMResponse(text="", model="m")) is False
    assert cache.store("k2", LLMResponse(text="   \n ", model="m")) is False
    assert cache.lookup("k") is None
    assert cache.lookup("k2") is None


def test_call_llm_caches_first_and_hits_second(tmp_path):
    backend = MockBackend(responses=["first"])
    r1 = call_llm(backend, "s", "u", model="test/model", cache_dir=tmp_path)
    assert r1.from_cache is False
    # Backend has no more scripted responses; a second call must hit the cache.
    r2 = call_llm(backend, "s", "u", model="test/model", cache_dir=tmp_path)
    assert r2.from_cache is True
    assert r2.text == "first"
    assert len(backend.calls) == 1  # backend only called once


def test_call_llm_mock_without_cache_dir_skips_cache(tmp_path):
    backend = MockBackend(responses=["a", "b"])
    r1 = call_llm(backend, "s", "u", model="test/model")
    r2 = call_llm(backend, "s", "u", model="test/model")
    assert (r1.text, r2.text) == ("a", "b")


def test_call_llm_empty_response_reaches_live_path_again(tmp_path):
    backend = MockBackend(responses=["", "recovered"])
    r1 = call_llm(backend, "s", "u", model="test/model", cache_dir=tmp_path)
    assert r1.text == ""
    r2 = call_llm(backend, "s", "u", model="test/model", cache_dir=tmp_path)
    assert r2.text == "recovered"  # empty was not cached


# --- cost --------------------------------------------------------------------


def test_cost_normal():
    cost = estimate_cost("test/model", 1000, 500, pricing=PRICING)
    assert cost == pytest.approx((1000 * 0.30 + 500 * 1.20) / 1_000_000)


def test_cost_cache_hit_discount():
    cost = estimate_cost("test/model", 1000, 0, cache_hit_tokens=400, pricing=PRICING)
    expected = (600 * 0.30 + 400 * 0.10) / 1_000_000
    assert cost == pytest.approx(expected)


def test_cost_unknown_model_raises_keyerror():
    with pytest.raises(KeyError):
        estimate_cost("nope/model", 1, 1, pricing=PRICING)


def test_cost_missing_cache_hit_falls_back_to_input_rate():
    cost = estimate_cost("no-cache/model", 100, 0, cache_hit_tokens=40, pricing=PRICING)
    # No cache_hit rate -> cache tokens billed at input rate.
    assert cost == pytest.approx(100 * 1.00 / 1_000_000)


def test_cost_negative_clamps_to_zero():
    assert estimate_cost("test/model", -5, -5, pricing=PRICING) == 0.0


def test_response_cost_cache_hit_is_free():
    resp = LLMResponse(text="x", model="test/model", input_tokens=1000, from_cache=True)
    assert response_cost("test/model", resp, pricing=PRICING) == 0.0


def test_response_cost_live_is_priced():
    resp = LLMResponse(text="x", model="test/model", input_tokens=1000, output_tokens=0)
    assert response_cost("test/model", resp, pricing=PRICING) > 0.0


def test_model_alias_and_fallback():
    assert platform.model_alias("test/model", pricing=PRICING) == "tm"
    assert platform.model_alias("no-cache/model", pricing=PRICING) == "no-cache/model"
    assert platform.model_alias("unknown", pricing=PRICING) == "unknown"
    assert platform.model_alias("x", pricing=None) == "x"


# --- budget ------------------------------------------------------------------


def test_estimate_request_tokens_ceiling():
    assert estimate_request_tokens("", "") == 0
    assert estimate_request_tokens("abcd", "") == 1
    assert estimate_request_tokens("abcde", "") == 2  # (5+3)//4


def test_check_request_fits_under_and_over():
    budgets = {"m": 10}
    assert check_request_fits(system="ab", user="cd", model="m", budgets=budgets) == 1
    with pytest.raises(BudgetExceededError) as ei:
        check_request_fits(system="x" * 100, user="", model="m", budgets=budgets, identifier="row-1")
    assert ei.value.identifier == "row-1"
    assert ei.value.budget == 10


def test_check_request_fits_unregistered_passes_through():
    tokens = check_request_fits(system="x" * 100, user="", model="unbudgeted", budgets={"m": 1})
    assert tokens == estimate_request_tokens("x" * 100, "")


def test_call_llm_budget_guard_fires_before_backend(tmp_path):
    backend = MockBackend(responses=["never"])
    with pytest.raises(BudgetExceededError):
        call_llm(
            backend, "x" * 100, "", model="m",
            input_budgets={"m": 1}, identifier="over",
        )
    assert backend.calls == []  # never reached the backend


def test_cost_budget_running_guard():
    budget = CostBudget(limit=1e-6)
    budget.charge(4e-7)
    assert budget.spent == pytest.approx(4e-7)
    with pytest.raises(BudgetExceededError):
        budget.charge(9e-7)  # would exceed


def test_call_llm_charges_cost_budget():
    backend = MockBackend(responses=[{"text": "ok", "input_tokens": 1000, "output_tokens": 500}])
    budget = CostBudget(limit=1.0)
    call_llm(backend, "s", "u", model="test/model", pricing=PRICING, cost_budget=budget)
    assert budget.spent > 0.0


# --- retry / halt ------------------------------------------------------------


def test_call_llm_retries_non_halt_then_succeeds(monkeypatch):
    monkeypatch.setattr(platform.time, "sleep", lambda *_: None)
    backend = MockBackend(responses=[ValueError("transient blip"), "recovered"])
    resp = call_llm(backend, "s", "u", model="test/model", retries=1, retry_sleep=0.01)
    assert resp.text == "recovered"


def test_call_llm_gives_up_after_retries(monkeypatch):
    monkeypatch.setattr(platform.time, "sleep", lambda *_: None)
    backend = MockBackend(responses=[ValueError("a"), ValueError("b")])
    with pytest.raises(ValueError):
        call_llm(backend, "s", "u", model="test/model", retries=1)


def test_call_llm_halt_is_not_retried_and_maps_to_halt_error(monkeypatch):
    monkeypatch.setattr(platform.time, "sleep", lambda *_: None)
    # A halt exception (rate-limit marker) must raise HaltError on attempt 1.
    backend = MockBackend(
        responses=[RuntimeError('api_error_status:429 hit your limit'), "unreached"]
    )
    with pytest.raises(HaltError) as ei:
        call_llm(backend, "s", "u", model="test/model", retries=3)
    assert ei.value.kind == platform.HALT_RATE_LIMIT
    assert len(backend.calls) == 1  # halted immediately, no retry


def test_classify_halt_text_precedence():
    assert platform.classify_halt_text("hit your limit") == platform.HALT_RATE_LIMIT
    assert platform.classify_halt_text("authentication_error") == platform.HALT_AUTH
    # Rate-limit wins when both present.
    both = 'authentication_error and "api_error_status":429'
    assert platform.classify_halt_text(both) == platform.HALT_RATE_LIMIT
    assert platform.classify_halt_text("all good") is None


# --- submit_validated --------------------------------------------------------


def _accept_validator(candidate, context):
    return []


def _reject_until_ok(candidate, context):
    # Reject any payload that does not contain "OK".
    if "OK" in candidate:
        return []
    return [Rejection(kind="needs_ok", severity=Severity.HARD, detail="must contain OK")]


def test_submit_validated_accepts_first_try():
    backend = MockBackend(responses=["OK payload"])
    result = submit_validated(
        backend=backend, system="s", user="u", model="test/model",
        parse_fn=lambda t: t, validators=[_accept_validator],
    )
    assert result.accepted
    assert result.attempts == 1
    assert result.payload == "OK payload"


def test_submit_validated_feeds_back_and_recovers():
    backend = MockBackend(responses=["bad", "now OK"])
    seen_feedback = []

    def feedback(original_user, response_text, feedback_text):
        seen_feedback.append(feedback_text)
        return f"{original_user}\n{feedback_text}"

    result = submit_validated(
        backend=backend, system="s", user="u", model="test/model",
        parse_fn=lambda t: t, validators=[_reject_until_ok],
        build_feedback=feedback, max_attempts=3,
    )
    assert result.accepted
    assert result.attempts == 2
    assert seen_feedback  # rejection feedback was rendered and fed back
    assert "must contain OK" in seen_feedback[0]


def test_submit_validated_exhausts_and_reports_rejections():
    backend = MockBackend(responses=["bad", "still bad", "nope"])
    result = submit_validated(
        backend=backend, system="s", user="u", model="test/model",
        parse_fn=lambda t: t, validators=[_reject_until_ok], max_attempts=3,
    )
    assert not result.accepted
    assert result.attempts == 3
    assert result.rejections and result.rejections[0].kind == "needs_ok"


def test_submit_validated_parse_error_becomes_rejection_and_retries():
    def parse(text):
        if text == "junk":
            raise ValueError("cannot parse")
        return {"value": text}

    backend = MockBackend(responses=["junk", "good"])
    result = submit_validated(
        backend=backend, system="s", user="u", model="test/model",
        parse_fn=parse, validators=[_accept_validator], max_attempts=3,
    )
    assert result.accepted
    assert result.attempts == 2
    assert result.payload == {"value": "good"}


def test_submit_validated_trailing_parse_failure_keeps_last_good_payload():
    def parse(text):
        if text == "OK":
            return {"ok": True}
        raise ValueError("bad parse")

    # First attempt parses+validates? _reject_until_ok needs "OK" in candidate;
    # candidate is the dict here, so use a validator that always rejects to
    # force a second attempt whose parse fails.
    def always_reject(candidate, context):
        return [Rejection(kind="x", detail="reject")]

    backend = MockBackend(responses=["OK", "junk"])
    result = submit_validated(
        backend=backend, system="s", user="u", model="test/model",
        parse_fn=parse, validators=[always_reject], max_attempts=2,
    )
    # Second attempt's parse failed, but the earlier good parse is retained.
    assert result.payload == {"ok": True}
    assert result.attempts == 2
    assert result.rejections[0].kind == "parse_error"


def test_submit_validated_per_attempt_cache_salt_busts_cache(tmp_path):
    # With a cache dir and identical prompts, the automatic per-attempt salt
    # must let each retry reach the live backend instead of replaying attempt 1.
    backend = MockBackend(responses=["bad", "OK"])
    result = submit_validated(
        backend=backend, system="s", user="u", model="test/model",
        parse_fn=lambda t: t, validators=[_reject_until_ok],
        max_attempts=2, cache_dir=tmp_path,
    )
    assert result.accepted
    assert result.attempts == 2
    assert len(backend.calls) == 2


def test_submit_validated_rejects_bad_max_attempts():
    with pytest.raises(ValueError):
        submit_validated(
            backend=MockBackend(responses=["x"]), system="s", user="u",
            model="m", parse_fn=lambda t: t, validators=[], max_attempts=0,
        )
