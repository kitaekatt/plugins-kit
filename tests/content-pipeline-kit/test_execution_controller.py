"""Tests for content_pipeline.execution.controller.

Pins the A-min.2 prepare/finalize lifecycle: terminal skips recorded via
claim + terminal fail_unit(terminal_state=UnitState.SKIPPED), ``skip:...``
error strings, unfinished_units as a set with holes and the halt-triggering
unit included, deterministic serial finalize order, finalize idempotence,
apply_unknown refusal absent a reconciliation hook, and the fail-closed
refusal of an ACCEPTED unit with no recorded accepted_text.
"""

from __future__ import annotations

import pytest

from content_pipeline.execution.controller import (
    ApplyUnknownError,
    GraphOrderMismatchError,
    MissingAcceptedTextError,
    RunAdapter,
    finalize_run,
    pause_run,
    prepare_run,
    resume_run,
    unfinished_units,
)
from content_pipeline.execution.model import UnitState
from content_pipeline.execution.store import ExecutionStore
from content_pipeline.execution.wave import ready_wave
from content_pipeline.pipeline.single_pass import Gate
from content_pipeline.pipeline.workunit import FlatChunkStrategy, GraphWalkStrategy, WorkUnit


def _new_store(tmp_path) -> ExecutionStore:
    return ExecutionStore(tmp_path / "run.db")


def _seeded_store(tmp_path, *, unit_ids=("u0", "u1", "u2")) -> ExecutionStore:
    store = _new_store(tmp_path)
    store.create_run(
        "run-1", driver="inline", backend="mock", model="test-model", adapter_version="1"
    )
    store.register_units("run-1", list(unit_ids))
    return store


FLAT_STRATEGY = FlatChunkStrategy(select=lambda store: [])


# -- prepare_run: gates -------------------------------------------------------


def test_prepare_run_records_terminal_skip_for_non_sticky_gate(tmp_path):
    store = _seeded_store(tmp_path, unit_ids=("u0", "u1"))
    work_units = [WorkUnit(id="u0"), WorkUnit(id="u1")]
    gate = Gate(name="single_speaker", predicate=lambda wu: "two speakers" if wu.id == "u0" else None)

    wave = prepare_run(store, "run-1", FLAT_STRATEGY, work_units, gates=[gate])

    assert [u.unit_id for u in wave] == ["u1"]
    u0 = store.get_unit("run-1", "u0")
    assert u0.state is UnitState.SKIPPED
    attempts = store.list_attempts("run-1", "u0")
    fail_attempt = [a for a in attempts if a.error]
    assert fail_attempt[-1].error == "skip:gate:single_speaker:two speakers"


def test_prepare_run_records_terminal_skip_for_sticky_gate_and_marks_unsupported(tmp_path):
    store = _seeded_store(tmp_path, unit_ids=("u0",))
    work_units = [WorkUnit(id="u0")]
    gate = Gate(name="missing_data", predicate=lambda wu: "no source", sticky=True)
    marked = []

    wave = prepare_run(
        store, "run-1", FLAT_STRATEGY, work_units, gates=[gate],
        mark_unsupported=lambda uid, reason: marked.append((uid, reason)),
    )

    assert wave == []
    assert marked == [("u0", "no source")]
    attempts = store.list_attempts("run-1", "u0")
    assert attempts[-1].error == "skip:unsupported:missing_data:no source"
    assert store.get_unit("run-1", "u0").state is UnitState.SKIPPED


def test_prepare_run_records_terminal_skip_for_up_to_date_freshness(tmp_path):
    store = _seeded_store(tmp_path, unit_ids=("u0", "u1"))
    work_units = [WorkUnit(id="u0"), WorkUnit(id="u1")]

    from content_pipeline.freshness.classify import FreshnessState

    def freshness_of(wu):
        return FreshnessState.FRESH if wu.id == "u0" else FreshnessState.MISSING

    wave = prepare_run(store, "run-1", FLAT_STRATEGY, work_units, freshness_of=freshness_of)

    assert [u.unit_id for u in wave] == ["u1"]
    attempts = store.list_attempts("run-1", "u0")
    assert attempts[-1].error == "skip:up_to_date"


