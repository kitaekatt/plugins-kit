"""Tests for content_pipeline.freshness.seed.

These cases mirror the deterministic-seeding invariant both source systems
rely on -- a stochastic gating decision seeded from stable identity, never
from run-local state, so repeated runs reproduce the same rolls and do not
perpetually churn freshness hashes -- translated to the plugin's neutral
vocabulary as the port-equivalence baseline. No game/domain concepts appear
in the test bodies: an "identifier" names a unit.
"""

import hashlib
import random

from content_pipeline.freshness.seed import deterministic_seed, seeded_random


def test_seed_is_stable_for_same_identifier():
    assert deterministic_seed("unit-42") == deterministic_seed("unit-42")


def test_seed_differs_for_different_identifiers():
    assert deterministic_seed("unit-a") != deterministic_seed("unit-b")


def test_seed_matches_source_formula():
    # Equivalence baseline: with salt="" and bits=64 the seed is exactly
    # int(sha256(identifier).hexdigest()[:16], 16) -- the formula both
    # source systems use -- so a port reproduces their rolls byte-for-byte.
    identifier = "unit-42"
    expected = int(hashlib.sha256(identifier.encode("utf-8")).hexdigest()[:16], 16)
    assert deterministic_seed(identifier) == expected


def test_seed_is_non_negative_and_bounded():
    seed = deterministic_seed("unit-42")
    assert 0 <= seed < 2 ** 64


def test_salt_changes_the_seed():
    assert deterministic_seed("unit-42") != deterministic_seed("unit-42", "salt")


def test_different_salts_decorrelate():
    a = deterministic_seed("unit-42", "phase-a")
    b = deterministic_seed("unit-42", "phase-b")
    assert a != b


def test_bits_parameter_widens_the_seed_space():
    narrow = deterministic_seed("unit-42", bits=32)
    wide = deterministic_seed("unit-42", bits=64)
    assert narrow < 2 ** 32
    assert wide < 2 ** 64
    # The narrow seed is the high hex prefix of the wide one.
    assert wide >> 32 == narrow


def test_invalid_bits_raise():
    import pytest

    with pytest.raises(ValueError):
        deterministic_seed("unit-42", bits=0)
    with pytest.raises(ValueError):
        deterministic_seed("unit-42", bits=7)  # not a multiple of 4


def test_seeded_random_is_reproducible():
    first = seeded_random("unit-42").random()
    second = seeded_random("unit-42").random()
    assert first == second


def test_seeded_random_matches_manual_seed():
    rng = seeded_random("unit-42")
    manual = random.Random(deterministic_seed("unit-42"))
    assert [rng.random() for _ in range(5)] == [manual.random() for _ in range(5)]


def test_seeded_random_decisions_stable_across_runs():
    # The invariant in practice: a "should this item generate?" coin flip
    # keyed on identity is identical every run, so it never churns hashes.
    def gate(identifier):
        return seeded_random(identifier).random() < 0.5

    run_one = {i: gate(f"unit-{i}") for i in range(50)}
    run_two = {i: gate(f"unit-{i}") for i in range(50)}
    assert run_one == run_two
