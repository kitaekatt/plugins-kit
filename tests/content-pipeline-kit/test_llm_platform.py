"""Behavioral tests for content_pipeline.llm.platform.

Pins cache, cost (hard-fail on unknown model), budget, and validate-until-valid
loop behavior on the generic surface.
Everything runs on MockBackend -- no network, no subprocess.
"""

import json
import os
import threading

import pytest

from content_pipeline.llm import platform
from content_pipeline.llm.backends import MockBackend
from content_pipeline.llm.platform import (
    BackendOptions,
    BudgetExceededError,
    CostBudget,
    EvaluationResult,
    HaltError,
    LLMResponse,
    ResponseCache,
    ValidationSpec,
    build_cache_key,
    call_llm,
    check_request_fits,
    estimate_cost,
    estimate_request_tokens,
    evaluate_submission,
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


# --- evaluate_submission (pure, extracted from submit_validated) -------------


def test_evaluate_submission_accepts_when_parse_and_validators_pass():
    spec = ValidationSpec(parse_fn=lambda t: t, validators=[_accept_validator])
    result = evaluate_submission("OK payload", spec)
    assert isinstance(result, EvaluationResult)
    assert result.parsed is True
    assert result.payload == "OK payload"
    assert result.rejections == []


def test_evaluate_submission_reports_validator_rejection():
    spec = ValidationSpec(parse_fn=lambda t: t, validators=[_reject_until_ok])
    result = evaluate_submission("bad", spec)
    assert result.parsed is True
    assert result.payload == "bad"  # parsed payload retained even when rejected
    assert len(result.rejections) == 1
    assert result.rejections[0].kind == "needs_ok"


def test_evaluate_submission_parse_error_yields_single_rejection():
    def parse(text):
        raise ValueError("cannot parse")

    spec = ValidationSpec(parse_fn=parse, validators=[_accept_validator])
    result = evaluate_submission("junk", spec)
    assert result.parsed is False
    assert result.payload is None
    assert len(result.rejections) == 1
    assert result.rejections[0].kind == "parse_error"
    assert result.rejections[0].severity == Severity.HARD
    assert "cannot parse" in result.rejections[0].detail


def test_evaluate_submission_rejection_ordering_matches_run_rules():
    # run_rules sorts by (kind, detail); evaluate_submission must not reorder.
    def validator_b(candidate, context):
        return [Rejection(kind="z_kind", detail="z detail")]

    def validator_a(candidate, context):
        return [Rejection(kind="a_kind", detail="a detail")]

    spec = ValidationSpec(parse_fn=lambda t: t, validators=[validator_b, validator_a])
    result = evaluate_submission("x", spec)
    assert [r.kind for r in result.rejections] == ["a_kind", "z_kind"]


def test_evaluate_submission_context_is_forwarded_to_validators():
    seen = []

    def validator(candidate, context):
        seen.append(context)
        return []

    spec = ValidationSpec(parse_fn=lambda t: t, validators=[validator], context="ctx-1")
    evaluate_submission("x", spec)
    assert seen == ["ctx-1"]


def test_evaluate_submission_is_pure_no_backend_no_io_needed():
    # Constructed with no backend, no cache_dir, no clock dependency: a
    # worker in another process can call this with nothing but (text, spec).
    spec = ValidationSpec(parse_fn=lambda t: {"v": t}, validators=[_accept_validator])
    result = evaluate_submission("hello", spec)
    assert result.parsed is True
    assert result.rejections == []
    assert result.payload == {"v": "hello"}


def test_evaluate_submission_matches_submit_validated_single_attempt_feedback():
    # Byte-compatibility check: running evaluate_submission directly on the
    # same text submit_validated would see produces the same rejection
    # feedback string submit_validated renders internally.
    spec = ValidationSpec(parse_fn=lambda t: t, validators=[_reject_until_ok])
    direct = evaluate_submission("bad", spec)

    backend = MockBackend(responses=["bad", "OK"])
    seen_feedback = []

    def feedback(original_user, response_text, feedback_text):
        seen_feedback.append(feedback_text)
        return f"{original_user}\n{feedback_text}"

    submit_validated(
        backend=backend, system="s", user="u", model="test/model",
        parse_fn=lambda t: t, validators=[_reject_until_ok],
        build_feedback=feedback, max_attempts=2,
    )
    from content_pipeline.validate import contract as contract_mod

    rendered = contract_mod.format_rejections(direct.rejections)
    assert rendered == seen_feedback[0]


# --- ResponseCache.store atomicity --------------------------------------------


def test_store_atomic_write_leaves_no_temp_file_on_success(tmp_path):
    cache = ResponseCache(tmp_path)
    resp = LLMResponse(text="hello", model="m")
    assert cache.store("k", resp) is True
    entries = list(tmp_path.iterdir())
    assert [p.name for p in entries] == ["k.json"]


def test_store_interrupted_write_leaves_no_partial_or_corrupt_entry(tmp_path, monkeypatch):
    # Prove the atomicity claim: a write that dies PARTWAY THROUGH must not
    # corrupt or truncate the pre-existing entry, and must not leave a stray
    # temp file behind.
    cache = ResponseCache(tmp_path)
    good = LLMResponse(text="first version", model="m")
    assert cache.store("k", good) is True
    original_bytes = (tmp_path / "k.json").read_bytes()

    real_fdopen = os.fdopen

    class ExplodingFile:
        """Wraps the real temp-file handle but crashes mid-write.

        Writes half the intended bytes to the REAL underlying file (so a
        naive non-atomic writer would leave a truncated target), flushes
        them to disk, then raises -- simulating a process crash after some
        bytes landed but before the write completed.
        """

        def __init__(self, fd):
            self._real = real_fdopen(fd, "w", encoding="utf-8")

        def write(self, data):
            half = len(data) // 2
            self._real.write(data[:half])
            self._real.flush()
            raise OSError("simulated crash mid-write")

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            self._real.close()
            return False

    def fake_fdopen(fd, mode="r", encoding=None):
        assert mode == "w"
        return ExplodingFile(fd)

    monkeypatch.setattr(platform.os, "fdopen", fake_fdopen)

    updated = LLMResponse(text="second version, must not land", model="m")
    with pytest.raises(OSError):
        cache.store("k", updated)

    # The visible cache entry is byte-identical to before the failed write --
    # the half-written temp file never became the target.
    assert (tmp_path / "k.json").read_bytes() == original_bytes
    # And it is still valid, complete JSON for the ORIGINAL response.
    data = json.loads((tmp_path / "k.json").read_text(encoding="utf-8"))
    assert data["text"] == "first version"
    # No leftover .tmp file: cleanup ran even though the write raised.
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "k.json"]
    assert leftovers == []


def test_store_concurrent_writers_never_produce_a_corrupt_file(tmp_path):
    cache = ResponseCache(tmp_path)
    # Large, distinguishable payloads: a non-atomic write interleaved by a
    # thread-scheduler pause mid-write would produce a spliced/corrupt file
    # that fails to parse as JSON, or parses to a value neither writer sent.
    contents = [f"writer-{i}-" + ("x" * 20000) for i in range(8)]
    responses = [LLMResponse(text=c, model="m") for c in contents]
    errors = []

    def write(resp):
        try:
            cache.store("shared-key", resp)
        except Exception as exc:  # pragma: no cover - surfaced via errors
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(r,)) for r in responses]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    raw = (tmp_path / "shared-key.json").read_text(encoding="utf-8")
    data = json.loads(raw)  # raises if the file is truncated/interleaved
    assert data["text"] in contents  # exactly one writer's payload won
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "shared-key.json"]
    assert leftovers == []