def test_prepare_run_up_to_date_unit_0_releases_unit_1_in_a_graph_run(tmp_path):
    """Finding 1, the exact wedge: prepare_run + GraphWalkStrategy + an
    up-to-date unit 0 must release unit 1, not wedge it forever. Before the
    fix, the terminal skip landed unit 0 in UnitState.FAILED, and
    wave._graph_ready_wave only treats ACCEPTED (or no predecessor) as
    satisfied -- so unit 1 could never become claimable through this path,
    for the life of the run."""
    store = _seeded_store(tmp_path, unit_ids=("u0", "u1"))
    work_units = [WorkUnit(id="u0"), WorkUnit(id="u1")]
    graph_strategy = GraphWalkStrategy(order=lambda s: ["u0", "u1"])

    from content_pipeline.freshness.classify import FreshnessState

    def freshness_of(wu):
        return FreshnessState.FRESH if wu.id == "u0" else FreshnessState.MISSING

    wave = prepare_run(
        store, "run-1", graph_strategy, work_units, freshness_of=freshness_of
    )

    assert [u.unit_id for u in wave] == ["u1"]
    assert store.get_unit("run-1", "u0").state is UnitState.SKIPPED

    # Not a transient artifact of this one call: a fresh ready_wave read
    # against the durable store confirms unit 1 stays claimable.
    again = ready_wave(store, "run-1", graph_strategy)
    assert [u.unit_id for u in again] == ["u1"]


def test_prepare_run_leaves_unmatched_pending_units_untouched(tmp_path):
    # "u1" has no corresponding WorkUnit -- out of scope for this prepare call.
    store = _seeded_store(tmp_path, unit_ids=("u0", "u1"))
    work_units = [WorkUnit(id="u0")]
    gate = Gate(name="always", predicate=lambda wu: "nope")

    prepare_run(store, "run-1", FLAT_STRATEGY, work_units, gates=[gate])

    assert store.get_unit("run-1", "u0").state is UnitState.SKIPPED
    assert store.get_unit("run-1", "u1").state is UnitState.PENDING


def test_prepare_run_returns_ready_wave_computed_after_skips_land(tmp_path):
    store = _seeded_store(tmp_path, unit_ids=("u0", "u1", "u2"))
    work_units = [WorkUnit(id="u0"), WorkUnit(id="u1"), WorkUnit(id="u2")]
    gate = Gate(name="drop_u1", predicate=lambda wu: "dropped" if wu.id == "u1" else None)

    wave = prepare_run(store, "run-1", FLAT_STRATEGY, work_units, gates=[gate])

    assert [u.unit_id for u in wave] == ["u0", "u2"]
    # Cross-check against a fresh ready_wave call: skips are durable, not a
    # transient view only prepare_run's own return value carries.
    again = ready_wave(store, "run-1", FLAT_STRATEGY)
    assert [u.unit_id for u in again] == ["u0", "u2"]


# -- prepare_run: graph order validated against registration order (2026-08-17) --


def test_prepare_run_graph_order_matching_registration_passes(tmp_path):
    """strategy.order() and registration agree -- prepare_run proceeds
    exactly as it did before this check existed."""
    store = _seeded_store(tmp_path, unit_ids=("u0", "u1", "u2"))
    work_units = [WorkUnit(id="u0"), WorkUnit(id="u1"), WorkUnit(id="u2")]
    graph_strategy = GraphWalkStrategy(order=lambda s: ["u0", "u1", "u2"])

    wave = prepare_run(store, "run-1", graph_strategy, work_units)

    assert [u.unit_id for u in wave] == ["u0"]


