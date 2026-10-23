"""task-kit's packaging: manifests, skill routing, PATH exposure.

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

EXPECTED_CAPABILITY_IDS = (
    "init", "validate", "work", "update", "hand_off", "close", "reopen", "archive", "delete", "move",
    "list", "triage", "review", "answers", "dashboard", "queue", "show", "items", "status", "sessions",
)


def skill_capabilities() -> dict[str, dict]:
    blocks = re.findall(r"```yaml\n(.*?)```", SKILL, re.S)
    for block in blocks:
        data = yaml.safe_load(block)
        if isinstance(data, dict) and "capability_skill" in data:
            return {c["id"]: c for c in data["capability_skill"]["capabilities"]}
    raise AssertionError("no capability_skill block in SKILL.md")


def markdown_heading_ids(text: str) -> set[str]:
    ids: set[str] = set()
    for heading in re.findall(r"^#{1,6}\s+(.+?)\s*#*\s*$", text, re.M):
        anchor = re.sub(r"[^\w\s-]", "", heading.lower()).strip()
        ids.add(re.sub(r"\s+", "-", anchor))
    return ids


def test_skill_keeps_the_exact_capability_surface_and_resolvable_details():
    caps = skill_capabilities()
    assert tuple(caps) == EXPECTED_CAPABILITY_IDS
    skill_dir = PLUGIN_ROOT / "skills" / "task"
    for capability_id, capability in caps.items():
        details = capability.get("details")
        assert isinstance(details, str), capability_id
        path_text, separator, heading = details.partition("#")
        assert separator and path_text and heading, (capability_id, details)
        path = skill_dir / path_text
        assert path.is_file(), (capability_id, details)
        assert heading in markdown_heading_ids(path.read_text(encoding="utf-8")), (capability_id, details)


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


def test_task_kit_adds_no_llm_kit_install_edge():
    """No plugin dependency and no auto/manual install entry for llm-kit.

    llm_kit arrives as the katsoft-llm-kit package (see the next test), not through the llm-kit plugin: without it
    task-kit still lists and manages tasks, and only model answering REFUSES with a diagnostic. So there is no
    ``install: "auto"`` edge and no ``plugin.json`` dependency, and no ``install: "manual"`` entry either.
    """
    assert not any("llm-kit" in p["ref"] for p in BOOTSTRAP["plugins"])
    assert "llm-kit" not in PLUGIN_JSON["dependencies"]


def test_the_work_capability_starts_with_the_work_verb_and_no_surface_names_task_hold():
    """Step 1 of ``work`` is the work verb: no scheduler remains to yield to, so there is no handshake before it.

    Red: put a step that runs ``task_hold.py`` back in front of the work verb, or name it in any capability or
    in a reference.
    """
    steps = skill_capabilities()["work"]["steps"]
    assert [step["n"] for step in steps] == [1, 2]
    assert steps[0]["action"].startswith("Run the work verb")
    assert not (PLUGIN_ROOT / "skills" / "task" / "scripts" / "task_hold.py").exists()
    surfaces = {"SKILL.md": SKILL}
    for reference in (PLUGIN_ROOT / "skills" / "task" / "references").glob("*.md"):
        surfaces[reference.name] = reference.read_text(encoding="utf-8")
    assert [name for name, text in surfaces.items() if "task_hold" in text] == []


def test_no_plugin_file_imports_the_retired_dashboard_package():
    """The dashboard package is gone; nothing under the plugin may import it (docstrings may name it).

    Red: add ``from task_system.dashboard import state`` (or ``from .dashboard import x``) to any plugin .py file.
    """
    pattern = re.compile(r"^\s*(?:from\s+(?:task_system\.dashboard|\.+dashboard)\b|from\s+task_system\s+import\s+.*\bdashboard\b"
                         r"|import\s+task_system\.dashboard\b)", re.M)
    offenders = [str(path.relative_to(PLUGIN_ROOT)) for path in PLUGIN_ROOT.rglob("*.py")
                 if "__pycache__" not in path.parts and pattern.search(path.read_text(encoding="utf-8"))]
    assert offenders == []
    assert not (PLUGIN_ROOT / "skills" / "task" / "scripts" / "task_system" / "dashboard").exists()


def test_llm_kit_comes_from_the_declared_package_not_a_shared_lib_link():
    """Red: put ``llm_kit`` back in ``shared_lib_imports`` (the link would beat the package in the venv), drop either
    requirement, widen a range, or lower ``requires_bootstrap`` (a bootstrap older than 0.160.0 does not revoke the
    link or stamp the released-package venv)."""
    assert "llm_kit" not in BOOTSTRAP["shared_lib_imports"]
    assert {"skills_kit_lib", "bootstrap_lib"} <= set(BOOTSTRAP["shared_lib_imports"])
    dependencies = PYPROJECT["project"]["dependencies"]
    assert "katsoft-llm-kit[config,sdk,local]>=1.1,<2" in dependencies
    assert "katsoft-contracts>=1.0,<2" in dependencies
    assert BOOTSTRAP["requires_bootstrap"] == "0.160.0"
