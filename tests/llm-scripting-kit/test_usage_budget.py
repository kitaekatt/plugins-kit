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


def _rollout(tmp_path, limits, name="rollout.jsonl"):
    path = tmp_path / name
    path.write_text(
        json.dumps({"type": "event_msg", "payload": {"type": "token_count"}})
        + "\n"
        + json.dumps({"type": "event_msg", "payload": {"type": "token_count", "rate_limits": limits}})
        + "\n"
    )
    return path


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
