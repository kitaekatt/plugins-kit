"""Equivalence between the legacy single-pass path and the tracked execution
path -- A-min.2's last unpinned exit-criterion leg: "equivalent three-unit
legacy and tracked inline runs produce equivalent applied content".

Both paths are driven with the SAME deterministic ``generate`` function (no
randomness, no backend, no network) so any divergence in the resulting
applied payloads is a real behavioral difference between the two code paths,
never an artifact of nondeterministic input. The other three legs of the
exit criterion are already pinned elsewhere and are deliberately not
repeated here:

- identical cache keys across paths --
  ``test_execution_driver_inline.py::test_d3_driver_produces_the_same_real_cache_key_as_the_untracked_path``
- a forced halt leaves an inspectable, full unfinished set --
  ``test_execution_driver_inline.py``'s halt tests
- resume completes a run without replaying accepted units --
  ``test_execution_driver_inline.py::test_resume_completes_a_run_without_replaying_accepted_units``

ORDER is part of the contract asserted here. Both paths process units in the
same caller-supplied order: the legacy path iterates ``units`` in the order
given; the tracked path's ``FlatChunkStrategy`` preserves ``select`` order
into registration order (``store.register_units`` assigns ordinals in
argument order), ``ready_wave``'s flat branch returns ``PENDING`` units in
ordinal order, and ``finalize_run`` applies in ordinal order -- which for a
freshly-registered run is registration order. Nothing in either contract
promises order in general (a different strategy or a concurrent driver could
reorder), but for THIS shape -- flat units, the concurrency-one inline
driver, a single wave -- the applied sequences are expected to line up
index-for-index, and asserting a list (not a set) is what would catch a
reordering regression between the two paths.

Legacy entry point chosen: ``pipeline.single_pass.run_single_pass``, not
``guarded_sweep``. ``guarded_sweep`` (``cli/budget.py``) wraps
``run_single_pass`` with a token/cost budget guard -- an orthogonal concern
this brief has no reason to exercise, and layering it in would only add
budget bookkeeping between the two paths' outputs to reconcile for no
comparison benefit. ``run_single_pass`` is the untracked path's actual
two-phase generate/apply loop -- the thing the tracked path replaces -- so it
is the better-matched entry point.

Both ``pipeline/single_pass.py`` and every ``execution/*`` module are
read-only here: driven as a consumer would, never edited.
"""

from __future__ import annotations

from content_pipeline.execution.controller import RunAdapter, finalize_run, prepare_run
from content_pipeline.execution.drivers.inline import run_wave
from content_pipeline.execution.model import UnitState
from content_pipeline.execution.store import ExecutionStore
from content_pipeline.freshness.classify import FreshnessState
from content_pipeline.pipeline.single_pass import Disposition, run_single_pass
from content_pipeline.pipeline.workunit import FlatChunkStrategy, WorkUnit


def _text_for(unit_id: str) -> str:
    """The one deterministic 'generation' both paths share -- no randomness,
    no backend, no network -- so any divergence in applied output reflects a
    real behavioral difference between the two execution paths."""
    return f"GENERATED-{unit_id}"


def _new_store(tmp_path) -> ExecutionStore:
    return ExecutionStore(tmp_path / "run.db")


def _seeded_store(tmp_path, unit_ids) -> ExecutionStore:
    store = _new_store(tmp_path)
    store.create_run(
        "run-1", driver="inline", backend="mock", model="test-model", adapter_version="1"
    )
    store.register_units("run-1", list(unit_ids))
    return store


FLAT_STRATEGY = FlatChunkStrategy(select=lambda store: [])


def _run_legacy(unit_ids, freshness_by_id):
    """Drive ``run_single_pass`` over ``unit_ids`` with the shared
    deterministic generate function. Returns ``(outcomes, applied)`` where
    ``applied`` is ``[(unit_id, payload), ...]`` in application order."""
    units = [WorkUnit(id=uid) for uid in unit_ids]
    applied = []
    outcomes = run_single_pass(
        units,
        freshness_of=lambda u: freshness_by_id[u.id],
        generate=lambda u: _text_for(u.id),
        apply=lambda u, payload: applied.append((u.id, payload)),
    )
    return outcomes, applied


