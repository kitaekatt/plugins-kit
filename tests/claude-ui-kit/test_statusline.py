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


def run_statusline(stdin_text, cwd):
    env = dict(os.environ)
    env.pop("BOOTSTRAP_BIN_JQ", None)  # use PATH jq deterministically
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


class TestBootstrapManifestDeclaresJq:
    def test_jq_declared_in_tools(self):
        manifest = json.loads((_PLUGIN_ROOT / "bootstrap.json").read_text(encoding="utf-8"))
        names = [t.get("name") for t in manifest.get("tools", [])]
        assert "jq" in names, (
            "statusline.sh uses jq; the dependency must stay declared in "
            "claude-ui-kit/bootstrap.json (X11), not satisfied transitively")
