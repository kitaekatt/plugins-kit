"""Tests for subscription-usage pacing (``conserve_usage``)."""

import json

import pytest

from llm_scripting_kit import (
    EndpointMetadataError,
    STATUS_AVAILABLE,
    STATUS_UNDER_QUOTA,
    STATUS_OUT_OF_QUOTA,
    STATUS_NO_DATA,
    ConserveConfigError,
    ConserveSpec,
    discover_model_entries,
    parse_conserve_usage,
)
from llm_scripting_kit import usage_budget
from llm_scripting_kit.model_endpoints import EndpointRegistry, load_endpoint_registry

DAY = 24 * 3600
WEEK = 7 * DAY
NOW = 1_800_000_000


def _snapshot(tmp_path, rate_limits):
    path = tmp_path / "rate-limits.json"
    path.write_text(json.dumps({"captured_at": NOW, "rate_limits": rate_limits}))
    return path


# --- the declaration ------------------------------------------------------


def test_bare_true_means_the_all_model_weekly_pool():
    spec = parse_conserve_usage(True, source="test", entry_id="opus")
    assert spec == ConserveSpec(pool="seven_day")


@pytest.mark.parametrize("value", [None, False])
def test_absent_or_false_is_not_opted_in(value):
    assert parse_conserve_usage(value, source="test", entry_id="opus") is None


def test_mapping_declares_pool_and_display_name():
    spec = parse_conserve_usage(
        {"pool": "model_scoped", "display_name": "Fable"}, source="test", entry_id="fable"
    )
    assert spec == ConserveSpec(pool="model_scoped", display_name="Fable")


@pytest.mark.parametrize(
    "value",
    [
        "seven_day",                      # a bare string is not a declaration
        {},                               # no pool
        {"pool": ""},                     # empty pool
        {"pool": "seven_day", "slack": 1},  # unknown key
        {"pool": "model_scoped", "display_name": 3},
    ],
)
def test_an_unreadable_declaration_is_refused_not_ignored(value):
    # Tolerating it would leave the entry opted in and never conserving, which
    # is indistinguishable from a working opt-in.
    with pytest.raises(ConserveConfigError):
        parse_conserve_usage(value, source="test", entry_id="fable")


def test_registry_reports_a_bad_declaration_as_invalid_metadata(tmp_path, monkeypatch):
    registry = tmp_path / "model-endpoints.yaml"
    registry.write_text(
        "version: 1\nmodels:\n  fable:\n    harness: claude\n    model: claude-fable-5\n"
        "    conserve_usage: {pool: 3}\n"
    )
    monkeypatch.setenv("MODEL_ENDPOINTS_REGISTRY", str(registry))
    with pytest.raises(EndpointMetadataError):
        load_endpoint_registry()


def test_registry_entry_carries_the_parsed_spec(tmp_path, monkeypatch):
    registry = tmp_path / "model-endpoints.yaml"
    registry.write_text(
        "version: 1\nmodels:\n  fable:\n    harness: claude\n    model: claude-fable-5\n"
        "    conserve_usage:\n      pool: model_scoped\n      display_name: Fable\n"
    )
    monkeypatch.setenv("MODEL_ENDPOINTS_REGISTRY", str(registry))
    entry = load_endpoint_registry().entries["fable"]
    assert entry.conserve_usage == ConserveSpec(pool="model_scoped", display_name="Fable")


def test_layered_config_entry_carries_the_parsed_spec():
    config = {
        "endpoints": {
            "opus": {"harness": "claude", "model": "claude-opus-5", "conserve_usage": True}
        }
    }
    entries = discover_model_entries(config=config, registry=EndpointRegistry()).entries
    assert entries["opus"].conserve_usage == ConserveSpec(pool="seven_day")


def test_layered_config_refuses_a_bad_declaration():
    config = {
        "endpoints": {
            "opus": {"harness": "claude", "model": "claude-opus-5", "conserve_usage": "yes"}
        }
    }
    with pytest.raises(EndpointMetadataError):
        discover_model_entries(config=config, registry=EndpointRegistry())


# --- the rule -------------------------------------------------------------


