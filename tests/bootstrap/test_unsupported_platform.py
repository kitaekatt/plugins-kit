"""Tests for the non-Ubuntu-Linux fail-fast surfacing in the engine.

detect_os() raising UnsupportedPlatformError is covered in
test_platform_detect.py. This file covers how the engine SURFACES that hard
error via ``engine._emit_unsupported_platform`` -- a clean descriptive hook
response in each output mode (background pending file, SessionStart stdout,
console plain text), NOT a per-item fix-all failure.
"""

import json
import types

import bootstrap_lib.engine as engine


def _args(console=False, background=False):
    return types.SimpleNamespace(console=console, background=background)


_MSG = (
    "Unsupported Linux distribution: detected Fedora Linux 39. Bootstrap "
    "supports only Ubuntu among Linux distributions."
)


class TestEmitUnsupportedPlatform:
    def test_background_writes_pending_file(self, tmp_path):
        engine._emit_unsupported_platform(_MSG, str(tmp_path), _args(background=True))
        pending = tmp_path / "bootstrap_display.pending"
        assert pending.is_file()
        resp = json.loads(pending.read_text())
        assert resp["continue"] is True
        assert resp["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
        assert _MSG in resp["systemMessage"]
        assert "not fixable" in resp["hookSpecificOutput"]["additionalContext"]

    def test_sessionstart_prints_json(self, tmp_path, capsys):
        engine._emit_unsupported_platform(_MSG, str(tmp_path), _args())
        out = capsys.readouterr().out
        resp = json.loads(out)
        assert resp["hookSpecificOutput"]["hookEventName"] == "SessionStart"
        assert _MSG in resp["systemMessage"]
        # No pending file written in foreground mode.
        assert not (tmp_path / "bootstrap_display.pending").exists()

    def test_console_prints_plain_text(self, tmp_path, capsys):
        engine._emit_unsupported_platform(_MSG, str(tmp_path), _args(console=True))
        out = capsys.readouterr().out
        assert _MSG in out
        # Console mode is plain text, not JSON.
        assert not out.strip().startswith("{")
        assert not (tmp_path / "bootstrap_display.pending").exists()
