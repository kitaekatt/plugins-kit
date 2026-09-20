"""scripts/skills_kit_tool.py: the documented launcher for audit/classify/tag.

md-domain's SKILL.md names the launcher as
``"${BOOTSTRAP_PYTHON:?...}" "${CLAUDE_PLUGIN_ROOT}/scripts/skills_kit_tool.py"
<command>``. These tests run it as a script from a directory that is NOT the
plugin root (the case the former ``-m skills_kit_lib.audit`` form could not
serve) under this test interpreter, with the re-exec disabled by the suite's
guard flag (tests/skills-kit/conftest.py), so they observe the dispatch; the
re-exec itself is the vendored bootstrap_guard's, pinned by its own tests.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL = REPO_ROOT / "plugins" / "skills-kit" / "scripts" / "skills_kit_tool.py"
SKILL = REPO_ROOT / "plugins" / "skills-kit" / "skills" / "md-domain" / "SKILL.md"


def _run(tmp_path, *args):
    env = dict(os.environ, _BOOTSTRAP_GUARD_VENV_REEXEC="1", PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run([sys.executable, str(TOOL), *args], cwd=tmp_path,
                          capture_output=True, text=True, env=env, timeout=120)


@pytest.fixture
def skill_copy(tmp_path):
    target = tmp_path / "work" / "SKILL.md"
    target.parent.mkdir()
    target.write_text(SKILL.read_text(encoding="utf-8"), encoding="utf-8")
    return target


@pytest.mark.parametrize("command", ["audit", "classify"])
def test_runs_the_command_from_any_directory(command, tmp_path, skill_copy):
    # Revert that turns this RED: drop the sys.path insert of the plugin root
    # in main() (skills_kit_lib is then not importable from tmp_path).
    run = _run(tmp_path, command, "work/SKILL.md")
    assert run.returncode == 0, run.stderr
    assert run.stdout.startswith(f"{command}: "), run.stdout
    assert "declared_type" in run.stdout


def test_relative_paths_resolve_against_the_caller(tmp_path, skill_copy):
    run = _run(tmp_path / "work", "classify", "SKILL.md")
    assert run.returncode == 0, run.stderr
    assert "classify: SKILL.md" in run.stdout


def test_exit_status_is_the_commands(tmp_path):
    run = _run(tmp_path, "audit", "no-such-file.md")
    assert run.returncode != 0


def test_unknown_command_is_a_usage_error(tmp_path):
    run = _run(tmp_path, "bogus")
    assert run.returncode == 2
    assert "usage: skills_kit_tool.py {audit|classify|tag}" in run.stderr


def test_reexec_precedes_any_skills_kit_lib_import():
    """Static: under __main__, the vendored guard re-execs before main() runs
    anything from skills_kit_lib, and the module imports none at top level."""
    tree = ast.parse(TOOL.read_text(encoding="utf-8"))
    top_imports = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert not any("skills_kit_lib" in ast.dump(n) for n in top_imports)
    main_block = next(n for n in tree.body if isinstance(n, ast.If)
                      and "__main__" in ast.dump(n.test))
    calls = [ast.dump(n) for n in ast.walk(main_block) if isinstance(n, ast.Call)]
    reexec = next(i for i, c in enumerate(calls) if "reexec_under_plugin_venv" in c)
    run_main = next(i for i, c in enumerate(calls) if "id='main'" in c)
    assert "skills-kit" in calls[reexec]
    assert reexec < run_main


def test_skill_md_names_the_launcher():
    from bootstrap_lib.interpreter_env import PLUGIN_CALL_SITE_EXPR as expr

    text = SKILL.read_text(encoding="utf-8")
    for command in ("audit", "classify", "tag"):
        assert (f"'{expr} "
                f'"${{CLAUDE_PLUGIN_ROOT}}/scripts/skills_kit_tool.py" {command}') in text
    assert "-m skills_kit_lib." not in text
