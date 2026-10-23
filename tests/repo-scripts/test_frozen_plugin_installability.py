"""Installability fix for the frozen providers: each declares katsoft-contracts.

content-pipeline-kit, job-kit and llm-scripting-kit link bootstrap_lib through
shared_lib_imports, and bootstrap_lib delegates its contract modules to the
katsoft-contracts distribution. A plugin venv installs only what its
pyproject.toml declares, so each frozen provider declares the requirement in
the one form bootstrap resolves a released-package target from
(name[extras]>=A.B[.C],<N). katsoft-contracts requires Python 3.12, and uv
refuses to resolve a project whose requires-python admits an older interpreter.

docs/planning/llm-kit/migration-plan.md section 5 limits a frozen plugin to
security, critical correctness and installability fixes, each with its own
version and a regression test; this is that test. It lives here and not in
tests/<plugin>/ because those directories are the legacy suites that
tests/llm-kit/test_repointed_suites.py reconciles against a ledger of exact
collection counts. Runtime code and the package __version__ constants are
untouched.
"""
from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest

REQUIREMENT = "katsoft-contracts>=1.0,<2"
PLUGINS = Path(__file__).resolve().parents[2] / "plugins"
FROZEN = ("content-pipeline-kit", "job-kit", "llm-scripting-kit")


def _problems(pyproject: dict, manifest: dict) -> list[str]:
    project = pyproject["project"]
    found = []
    if REQUIREMENT not in project.get("dependencies", []):
        found.append(f"dependencies lack {REQUIREMENT!r}")
    floor = re.fullmatch(r">=(\d+)\.(\d+)", project.get("requires-python", ""))
    if floor is None or (int(floor[1]), int(floor[2])) < (3, 12):
        found.append(f"requires-python {project.get('requires-python')!r} admits "
                     "an interpreter older than katsoft-contracts supports (3.12)")
    if project.get("version") != manifest.get("version"):
        found.append("pyproject version differs from plugin.json")
    return found


def _read(plugin: str, root: Path = PLUGINS) -> tuple[dict, dict]:
    base = root / plugin
    pyproject = tomllib.loads((base / "pyproject.toml").read_text(encoding="utf-8"))
    manifest = json.loads((base / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    return pyproject, manifest


@pytest.mark.parametrize("plugin", FROZEN)
def test_the_frozen_plugin_declares_katsoft_contracts_in_the_restricted_form(plugin):
    assert _problems(*_read(plugin)) == []


@pytest.mark.parametrize("plugin", FROZEN)
def test_the_check_names_each_way_the_declaration_can_be_lost(plugin):
    """Counterfactual: the same check, run on a declaration that lost each part."""
    pyproject, manifest = _read(plugin)
    project = pyproject["project"]
    without = {**pyproject, "project": {**project, "dependencies": [
        d for d in project["dependencies"] if not d.startswith("katsoft-contracts")]}}
    assert _problems(without, manifest) == [f"dependencies lack {REQUIREMENT!r}"]
    old_python = {**pyproject, "project": {**project, "requires-python": ">=3.10"}}
    assert len(_problems(old_python, manifest)) == 1
    drifted = {**manifest, "version": "0.0.0"}
    assert _problems(pyproject, drifted) == ["pyproject version differs from plugin.json"]