def test_prepare_run_graph_order_permuted_registration_raises_naming_first_divergence(tmp_path):
    """The exact silent defect this check exists to catch: units registered
    in an order different from what strategy.order() claims. Before this
    check, prepare_run would proceed anyway -- wave._graph_ready_wave
    linearizes purely by registered ordinal and never consults order() at
    all, so a successor could be generated before its predecessor is
    applied, silently. Registration order is u0, u1, u2 (see _seeded_store);
    the strategy's order says u1 comes first -- diverging at position 0
    (expected "u1" per the strategy, but the unit registered at ordinal 0
    is "u0")."""
    store = _seeded_store(tmp_path, unit_ids=("u0", "u1", "u2"))
    work_units = [WorkUnit(id="u0"), WorkUnit(id="u1"), WorkUnit(id="u2")]
    graph_strategy = GraphWalkStrategy(order=lambda s: ["u1", "u0", "u2"])

    with pytest.raises(GraphOrderMismatchError) as exc_info:
        prepare_run(store, "run-1", graph_strategy, work_units)

    assert exc_info.value.position == 0
    assert exc_info.value.expected == "u1"
    assert exc_info.value.actual == "u0"


def test_prepare_run_graph_order_uses_the_caller_supplied_graph_source(tmp_path):
    """order() is called with graph_source -- the CONSUMER's own store, not
    the ExecutionStore -- and prepare_run must pass it through unchanged."""
    store = _seeded_store(tmp_path, unit_ids=("u0", "u1"))
    work_units = [WorkUnit(id="u0"), WorkUnit(id="u1")]
    consumer_store = {"nodes": ["u0", "u1"]}
    graph_strategy = GraphWalkStrategy(order=lambda s: s["nodes"])

    wave = prepare_run(
        store, "run-1", graph_strategy, work_units, graph_source=consumer_store
    )

    assert [u.unit_id for u in wave] == ["u0"]


def test_prepare_run_flat_strategy_any_registration_order_unaffected(tmp_path):
    """A flat strategy carries no ordering claim -- no validation ever runs,
    regardless of registration order or work_units order."""
    store = _seeded_store(tmp_path, unit_ids=("u2", "u0", "u1"))
    work_units = [WorkUnit(id="u0"), WorkUnit(id="u1"), WorkUnit(id="u2")]

    wave = prepare_run(store, "run-1", FLAT_STRATEGY, work_units)

    assert [u.unit_id for u in wave] == ["u2", "u0", "u1"]


# -- unfinished_units ----------------------------------------------------------


def test_unfinished_units_excludes_accepted_and_failed(tmp_path):
    store = _seeded_store(tmp_path, unit_ids=("u0", "u1", "u2"))
    r0 = store.claim_unit("run-1", "u0", "w")
    store.accept_unit("run-1", "u0", r0.fencing_token)
    r1 = store.claim_unit("run-1", "u1", "w")
    store.fail_unit("run-1", "u1", r1.fencing_token, terminal=True)

    unfinished = unfinished_units(store, "run-1")
    assert [u.unit_id for u in unfinished] == ["u2"]


def test_unfinished_units_is_a_set_with_holes_and_original_ordinals(tmp_path):
    store = _new_store(tmp_path)
    store.create_run("run-1", driver="inline", backend="mock", model="m", adapter_version="1")
    store.register_units("run-1", ["u0", "u1"])
    r0 = store.claim_unit("run-1", "u0", "w")
    store.accept_unit("run-1", "u0", r0.fencing_token)  # ordinal 0 now terminal (a hole)
    store.register_units("run-1", ["u2", "u3"])

    unfinished = unfinished_units(store, "run-1")
    assert [(u.unit_id, u.ordinal) for u in unfinished] == [("u1", 1), ("u2", 2), ("u3", 3)]


