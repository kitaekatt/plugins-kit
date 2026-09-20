"""Solver bounds and deterministic ties.

Three properties of the meta-group solver in scene-layers.py:

1. `export_designs()` renders a registry-based design straight from the
   registry family; it does not solve for a minimum, so a scene set that is
   over the solver's own caps still exports as long as the registry
   expresses every scene.
2. `solve()` refuses a scene set over its caps BEFORE enumerating any
   candidate pool -- a pre-check on cell and atom counts precedes
   `cell_union_pool`, which would otherwise pay an unbounded 2**m
   enumeration per scene.
3. The solver's output is a pure function of its input: the same scenes
   produce byte-identical `--export-groups` output and the same minimum
   family under PYTHONHASHSEED 1 and 2, because every size-based sort keys
   on a total order rather than raw set/frozenset iteration.

No network: every scene set here is in-memory or a `--cells` fixture file.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "plugins" / "hue-kit" / "scripts" / "scene-layers.py"


def _xy_cell(lights, xy=(0.4, 0.4), bri=100):
    return {"lights": list(lights), "mode": "xy", "xy": list(xy), "bri": bri}


def _solver_scene(name, cell_light_lists, off=()):
    """Build a scene dict in the shape `solve()` consumes directly (as
    `build_model` would produce it): cells and off as frozensets, not raw
    colour dicts."""
    cells = [frozenset(x) for x in cell_light_lists]
    lit = frozenset().union(*cells) if cells else frozenset()
    return {"name": name, "cells": cells, "L": lit, "off": frozenset(off)}


class TestExportDesignsIgnoresTheSolverCaps:
    """Premise 1: export_designs() renders straight from the registry family
    and must not depend on the (capped) solver -- a scene set that pushes
    atoms over the cap still exports when the registry expresses it."""

    def test_export_designs_succeeds_past_the_atom_cap(self, scene_layers,
                                                        monkeypatch):
        n = 21
        universe = [f"L{k}" for k in range(n)]
        scenes = []
        for k in range(n):
            lit = universe[k]
            off = [u for u in universe if u != lit]
            scenes.append({"name": f"scene{k}", "cells": [_xy_cell([lit])],
                            "off_lights": off})
        data = {"universe": universe, "light_groups": {}, "scenes": scenes}

        fam = [("ALL", frozenset(universe))] + \
              [(f"G{k}", frozenset([universe[k]])) for k in range(n)]
        monkeypatch.setattr(scene_layers, "load_group_registry",
                            lambda *a, **k: fam)

        text, n_groups = scene_layers.export_designs(data)
        assert n_groups == len(fam)
        for k in range(n):
            assert f"scene{k}" in text


class TestSolveRefusesBeforeEnumerating:
    """Premise 2: a scene over the cell cap makes solve() refuse before
    cell_union_pool ever runs."""

    def test_solve_refuses_a_scene_over_the_cell_cap(self, scene_layers,
                                                      monkeypatch):
        lights = [f"L{i}" for i in range(24)]
        scenes = [_solver_scene("Scene", [[light] for light in lights])]

        def _boom(*_a, **_k):
            raise AssertionError("cell_union_pool must not be entered")
        monkeypatch.setattr(scene_layers, "cell_union_pool", _boom)

        with pytest.raises(SystemExit) as excinfo:
            scene_layers.solve(scenes)
        message = str(excinfo.value)
        assert "24" in message
        assert "split" in message


class TestDeterministicTies:
    """Premise 3: express() already totally orders its candidates by
    (len(g), sorted(g)); the remaining nondeterminism was hash-order
    iteration in the size sorts of min_family/slack_family/export_groups.
    Verified across real subprocesses under different PYTHONHASHSEED values
    -- an in-process check cannot vary the hash seed of a running
    interpreter."""

    def _env(self, seed):
        env = dict(os.environ)
        env["PYTHONHASHSEED"] = str(seed)
        # job-kit's suite sets this process-wide (tests/job-kit/conftest.py)
        # to block ITS OWN scripts' re-exec during ITS tests; a real
        # subprocess launched here is not a re-exec and must not inherit
        # that guard, or scene-layers.py runs under the bare venv (no
        # requests/yaml) instead of the hue-kit plugin venv.
        env.pop("_BOOTSTRAP_GUARD_VENV_REEXEC", None)
        return env

    def _run(self, cells_path, seed):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--cells", str(cells_path),
             "--export-groups", "-"],
            capture_output=True, check=True, env=self._env(seed))
        return result.stdout

    def _run_json(self, cells_path, seed):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--cells", str(cells_path),
             "--json"],
            capture_output=True, check=True, env=self._env(seed))
        return result.stdout

    def _run_report(self, cells_path, seed):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--cells", str(cells_path)],
            capture_output=True, check=True, env=self._env(seed))
        return result.stdout

    def test_export_groups_deterministic_with_two_equal_size_groups(
            self, tmp_path):
        # A single scene with two singleton cells forces Fmin = {{A}, {B}}
        # -- both groups the same size, so only a total-order tiebreak (not
        # raw set iteration) fixes their listing order.
        data = {
            "universe": ["A", "B"],
            "light_groups": {},
            "scenes": [{"name": "S1", "cells": [
                _xy_cell(["A"], xy=(0.6, 0.3)),
                _xy_cell(["B"], xy=(0.15, 0.06)),
            ]}],
        }
        cells_path = tmp_path / "equal_groups.json"
        cells_path.write_text(json.dumps(data))

        out1 = self._run(cells_path, 1)
        out2 = self._run(cells_path, 2)
        assert out1 == out2

    def test_json_result_deterministic_with_two_equal_size_groups(
            self, tmp_path):
        # Same fixture as the --export-groups case above: json_result()'s
        # family listing is a separate size sort over the same tied family
        # and needs the same total-order tiebreak.
        data = {
            "universe": ["A", "B"],
            "light_groups": {},
            "scenes": [{"name": "S1", "cells": [
                _xy_cell(["A"], xy=(0.6, 0.3)),
                _xy_cell(["B"], xy=(0.15, 0.06)),
            ]}],
        }
        cells_path = tmp_path / "equal_groups_json.json"
        cells_path.write_text(json.dumps(data))

        out1 = self._run_json(cells_path, 1)
        out2 = self._run_json(cells_path, 2)
        assert out1 == out2

    def test_text_report_deterministic_with_two_equal_size_groups(
            self, tmp_path):
        # Same fixture again: report()'s "GLOBAL META-GROUP FAMILY" listing
        # is a third size sort over the same tied family, and it is the
        # output a user reads and diffs run to run.
        data = {
            "universe": ["A", "B"],
            "light_groups": {},
            "scenes": [{"name": "S1", "cells": [
                _xy_cell(["A"], xy=(0.6, 0.3)),
                _xy_cell(["B"], xy=(0.15, 0.06)),
            ]}],
        }
        cells_path = tmp_path / "equal_groups_report.json"
        cells_path.write_text(json.dumps(data))

        out1 = self._run_report(cells_path, 1)
        out2 = self._run_report(cells_path, 2)
        assert out1 == out2

    def test_export_groups_deterministic_with_two_optimal_families(
            self, tmp_path):
        # Two scenes over a 4-light universe, each a 2+2 split along a
        # different pairing of the same 4 lights. The certified minimum has
        # more than one family of equal size AND equal pairwise overlap;
        # which one min_family records first depends on candidate order.
        data = {
            "universe": ["A", "B", "C", "D"],
            "light_groups": {},
            "scenes": [
                {"name": "S1", "cells": [
                    _xy_cell(["A", "B"], xy=(0.6, 0.3)),
                    _xy_cell(["C", "D"], xy=(0.15, 0.06)),
                ]},
                {"name": "S2", "cells": [
                    _xy_cell(["A", "C"], xy=(0.3, 0.4), bri=80),
                    _xy_cell(["B", "D"], xy=(0.5, 0.2), bri=60),
                ]},
            ],
        }
        cells_path = tmp_path / "tied_families.json"
        cells_path.write_text(json.dumps(data))

        out1 = self._run(cells_path, 1)
        out2 = self._run(cells_path, 2)
        assert out1 == out2
