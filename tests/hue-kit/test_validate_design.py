"""Truthful verdicts and exit codes: `validate_design` /
`_scene_pending` against a fake bridge (fake `smg.clip_get`, no network).

A design scene MISSING on the bridge must count as a discrepancy (not be
silently skipped and read as a match), and an `--scene` / `only` filter that
names nothing in the design must fail loud rather than silently validate 0
scenes as a "clean" run.
"""

import functools

import pytest


LIGHTS = [
    {"id": "L1", "metadata": {"name": "Lamp"}},
    {"id": "L2", "metadata": {"name": "Lamp2"}},
]
ZONES = [
    {"id": "Z1", "metadata": {"name": "Room"},
     "children": [{"rid": "L1"}, {"rid": "L2"}]},
]
LIVE_SCENE = {
    "id": "S1", "metadata": {"name": "Relax"}, "group": {"rid": "Z1"},
    "actions": [
        {"target": {"rid": "L1", "rtype": "light"},
         "action": {"on": {"on": True}, "dimming": {"brightness": 50.0},
                    "color": {"xy": {"x": 0.3, "y": 0.3}}}},
        {"target": {"rid": "L2", "rtype": "light"},
         "action": {"on": {"on": False}}},
    ],
}


def _fake_clip_get(resource):
    return {
        "light": LIGHTS,
        "zone": ZONES,
        "room": [],
        "scene": [LIVE_SCENE],
    }[resource]


@pytest.fixture
def fake_bridge(scene_layers, tmp_path, monkeypatch):
    """A minimal two-light, one-zone, one-scene fake bridge, plus a registry
    bound to a tmp scene-groups.yaml -- load_group_registry's default `path`
    argument is bound to GROUPS_YAML at import time, so it is rebound per-test
    via functools.partial rather than relying on the module-level default."""
    monkeypatch.setattr(scene_layers.smg, "clip_get",
                        lambda session, resource: _fake_clip_get(resource))
    registry_path = tmp_path / "scene-groups.yaml"
    registry_path.write_text("groups:\n  - name: G\n    zones: [Room]\n")
    monkeypatch.setattr(
        scene_layers, "load_group_registry",
        functools.partial(scene_layers.load_group_registry, path=registry_path))
    return object()  # the session; clip_get ignores it


class TestMissingSceneIsADiscrepancy:
    def test_missing_scene_exits_discrepancy_not_zero(self, scene_layers, fake_bridge):
        design = {"scenes": [{"name": "Ghost", "layers": []}]}

        rc = scene_layers.validate_design(fake_bridge, design, None)

        assert rc != 0
        assert rc == scene_layers.EXIT_DISCREPANCY


class TestUnmatchedFilterIsAnError:
    def test_unmatched_only_raises_naming_the_scene(self, scene_layers, fake_bridge):
        design = {"scenes": [{"name": "Relax", "layers": [
            {"group": "G", "xy": [0.3, 0.3], "bri": 50},
        ]}]}

        with pytest.raises(SystemExit) as excinfo:
            scene_layers.validate_design(fake_bridge, design, {"Relx"})

        assert "Relx" in str(excinfo.value)