def _run_tracked(tmp_path, unit_ids, freshness_by_id):
    """Drive ``prepare_run`` -> ``run_wave`` -> ``finalize_run`` over the same
    units with the SAME deterministic generate function. Returns
    ``(store, accepted, applied)`` where ``applied`` is
    ``[(unit_id, payload), ...]`` in application order."""
    store = _seeded_store(tmp_path, unit_ids)
    work_units = [WorkUnit(id=uid) for uid in unit_ids]
    applied = []
    adapter = RunAdapter(
        parse_fn=lambda text: text,  # identity: applied payload == generated text
        apply=lambda unit_id, payload: applied.append((unit_id, payload)),
    )

    wave = prepare_run(
        store,
        "run-1",
        FLAT_STRATEGY,
        work_units,
        freshness_of=lambda wu: freshness_by_id[wu.id],
    )
    accepted = run_wave(store, "run-1", wave, adapter, generate=lambda wu: _text_for(wu.id))
    finalize_run(store, "run-1", adapter)
    return store, accepted, applied


# -- 1. three units, all generating ------------------------------------------


def test_three_generating_units_apply_the_same_payloads_in_the_same_order(tmp_path):
    unit_ids = ("u0", "u1", "u2")
    fresh = {uid: FreshnessState.MISSING for uid in unit_ids}

    outcomes, legacy_applied = _run_legacy(unit_ids, fresh)
    assert [o.disposition for o in outcomes] == [Disposition.GENERATED] * 3

    store, accepted, tracked_applied = _run_tracked(tmp_path, unit_ids, fresh)
    assert accepted == list(unit_ids)

    assert tracked_applied == legacy_applied
    assert legacy_applied == [(uid, _text_for(uid)) for uid in unit_ids]


# -- 2. a unit that is up to date / gated ------------------------------------


def test_an_up_to_date_unit_is_skipped_by_both_paths_and_the_other_units_still_match(
    tmp_path,
):
    """u1 is FRESH; u0 and u2 are MISSING.

    The legacy path treats u1 as ``Disposition.UP_TO_DATE`` and continues the
    sweep inline (no separate prepare phase; a skip is never recorded
    anywhere). The tracked path's ``prepare_run`` records u1 as a terminal
    ``UnitState.SKIPPED`` unit up front, so u1 is never even presented to
    ``run_wave``. Those bookkeeping differences are the documented, intended
    divergence between the two paths (see the module docstring and the
    brief's "Where the paths may legitimately differ" section) -- this test
    does not assert them equal. What it asserts is that u0 and u2's APPLIED
    payloads still match exactly across both paths despite the differently
    shaped skip of u1.
    """
    unit_ids = ("u0", "u1", "u2")
    fresh = {
        "u0": FreshnessState.MISSING,
        "u1": FreshnessState.FRESH,
        "u2": FreshnessState.MISSING,
    }

    outcomes, legacy_applied = _run_legacy(unit_ids, fresh)
    by_id = {o.unit_id: o for o in outcomes}
    assert by_id["u0"].disposition is Disposition.GENERATED
    assert by_id["u1"].disposition is Disposition.UP_TO_DATE
    assert by_id["u2"].disposition is Disposition.GENERATED

    store, accepted, tracked_applied = _run_tracked(tmp_path, unit_ids, fresh)
    assert accepted == ["u0", "u2"]  # u1 never presented to run_wave at all
    assert store.get_unit("run-1", "u1").state is UnitState.SKIPPED  # tracked-only terminal state

    assert tracked_applied == legacy_applied
    assert legacy_applied == [("u0", _text_for("u0")), ("u2", _text_for("u2"))]


# -- 3. identical cache keys across the two paths ----------------------------
#
# Skipped -- would duplicate an existing test, not a gap. The D3 regression
# test already makes this exact comparison: the REAL build_cache_key output
# for an untracked-shaped call vs. the tracked inline driver's actual
# on-disk cache write, for the same backend/model/system/user, byte-for-byte
# (test_execution_driver_inline.py::
# test_d3_driver_produces_the_same_real_cache_key_as_the_untracked_path).
#
# The legacy `run_single_pass` path exercised in THIS file never touches
# `llm.platform.call_llm` / `build_cache_key` at all -- its `generate` is a
# plain caller-supplied callable with no backend involved (see
# test_pipeline_single_pass.py's module docstring: "MockBackend is
# unnecessary here -- generate/apply are injected callables"). Driving it
# through a backend here, just to get a second cache-key comparison, would
# re-run the D3 test's own assertion under a different name rather than
# compare anything D3 does not already cover.
