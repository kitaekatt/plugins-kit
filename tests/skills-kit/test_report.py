"""Smoke tests for the skill-audit report.py roster rendering (arch-review S17:
the report belt shipped untested; S1 proved the cost).

Covers render_roster over a corpus discovered from a temp tree (user +
project + plugin tiers) and the documented `report.py roster -` invocation
as a subprocess.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from skills_kit_lib.corpus import discover_corpus

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "skills-kit"
REPORT = PLUGIN_ROOT / "skills" / "md-domain" / "scripts" / "report.py"

# Load report.py under a unique module name (its directory is not on the
# pytest pythonpath, and `discover` would collide anyway).
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("skill_audit_report", REPORT)
report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(report)


SKILL_MD = """---
name: {name}
description: a {name} skill
skill-type: technique-skill
---
# {name}

```yaml
technique_skill:
  identity: i
```
"""


def _make_tier_tree(tmp_path: Path) -> dict:
    home = tmp_path / "home"
    user_skill = home / ".claude" / "skills" / "u-skill"
    user_skill.mkdir(parents=True)
    (user_skill / "SKILL.md").write_text(SKILL_MD.format(name="u-skill"), encoding="utf-8")

    proj = tmp_path / "proj"
    proj_skill = proj / ".claude" / "skills" / "p-skill"
    proj_skill.mkdir(parents=True)
    (proj_skill / "SKILL.md").write_text(SKILL_MD.format(name="p-skill"), encoding="utf-8")

    install = tmp_path / "install"
    plug_skill = install / "skills" / "g-skill"
    plug_skill.mkdir(parents=True)
    (plug_skill / "SKILL.md").write_text(SKILL_MD.format(name="g-skill"), encoding="utf-8")

    manifest = tmp_path / "installed_plugins.json"
    manifest.write_text(json.dumps({
        "plugins": {"demo@mkt": [{"installPath": str(install), "version": "2.0"}]}
    }), encoding="utf-8")

    return {"home": home, "project_root": proj, "manifest": manifest}


class TestRenderRoster:
    def test_roster_contains_all_tiers(self, tmp_path):
        t = _make_tier_tree(tmp_path)
        corpus = discover_corpus(
            home=t["home"],
            project_root=t["project_root"],
            installed_plugins_json=t["manifest"],
        )
        text = report.render_roster(corpus)
        assert "# Skill Roster" in text
        assert "**u-skill**" in text
        assert "**p-skill**" in text
        assert "**g-skill**" in text
        assert "Plugin: demo (mkt, v2.0)" in text
        assert "### technique-skill" in text

    def test_group_by_type_uses_contract_root(self, tmp_path):
        t = _make_tier_tree(tmp_path)
        corpus = discover_corpus(
            home=t["home"],
            project_root=t["project_root"],
            installed_plugins_json=t["manifest"],
        )
        groups = report.group_by_type(corpus.user)
        assert ("technique-skill", "auto") in groups


class TestDocumentedInvocation:
    def test_roster_stdout_subprocess(self, tmp_path):
        """`report.py roster -` is the documented stdout form."""
        t = _make_tier_tree(tmp_path)
        env = dict(os.environ)
        env["PYTHONPATH"] = str(PLUGIN_ROOT)
        proc = subprocess.run(
            [sys.executable, str(REPORT), "roster", "-", "--cwd", str(t["project_root"])],
            capture_output=True, text=True, env=env, timeout=120,
        )
        assert proc.returncode == 0, proc.stderr
        assert "# Skill Roster" in proc.stdout

    def test_no_args_prints_usage(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(PLUGIN_ROOT)
        proc = subprocess.run(
            [sys.executable, str(REPORT)],
            capture_output=True, text=True, env=env, timeout=120,
        )
        assert proc.returncode == 0
        assert "roster" in proc.stdout and "hierarchy" in proc.stdout
