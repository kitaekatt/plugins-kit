"""Tests for statusline.sh hygiene fixes.

X14: malformed stdin must produce a minimal fallback line (exit 0), not a
silent blank (set -euo pipefail used to kill the script on jq parse failure).
X11: the plugin's bootstrap.json must declare its jq dependency explicitly
instead of relying on the bootstrap plugin's manifest transitively.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

_PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "plugins" / "claude-ui-kit"
_STATUSLINE = _PLUGIN_ROOT / "scripts" / "statusline.sh"

_HAS_TOOLS = shutil.which("bash") and shutil.which("jq")


def run_statusline(stdin_text, cwd, extra_env=None):
    env = dict(os.environ)
    env.pop("BOOTSTRAP_BIN_JQ", None)  # use PATH jq deterministically
    env.pop("STATUSLINE_SHOW_MODEL", None)  # test the default unless overridden
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(_STATUSLINE)], input=stdin_text, cwd=cwd,
        env=env, capture_output=True, text=True, timeout=30)


@pytest.mark.skipif(not _HAS_TOOLS, reason="bash + jq required")
class TestMalformedStdinFallback:
    def test_malformed_json_emits_fallback_line(self, tmp_path):
        result = run_statusline("this is not json", tmp_path)
        assert result.returncode == 0
        assert result.stdout.strip(), "fallback must not be blank"

    def test_empty_stdin_emits_fallback_line(self, tmp_path):
        result = run_statusline("", tmp_path)
        assert result.returncode == 0
        assert result.stdout.strip()

    def test_valid_json_renders_normal_line(self, tmp_path):
        payload = json.dumps({
            "model": {"display_name": "TestModel", "id": "m-1"},
            "cwd": str(tmp_path / "myproj"),
        })
        result = run_statusline(payload, tmp_path)
        assert result.returncode == 0
        assert "myproj" in result.stdout


@pytest.mark.skipif(not _HAS_TOOLS, reason="bash + jq required")
class TestModelEffortSegment:
    """Model + effort meter segment (on by default, STATUSLINE_SHOW_MODEL=0 hides)."""

    _GLYPHS = {"low": "▁", "medium": "▃", "high": "▅",
               "xhigh": "▇", "max": "█"}

    @staticmethod
    def _payload(display_name="Fable 5", effort=None):
        data = {"model": {"display_name": display_name, "id": "m-1"},
                "cwd": "/tmp/myproj"}
        if effort is not None:
            data["effort"] = {"level": effort}
        return json.dumps(data)

    @pytest.mark.parametrize("level", list(_GLYPHS))
    def test_effort_level_renders_meter_glyph(self, tmp_path, level):
        result = run_statusline(self._payload(effort=level), tmp_path)
        assert result.returncode == 0
        assert f"{self._GLYPHS[level]} Fable" in result.stdout

    def test_version_stripped_from_display_name(self, tmp_path):
        result = run_statusline(self._payload("Opus 4.8", effort="high"), tmp_path)
        assert "Opus" in result.stdout
        assert "4.8" not in result.stdout

    def test_effort_absent_renders_bare_model_name(self, tmp_path):
        result = run_statusline(self._payload(), tmp_path)
        assert "Fable" in result.stdout
        for glyph in self._GLYPHS.values():
            assert glyph not in result.stdout

    def test_show_model_zero_hides_segment(self, tmp_path):
        result = run_statusline(self._payload(effort="xhigh"), tmp_path,
                                extra_env={"STATUSLINE_SHOW_MODEL": "0"})
        assert result.returncode == 0
        assert "Fable" not in result.stdout
        assert "myproj" in result.stdout


class TestBootstrapManifestDeclaresJq:
    def test_jq_declared_in_tools(self):
        manifest = json.loads((_PLUGIN_ROOT / "bootstrap.json").read_text(encoding="utf-8"))
        names = [t.get("name") for t in manifest.get("tools", [])]
        assert "jq" in names, (
            "statusline.sh uses jq; the dependency must stay declared in "
            "claude-ui-kit/bootstrap.json (X11), not satisfied transitively")