def test_unfinished_units_includes_the_halt_triggering_unit(tmp_path):
    # A unit returned to PENDING by a halting driver (terminal=False) is
    # unfinished, not terminal -- unfinished_units must still surface it.
    store = _seeded_store(tmp_path, unit_ids=("u0", "u1"))
    r0 = store.claim_unit("run-1", "u0", "w")
    store.fail_unit("run-1", "u0", r0.fencing_token, error="halt:rate_limit", terminal=False)
    store.set_halt("run-1", "rate_limit", "hit your limit")

    unfinished = unfinished_units(store, "run-1")
    assert [u.unit_id for u in unfinished] == ["u0", "u1"]


# -- pause_run / resume_run -----------------------------------------------------


def test_pause_run_sets_halt_kind_pause(tmp_path):
    store = _seeded_store(tmp_path, unit_ids=("u0",))
    pause_run(store, "run-1", detail="operator requested")
    run = store.get_run("run-1")
    assert run.halted_kind == "pause"
    assert run.halted_detail == "operator requested"


def test_resume_run_clears_halt(tmp_path):
    store = _seeded_store(tmp_path, unit_ids=("u0",))
    pause_run(store, "run-1")
    resume_run(store, "run-1")
    run = store.get_run("run-1")
    assert run.halted_kind is None


# -- finalize_run: deterministic order ------------------------------------------


def test_finalize_run_applies_in_ordinal_order_regardless_of_accept_order(tmp_path):
    store = _seeded_store(tmp_path, unit_ids=("u0", "u1", "u2"))
    # Accept out of ordinal order: u2, then u0, then u1.
    for uid in ("u2", "u0", "u1"):
        claim = store.claim_unit("run-1", uid, "w")
        store.accept_unit("run-1", uid, claim.fencing_token, text=f"text-{uid}")

    applied_order = []
    adapter = RunAdapter(
        parse_fn=lambda text: text,
        apply=lambda unit_id, payload: applied_order.append(unit_id),
    )
    finalize_run(store, "run-1", adapter)

    assert applied_order == ["u0", "u1", "u2"]


def test_finalize_run_recovers_payload_via_parse_fn_from_accepted_text(tmp_path):
    store = _seeded_store(tmp_path, unit_ids=("u0",))
    claim = store.claim_unit("run-1", "u0", "w")
    store.accept_unit("run-1", "u0", claim.fencing_token, text="RAW-TEXT")

    seen_payloads = []
    adapter = RunAdapter(
        parse_fn=lambda text: text.lower(),
        apply=lambda unit_id, payload: seen_payloads.append(payload),
    )
    finalize_run(store, "run-1", adapter)

    assert seen_payloads == ["raw-text"]


def test_finalize_run_records_apply_started_and_succeeded(tmp_path):
    store = _seeded_store(tmp_path, unit_ids=("u0",))
    claim = store.claim_unit("run-1", "u0", "w")
    store.accept_unit("run-1", "u0", claim.fencing_token, text="t")

    adapter = RunAdapter(parse_fn=lambda t: t, apply=lambda uid, payload: None)
    finalize_run(store, "run-1", adapter)

    kinds = [a.kind.value for a in store.list_attempts("run-1", "u0")]
    assert "apply_started" in kinds
    assert "apply_succeeded" in kinds
    assert kinds.index("apply_started") < kinds.index("apply_succeeded")


# -- finalize_run: idempotence and apply_unknown --------------------------------


def test_finalize_run_is_idempotent_never_replays_a_successful_apply(tmp_path):
    store = _seeded_store(tmp_path, unit_ids=("u0", "u1"))
    for uid in ("u0", "u1"):
        claim = store.claim_unit("run-1", uid, "w")
        store.accept_unit("run-1", uid, claim.fencing_token, text=f"t-{uid}")

    apply_calls = []
    adapter = RunAdapter(
        parse_fn=lambda t: t, apply=lambda uid, payload: apply_calls.append(uid)
    )

    first = finalize_run(store, "run-1", adapter)
    assert first == ["u0", "u1"]
    assert apply_calls == ["u0", "u1"]

    second = finalize_run(store, "run-1", adapter)
    assert second == []
    assert apply_calls == ["u0", "u1"]  # not replayed


