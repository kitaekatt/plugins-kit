"""Smoke test for the documented standalone invocation of skill_hierarchy_report.py.

The script docstring documents direct execution:

    python skill_hierarchy_report.py [--project-root PATH] [--out PATH]
                                     [--installed-plugins PATH]
                                     [--user-skills PATH]

That entry point once crashed with NameError at ``sys.exit(main(sys.argv[1:]))``
because the module used ``sys`` without importing it (it only worked via
report.py's ``render_html`` import path). This test runs the standalone path
in a subprocess against empty inputs and asserts it completes.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent.parent / "plugins" / "skills-kit"
_SCRIPT = _PLUGIN_ROOT / "skills" / "md-domain" / "scripts" / "skill_hierarchy_report.py"


class TestStandaloneInvocation:
    def test_documented_standalone_invocation_runs(self, tmp_path):
        project_root = tmp_path / "project"
        project_root.mkdir()
        user_skills = tmp_path / "user-skills"
        user_skills.mkdir()
        installed_plugins = tmp_path / "installed_plugins.json"
        installed_plugins.write_text(json.dumps({"plugins": {}}), encoding="utf-8")
        out = tmp_path / "report.html"

        env = dict(os.environ)
        # skills_kit_lib lives at the plugin root (mirrors the repo pytest
        # pythonpath entry "plugins/skills-kit").
        env["PYTHONPATH"] = os.pathsep.join(
            p for p in (str(_PLUGIN_ROOT), env.get("PYTHONPATH")) if p
        )

        result = subprocess.run(
            [
                sys.executable,
                str(_SCRIPT),
                "--project-root", str(project_root),
                "--user-skills", str(user_skills),
                "--installed-plugins", str(installed_plugins),
                "--out", str(out),
            ],
            capture_output=True,
            text=True,
            env=env,
        )

        assert result.returncode == 0, result.stderr
        assert "Traceback" not in result.stderr
        assert out.is_file()
        assert f"Wrote {out.resolve()}" in result.stdout