def test_behind_pace_is_conserved(tmp_path):
    # 20% of quota left with half the week still to run.
    path = _snapshot(tmp_path, {"seven_day": {"used_percentage": 80, "resets_at": NOW + WEEK // 2}})
    budget = usage_budget.read_claude_pool(
        ConserveSpec(pool="seven_day"), now=NOW, snapshot=path
    )
    assert budget.status == STATUS_UNDER_QUOTA
    assert budget.deprioritized is True
    assert budget.usable is True
    assert budget.remaining == pytest.approx(0.2)
    assert budget.window_remaining == pytest.approx(0.5)


def test_ahead_of_pace_is_available(tmp_path):
    path = _snapshot(tmp_path, {"seven_day": {"used_percentage": 20, "resets_at": NOW + WEEK // 2}})
    budget = usage_budget.read_claude_pool(
        ConserveSpec(pool="seven_day"), now=NOW, snapshot=path
    )
    assert budget.status == STATUS_AVAILABLE
    assert budget.deprioritized is False


def test_exactly_on_pace_is_available(tmp_path):
    # "at least the window fraction" -- the boundary is available, and stays
    # available despite the float error in `1 - 50/100` (see _PACE_EPSILON).
    path = _snapshot(tmp_path, {"seven_day": {"used_percentage": 50, "resets_at": NOW + WEEK // 2}})
    budget = usage_budget.read_claude_pool(
        ConserveSpec(pool="seven_day"), now=NOW, snapshot=path
    )
    assert budget.status == STATUS_AVAILABLE


def test_five_hour_pool_uses_the_five_hour_window(tmp_path):
    # Same percentages as the conserved seven-day case; only the window length
    # differs, and with 1h of a 5h window left the burn-down is ahead of pace.
    path = _snapshot(tmp_path, {"five_hour": {"used_percentage": 80, "resets_at": NOW + 3600}})
    budget = usage_budget.read_claude_pool(
        ConserveSpec(pool="five_hour"), now=NOW, snapshot=path
    )
    assert budget.status == STATUS_AVAILABLE
    assert budget.window_remaining == pytest.approx(0.2)


# --- pools ----------------------------------------------------------------


def test_model_scoped_selects_its_bucket_by_display_name(tmp_path):
    path = _snapshot(
        tmp_path,
        {
            "seven_day": {"used_percentage": 5, "resets_at": NOW + WEEK // 2},
            "model_scoped": [
                {"display_name": "Sonnet", "utilization": 10, "resets_at": NOW + WEEK // 2},
                {"display_name": "Fable", "utilization": 90, "resets_at": NOW + WEEK // 2},
            ],
        },
    )
    budget = usage_budget.read_claude_pool(
        ConserveSpec(pool="model_scoped", display_name="Fable"), now=NOW, snapshot=path
    )
    # The Fable bucket is behind pace even though the all-model window is not:
    # reading the wrong pool would invert this verdict.
    assert budget.status == STATUS_UNDER_QUOTA
    assert budget.remaining == pytest.approx(0.1)


def test_model_scoped_reads_an_iso_reset_time(tmp_path):
    # model_scoped entries carry an ISO 8601 string where the top-level
    # windows carry an epoch; a reader handling one shape loses the other.
    path = _snapshot(
        tmp_path,
        {
            "model_scoped": [
                {
                    "display_name": "Fable",
                    "utilization": 90,
                    "resets_at": "2027-01-15T14:40:00+00:00",
                }
            ]
        },
    )
    budget = usage_budget.read_claude_pool(
        ConserveSpec(pool="model_scoped", display_name="Fable"),
        now=NOW,
        snapshot=path,
    )
    assert budget.status in (STATUS_AVAILABLE, STATUS_UNDER_QUOTA)
    assert budget.resets_at is not None


def test_missing_model_scoped_bucket_names_what_is_present(tmp_path):
    path = _snapshot(
        tmp_path,
        {"model_scoped": [{"display_name": "Sonnet", "utilization": 10, "resets_at": NOW + 10}]},
    )
    budget = usage_budget.read_claude_pool(
        ConserveSpec(pool="model_scoped", display_name="Fable"), now=NOW, snapshot=path
    )
    assert budget.status == STATUS_NO_DATA
    assert "Sonnet" in budget.detail


# --- failing open ---------------------------------------------------------


def test_absent_snapshot_is_no_data_not_conserved(tmp_path):
    budget = usage_budget.read_claude_pool(
        ConserveSpec(pool="seven_day"), now=NOW, snapshot=tmp_path / "missing.json"
    )
    assert budget.status == STATUS_NO_DATA
    assert budget.deprioritized is False


def test_absent_pool_is_no_data(tmp_path):
    # The state on an account whose server never emits the per-model bucket.
    path = _snapshot(tmp_path, {"seven_day": {"used_percentage": 99, "resets_at": NOW + WEEK}})
    budget = usage_budget.read_claude_pool(
        ConserveSpec(pool="model_scoped", display_name="Fable"), now=NOW, snapshot=path
    )
    assert budget.status == STATUS_NO_DATA
    assert budget.deprioritized is False


def test_a_window_that_already_reset_is_no_data(tmp_path):
    path = _snapshot(tmp_path, {"seven_day": {"used_percentage": 99, "resets_at": NOW - 10}})
    budget = usage_budget.read_claude_pool(
        ConserveSpec(pool="seven_day"), now=NOW, snapshot=path
    )
    assert budget.status == STATUS_NO_DATA


def test_malformed_snapshot_is_no_data(tmp_path):
    path = tmp_path / "rate-limits.json"
    path.write_text("{not json")
    budget = usage_budget.read_claude_pool(
        ConserveSpec(pool="seven_day"), now=NOW, snapshot=path
    )
    assert budget.status == STATUS_NO_DATA


def test_a_harness_with_no_usage_source_is_no_data():
    budget = usage_budget.evaluate(ConserveSpec(pool="seven_day"), "opencode", now=NOW)
    assert budget.status == STATUS_NO_DATA
    assert budget.deprioritized is False


# --- codex ----------------------------------------------------------------


def _rollout(tmp_path, limits, name="rollout.jsonl", timestamp=None, error_message=None):
    path = tmp_path / name
    lines = [json.dumps({"type": "event_msg", "payload": {"type": "token_count"}})]
    rate_limits_event = {"type": "event_msg", "payload": {"type": "token_count", "rate_limits": limits}}
    if timestamp is not None:
        rate_limits_event = {"timestamp": timestamp, **rate_limits_event}
    lines.append(json.dumps(rate_limits_event))
    if error_message is not None:
        error_event = {
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "error": {"message": error_message, "codex_error_info": "usage_limit_exceeded"},
            },
        }
        if timestamp is not None:
            error_event = {"timestamp": timestamp, **error_event}
        lines.append(json.dumps(error_event))
    path.write_text("\n".join(lines) + "\n")
    return path


def _reset_epoch(month_abbr, day, year, hour, minute, ampm):
    """Independently compute the epoch the module's own reset-text parser
    should produce, so tests assert against the parsing RULE rather than a
    hardcoded number tied to one timezone."""
    import time as _time
    from datetime import datetime as _datetime

    month_num = _time.strptime(month_abbr, "%b").tm_mon
    hour_i = hour % 12
    if ampm.upper() == "PM":
        hour_i += 12
    return int(_time.mktime(_datetime(year, month_num, day, hour_i, minute).timetuple()))


def test_codex_uses_the_window_minutes_it_reports(tmp_path):
    _rollout(
        tmp_path,
        {"primary": {"used_percent": 80.0, "window_minutes": 10080, "resets_at": NOW + WEEK // 2}},
    )
    budget = usage_budget.read_codex_pool(
        ConserveSpec(pool="primary"), now=NOW, sessions_dir=tmp_path
    )
    assert budget.status == STATUS_UNDER_QUOTA
    assert budget.window_remaining == pytest.approx(0.5)


def test_codex_maps_the_neutral_default_pool_to_primary(tmp_path):
    # `conserve_usage: true` yields the harness-neutral `seven_day`, a name
    # codex never emits; it has to resolve to codex's principal window.
    _rollout(
        tmp_path,
        {"primary": {"used_percent": 10.0, "window_minutes": 10080, "resets_at": NOW + WEEK // 2}},
    )
    budget = usage_budget.read_codex_pool(
        ConserveSpec(pool="seven_day"), now=NOW, sessions_dir=tmp_path
    )
    assert budget.status == STATUS_AVAILABLE


def test_codex_reads_the_newest_rollout(tmp_path):
    old = _rollout(
        tmp_path,
        {"primary": {"used_percent": 5.0, "window_minutes": 10080, "resets_at": NOW + WEEK // 2}},
        name="old.jsonl",
    )
    new = _rollout(
        tmp_path,
        {"primary": {"used_percent": 95.0, "window_minutes": 10080, "resets_at": NOW + WEEK // 2}},
        name="new.jsonl",
    )
    import os

    os.utime(old, (NOW - 100, NOW - 100))
    os.utime(new, (NOW, NOW))
    budget = usage_budget.read_codex_pool(
        ConserveSpec(pool="primary"), now=NOW, sessions_dir=tmp_path
    )
    assert budget.remaining == pytest.approx(0.05)


def test_codex_with_no_rollout_is_no_data(tmp_path):
    budget = usage_budget.read_codex_pool(
        ConserveSpec(pool="primary"), now=NOW, sessions_dir=tmp_path / "absent"
    )
    assert budget.status == STATUS_NO_DATA


def test_codex_window_without_a_length_is_no_data(tmp_path):
    _rollout(tmp_path, {"primary": {"used_percent": 80.0, "resets_at": NOW + WEEK // 2}})
    budget = usage_budget.read_codex_pool(
        ConserveSpec(pool="primary"), now=NOW, sessions_dir=tmp_path
    )
    assert budget.status == STATUS_NO_DATA


def test_codex_healthy_reading_with_has_credits_false_is_not_out_of_quota(tmp_path):
    # `credits.has_credits: false` ALONE is not exhaustion: 39 of 60 sampled
    # live rollouts (2026-09-13T02:16Z..2026-09-15T17:55Z) carry this exact
    # `credits` block -- {"has_credits": false, "unlimited": false,
    # "balance": "0"} -- describing purchased EXTRA credits, which this plan
    # never has, ALONGSIDE a perfectly normal `primary` window. A `primary`
    # mapping being present must always go through the ordinary window logic,
    # whatever `credits` says. This is the case an earlier (wrong) fix broke:
    # it read `has_credits: false` alone as exhaustion and would have dropped
    # a healthy codex seat permanently.
    _rollout(
        tmp_path,
        {
            "limit_id": "premium",
            "primary": {"used_percent": 10.0, "window_minutes": 300, "resets_at": NOW + 150 * 60},
            "secondary": None,
            "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
        },
    )
    budget = usage_budget.read_codex_pool(
        ConserveSpec(pool="seven_day"), now=NOW, sessions_dir=tmp_path
    )
    assert budget.status == STATUS_AVAILABLE
    assert budget.usable is True


def test_codex_exhausted_with_parseable_future_reset_is_out_of_quota(tmp_path):
    # The real exhausted shape (observed live 2026-09-16, 21 of 60 sampled
    # rollouts 2026-09-15T18:02Z..2026-09-16T16:22Z): both windows null, same
    # credits block, and a sibling `task_complete` event's error text names
    # when it resets.
    reset_epoch = _reset_epoch("Jan", 20, 2027, 3, 34, "PM")
    assert NOW < reset_epoch  # the test's own premise: reset is in the future
    _rollout(
        tmp_path,
        {
            "primary": None,
            "secondary": None,
            "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
        },
        error_message=(
            "You've hit your usage limit. Visit https://chatgpt.com/codex/settings/usage "
            "to purchase more credits or try again at Jan 20th, 2027 3:34 PM."
        ),
    )
    budget = usage_budget.read_codex_pool(
        ConserveSpec(pool="seven_day"), now=NOW, sessions_dir=tmp_path
    )
    assert budget.status == STATUS_OUT_OF_QUOTA
    assert budget.usable is False
    assert budget.resets_at == reset_epoch


def test_codex_exhausted_reading_is_no_data_once_its_parsed_reset_has_passed(tmp_path):
    reset_epoch = _reset_epoch("Jan", 20, 2027, 3, 34, "PM")
    _rollout(
        tmp_path,
        {
            "primary": None,
            "secondary": None,
            "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
        },
        error_message=(
            "You've hit your usage limit. Visit https://chatgpt.com/codex/settings/usage "
            "to purchase more credits or try again at Jan 20th, 2027 3:34 PM."
        ),
    )
    budget = usage_budget.read_codex_pool(
        ConserveSpec(pool="seven_day"), now=reset_epoch + 60, sessions_dir=tmp_path
    )
    assert budget.status == STATUS_NO_DATA


def test_codex_exhausted_with_no_error_message_is_out_of_quota_while_recent(tmp_path):
    # No parseable reset time -- latched out-of-quota only while the reading
    # is recent (bounded by _CODEX_EXHAUSTION_LATCH_SECONDS, codex's own
    # 5-hour primary window).
    _rollout(
        tmp_path,
        {
            "primary": None,
            "secondary": None,
            "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
        },
        timestamp=NOW - 3600,  # 1h old: within the 5h latch
    )
    budget = usage_budget.read_codex_pool(
        ConserveSpec(pool="seven_day"), now=NOW, sessions_dir=tmp_path
    )
    assert budget.status == STATUS_OUT_OF_QUOTA
    assert budget.usable is False


def test_codex_exhausted_with_no_error_message_is_no_data_once_stale(tmp_path):
    _rollout(
        tmp_path,
        {
            "primary": None,
            "secondary": None,
            "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
        },
        timestamp=NOW - 6 * 3600,  # 6h old: past the 5h latch
    )
    budget = usage_budget.read_codex_pool(
        ConserveSpec(pool="seven_day"), now=NOW, sessions_dir=tmp_path
    )
    assert budget.status == STATUS_NO_DATA


def test_codex_latched_exhaustion_expires_within_a_pinned_session(tmp_path, monkeypatch):
    # A pinned verdict is recomputed only once its resets_at has passed, so a
    # latched verdict must carry one or it holds for the whole session.
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    _rollout(
        sessions,
        {
            "primary": None,
            "secondary": None,
            "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
        },
        timestamp=NOW - 3600,
    )
    monkeypatch.setattr(usage_budget, "CODEX_SESSIONS_DIR", sessions)
    cache = tmp_path / "verdicts.json"
    env = {"CLAUDE_CODE_SESSION_ID": "s1"}
    spec = ConserveSpec(pool="seven_day")
    first = usage_budget.pinned_evaluate(
        "sol", spec, "codex", now=NOW, cache_path=cache, environ=env
    )
    later = usage_budget.pinned_evaluate(
        "sol", spec, "codex", now=NOW + 5 * 3600, cache_path=cache, environ=env
    )
    assert first.status == STATUS_OUT_OF_QUOTA
    assert first.resets_at == NOW - 3600 + 5 * 3600
    assert later.status == STATUS_NO_DATA


def test_codex_unlimited_credits_is_not_treated_as_exhausted(tmp_path):
    # has_credits: false paired with unlimited: true must not fail closed --
    # unlimited plans can report has_credits false while still able to serve.
    _rollout(
        tmp_path,
        {
            "primary": None,
            "secondary": None,
            "credits": {"has_credits": False, "unlimited": True, "balance": "0"},
        },
    )
    budget = usage_budget.read_codex_pool(
        ConserveSpec(pool="seven_day"), now=NOW, sessions_dir=tmp_path
    )
    assert budget.status == STATUS_NO_DATA


def test_codex_credits_with_credit_left_falls_through_to_window_logic(tmp_path):
    # has_credits: true is not the exhaustion shape; unrecognised beyond that
    # must fail closed to no-data rather than guessing.
    _rollout(
        tmp_path,
        {
            "primary": None,
            "secondary": None,
            "credits": {"has_credits": True, "unlimited": False, "balance": "500"},
        },
    )
    budget = usage_budget.read_codex_pool(
        ConserveSpec(pool="seven_day"), now=NOW, sessions_dir=tmp_path
    )
    assert budget.status == STATUS_NO_DATA


# --- session pinning ------------------------------------------------------


def test_a_verdict_is_pinned_for_the_session(tmp_path, monkeypatch):
    cache = tmp_path / "verdicts.json"
    calls = []

    def fake_evaluate(spec, harness, *, now=None):
        calls.append(harness)
        return usage_budget.Budget(status=STATUS_AVAILABLE, pool=spec.pool, detail="first")

    monkeypatch.setattr(usage_budget, "evaluate", fake_evaluate)
    env = {"CLAUDE_CODE_SESSION_ID": "s1"}
    spec = ConserveSpec(pool="seven_day")
    first = usage_budget.pinned_evaluate(
        "fable", spec, "claude", now=NOW, cache_path=cache, environ=env
    )
    second = usage_budget.pinned_evaluate(
        "fable", spec, "claude", now=NOW + 3600, cache_path=cache, environ=env
    )
    assert first.status == second.status == STATUS_AVAILABLE
    assert len(calls) == 1, "an available verdict must not be recomputed mid-session"


def test_a_new_session_recomputes(tmp_path, monkeypatch):
    cache = tmp_path / "verdicts.json"
    calls = []

    def fake_evaluate(spec, harness, *, now=None):
        calls.append(harness)
        return usage_budget.Budget(status=STATUS_AVAILABLE, pool=spec.pool, detail="x")

    monkeypatch.setattr(usage_budget, "evaluate", fake_evaluate)
    spec = ConserveSpec(pool="seven_day")
    usage_budget.pinned_evaluate(
        "fable", spec, "claude", now=NOW, cache_path=cache,
        environ={"CLAUDE_CODE_SESSION_ID": "s1"},
    )
    usage_budget.pinned_evaluate(
        "fable", spec, "claude", now=NOW, cache_path=cache,
        environ={"CLAUDE_CODE_SESSION_ID": "s2"},
    )
    assert len(calls) == 2


def test_a_changed_declaration_is_not_served_from_the_pin(tmp_path, monkeypatch):
    cache = tmp_path / "verdicts.json"
    calls = []

    def fake_evaluate(spec, harness, *, now=None):
        calls.append(spec.pool)
        return usage_budget.Budget(status=STATUS_AVAILABLE, pool=spec.pool, detail="x")

    monkeypatch.setattr(usage_budget, "evaluate", fake_evaluate)
    env = {"CLAUDE_CODE_SESSION_ID": "s1"}
    usage_budget.pinned_evaluate(
        "fable", ConserveSpec(pool="seven_day"), "claude", now=NOW,
        cache_path=cache, environ=env,
    )
    usage_budget.pinned_evaluate(
        "fable", ConserveSpec(pool="model_scoped", display_name="Fable"), "claude",
        now=NOW, cache_path=cache, environ=env,
    )
    assert calls == ["seven_day", "model_scoped"]


def test_a_float_resets_at_survives_the_pinned_round_trip(tmp_path, monkeypatch):
    """The rehydration in pinned_evaluate used isinstance(x, int) to decide
    whether to keep a cached resets_at, while the EXPIRY check just above it
    accepts isinstance(x, (int, float)) -- so a float epoch (e.g. from a
    harness snapshot that reports sub-second timestamps) was used correctly
    to decide the verdict was not yet expired, and then reported back with
    resets_at=None, i.e. "no reset known", instead of the value that was just
    used to make that very decision.
    """
    cache = tmp_path / "verdicts.json"

    def fake_evaluate(spec, harness, *, now=None):
        return usage_budget.Budget(
            status=STATUS_UNDER_QUOTA, pool=spec.pool, detail="x", resets_at=NOW + 100.0
        )

    monkeypatch.setattr(usage_budget, "evaluate", fake_evaluate)
    env = {"CLAUDE_CODE_SESSION_ID": "s1"}
    spec = ConserveSpec(pool="seven_day")
    usage_budget.pinned_evaluate("fable", spec, "claude", now=NOW, cache_path=cache, environ=env)
    # Read back within the window (not yet expired) -- served from the pin.
    held = usage_budget.pinned_evaluate(
        "fable", spec, "claude", now=NOW + 50, cache_path=cache, environ=env
    )
    assert held.status == STATUS_UNDER_QUOTA
    assert held.resets_at is not None


def test_a_conserved_verdict_is_recomputed_once_its_window_resets(tmp_path, monkeypatch):
    cache = tmp_path / "verdicts.json"
    statuses = iter([STATUS_UNDER_QUOTA, STATUS_AVAILABLE])

    def fake_evaluate(spec, harness, *, now=None):
        return usage_budget.Budget(
            status=next(statuses), pool=spec.pool, detail="x", resets_at=NOW + 100
        )

    monkeypatch.setattr(usage_budget, "evaluate", fake_evaluate)
    env = {"CLAUDE_CODE_SESSION_ID": "s1"}
    spec = ConserveSpec(pool="seven_day")
    first = usage_budget.pinned_evaluate(
        "fable", spec, "claude", now=NOW, cache_path=cache, environ=env
    )
    # Before the reset the conserved verdict still stands...
    held = usage_budget.pinned_evaluate(
        "fable", spec, "claude", now=NOW + 50, cache_path=cache, environ=env
    )
    # ...and after it, capacity can only have been restored.
    after = usage_budget.pinned_evaluate(
        "fable", spec, "claude", now=NOW + 200, cache_path=cache, environ=env
    )
    assert (first.status, held.status, after.status) == (
        STATUS_UNDER_QUOTA,
        STATUS_UNDER_QUOTA,
        STATUS_AVAILABLE,
    )


def test_without_a_session_key_nothing_is_pinned(tmp_path, monkeypatch):
    cache = tmp_path / "verdicts.json"
    calls = []

    def fake_evaluate(spec, harness, *, now=None):
        calls.append(harness)
        return usage_budget.Budget(status=STATUS_AVAILABLE, pool=spec.pool, detail="x")

    monkeypatch.setattr(usage_budget, "evaluate", fake_evaluate)
    spec = ConserveSpec(pool="seven_day")
    for _ in range(2):
        usage_budget.pinned_evaluate(
            "fable", spec, "claude", now=NOW, cache_path=cache, environ={}
        )
    assert len(calls) == 2
    assert not cache.exists()


def test_an_unwritable_cache_never_fails_the_caller(tmp_path, monkeypatch):
    monkeypatch.setattr(
        usage_budget,
        "evaluate",
        lambda spec, harness, *, now=None: usage_budget.Budget(
            status=STATUS_AVAILABLE, pool=spec.pool, detail="x"
        ),
    )
    unwritable = tmp_path / "file-not-a-dir" / "verdicts.json"
    (tmp_path / "file-not-a-dir").write_text("blocking file")
    budget = usage_budget.pinned_evaluate(
        "fable", ConserveSpec(pool="seven_day"), "claude", now=NOW,
        cache_path=unwritable, environ={"CLAUDE_CODE_SESSION_ID": "s1"},
    )
    assert budget.status == STATUS_AVAILABLE


# --- the CLI surface ------------------------------------------------------


def test_usage_verb_reports_each_opted_in_endpoint(monkeypatch, capsys):
    from llm_scripting_kit import cli
    from llm_scripting_kit.models import ModelDiscovery
    from llm_scripting_kit.model_endpoints import EndpointEntry, HARNESS_KIND

    entries = {
        "fable": EndpointEntry(
            id="fable", base_url=None, model="claude-fable-5", kind=HARNESS_KIND,
            harness="claude",
            conserve_usage=ConserveSpec(pool="model_scoped", display_name="Fable"),
        ),
        "sonnet": EndpointEntry(
            id="sonnet", base_url=None, model="claude-sonnet-5", kind=HARNESS_KIND,
            harness="claude",
        ),
    }
    monkeypatch.setattr(
        cli, "discover_model_entries", lambda **kw: ModelDiscovery(entries)
    )
    monkeypatch.setattr(
        usage_budget,
        "pinned_evaluate",
        lambda entry_id, spec, harness: usage_budget.Budget(
            status=STATUS_UNDER_QUOTA, pool=spec.pool, detail="behind pace"
        ),
    )
    assert cli.main(["usage"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "fable: under-quota -- behind pace" in out
    assert "sonnet" not in out, "an endpoint that did not opt in is not reported"


def test_usage_verb_no_pin_evaluates_now(monkeypatch, capsys):
    from llm_scripting_kit import cli
    from llm_scripting_kit.models import ModelDiscovery
    from llm_scripting_kit.model_endpoints import EndpointEntry, HARNESS_KIND

    entries = {
        "opus": EndpointEntry(
            id="opus", base_url=None, model="claude-opus-5", kind=HARNESS_KIND,
            harness="claude", conserve_usage=ConserveSpec(pool="seven_day"),
        )
    }
    monkeypatch.setattr(cli, "discover_model_entries", lambda **kw: ModelDiscovery(entries))
    monkeypatch.setattr(
        usage_budget,
        "pinned_evaluate",
        lambda *a, **k: pytest.fail("--no-pin must not read or write the pin"),
    )
    monkeypatch.setattr(
        usage_budget,
        "evaluate",
        lambda spec, harness, **kw: usage_budget.Budget(
            status=STATUS_AVAILABLE, pool=spec.pool, detail="fresh"
        ),
    )
    assert cli.main(["usage", "--no-pin", "--json"]) == cli.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["pinned"] is False
    assert payload["verdicts"]["opus"]["budget"]["status"] == STATUS_AVAILABLE
    assert payload["verdicts"]["opus"]["conserve_usage"] == {"pool": "seven_day"}


def test_usage_verb_says_so_when_nothing_opted_in(monkeypatch, capsys):
    from llm_scripting_kit import cli
    from llm_scripting_kit.models import ModelDiscovery

    monkeypatch.setattr(cli, "discover_model_entries", lambda **kw: ModelDiscovery({}))
    assert cli.main(["usage"]) == cli.EXIT_OK
    assert "no endpoint declares conserve_usage" in capsys.readouterr().out


def test_model_scoped_without_a_display_name_is_refused():
    # The pool is an array; with no label there is no bucket to select, so the
    # opt-in could only ever return no-data -- indistinguishable from a working
    # one, which is exactly what parse-time refusal exists to prevent.
    with pytest.raises(ConserveConfigError):
        parse_conserve_usage(
            {"pool": "model_scoped"}, source="test", entry_id="fable"
        )


# --- the two consequences: de-prioritize vs disable ------------------------


def test_a_spent_pool_is_out_of_quota_not_merely_under(tmp_path):
    # An empty pool is behind pace by definition, so the exhaustion test has to
    # run FIRST or a model that cannot answer a call stays in selection.
    path = _snapshot(tmp_path, {"seven_day": {"used_percentage": 100, "resets_at": NOW + WEEK // 2}})
    budget = usage_budget.read_claude_pool(
        ConserveSpec(pool="seven_day"), now=NOW, snapshot=path
    )
    assert budget.status == STATUS_OUT_OF_QUOTA
    assert budget.usable is False
    assert budget.deprioritized is False


def test_under_quota_stays_usable(tmp_path):
    path = _snapshot(tmp_path, {"seven_day": {"used_percentage": 80, "resets_at": NOW + WEEK // 2}})
    budget = usage_budget.read_claude_pool(
        ConserveSpec(pool="seven_day"), now=NOW, snapshot=path
    )
    # The whole point of the split: behind pace costs it priority, not its seat.
    assert budget.status == STATUS_UNDER_QUOTA
    assert budget.usable is True
    assert budget.deprioritized is True


def test_no_data_is_usable_and_not_deprioritized():
    budget = usage_budget.evaluate(ConserveSpec(pool="seven_day"), "opencode", now=NOW)
    assert budget.status == STATUS_NO_DATA
    assert budget.usable is True
    assert budget.deprioritized is False


def test_an_out_of_quota_verdict_is_recomputed_once_its_window_resets(tmp_path, monkeypatch):
    cache = tmp_path / "verdicts.json"
    statuses = iter([STATUS_OUT_OF_QUOTA, STATUS_AVAILABLE])

    def fake_evaluate(spec, harness, *, now=None):
        return usage_budget.Budget(
            status=next(statuses), pool=spec.pool, detail="x", resets_at=NOW + 100
        )

    monkeypatch.setattr(usage_budget, "evaluate", fake_evaluate)
    env = {"CLAUDE_CODE_SESSION_ID": "s1"}
    spec = ConserveSpec(pool="seven_day")
    before = usage_budget.pinned_evaluate(
        "fable", spec, "claude", now=NOW + 50, cache_path=cache, environ=env
    )
    after = usage_budget.pinned_evaluate(
        "fable", spec, "claude", now=NOW + 200, cache_path=cache, environ=env
    )
    assert (before.status, after.status) == (STATUS_OUT_OF_QUOTA, STATUS_AVAILABLE)


# --- verdict write-back on an observed quota/credit halt -------------------
#
# The register entry says a pinned verdict is "never re-evaluated downward";
# record_observed_halt is the one exception -- a caller that OBSERVED a real
# dispatch failure writes that fact back so the session does not re-select
# the same exhausted endpoint on a stale AVAILABLE verdict. See
# docs/planning/quota-resilient-dispatch/declaration-format-design.md,
# Decision 6, "Stale verdict".


def test_record_observed_halt_writes_out_of_quota_with_its_own_reset_time(tmp_path):
    cache = tmp_path / "verdicts.json"
    env = {"CLAUDE_CODE_SESSION_ID": "s1"}
    spec = ConserveSpec(pool="seven_day")

    written = usage_budget.record_observed_halt(
        "astra", spec, resets_at=NOW + 3600, now=NOW, cache_path=cache, environ=env,
    )
    assert written.status == STATUS_OUT_OF_QUOTA
    assert written.resets_at == NOW + 3600

    read_back = usage_budget.pinned_evaluate(
        "astra", spec, "codex", now=NOW + 10, cache_path=cache, environ=env,
    )
    assert read_back.status == STATUS_OUT_OF_QUOTA
    assert read_back.resets_at == NOW + 3600


def test_record_observed_halt_without_a_reset_time_latches_five_hours(tmp_path):
    cache = tmp_path / "verdicts.json"
    env = {"CLAUDE_CODE_SESSION_ID": "s1"}
    spec = ConserveSpec(pool="seven_day")

    written = usage_budget.record_observed_halt(
        "astra", spec, now=NOW, cache_path=cache, environ=env,
    )
    assert written.status == STATUS_OUT_OF_QUOTA
    assert written.resets_at == NOW + 5 * 3600


def test_record_observed_halt_overrides_a_prior_available_verdict(tmp_path, monkeypatch):
    # This is the actual override: pinned_evaluate on its own never moves an
    # AVAILABLE verdict downward, but record_observed_halt writes past it
    # directly on an observed failure.
    cache = tmp_path / "verdicts.json"
    env = {"CLAUDE_CODE_SESSION_ID": "s1"}
    spec = ConserveSpec(pool="seven_day")

    monkeypatch.setattr(
        usage_budget, "evaluate",
        lambda spec, harness, *, now=None: usage_budget.Budget(
            status=STATUS_AVAILABLE, pool=spec.pool, detail="fresh",
        ),
    )
    pinned = usage_budget.pinned_evaluate(
        "astra", spec, "codex", now=NOW, cache_path=cache, environ=env,
    )
    assert pinned.status == STATUS_AVAILABLE

    usage_budget.record_observed_halt(
        "astra", spec, resets_at=NOW + 60, now=NOW, cache_path=cache, environ=env,
    )
    after = usage_budget.pinned_evaluate(
        "astra", spec, "codex", now=NOW + 1, cache_path=cache, environ=env,
    )
    assert after.status == STATUS_OUT_OF_QUOTA
    assert after.resets_at == NOW + 60


def test_record_observed_halt_without_a_session_key_writes_nothing(tmp_path):
    cache = tmp_path / "verdicts.json"
    spec = ConserveSpec(pool="seven_day")
    result = usage_budget.record_observed_halt(
        "astra", spec, resets_at=NOW + 60, now=NOW, cache_path=cache, environ={},
    )
    assert result is None
    assert not cache.exists()


# --- an observed halt spends the whole pool (drill finding F4) --------------
#
# Entries on the same harness account reading the same pool share one quota:
# the halt one of them observed is a fact about all of them, so the write-back
# covers every sibling -- the same set describe labels "shares <pool> with".


def _pool_entries():
    from llm_scripting_kit.model_endpoints import HARNESS_KIND, EndpointEntry

    def entry(name, harness, spec):
        return EndpointEntry(
            id=name, base_url=None, model=f"model-{name}", kind=HARNESS_KIND,
            harness=harness, conserve_usage=spec,
        )

    seven = ConserveSpec(pool="seven_day")
    return {
        "luna": entry("luna", "codex", seven),
        "sol": entry("sol", "codex", ConserveSpec(pool="seven_day")),
        "sol-5h": entry("sol-5h", "codex", ConserveSpec(pool="primary")),
        "fable": entry("fable", "claude", ConserveSpec(pool="seven_day")),
        "plain": entry("plain", "codex", None),
    }


def test_quota_pool_key_matches_same_harness_and_pool_only():
    entries = _pool_entries()
    key = usage_budget.quota_pool_key
    luna, sol = entries["luna"], entries["sol"]
    assert key(luna.harness, luna.conserve_usage) == key(sol.harness, sol.conserve_usage)
    assert key("Codex", luna.conserve_usage) == key("codex", sol.conserve_usage)
    assert key("codex", ConserveSpec(pool="primary")) != key("codex", luna.conserve_usage)
    assert key("claude", luna.conserve_usage) != key("codex", luna.conserve_usage)
    assert key("codex", None) is None
    assert key(None, luna.conserve_usage) is None


def test_record_observed_halt_with_entries_spends_every_sibling_on_the_pool(tmp_path, monkeypatch):
    cache = tmp_path / "verdicts.json"
    env = {"CLAUDE_CODE_SESSION_ID": "s1"}
    entries = _pool_entries()
    monkeypatch.setattr(
        usage_budget, "evaluate",
        lambda spec, harness, *, now=None: usage_budget.Budget(
            status=STATUS_AVAILABLE, pool=spec.pool, detail="fresh",
        ),
    )
    # Every paced entry is pinned AVAILABLE first, as a session's describe does.
    for name in ("luna", "sol", "sol-5h", "fable"):
        e = entries[name]
        assert usage_budget.pinned_evaluate(
            name, e.conserve_usage, e.harness, now=NOW, cache_path=cache, environ=env,
        ).status == STATUS_AVAILABLE

    written = usage_budget.record_observed_halt(
        "luna", entries["luna"].conserve_usage, entries=entries,
        resets_at=NOW + 3600, now=NOW, cache_path=cache, environ=env,
    )
    assert written.status == STATUS_OUT_OF_QUOTA

    def read(name):
        e = entries[name]
        return usage_budget.pinned_evaluate(
            name, e.conserve_usage, e.harness, now=NOW + 10, cache_path=cache, environ=env,
        )

    assert read("luna").status == STATUS_OUT_OF_QUOTA
    assert (read("sol").status, read("sol").resets_at) == (STATUS_OUT_OF_QUOTA, NOW + 3600)
    # A different pool on the same account, and the same pool name on another
    # harness, are different quotas: their AVAILABLE pins stand.
    assert read("sol-5h").status == STATUS_AVAILABLE
    assert read("fable").status == STATUS_AVAILABLE
    # An entry without conserve_usage has no verdict to write.
    stored = json.loads(cache.read_text())["verdicts"]
    assert "plain" not in stored


def test_record_observed_halt_without_entries_writes_only_the_named_entry(tmp_path):
    cache = tmp_path / "verdicts.json"
    env = {"CLAUDE_CODE_SESSION_ID": "s1"}
    usage_budget.record_observed_halt(
        "luna", ConserveSpec(pool="seven_day"),
        resets_at=NOW + 60, now=NOW, cache_path=cache, environ=env,
    )
    assert set(json.loads(cache.read_text())["verdicts"]) == {"luna"}
