"""I02: scene identity is name-keyed and whole-home (settled product decision
-- no room-qualified key, no composite key). Two defects follow from that:

1. `_bridge_maps` (name2rids + scene_by_name) and `extract_from_bridge` build
   a name->scene mapping that is last-wins, so two live scenes sharing a name
   silently collapse to one everywhere downstream. Both must refuse loudly,
   BEFORE any write, naming the duplicated scene and its owning rooms/zones.

2. Generated YAML (export_groups / export_designs) used to emit scene, group,
   zone and light names unquoted, so a name like `Off`, `Relax #2`,
   `Movie: night`, `[Test]` or `007` changed identity on reload under YAML
   1.1's bool / comment / flow-sequence / octal rules. Every such name must
   now round-trip through json.dumps (a valid YAML scalar) unchanged.
"""

import functools

import pytest
import yaml


LIGHTS = [{"id": "L1", "metadata": {"name": "Lamp"}}]
ROOMS = [
    {"id": "R1", "metadata": {"name": "Living Room"}},
    {"id": "R2", "metadata": {"name": "Bedroom"}},
]


def _dup_scene(sid, room_rid):
    return {
        "id": sid, "metadata": {"name": "Relax"}, "group": {"rid": room_rid},
        "actions": [],
    }


def _fake_clip_get(scenes):
    def go(session, resource):
        return {"light": LIGHTS, "zone": [], "room": ROOMS,
                "scene": scenes}[resource]
    return go


class _NoPutSession:
    """A session whose `put` must never be called -- both code paths under
    test refuse before touching the bridge at all."""

    def put(self, *a, **k):
        raise AssertionError("must not PUT -- duplicate scenes must be "
                             "refused before any write")


class TestDuplicateSceneNamesAreRefused:
    def test_bridge_maps_refuses_and_names_both_owners(
            self, scene_layers, monkeypatch):
        scenes = [_dup_scene("S1", "R1"), _dup_scene("S2", "R2")]
        monkeypatch.setattr(scene_layers.smg, "clip_get",
                            _fake_clip_get(scenes))
        session = _NoPutSession()

        with pytest.raises(SystemExit) as excinfo:
            scene_layers._bridge_maps(session)

        msg = str(excinfo.value)
        assert "Relax" in msg
        assert "Living Room" in msg
        assert "Bedroom" in msg

    def test_extract_from_bridge_refuses_and_names_both_owners(
            self, scene_layers, monkeypatch):
        scenes = [_dup_scene("S1", "R1"), _dup_scene("S2", "R2")]
        monkeypatch.setattr(scene_layers.smg, "clip_get",
                            _fake_clip_get(scenes))
        session = _NoPutSession()
        monkeypatch.setattr(scene_layers, "bridge_session", lambda: session)

        with pytest.raises(SystemExit) as excinfo:
            scene_layers.extract_from_bridge()

        msg = str(excinfo.value)
        assert "Relax" in msg
        assert "Living Room" in msg
        assert "Bedroom" in msg


# ===========================================================================
# Round trip: names that break unquoted YAML must survive export -> reload.
# ===========================================================================
UNIVERSE = ["Lamp #2", "Hall: ceiling", "Desk, left", "Off", "007"]
ZONE_NAME = "Zone: A"
ZONE_LIGHTS = ["Lamp #2", "Off"]


def _lit_cell(bri, x, y):
    return {"lights": list(UNIVERSE), "mode": "xy", "bri": bri, "xy": [x, y]}


def _designs_data():
    return {
        "universe": UNIVERSE,
        "light_groups": {ZONE_NAME: ZONE_LIGHTS},
        "scenes": [
            {"name": "Off", "scale": 0.0, "cells": [],
             "off_lights": list(UNIVERSE)},
            {"name": "Relax #2", "scale": 50.0,
             "cells": [_lit_cell(50.0, 0.55, 0.35)], "off_lights": []},
            {"name": "Movie: night", "scale": 20.0,
             "cells": [_lit_cell(20.0, 0.60, 0.30)], "off_lights": []},
            {"name": "[Test]", "scale": 80.0,
             "cells": [_lit_cell(80.0, 0.40, 0.40)], "off_lights": []},
        ],
    }


class TestGeneratedYamlRoundTrips:
    def test_export_groups_round_trips_light_and_zone_names(
            self, scene_layers, monkeypatch):
        data = {
            "universe": UNIVERSE,
            "light_groups": {ZONE_NAME: ZONE_LIGHTS},
            "scenes": [],
        }
        U = frozenset(UNIVERSE)
        zone_a = frozenset(ZONE_LIGHTS)

        def fake_solve(scenes):
            F = {U, zone_a}
            return F, F, {}, F, F

        monkeypatch.setattr(scene_layers, "solve", fake_solve)

        text = scene_layers.export_groups(data)
        doc = yaml.safe_load(text)

        seen_lights = set()
        seen_zones = set()
        for entry in doc["groups"]:
            seen_lights.update(entry.get("lights", []))
            seen_zones.update(entry.get("zones", []))

        assert seen_lights == set(UNIVERSE)
        assert seen_zones == {ZONE_NAME}

    def test_export_designs_round_trips_scene_and_group_names(
            self, scene_layers, tmp_path, monkeypatch):
        registry_path = tmp_path / "scene-groups.yaml"
        registry_path.write_text(
            yaml.safe_dump({"groups": [{"name": "G", "lights": UNIVERSE}]}))
        monkeypatch.setattr(
            scene_layers, "load_group_registry",
            functools.partial(scene_layers.load_group_registry,
                              path=registry_path))

        text, _n_groups = scene_layers.export_designs(_designs_data())
        doc = yaml.safe_load(text)

        names = [sc["name"] for sc in doc["scenes"]]
        assert names == ["Off", "Relax #2", "Movie: night", "[Test]"]

        for sc in doc["scenes"]:
            for layer in sc.get("layers") or []:
                assert layer["group"] == "G"