def test_finalize_run_refuses_on_apply_unknown_absent_reconciliation(tmp_path):
    store = _seeded_store(tmp_path, unit_ids=("u0",))
    claim = store.claim_unit("run-1", "u0", "w")
    store.accept_unit("run-1", "u0", claim.fencing_token, text="t")
    store.record_apply_started("run-1", "u0")  # crash before apply_succeeded

    adapter = RunAdapter(parse_fn=lambda t: t, apply=lambda uid, payload: None)
    with pytest.raises(ApplyUnknownError):
        finalize_run(store, "run-1", adapter)


def test_finalize_run_apply_unknown_reconciled_as_landed_skips_reapply(tmp_path):
    store = _seeded_store(tmp_path, unit_ids=("u0",))
    claim = store.claim_unit("run-1", "u0", "w")
    store.accept_unit("run-1", "u0", claim.fencing_token, text="t")
    store.record_apply_started("run-1", "u0")

    apply_calls = []
    adapter = RunAdapter(
        parse_fn=lambda t: t,
        apply=lambda uid, payload: apply_calls.append(uid),
        reconcile=lambda uid: True,
    )
    applied = finalize_run(store, "run-1", adapter)

    assert applied == []  # apply() itself was never (re)invoked
    assert apply_calls == []
    kinds = [a.kind.value for a in store.list_attempts("run-1", "u0")]
    assert kinds.count("apply_succeeded") == 1


def test_finalize_run_apply_unknown_reconciled_as_not_landed_reapplies(tmp_path):
    store = _seeded_store(tmp_path, unit_ids=("u0",))
    claim = store.claim_unit("run-1", "u0", "w")
    store.accept_unit("run-1", "u0", claim.fencing_token, text="t")
    store.record_apply_started("run-1", "u0")

    apply_calls = []
    adapter = RunAdapter(
        parse_fn=lambda t: t,
        apply=lambda uid, payload: apply_calls.append(uid),
        reconcile=lambda uid: False,
    )
    applied = finalize_run(store, "run-1", adapter)

    assert applied == ["u0"]
    assert apply_calls == ["u0"]


# -- finalize_run: fail closed on a None accepted_text (finding 3) ---------------


def test_finalize_run_refuses_accepted_unit_with_no_recorded_text(tmp_path):
    """Defect (finding 3): accept_unit permits omitting `text` (cli/run.py's
    `accept` never passes it; a pre-0.7.2 row may also have migrated to
    NULL). Without a guard, finalize_run called parse_fn(None) and applied
    whatever an identity parse_fn returned -- a unit marked applied with no
    payload, fail-OPEN against D6's posture. finalize_run must refuse with a
    typed error naming the unit, and must never call apply."""
    store = _seeded_store(tmp_path, unit_ids=("u0",))
    claim = store.claim_unit("run-1", "u0", "w")
    store.accept_unit("run-1", "u0", claim.fencing_token)  # no text= -- accepted_text stays NULL

    assert store.get_unit("run-1", "u0").accepted_text is None

    apply_calls = []
    adapter = RunAdapter(
        parse_fn=lambda text: text,  # an identity parse_fn: would happily "parse" None
        apply=lambda uid, payload: apply_calls.append((uid, payload)),
    )

    with pytest.raises(MissingAcceptedTextError) as exc_info:
        finalize_run(store, "run-1", adapter)

    assert exc_info.value.unit_id == "u0"
    assert apply_calls == []  # apply must never have been called
    kinds = [a.kind.value for a in store.list_attempts("run-1", "u0")]
    assert "apply_started" not in kinds  # no apply-attempt row left behind either


# -- finding 4: pin the idempotence SCAN RULE, not just its current output -------
#
# _last_apply_kind must resolve by attempt INSERTION ORDER (list_attempts
# orders by the attempts table's autoincrement id), never by `at=` alone.
# The tests below would still pass against a wrong "any APPLY_SUCCEEDED in
# the log wins" implementation, so each one is built to fail against that
# wrong implementation specifically -- see the inline comments.


