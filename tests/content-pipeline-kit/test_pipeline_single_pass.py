"""Tests for content_pipeline.pipeline.single_pass.

Translates the first-pass two-phase generate/apply behaviors: the ordered gate
sequence (sticky-unsupported vs. transient skip), the freshness gate (only
missing/stale generate; missing-only sweeps leave stale alone), the
generate/apply split (dry-run previews without side effects), per-unit error
isolation, and deterministic per-unit seeding. MockBackend is unnecessary here
-- generate/apply are injected callables.
"""

import pytest

from content_pipeline.freshness.classify import FreshnessState
from content_pipeline.pipeline.single_pass import (
    Disposition,
    Gate,
    run,
    run_single_pass,
    seed_for,
)
from content_pipeline.pipeline.workunit import WorkUnit


def _units(*ids):
    return [WorkUnit(id=i) for i in ids]


# -- run (stage-fold) ---------------------------------------------------------

def test_run_folds_stages():
    stages = [lambda store, ctx: store + [1], lambda store, ctx: store + [2]]
    assert run([], stages) == [1, 2]


def test_run_tolerates_mutating_stages_returning_none():
    def mutate(store, ctx):
        store.append("x")
        return None

    out = run([], [mutate])
    assert out == ["x"]


# -- gates --------------------------------------------------------------------

def test_sticky_gate_marks_unsupported_and_records():
    recorded = {}
    gate = Gate(
        name="single_speaker",
        predicate=lambda u: "multi" if u.id == "bad" else None,
        sticky=True,
    )
    outcomes = run_single_pass(
        _units("bad", "ok"),
        freshness_of=lambda u: FreshnessState.MISSING,
        generate=lambda u: "gen",
        gates=[gate],
        mark_unsupported=lambda uid, reason: recorded.__setitem__(uid, reason),
    )
    bad = outcomes[0]
    assert bad.disposition is Disposition.UNSUPPORTED
    assert bad.gate == "single_speaker"
    assert recorded == {"bad": "multi"}
    assert outcomes[1].disposition is Disposition.GENERATED


def test_non_sticky_gate_is_transient_skip_not_recorded():
    recorded = {}
    gate = Gate(
        name="auto_marker",
        predicate=lambda u: "test-conv" if u.id == "skip" else None,
        sticky=False,
    )
    outcomes = run_single_pass(
        _units("skip"),
        freshness_of=lambda u: FreshnessState.MISSING,
        generate=lambda u: "gen",
        gates=[gate],
        mark_unsupported=lambda uid, reason: recorded.__setitem__(uid, reason),
    )
    assert outcomes[0].disposition is Disposition.SKIPPED
    assert recorded == {}  # transient skip is never recorded sticky


def test_first_firing_gate_wins():
    # Order is significant: the override gate runs before the structural gate,
    # so the override reason wins when both apply.
    gates = [
        Gate(name="override", predicate=lambda u: "override-reason", sticky=True),
        Gate(name="structural", predicate=lambda u: "structural-reason", sticky=True),
    ]
    outcomes = run_single_pass(
        _units("u"),
        freshness_of=lambda u: FreshnessState.MISSING,
        generate=lambda u: "gen",
        gates=gates,
    )
    assert outcomes[0].gate == "override"
    assert outcomes[0].reason == "override-reason"


# -- freshness gate -----------------------------------------------------------

def test_fresh_unit_is_up_to_date_no_generate():
    calls = []
    outcomes = run_single_pass(
        _units("u"),
        freshness_of=lambda u: FreshnessState.FRESH,
        generate=lambda u: calls.append(u.id) or "gen",
    )
    assert outcomes[0].disposition is Disposition.UP_TO_DATE
    assert calls == []


def test_missing_only_sweep_leaves_stale_alone():
    states = {"m": FreshnessState.MISSING, "s": FreshnessState.STALE}
    outcomes = run_single_pass(
        _units("m", "s"),
        freshness_of=lambda u: states[u.id],
        generate=lambda u: "gen",
        include_stale=False,
    )
    by_id = {o.unit_id: o for o in outcomes}
    assert by_id["m"].disposition is Disposition.GENERATED
    assert by_id["s"].disposition is Disposition.UP_TO_DATE  # stale left alone


def test_default_sweep_regenerates_stale():
    outcomes = run_single_pass(
        _units("s"),
        freshness_of=lambda u: FreshnessState.STALE,
        generate=lambda u: "gen",
    )
    assert outcomes[0].disposition is Disposition.GENERATED


# -- generate / apply split ---------------------------------------------------

def test_apply_runs_after_generate():
    applied = []
    outcomes = run_single_pass(
        _units("u"),
        freshness_of=lambda u: FreshnessState.MISSING,
        generate=lambda u: {"pick": "v"},
        apply=lambda u, payload: applied.append((u.id, payload)),
    )
    assert applied == [("u", {"pick": "v"})]
    assert outcomes[0].applied is True


def test_dry_run_previews_without_generate_or_apply():
    calls = []
    outcomes = run_single_pass(
        _units("u"),
        freshness_of=lambda u: FreshnessState.MISSING,
        generate=lambda u: calls.append("gen") or "g",
        apply=lambda u, p: calls.append("apply"),
        dry_run=True,
    )
    assert outcomes[0].disposition is Disposition.GENERATED
    assert outcomes[0].applied is False
    assert calls == []  # neither side effect ran


def test_generate_returning_none_skips_apply():
    applied = []
    outcomes = run_single_pass(
        _units("u"),
        freshness_of=lambda u: FreshnessState.MISSING,
        generate=lambda u: None,
        apply=lambda u, p: applied.append(u.id),
    )
    assert applied == []
    assert outcomes[0].applied is False


def test_generate_error_isolated_per_unit():
    def generate(u):
        if u.id == "boom":
            raise RuntimeError("bad unit")
        return "ok"

    outcomes = run_single_pass(
        _units("boom", "fine"),
        freshness_of=lambda u: FreshnessState.MISSING,
        generate=generate,
    )
    by_id = {o.unit_id: o for o in outcomes}
    assert by_id["boom"].disposition is Disposition.ERROR
    assert "bad unit" in by_id["boom"].error
    assert by_id["fine"].disposition is Disposition.GENERATED  # sweep continued


# -- deterministic seeding ----------------------------------------------------

def test_seed_for_is_stable_and_id_specific():
    assert seed_for("conv_a") == seed_for("conv_a")
    assert seed_for("conv_a") != seed_for("conv_b")
