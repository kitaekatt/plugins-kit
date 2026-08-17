"""Tests for content_pipeline.execution.drivers.inline.

Pins the A-min.2 concurrency-one inline driver: typed halt actually STOPS
claiming further units (not merely records the halt), the halt-triggering
unit returns to PENDING, resume completes a run without replaying accepted
units, D4's post-halt valid-fence-accepted / stale-fence-rejected guarantee
survives through this layer, and -- the sharpest one -- D3's cache-key
byte-identity: the driver must present ``backend.name`` to the REAL
``build_cache_key`` completely unchanged from what the untracked
``call_llm``/``submit_validated`` path would produce.
"""

from __future__ import annotations

import pytest

from content_pipeline.execution.model import StaleFenceError, UnitState
from content_pipeline.execution.store import ExecutionStore
from content_pipeline.execution.wave import ready_wave
from content_pipeline.execution.drivers.inline import run_wave
from content_pipeline.llm.backends import MockBackend
from content_pipeline.llm.platform import BackendOptions, HaltError, build_cache_key
from content_pipeline.pipeline.workunit import FlatChunkStrategy, WorkUnit


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


def _wave(store, unit_ids):
    return [store.get_unit("run-1", uid) for uid in unit_ids]


# -- typed halt stops claiming ---------------------------------------------------


def test_halt_stops_claiming_further_units_on_the_tracked_path(tmp_path):
    store = _seeded_store(tmp_path, unit_ids=("u0", "u1"))
    wave = _wave(store, ["u0", "u1"])

    def generate(work_unit):
        if work_unit.id == "u0":
            raise HaltError("rate_limit", "hit your limit")
        raise AssertionError("u1 must never be reached: claiming must have stopped")

    accepted = run_wave(store, "run-1", wave, generate=generate)

    assert accepted == []
    assert store.get_run("run-1").halted_kind == "rate_limit"

    # The strongest proof claiming actually stopped: a LATER unit is still
    # PENDING and was never claimed -- not merely that set_halt was called.
    u1 = store.get_unit("run-1", "u1")
    assert u1.state is UnitState.PENDING
    assert u1.claimed_by is None


def test_halt_triggering_unit_returns_to_pending_and_is_in_the_unfinished_set(tmp_path):
    store = _seeded_store(tmp_path, unit_ids=("u0", "u1"))
    wave = _wave(store, ["u0", "u1"])

    def generate(work_unit):
        if work_unit.id == "u0":
            raise HaltError("auth", "bad creds")
        return "text"

    run_wave(store, "run-1", wave, generate=generate)

    u0 = store.get_unit("run-1", "u0")
    assert u0.state is UnitState.PENDING

    from content_pipeline.execution.controller import unfinished_units

    assert [u.unit_id for u in unfinished_units(store, "run-1")] == ["u0", "u1"]


# -- a halt already set by the time the loop reaches the NEXT unit's claim -------


def test_run_wave_returns_accepted_units_instead_of_raising_when_halt_is_already_set_at_next_claim(
    tmp_path,
):
    """Defect (finding 2): a peer sets the halt WHILE unit 0's own generation
    is in flight, and unit 0's generate still returns text (no HaltError
    raised for THIS call). D4 means unit 0's accept still lands (a valid
    fence is never blocked by halt). But the loop's second iteration then
    calls store.claim_unit for unit 1 against an ALREADY-halted run, which
    raises RunHaltedError -- a case the old code never caught, so it
    propagated out of run_wave and the caller lost the units already
    accepted in this call. run_wave must catch it, stop claiming, and return
    what was accepted -- exactly the module's documented contract."""
    store = _seeded_store(tmp_path, unit_ids=("u0", "u1"))
    wave = _wave(store, ["u0", "u1"])

    def generate(work_unit):
        if work_unit.id == "u0":
            # Simulate a peer process halting the run out-of-band, mid
            # generation, WITHOUT this call raising HaltError itself.
            store.set_halt("run-1", "rate_limit", "a peer worker hit the limit")
            return "text-u0"
        raise AssertionError("u1's generate must never run: claiming must have stopped")

    accepted = run_wave(store, "run-1", wave, generate=generate)

    assert accepted == ["u0"]
    assert store.get_unit("run-1", "u0").state is UnitState.ACCEPTED
    u1 = store.get_unit("run-1", "u1")
    assert u1.state is UnitState.PENDING
    assert u1.claimed_by is None


# -- resume without replay -------------------------------------------------------