def test_finalize_run_treats_started_succeeded_started_as_apply_unknown(tmp_path):
    """A crash during RE-apply: started, succeeded, started (no closing
    succeeded) must read as apply_unknown, NOT as applied. A wrong "any
    APPLY_SUCCEEDED in the log wins" implementation would see the
    APPLY_SUCCEEDED row and treat this unit as already applied -- silently
    skipping the crashed re-apply instead of refusing."""
    store = _seeded_store(tmp_path, unit_ids=("u0",))
    claim = store.claim_unit("run-1", "u0", "w")
    store.accept_unit("run-1", "u0", claim.fencing_token, text="t")
    store.record_apply_started("run-1", "u0", at=1.0)
    store.record_apply_succeeded("run-1", "u0", at=2.0)
    store.record_apply_started("run-1", "u0", at=3.0)  # crashed before a second succeeded

    adapter = RunAdapter(parse_fn=lambda t: t, apply=lambda uid, payload: None)
    with pytest.raises(ApplyUnknownError):
        finalize_run(store, "run-1", adapter)


def test_finalize_run_treats_two_starts_then_one_succeeded_as_applied(tmp_path):
    """Two apply_started (e.g. a retried finalize pass whose first attempt
    never got as far as recording its own apply_started a second time before
    crashing -- any sequence with more STARTED than SUCCEEDED rows before the
    trailing SUCCEEDED) followed by ONE apply_succeeded must read as applied
    -- the same idempotence skip as a clean started/succeeded pair, keyed off
    the LAST attempt, not a count comparison."""
    store = _seeded_store(tmp_path, unit_ids=("u0",))
    claim = store.claim_unit("run-1", "u0", "w")
    store.accept_unit("run-1", "u0", claim.fencing_token, text="t")
    store.record_apply_started("run-1", "u0", at=1.0)
    store.record_apply_started("run-1", "u0", at=2.0)
    store.record_apply_succeeded("run-1", "u0", at=3.0)

    apply_calls = []
    adapter = RunAdapter(
        parse_fn=lambda t: t, apply=lambda uid, payload: apply_calls.append(uid)
    )
    applied = finalize_run(store, "run-1", adapter)

    assert applied == []  # already applied; not replayed
    assert apply_calls == []


def test_finalize_run_scan_resolves_by_insertion_order_when_timestamps_are_equal(tmp_path):
    """Both the started and succeeded rows share the SAME `at=` -- the scan
    must still resolve by insertion order (list_attempts orders by the
    attempts table's autoincrement id), not by re-sorting on `at`, which
    would make the outcome undefined (or implementation-dependent) for a
    tied timestamp. A max-by-timestamp implementation could pick either row
    when both share `at`; this pins that started-then-succeeded (in
    insertion order) reads as applied regardless of the tie."""
    store = _seeded_store(tmp_path, unit_ids=("u0",))
    claim = store.claim_unit("run-1", "u0", "w")
    store.accept_unit("run-1", "u0", claim.fencing_token, text="t")
    same_at = 100.0
    store.record_apply_started("run-1", "u0", at=same_at)
    store.record_apply_succeeded("run-1", "u0", at=same_at)

    attempts = store.list_attempts("run-1", "u0")
    apply_related = [a for a in attempts if a.kind.value.startswith("apply_")]
    assert len(apply_related) == 2
    assert apply_related[0].at == apply_related[1].at == same_at  # the tie, confirmed

    apply_calls = []
    adapter = RunAdapter(
        parse_fn=lambda t: t, apply=lambda uid, payload: apply_calls.append(uid)
    )
    applied = finalize_run(store, "run-1", adapter)

    assert applied == []  # already applied; not replayed despite the timestamp tie
    assert apply_calls == []
