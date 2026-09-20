"""build_model's two guard rails against a malformed --cells export.

build_model requires each scene's lit cells to partition disjointly and, once
merged with its off_lights, to cover exactly the declared universe -- no
more, no less. Both violations must raise SystemExit with a message naming
the offending scene and the specific lights at fault, not a bare KeyError or
a silent wrong model.
"""

from __future__ import annotations

import pytest


def _xy_cell(lights, xy=(0.4, 0.4), bri=100):
    return {"lights": list(lights), "mode": "xy", "xy": list(xy), "bri": bri}


class TestOverlappingCellsRefused:
    """Two lit cells in the same scene sharing a light is a modelling error
    -- a light cannot hold two colours in one scene -- and must be refused
    before any solving happens."""

    def test_overlap_raises_naming_the_scene_and_shared_lights(self, scene_layers):
        data = {
            "universe": ["A", "B"],
            "light_groups": {},
            "scenes": [{"name": "Overlap Scene", "cells": [
                _xy_cell(["A", "B"], xy=(0.1, 0.1)),
                _xy_cell(["A"], xy=(0.2, 0.2)),
            ]}],
        }

        with pytest.raises(SystemExit) as excinfo:
            scene_layers.build_model(data)

        message = str(excinfo.value)
        assert "Overlap Scene" in message
        assert "'A'" in message

    def test_no_overlap_between_disjoint_cells_builds_cleanly(self, scene_layers):
        """Counterpart: disjoint cells covering the whole universe build
        without raising, so the guard is specific to actual overlap."""
        data = {
            "universe": ["A", "B"],
            "light_groups": {},
            "scenes": [{"name": "Clean Scene", "cells": [
                _xy_cell(["A"], xy=(0.1, 0.1)),
                _xy_cell(["B"], xy=(0.2, 0.2)),
            ]}],
        }

        _, _, scenes = scene_layers.build_model(data)

        assert scenes[0]["name"] == "Clean Scene"


class TestUniverseMismatchRefused:
    """A scene's cells + off_lights must equal the declared universe exactly
    -- an unknown light (not in universe) or a light the scene never
    accounts for must both be refused, and the message must name both sets
    so the mismatch is diagnosable from the error text alone."""

    def test_unknown_and_unaccounted_lights_both_named(self, scene_layers):
        data = {
            "universe": ["A", "B"],
            "light_groups": {},
            "scenes": [{"name": "Mismatch Scene", "cells": [
                _xy_cell(["A", "C"], xy=(0.1, 0.1)),  # C is not in universe
                # B is never mentioned -- unaccounted
            ]}],
        }

        with pytest.raises(SystemExit) as excinfo:
            scene_layers.build_model(data)

        message = str(excinfo.value)
        assert "Mismatch Scene" in message
        assert "'C'" in message
        assert "'B'" in message

    def test_off_lights_count_toward_universe_coverage(self, scene_layers):
        """A light accounted for via off_lights (not a lit cell) satisfies
        the universe check -- off_lights is a legitimate second source of
        coverage, not a bypass of it."""
        data = {
            "universe": ["A", "B"],
            "light_groups": {},
            "scenes": [{"name": "Off Scene", "cells": [
                _xy_cell(["A"], xy=(0.1, 0.1)),
            ], "off_lights": ["B"]}],
        }

        _, _, scenes = scene_layers.build_model(data)

        assert scenes[0]["off"] == frozenset(["B"])
