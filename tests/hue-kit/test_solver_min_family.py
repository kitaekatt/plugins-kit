"""Pin the solver's core primitives against a hand-verified minimum family.

Fixture: three lights (A, B, C), two scenes --

    S1: cells [{A, B}, {C}]
    S2: cells [{A}, {B, C}]

Each light carries a distinct pair of (S1-cell, S2-cell) membership, so the
common refinement (atoms_of) is the three singletons {A}, {B}, {C} -- no two
lights are ever painted identically across both scenes.

A single group cannot express both scenes: each scene has two non-empty
cells with different final light-membership, so at least two distinct
groups are required to reproduce either scene's partition (one group,
reused for two differently-colored cells in the same scene, would leave
both cells painted with whichever layer applied last). The family
F = {{A, B}, {B, C}} reproduces both:

    S1: paint {B, C} first (covers cell {C}), then {A, B} on top (covers
        cell {A, B}) -- C keeps the {B, C} layer, A and B end on {A, B}.
    S2: paint {A, B} first (covers cell {A}), then {B, C} on top (covers
        cell {B, C}) -- A keeps the {A, B} layer, B and C end on {B, C}.

So the certified minimum family size for this fixture is 2, independent of
any solver run -- it is verified by the paragraph above, not by the code
under test.
"""

from __future__ import annotations


def _xy_cell(lights, xy=(0.4, 0.4), bri=100):
    return {"lights": list(lights), "mode": "xy", "xy": list(xy), "bri": bri}


def _fixture():
    return {
        "universe": ["A", "B", "C"],
        "light_groups": {},
        "scenes": [
            {"name": "S1", "cells": [
                _xy_cell(["A", "B"], xy=(0.4, 0.4)),
                _xy_cell(["C"], xy=(0.1, 0.1)),
            ]},
            {"name": "S2", "cells": [
                _xy_cell(["A"], xy=(0.3, 0.3)),
                _xy_cell(["B", "C"], xy=(0.2, 0.2)),
            ]},
        ],
    }


class TestAtomsOfRefinesToSingletons:
    """atoms_of returns the common refinement of every scene's cell
    partition -- here, one atom per light, since each light's (S1-cell,
    S2-cell) pair is unique."""

    def test_atoms_are_the_three_singletons(self, scene_layers):
        _, _, scenes = scene_layers.build_model(_fixture())

        atoms = scene_layers.atoms_of(scenes)

        assert sorted(sorted(a) for a in atoms) == [["A"], ["B"], ["C"]]


class TestMinFamilyFindsTheCertifiedMinimum:
    """min_family, searched over every union of the atoms, returns a family
    of exactly the certified minimum size (2), not the naive 3-singleton
    upper bound."""

    def test_min_family_size_equals_the_certified_minimum(self, scene_layers):
        _, _, scenes = scene_layers.build_model(_fixture())
        atoms = scene_layers.atoms_of(scenes)
        pool = scene_layers.unions_of(atoms)

        fmin = scene_layers.min_family(pool, scenes)

        assert len(fmin) == 2


class TestExpressAndBakeReproduceGroundTruth:
    """express() finds a bottom-to-top layer stack over the certified
    minimum family, and bake_ok() confirms painting that stack reproduces
    each scene's exact per-light cell assignment (not merely "some
    plausible" result)."""

    def test_both_scenes_bake_exactly_over_the_minimum_family(self, scene_layers):
        _, _, scenes = scene_layers.build_model(_fixture())
        atoms = scene_layers.atoms_of(scenes)
        pool = scene_layers.unions_of(atoms)
        fmin = scene_layers.min_family(pool, scenes)

        for scene in scenes:
            layers = scene_layers.express(scene, fmin, want_layers=True)
            assert layers is not None
            assert scene_layers.bake_ok(scene, layers)

    def test_express_without_want_layers_is_a_boolean_feasibility_check(
            self, scene_layers):
        _, _, scenes = scene_layers.build_model(_fixture())
        atoms = scene_layers.atoms_of(scenes)
        pool = scene_layers.unions_of(atoms)
        fmin = scene_layers.min_family(pool, scenes)

        assert scene_layers.express(scenes[0], fmin) is True

    def test_express_fails_over_a_family_too_small_to_cover_a_cell(
            self, scene_layers):
        """A family missing any group that is a superset of {A, B} cannot
        express S1's first cell -- express must report infeasibility (None),
        not a wrong layer stack."""
        _, _, scenes = scene_layers.build_model(_fixture())
        too_small = {frozenset(["C"])}

        assert scene_layers.express(scenes[0], too_small) is None
        assert scene_layers.express(scenes[0], too_small,
                                     want_layers=True) is None
