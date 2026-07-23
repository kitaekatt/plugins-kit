"""Tests for content_pipeline.freshness.classify.

These cases mirror the state-classifier behavior pinned by BOTH source
systems' suites -- the single-hash pipeline's ``player``/``hold``/``missing``/
``stale``/``fresh`` line states and the two-tier pipeline's ``human > missing
> stale > fresh`` translation states -- translated to the plugin's neutral
vocabulary as the port-equivalence baseline. No game/domain concepts appear
in the test bodies: a "record" carries a human value, a machine value, and a
recorded generation hash; ``excluded`` is the caller's domain verdict that an
item does not participate in generation.
"""

from content_pipeline.freshness.classify import (
    FreshnessState,
    bucket_counts,
    classify,
    needs_generation,
)


def _record(*, human="", machine="", generation_hash=""):
    return {"human": human, "machine": machine, "generation_hash": generation_hash}


# -- priority: HUMAN wins -----------------------------------------------------

def test_human_wins_even_if_stale():
    rec = _record(human="edited", machine="old-machine", generation_hash="stale")
    assert classify(rec, "current") is FreshnessState.HUMAN


def test_human_beats_missing():
    rec = _record(human="edited", machine="")
    assert classify(rec, "any") is FreshnessState.HUMAN


def test_human_beats_excluded():
    # Union decision: a human value wins even on an otherwise-excluded item.
    rec = _record(human="edited")
    assert classify(rec, "any", excluded=True) is FreshnessState.HUMAN


def test_whitespace_human_is_not_a_human_value():
    rec = _record(human="   ", machine="m", generation_hash="H")
    assert classify(rec, "H") is FreshnessState.FRESH


# -- EXCLUDED -----------------------------------------------------------------

def test_excluded_when_flag_set_and_no_human():
    rec = _record(machine="", generation_hash="")
    assert classify(rec, "any", excluded=True) is FreshnessState.EXCLUDED


def test_excluded_beats_missing():
    # An excluded item with no machine value classifies EXCLUDED, not MISSING.
    rec = _record(machine="")
    assert classify(rec, "any", excluded=True) is FreshnessState.EXCLUDED


# -- MISSING ------------------------------------------------------------------

def test_missing_when_machine_absent():
    rec = _record(machine="")
    assert classify(rec, "any") is FreshnessState.MISSING


def test_missing_when_record_is_none():
    assert classify(None, "any") is FreshnessState.MISSING


def test_missing_beats_stale():
    # Machine empty + non-matching hash -> MISSING (not STALE).
    rec = _record(machine="", generation_hash="old")
    assert classify(rec, "new") is FreshnessState.MISSING


# -- STALE --------------------------------------------------------------------

def test_stale_when_hash_mismatch():
    rec = _record(machine="value", generation_hash="old")
    assert classify(rec, "new") is FreshnessState.STALE


def test_empty_recorded_hash_on_present_value_is_stale():
    # Deviation #2: a present machine value with no recorded hash is STALE
    # (can't verify -> needs regen), matching this package's state defs.
    rec = _record(machine="value", generation_hash="")
    assert classify(rec, "current") is FreshnessState.STALE


# -- FRESH --------------------------------------------------------------------

def test_fresh_when_hash_matches():
    rec = _record(machine="value", generation_hash="H")
    assert classify(rec, "H") is FreshnessState.FRESH


# -- configurable field names -------------------------------------------------

def test_custom_field_names():
    rec = {"h": "", "m": "value", "gh": "H"}
    state = classify(
        rec, "H", human_field="h", machine_field="m", hash_field="gh"
    )
    assert state is FreshnessState.FRESH


def test_dataclass_record_is_duck_typed():
    from dataclasses import dataclass

    @dataclass
    class Rec:
        human: str = ""
        machine: str = ""
        generation_hash: str = ""

    assert classify(Rec(machine="v", generation_hash="H"), "H") is FreshnessState.FRESH


# -- needs_generation ---------------------------------------------------------

def test_needs_generation_missing_and_stale():
    assert needs_generation(FreshnessState.MISSING) is True
    assert needs_generation(FreshnessState.STALE) is True


def test_needs_generation_missing_only_mode_leaves_stale_alone():
    assert needs_generation(FreshnessState.MISSING, include_stale=False) is True
    assert needs_generation(FreshnessState.STALE, include_stale=False) is False


def test_needs_generation_false_for_terminal_states():
    for state in (FreshnessState.HUMAN, FreshnessState.EXCLUDED, FreshnessState.FRESH):
        assert needs_generation(state) is False


# -- bucket_counts (coverage over one predicate) ------------------------------

def test_bucket_counts_covers_every_state():
    counts = bucket_counts([])
    assert set(counts) == set(FreshnessState)
    assert all(v == 0 for v in counts.values())


def test_bucket_counts_tallies():
    records = [
        _record(human="h"),
        _record(machine="v", generation_hash="H"),          # fresh vs "H"
        _record(machine="v", generation_hash="old"),         # stale vs "H"
        _record(machine=""),                                 # missing
    ]
    states = [classify(r, "H") for r in records]
    excluded_state = classify(_record(), "H", excluded=True)
    states.append(excluded_state)
    counts = bucket_counts(states)
    assert counts[FreshnessState.HUMAN] == 1
    assert counts[FreshnessState.FRESH] == 1
    assert counts[FreshnessState.STALE] == 1
    assert counts[FreshnessState.MISSING] == 1
    assert counts[FreshnessState.EXCLUDED] == 1


def test_needs_set_and_buckets_agree_via_one_predicate():
    # The invariant both source systems rely on: the "needs regen" set and
    # the coverage buckets are derived from the SAME classify predicate, so
    # they cannot drift apart.
    records = [
        _record(machine=""),                            # missing -> needs
        _record(machine="v", generation_hash="old"),    # stale   -> needs
        _record(machine="v", generation_hash="H"),       # fresh   -> not
        _record(human="h"),                              # human   -> not
    ]
    states = [classify(r, "H") for r in records]
    needs = [s for s in states if needs_generation(s)]
    counts = bucket_counts(states)
    assert len(needs) == counts[FreshnessState.MISSING] + counts[FreshnessState.STALE]
