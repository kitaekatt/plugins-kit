"""Tests for content_pipeline.pipeline.single_pass.

Pins the stage-fold ``run`` and deterministic per-unit seeding (``seed_for``).
"""

from content_pipeline.pipeline.single_pass import run, seed_for


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


# -- deterministic seeding ----------------------------------------------------

def test_seed_for_is_stable_and_id_specific():
    assert seed_for("conv_a") == seed_for("conv_a")
    assert seed_for("conv_a") != seed_for("conv_b")
