"""Partial-failure reporting and input validation against a fake bridge
(fake `smg.clip_get`, no network).

1: a fake session whose `put` fails for the second of two pending scenes
must not raise -- `apply_design` must continue past the failure, report it
with the CLIP `errors[].description` (or the exception text when the bridge
never got a chance to reply, e.g. a ConnectionError), return 1, and leave
the failed scene's backup equal to its original live definition.

2: `_layer_action` must reject an out-of-range `bri`, `xy`, or `ct` with
a SystemExit naming the field -- never let the dry-run pass a value that
would be rejected mid-write.

3: `load_group_registry` (given the universe) and the design loaders
(`_bake_targets` / `_layer_action`) must turn every malformed input into one
SystemExit naming the file/scene and the field -- never a bare KeyError,
ValueError, or AttributeError.
"""

import functools
import json

import pytest


LIGHTS = [
    {"id": "L1", "metadata": {"name": "Lamp"}},
    {"id": "L2", "metadata": {"name": "Lamp2"}},
]
ZONES = [
    {"id": "Z1", "metadata": {"name": "Room"},
     "children": [{"rid": "L1"}, {"rid": "L2"}]},
]


def _live_scene(sid, name, bri1=50.0):
    return {
        "id": sid, "metadata": {"name": name}, "group": {"rid": "Z1"},
        "actions": [
            {"target": {"rid": "L1", "rtype": "light"},
             "action": {"on": {"on": True}, "dimming": {"brightness": bri1},
                        "color": {"xy": {"x": 0.3, "y": 0.3}}}},
            {"target": {"rid": "L2", "rtype": "light"},
             "action": {"on": {"on": False}}},
        ],
    }


def _fake_clip_get(scenes):
    def go(session, resource):
        return {"light": LIGHTS, "zone": ZONES, "room": [],
                "scene": scenes}[resource]
    return go


@pytest.fixture
def fake_bridge(scene_layers, tmp_path, monkeypatch):
    """Two lights, one zone, a registry bound to a tmp scene-groups.yaml, and
    BACKUP_DIR repointed into tmp_path (hard constraint: never write to the
    repo's own tmp/)."""
    monkeypatch.setattr(scene_layers, "BACKUP_DIR", tmp_path / "backups")
    registry_path = tmp_path / "scene-groups.yaml"
    registry_path.write_text("groups:\n  - name: G\n    zones: [Room]\n")
    monkeypatch.setattr(
        scene_layers, "load_group_registry",
        functools.partial(scene_layers.load_group_registry, path=registry_path))
    return registry_path


class _FakeHTTPError(Exception):
    """Stand-in for requests.exceptions.HTTPError -- the real `requests` is a
    minimal stub in conftest.py (no `.exceptions`), so production code must
    not depend on that hierarchy; it only reads `.response` off whatever it
    catches."""

    def __init__(self, response):
        super().__init__(f"status {response.status_code}")
        self.response = response


class _FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise _FakeHTTPError(self)

    def json(self):
        return self._body


class _FakeSession:
    """`put` fails for `fail_scene_id` (a 400 with a CLIP error body, or a
    raised exception when `raise_exc` is given); every other scene's `put`
    succeeds with an empty-errors 200."""

    def __init__(self, fail_scene_id, raise_exc=None):
        self.fail_scene_id = fail_scene_id
        self.raise_exc = raise_exc
        self.puts = []

    def put(self, url, json, timeout, verify):
        self.puts.append(url)
        if self.fail_scene_id in url:
            if self.raise_exc is not None:
                raise self.raise_exc
            return _FakeResponse(
                400, {"errors": [{"description": "invalid value"}]})
        return _FakeResponse(200, {"errors": []})


class TestApplyContinuesPastAFailedScene:
    def test_bad_put_body_is_reported_and_run_continues(
            self, scene_layers, fake_bridge, monkeypatch, capsys):
        # Fresh dicts per test: apply_design mutates a scene's `actions` list
        # in place (before the PUT is attempted), so a shared/module-level
        # scene object would not reflect its ORIGINAL definition by the time
        # this test compares the backup against it.
        scene_2_original = json.loads(json.dumps(_live_scene("S2", "Movie")))
        monkeypatch.setattr(
            scene_layers.smg, "clip_get",
            _fake_clip_get([_live_scene("S1", "Relax"), _live_scene("S2", "Movie")]))
        session = _FakeSession(fail_scene_id="S2")
        design = {"scenes": [
            {"name": "Relax", "layers": [
                {"group": "G", "xy": [0.31, 0.31], "bri": 55}]},
            {"name": "Movie", "layers": [
                {"group": "G", "xy": [0.31, 0.31], "bri": 55}]},
        ]}

        rc = scene_layers.apply_design(session, design, None, assume_yes=True)

        out = capsys.readouterr().out
        assert rc == 1, "a partial failure must still exit nonzero"
        assert "Relax" in out and "OK" in out
        assert "invalid value" in out, (
            "the CLIP errors[].description must reach the report")
        assert len(session.puts) == 2, (
            "the second scene's failure must not stop the loop -- both "
            "scenes must have been attempted"
        )
        # the failed scene's backup must equal its ORIGINAL live definition
        backups = list((scene_layers.BACKUP_DIR).glob("*movie*.json"))
        assert len(backups) == 1
        assert json.loads(backups[0].read_text()) == scene_2_original

    @pytest.mark.parametrize("exc", [ConnectionError("no route"), TimeoutError()])
    def test_put_raising_is_reported_and_run_continues(
            self, scene_layers, fake_bridge, monkeypatch, capsys, exc):
        monkeypatch.setattr(
            scene_layers.smg, "clip_get",
            _fake_clip_get([_live_scene("S1", "Relax"), _live_scene("S2", "Movie")]))
        session = _FakeSession(fail_scene_id="S2", raise_exc=exc)
        design = {"scenes": [
            {"name": "Relax", "layers": [
                {"group": "G", "xy": [0.31, 0.31], "bri": 55}]},
            {"name": "Movie", "layers": [
                {"group": "G", "xy": [0.31, 0.31], "bri": 55}]},
        ]}

        rc = scene_layers.apply_design(session, design, None, assume_yes=True)

        assert rc == 1
        assert len(session.puts) == 2


