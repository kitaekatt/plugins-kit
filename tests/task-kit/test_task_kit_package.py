"""task-kit's packaging for the dashboard: manifests, skill routing, PATH exposure.

These pin the facts that make `task-kit` typed in any terminal reach the
launcher and that `/task dashboard` routes to it without colliding with the
review verb's keywords.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from pathlib import Path

import pytest
import yaml
from dashboard_support import PLUGIN_ROOT, REPO_ROOT

PLUGIN_JSON = json.loads((PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
BOOTSTRAP = json.loads((PLUGIN_ROOT / "bootstrap.json").read_text(encoding="utf-8"))
PYPROJECT = tomllib.loads((PLUGIN_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
SKILL = (PLUGIN_ROOT / "skills" / "task" / "SKILL.md").read_text(encoding="utf-8")


def skill_capabilities() -> dict[str, dict]:
    blocks = re.findall(r"```yaml\n(.*?)```", SKILL, re.S)
    for block in blocks:
        data = yaml.safe_load(block)
        if isinstance(data, dict) and "capability_skill" in data:
            return {c["id"]: c for c in data["capability_skill"]["capabilities"]}
    raise AssertionError("no capability_skill block in SKILL.md")


def test_plugin_json_and_pyproject_state_one_version():
    """The bump property (a changed plugin carries a version past the last publish) belongs to its producer,
    ``scripts/pre-commit-version-check.sh``. A copy of it here only re-typed a floor that went stale."""
    assert PLUGIN_JSON["version"] == PYPROJECT["project"]["version"]


def test_psutil_is_declared_for_install_and_for_verification():
    assert any(d.startswith("psutil") for d in PYPROJECT["project"]["dependencies"])
    assert "psutil" in BOOTSTRAP["venv"]["check_imports"]


def test_bin_is_synced_to_the_data_directory_that_path_entries_names():
    assert BOOTSTRAP["sync_to_data"] == [{"src": "bin", "dst": "bin"}]
    (entry,) = BOOTSTRAP["path_entries"]
    assert entry == "~/.claude/plugins/data/plugins-kit/task-kit/bin"
    for name in ("task-kit", "task-kit.cmd", "task_kit_entrypoint.py"):
        assert (PLUGIN_ROOT / "bin" / name).is_file()


def test_the_posix_shim_is_executable_in_git():
    import subprocess

    out = subprocess.run(["git", "ls-files", "-s", "plugins/task-kit/bin/task-kit"], cwd=REPO_ROOT,
                         capture_output=True, text=True).stdout
    if out:  # untracked in a pre-commit run is checked once it is staged
        assert out.startswith("100755")


def test_dashboard_routing_owns_the_task_dashboard_keyword_and_review_does_not():
    caps = skill_capabilities()
    assert "task dashboard" in caps["dashboard"]["keywords"]
    assert "/task dashboard" in caps["dashboard"]["keywords"]
    assert "task dashboard" not in caps["review"]["keywords"]
    keyword_owners = [i for i, c in caps.items() if "task dashboard" in c["keywords"]]
    assert keyword_owners == ["dashboard"]
    step = caps["dashboard"]["steps"][0]["action"]
    assert 'bin/task-kit' in step and "CLAUDE_PLUGIN_ROOT" in step


def test_dashboard_is_not_a_task_py_verb():
    cli = (PLUGIN_ROOT / "skills" / "task" / "scripts" / "task.py").read_text(encoding="utf-8")
    assert "dashboard" not in cli.lower()


def test_the_dashboard_adds_no_llm_kit_install_edge():
    """No plugin dependency and no auto/manual install entry for llm-kit. (llm_kit may
    appear in shared_lib_imports for the pre-existing summary-repair import.)

    llm-kit is published, but optional: without it task-kit still lists and manages tasks, and only the
    autonomous run actions REFUSE. So there is no ``install: "auto"`` edge and no ``plugin.json`` dependency.
    The refusal message names ``claude plugin install llm-kit@plugins-kit``, and that message is the
    discoverability, so there is no ``install: "manual"`` entry either.
    """
    assert not any("llm-kit" in p["ref"] for p in BOOTSTRAP["plugins"])
    assert "llm-kit" not in PLUGIN_JSON["dependencies"]


def test_the_work_capability_runs_the_handshake_first_and_never_waits_on_it():
    """Step 1 of ``work`` is the dashboard handshake; it cannot gate step 2, the work verb itself.

    Red: delete step 1 (or its command) from the ``work`` capability.
    """
    steps = skill_capabilities()["work"]["steps"]
    assert [step["n"] for step in steps][:2] == [1, 2]
    handshake, work = steps[0]["action"], steps[1]["action"]
    assert 'task_hold.py <ref> --session "${CLAUDE_SESSION_ID}"' in handshake
    assert "never fails and never blocks the work verb" in handshake
    assert "continue to step 2" in handshake
    assert "Run the work verb" in work
    reference = (PLUGIN_ROOT / "skills" / "task" / "references" / "dashboard.md").read_text(encoding="utf-8")
    assert "## The /task work handshake" in reference and "## Launcher exit codes" in reference
