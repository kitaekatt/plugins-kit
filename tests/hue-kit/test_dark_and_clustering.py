"""One definition of dark, and a clustering property that survives export.

1. `smg.action_sig(a).mode == "off"` and `scene_layers._effectively_off(a)`
   must agree on every action -- both delegate to the single `smg.is_dark`
   test, so a raw bridge action and its analyzed signature can never
   disagree about darkness.
2. A cluster's emitted representative (the value `_cell_cfg` writes into the
   design, which `_layer_action` then turns into the target bridge action)
   must lie within `smg.BRI_TOL` / `smg.XY_TOL` of every one of the
   cluster's own members, so `_action_diff(member, target)` is always None --
   a scene built from those members validates clean on the first export.
3. `smg.layered_report`'s family heading names the family "certified
   minimum" only when the caller passes a certification result.

No network: every fixture below is an in-memory action/scene/cluster dict.
"""

from __future__ import annotations

import yaml


def _xy_action(on, bri=None, x=0.4, y=0.4):
    action = {"on": {"on": on}}
    if bri is not None:
        action["dimming"] = {"brightness": bri}
    if x is not None:
        action["color"] = {"xy": {"x": x, "y": y}}
    return action


class TestOneDefinitionOfDark:
    def test_action_sig_and_effectively_off_agree(self, scene_layers):
        smg = scene_layers.smg
        cases = [
            {"on": {"on": True}, "dimming": {"brightness": 0},
             "color": {"xy": {"x": 0.4, "y": 0.4}}},   # on:true, bri:0
            {"dimming": {"brightness": 50},
             "color": {"xy": {"x": 0.4, "y": 0.4}}},   # no "on" field
            {"on": {"on": False}},                       # explicit off
            {"on": {"on": True}, "dimming": {"brightness": 50},
             "color": {"xy": {"x": 0.4, "y": 0.4}}},   # ordinary lit action
        ]
        for action in cases:
            sig_off = smg.action_sig(action).mode == "off"
            eff_off = scene_layers._effectively_off(action)
            assert sig_off == eff_off, (action, sig_off, eff_off)

    def test_on_true_zero_brightness_is_dark(self, scene_layers):
        smg = scene_layers.smg
        action = _xy_action(True, bri=0)
        assert smg.action_sig(action).mode == "off"
        assert scene_layers._effectively_off(action) is True

    def test_missing_on_field_is_dark(self, scene_layers):
        smg = scene_layers.smg
        action = {"dimming": {"brightness": 50},
                  "color": {"xy": {"x": 0.4, "y": 0.4}}}
        assert smg.action_sig(action).mode == "off"
        assert scene_layers._effectively_off(action) is True

    def test_ordinary_lit_action_is_not_dark(self, scene_layers):
        smg = scene_layers.smg
        action = _xy_action(True, bri=50)
        assert smg.action_sig(action).mode != "off"
        assert scene_layers._effectively_off(action) is False


class TestClusterWithinTolerance:
    def _scene(self, bris):
        rids = [f"r{i}" for i in range(len(bris))]
        actions = [
            {"target": {"rid": rid},
             "action": _xy_action(True, bri=bri)}
            for rid, bri in zip(rids, bris)
        ]
        lights = {rid: f"L{i}" for i, rid in enumerate(rids)}
        scene = {"metadata": {"name": "Test"}, "actions": actions}
        return scene, lights

    def _live_by_light(self, scene, lights):
        by_rid = {a["target"]["rid"]: a["action"] for a in scene["actions"]}
        return {lights[rid]: action for rid, action in by_rid.items()}

    def test_every_member_validates_clean_against_the_emitted_representative(
            self, scene_layers):
        smg = scene_layers.smg
        bris = [10.0, 11.5, 12.25, 12.75]
        scene, lights = self._scene(bris)
        live_by_light = self._live_by_light(scene, lights)

        res = smg.analyze_scene(scene, lights, {}, "Owner")
        assert len(res.clusters) >= 1

        for cluster in res.clusters:
            sig = cluster.sig
            cell = {"lights": list(cluster.lights), "mode": sig.mode,
                    "bri": sig.bri}
            if sig.mode == "xy":
                cell["xy"] = [sig.x, sig.y]
            cfg, _hsl = scene_layers._cell_cfg(cell)
            layer = yaml.safe_load("{ group: G, " + cfg + " }")
            target = scene_layers._layer_action("Test", layer)
            for light_name in cluster.lights:
                live = live_by_light[light_name]
                assert scene_layers._action_diff(live, target) is None, (
                    light_name, live, target)

    def test_clustering_does_not_merge_across_tolerance(self, scene_layers):
        """Sanity check on the fixture itself: without the tightening pass a
        naive reader might expect one 4-member cluster; the property this
        unit adds is that whatever clusters come out, each one's own members
        validate clean -- checked structurally too, via mean distance."""
        smg = scene_layers.smg
        bris = [10.0, 11.5, 12.25, 12.75]
        scene, lights = self._scene(bris)
        res = smg.analyze_scene(scene, lights, {}, "Owner")
        for cluster in res.clusters:
            member_bris = [
                smg.action_sig(a["action"]).bri
                for a in scene["actions"]
                if lights[a["target"]["rid"]] in cluster.lights
            ]
            rep = cluster.sig.bri
            for b in member_bris:
                assert abs(b - rep) <= smg.BRI_TOL


class TestCertifiedMinimumGated:
    def test_absent_without_certification(self, scene_layers):
        smg = scene_layers.smg
        html = smg.layered_report([], [], {})
        assert "certified minimum" not in html

    def test_present_with_certification(self, scene_layers):
        smg = scene_layers.smg
        html = smg.layered_report([], [], {}, certified=True)
        assert "certified minimum" in html