def test_resume_completes_a_run_without_replaying_accepted_units(tmp_path):
    store = _seeded_store(tmp_path, unit_ids=("u0", "u1", "u2"))
    wave = _wave(store, ["u0", "u1", "u2"])

    def halting_generate(work_unit):
        if work_unit.id == "u1":
            raise HaltError("rate_limit", "hit your limit")
        return f"text-{work_unit.id}"

    first_pass = run_wave(store, "run-1", wave, generate=halting_generate)
    assert first_pass == ["u0"]
    assert store.get_run("run-1").halted_kind == "rate_limit"

    from content_pipeline.execution.controller import resume_run

    resume_run(store, "run-1")
    assert store.get_run("run-1").halted_kind is None

    # A fresh ready wave excludes u0 (already ACCEPTED) -- proving a resumed
    # pass never re-presents it to the driver.
    second_wave = ready_wave(store, "run-1", FLAT_STRATEGY)
    assert [u.unit_id for u in second_wave] == ["u1", "u2"]

    calls = []

    def tracking_generate(work_unit):
        calls.append(work_unit.id)
        return f"text-{work_unit.id}"

    second_pass = run_wave(store, "run-1", second_wave, generate=tracking_generate)

    assert second_pass == ["u1", "u2"]
    assert calls == ["u1", "u2"]  # u0 never replayed
    assert store.get_unit("run-1", "u0").accepted_text == "text-u0"


# -- D4 through the driver -------------------------------------------------------


def test_d4_valid_fence_accepted_despite_halt_set_mid_flight_through_the_driver(tmp_path):
    store = _seeded_store(tmp_path, unit_ids=("u0",))
    wave = _wave(store, ["u0"])

    # Simulate a concurrent peer worker triggering the run halt WHILE this
    # unit's own generation is still in flight with an already-valid claim.
    def generate(work_unit):
        store.set_halt("run-1", "rate_limit", "a peer worker hit the limit")
        return "text-u0"

    accepted = run_wave(store, "run-1", wave, generate=generate)

    assert accepted == ["u0"]  # D4: a valid-fence submission is never blocked by halt
    assert store.get_unit("run-1", "u0").state is UnitState.ACCEPTED
    assert store.get_run("run-1").halted_kind == "rate_limit"


def test_d4_stale_fence_still_rejected_against_a_unit_the_driver_claimed(tmp_path):
    store = _seeded_store(tmp_path, unit_ids=("u0",))
    wave = _wave(store, ["u0"])

    run_wave(store, "run-1", wave, generate=lambda wu: "text-u0")
    assert store.get_unit("run-1", "u0").state is UnitState.ACCEPTED

    store.set_halt("run-1", "rate_limit", "hit your limit")

    # A late/duplicate submission carrying a stale token (anything other
    # than the driver's own claim token, which was 1 for a unit's first-ever
    # claim) is rejected regardless of halt state -- D4 is narrower than
    # "halt never blocks a submission".
    with pytest.raises(StaleFenceError):
        store.accept_unit("run-1", "u0", 0)


# -- D3: cache-key stability regression -------------------------------------------


def test_d3_driver_produces_the_same_real_cache_key_as_the_untracked_path(tmp_path):
    cache_dir = tmp_path / "cache"
    store = _seeded_store(tmp_path, unit_ids=("u0",))
    wave = _wave(store, ["u0"])

    backend = MockBackend(responses=["GENERATED-TEXT"])
    system = "system prompt"
    user = "user prompt"
    model = "test-model"

    # 1. The REAL build_cache_key, called exactly as the untracked path
    #    (a direct call_llm / submit_validated call) would build it: from
    #    the backend's own `.name`, unmodified.
    untracked_key = build_cache_key(
        backend=backend.name,
        model=model,
        system=system,
        user=user,
        options=BackendOptions(),
    )

    # 2. Run the SAME request through the new inline driver. This is a
    #    SEPARATE MockBackend instance and a SEPARATE real call into
    #    build_cache_key (via submit_validated -> call_llm) -- neither
    #    build_cache_key call is stubbed.
    tracked_backend = MockBackend(responses=["GENERATED-TEXT"])
    accepted = run_wave(
        store,
        "run-1",
        wave,
        backend=tracked_backend,
        model=model,
        system_for=lambda wu: system,
        user_for=lambda wu: user,
        parse_fn=lambda text: text,
        validators=[],
        cache_dir=cache_dir,
    )
    assert accepted == ["u0"]

    # The response cache write is keyed by build_cache_key's own output --
    # read the actual on-disk key back out rather than re-deriving it, so
    # this proves what the driver's REAL call_llm invocation actually used.
    cached_files = list(cache_dir.glob("*.json"))
    assert len(cached_files) == 1
    tracked_key = cached_files[0].stem

    assert tracked_key == untracked_key
