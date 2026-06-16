"""Tests for awesome-kit's custom bootstrap script message routing.

The bug these guard against: the steady-state "chromium already installed
(cached)" branch must route to ``log_ok`` (verbose-only) so a healthy bootstrap
stays silent. Routing it to ``log`` (action, always shown) makes the line
re-display on every session forever -- the original report.
"""

import importlib.util
from pathlib import Path

_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "plugins" / "awesome-kit" / "custom_bootstrap.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("awesome_custom_bootstrap", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


custom_bootstrap = _load_module()


class _RecordingCtx:
    """Captures action (log) vs verbose-only (log_ok) routing."""

    def __init__(self, data_dir):
        self.data_dir = str(data_dir)
        self.project_dir = None
        self.actions = []
        self.oks = []
        self.failures = []

    def log(self, msg):
        self.actions.append(msg)

    def log_ok(self, msg):
        self.oks.append(msg)

    def add_failure(self, failure_type, **kwargs):
        self.failures.append({"type": failure_type, **kwargs})


class TestChromiumCachedRouting:
    def test_cached_marker_routes_to_verbose_only(self, tmp_path):
        # Marker present -> steady state -> must NOT emit an action entry.
        marker = tmp_path / custom_bootstrap.MARKER_NAME
        marker.write_text("ok\n", encoding="utf-8")

        ctx = _RecordingCtx(tmp_path)
        custom_bootstrap.bootstrap(ctx)

        assert ctx.actions == [], "cached steady state must not produce an action entry"
        assert any("already installed (cached)" in m for m in ctx.oks)
