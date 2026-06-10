"""Tests for hooks/userpromptsubmit/ue-console-cmd.sh.

U3 regression: when the unreal-engine MCP server is NOT configured (no
.mcp.json, or .mcp.json without an unreal-engine entry), the hook must
self-disable -- exit 0 with NO output -- instead of emitting
decision:"block". The old block behavior ate every prompt starting with
">" in every non-UE project that had unreal-kit enabled at user scope.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_PLUGIN_DIR = Path(__file__).resolve().parent.parent.parent / "plugins" / "unreal-kit"
_HOOK = _PLUGIN_DIR / "hooks" / "userpromptsubmit" / "ue-console-cmd.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("jq") is None,
    reason="requires bash and jq on PATH",
)


def _run_hook(prompt: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(_HOOK)],
        input=json.dumps({"prompt": prompt, "cwd": str(cwd)}),
        capture_output=True,
        text=True,
        timeout=60,
    )


def _write_mcp_json(cwd: Path, with_unreal: bool) -> None:
    servers = {"unreal-engine": {"command": "node", "args": ["server.js"]}} if with_unreal else {"other": {"command": "x"}}
    (cwd / ".mcp.json").write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


class TestSelfDisable:
    def test_non_console_prompt_passes_through(self, tmp_path):
        result = _run_hook("hello world", tmp_path)
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_no_mcp_json_passes_prompt_through_silently(self, tmp_path):
        """U3: unconfigured project + '>' prompt -> no output, no block."""
        result = _run_hook("> stat fps", tmp_path)
        assert result.returncode == 0
        assert result.stdout.strip() == ""
        assert "block" not in result.stdout

    def test_mcp_json_without_unreal_engine_passes_through(self, tmp_path):
        _write_mcp_json(tmp_path, with_unreal=False)
        result = _run_hook("> stat fps", tmp_path)
        assert result.returncode == 0
        assert result.stdout.strip() == ""


class TestConfiguredBehavior:
    def test_console_command_emits_additional_context(self, tmp_path):
        _write_mcp_json(tmp_path, with_unreal=True)
        result = _run_hook("> stat fps", tmp_path)
        assert result.returncode == 0
        out = json.loads(result.stdout)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "stat fps" in ctx
        assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"

    def test_empty_command_after_gt_blocks_with_usage(self, tmp_path):
        """In a CONFIGURED project, a bare '>' is a genuine usage error."""
        _write_mcp_json(tmp_path, with_unreal=True)
        result = _run_hook(">", tmp_path)
        assert result.returncode == 0
        out = json.loads(result.stdout)
        assert out["decision"] == "block"
        assert "Usage" in out["reason"]