class TestLayerActionValueRanges:
    def test_bri_over_100_raises_naming_field(self, scene_layers):
        with pytest.raises(SystemExit) as excinfo:
            scene_layers._layer_action(
                "s", {"group": "G", "bri": 150, "xy": [0.3, 0.3]})
        assert "bri" in str(excinfo.value)

    def test_xy_outside_unit_range_raises_naming_field(self, scene_layers):
        with pytest.raises(SystemExit) as excinfo:
            scene_layers._layer_action(
                "s", {"group": "G", "bri": 50, "xy": [1.5, 0.3]})
        assert "xy" in str(excinfo.value)

    def test_ct_outside_mirek_range_raises_naming_field(self, scene_layers):
        with pytest.raises(SystemExit) as excinfo:
            scene_layers._layer_action(
                "s", {"group": "G", "bri": 50, "ct": 50})
        assert "ct" in str(excinfo.value)


class TestLoadGroupRegistryValidation:
    def test_unknown_light_name_raises_naming_it(self, scene_layers, tmp_path):
        path = tmp_path / "scene-groups.yaml"
        path.write_text("groups:\n  - name: G\n    lights: [Ghost]\n")

        with pytest.raises(SystemExit) as excinfo:
            scene_layers.load_group_registry({}, ["Lamp", "Lamp2"], path=path)

        assert "Ghost" in str(excinfo.value)

    def test_zones_given_as_a_string_names_the_field_not_a_character(
            self, scene_layers, tmp_path):
        path = tmp_path / "scene-groups.yaml"
        path.write_text("groups:\n  - name: G\n    zones: Kitchen\n")

        with pytest.raises(SystemExit) as excinfo:
            scene_layers.load_group_registry(
                {"Kitchen": ["Lamp"]}, ["Lamp"], path=path)

        msg = str(excinfo.value)
        assert "zones" in msg
        assert "'K'" not in msg

    def test_list_rooted_document_names_the_file(self, scene_layers, tmp_path):
        path = tmp_path / "scene-groups.yaml"
        path.write_text("- not\n- a\n- mapping\n")

        with pytest.raises(SystemExit) as excinfo:
            scene_layers.load_group_registry({}, [], path=path)

        assert str(path) in str(excinfo.value)

    def test_missing_name_raises_naming_the_field(self, scene_layers, tmp_path):
        path = tmp_path / "scene-groups.yaml"
        path.write_text("groups:\n  - zones: [Room]\n")

        with pytest.raises(SystemExit) as excinfo:
            scene_layers.load_group_registry({"Room": ["Lamp"]}, ["Lamp"], path=path)

        assert "name" in str(excinfo.value)


class TestDesignLoaderValidation:
    def test_missing_group_raises_naming_the_field(self, scene_layers):
        with pytest.raises(SystemExit) as excinfo:
            scene_layers._bake_targets(
                "s", [{"bri": 50, "xy": [0.3, 0.3]}], {"G": frozenset({"Lamp"})},
                ["Lamp"])
        assert "group" in str(excinfo.value)

    def test_missing_bri_raises_naming_the_field(self, scene_layers):
        with pytest.raises(SystemExit) as excinfo:
            scene_layers._bake_targets(
                "s", [{"group": "G", "xy": [0.3, 0.3]}], {"G": frozenset({"Lamp"})},
                ["Lamp"])
        assert "bri" in str(excinfo.value)

    def test_xy_with_three_values_raises_naming_the_field(self, scene_layers):
        with pytest.raises(SystemExit) as excinfo:
            scene_layers._bake_targets(
                "s", [{"group": "G", "bri": 50, "xy": [0.3, 0.3, 0.1]}],
                {"G": frozenset({"Lamp"})}, ["Lamp"])
        assert "xy" in str(excinfo.value)


class TestLoadGroupRegistryCallersPassUniverse:
    """Premise 3 companion: every in-module caller must pass a universe, not
    just the standalone unit tests above."""

    def test_resolve_registry_passes_universe(self, scene_layers, fake_bridge,
                                              monkeypatch):
        monkeypatch.setattr(scene_layers.smg, "clip_get",
                            _fake_clip_get([_live_scene("S1", "Relax")]))
        registry, universe = scene_layers._resolve_registry(object())
        assert universe == ["Lamp", "Lamp2"]
        assert registry["G"] == frozenset({"Lamp", "Lamp2"})
