"""Pinning tests for the cache-report SKILL.md invocation path.

The skill previously resolved cache_report.py via installed_plugins.json using
a registry key format that does not exist ("plugins-kit:cache-kit"; actual
keys are "<plugin>@<marketplace>"), so the inline invocation raised KeyError.
The fix is to invoke the script via ${CLAUDE_PLUGIN_ROOT}, which Claude Code
expands to the plugin's install path at runtime. These tests pin that:

1. The broken registry-lookup pattern never returns to SKILL.md.
2. The documented ${CLAUDE_PLUGIN_ROOT}-relative script actually exists and runs.
"""

import os
import subprocess
import sys
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent.parent / "plugins" / "cache-kit"
_SKILL_MD = _PLUGIN_ROOT / "skills" / "cache-report" / "SKILL.md"
_SCRIPT_RELPATH = "scripts/cache_report.py"


class TestSkillInvocationPath:
    def test_skill_md_does_not_use_registry_lookup(self):
        """The installed_plugins.json lookup (and its nonexistent key format)
        must not reappear in SKILL.md."""
        text = _SKILL_MD.read_text(encoding="utf-8")
        assert "installed_plugins.json" not in text
        assert "plugins-kit:cache-kit" not in text

    def test_skill_md_invokes_script_via_plugin_root(self):
        """SKILL.md must invoke the script through ${CLAUDE_PLUGIN_ROOT}."""
        text = _SKILL_MD.read_text(encoding="utf-8")
        assert f"${{CLAUDE_PLUGIN_ROOT}}/{_SCRIPT_RELPATH}" in text

    def test_documented_script_exists_at_plugin_root(self):
        """${CLAUDE_PLUGIN_ROOT} expands to the plugin root; the documented
        relative path must exist there."""
        assert (_PLUGIN_ROOT / _SCRIPT_RELPATH).is_file()

    def test_documented_script_runs_without_traceback(self, tmp_path):
        """The script must start and produce a report or a clean error --
        never an import/syntax traceback."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        result = subprocess.run(
            [sys.executable, str(_PLUGIN_ROOT / _SCRIPT_RELPATH)],
            capture_output=True,
            text=True,
            cwd=str(tmp_path),
            env={**os.environ, "HOME": str(fake_home), "USERPROFILE": str(fake_home)},
        )
        # With an empty HOME there are no transcripts: clean exit-1 error.
        assert result.returncode in (0, 1)
        assert "Traceback" not in result.stderr
