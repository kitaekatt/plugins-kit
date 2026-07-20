"""Scene-diff and bridge-fingerprint semantics.

The zero-brightness cases below are a regression guard for a round-trip bug:
some scenes (the Hue app writes them this way) store a dark light as
`on: true, brightness: 0` rather than `on: false`. The analyzer reads that as
off, so `export` materialises the scene as all-off -- but the diff used to
compare against the raw `on: true` and report a discrepancy. The result was a
scene that could never validate clean: every `validate` reported the same
lights, and `apply` could not silence it, because the design and the bridge
already agreed about the only thing that matters (the light is dark).
"""

OFF = {"on": {"on": False}}
ON_FULL = {"on": {"on": True}, "dimming": {"brightness": 100.0}}
ON_ZERO = {"on": {"on": True}, "dimming": {"brightness": 0.0},
           "color": {"xy": {"x": 0.6897, "y": 0.3096}}}


class TestEffectivelyOff:
    def test_explicit_off(self, scene_layers):
        assert scene_layers._effectively_off(OFF) is True

    def test_absent_action_is_off(self, scene_layers):
        """A light the scene never sets: the layered model bakes it as OFF."""
        assert scene_layers._effectively_off({}) is True

    def test_on_at_zero_brightness_is_off(self, scene_layers):
        assert scene_layers._effectively_off(ON_ZERO) is True

    def test_on_with_brightness_is_not_off(self, scene_layers):
        assert scene_layers._effectively_off(ON_FULL) is False

    def test_on_without_dimming_is_not_off(self, scene_layers):
        """No dimming key means unspecified, not zero -- the light is lit."""
        assert scene_layers._effectively_off({"on": {"on": True}}) is False


class TestActionDiff:
    def test_zero_brightness_matches_explicit_off(self, scene_layers):
        """The regression: these two describe the same dark bulb."""
        assert scene_layers._action_diff(ON_ZERO, OFF) is None
        assert scene_layers._action_diff(OFF, ON_ZERO) is None

    def test_off_matches_off(self, scene_layers):
        assert scene_layers._action_diff(OFF, OFF) is None

    def test_lit_vs_dark_still_differs(self, scene_layers):
        """The fix must not swallow a real on/off difference."""
        assert scene_layers._action_diff(ON_FULL, OFF) is not None
        assert scene_layers._action_diff(OFF, ON_FULL) is not None

    def test_zero_brightness_vs_lit_differs(self, scene_layers):
        assert scene_layers._action_diff(ON_ZERO, ON_FULL) is not None

    def test_brightness_difference_reported(self, scene_layers):
        dim = {"on": {"on": True}, "dimming": {"brightness": 42.0}}
        assert "bri" in scene_layers._action_diff(ON_FULL, dim)

    def test_brightness_within_tolerance_is_no_diff(self, scene_layers):
        near = {"on": {"on": True}, "dimming": {"brightness": 100.5}}
        assert scene_layers._action_diff(ON_FULL, near) is None


class TestBridgeFingerprint:
    """The fingerprint covers the bridge's SHAPE only -- what exists, not how it
    looks -- so that structural change and restyling stay separately detectable.
    """

    def _data(self, **over):
        base = {
            "universe": ["a", "b"],
            "zone_lightsets": {"Kitchen": ["a"], "Hall": ["b"]},
            "scenes": [{"name": "Night", "cells": []},
                       {"name": "Read", "cells": []}],
        }
        base.update(over)
        return base

    def test_stable_across_calls(self, scene_layers):
        assert (scene_layers.bridge_fingerprint(self._data())
                == scene_layers.bridge_fingerprint(self._data()))

    def test_ordering_does_not_matter(self, scene_layers):
        shuffled = self._data(
            universe=["b", "a"],
            scenes=[{"name": "Read", "cells": []}, {"name": "Night", "cells": []}])
        assert (scene_layers.bridge_fingerprint(shuffled)
                == scene_layers.bridge_fingerprint(self._data()))

    def test_added_light_changes_it(self, scene_layers):
        assert (scene_layers.bridge_fingerprint(self._data(universe=["a", "b", "c"]))
                != scene_layers.bridge_fingerprint(self._data()))

    def test_added_scene_changes_it(self, scene_layers):
        """The case --validate-design is blind to: it iterates the DESIGN's
        scenes, so a scene that only exists on the bridge is invisible to it."""
        more = self._data(scenes=[{"name": "Night", "cells": []},
                                  {"name": "Read", "cells": []},
                                  {"name": "Party", "cells": []}])
        assert (scene_layers.bridge_fingerprint(more)
                != scene_layers.bridge_fingerprint(self._data()))

    def test_rezoned_light_changes_it(self, scene_layers):
        rezoned = self._data(zone_lightsets={"Kitchen": ["a", "b"], "Hall": []})
        assert (scene_layers.bridge_fingerprint(rezoned)
                != scene_layers.bridge_fingerprint(self._data()))

    def test_colour_change_does_not_change_it(self, scene_layers):
        """Shape only: a restyle is validate's job, not the fingerprint's."""
        restyled = self._data(scenes=[
            {"name": "Night", "cells": [{"mode": "xy", "bri": 50}]},
            {"name": "Read", "cells": []}])
        assert (scene_layers.bridge_fingerprint(restyled)
                == scene_layers.bridge_fingerprint(self._data()))
