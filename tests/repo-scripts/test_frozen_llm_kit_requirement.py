"""Frozen providers that reach ``bootstrap_lib.codex`` declare katsoft-llm-kit.

``bootstrap_lib.codex`` is an exact alias of ``llm_kit.adapters.codex_command``
(``bootstrap_lib/_llm_kit_alias.py``), so a venv that links ``bootstrap_lib`` and
``llm_scripting_kit`` resolves a codex dispatch only when it also holds
``katsoft-llm-kit`` >= 1.3. A venv installs only what its pyproject.toml
declares, so every plugin that links both declares the requirement in the
restricted form bootstrap resolves a released-package target from.
"""
from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

REQUIREMENT = "katsoft-llm-kit>=1.3,<2"
PLUGINS = Path(__file__).resolve().parents[2] / "plugins"


def _missing(pyproject: dict) -> list[str]:
    deps = pyproject["project"].get("dependencies", [])
    return [] if REQUIREMENT in deps else [f"dependencies lack {REQUIREMENT!r}"]


def _linking_both(plugins: Path) -> list[str]:
    found = []
    for manifest in sorted(plugins.glob("*/bootstrap.json")):
        links = json.loads(manifest.read_text(encoding="utf-8")).get("shared_lib_imports", [])
        if "bootstrap_lib" in links and "llm_scripting_kit" in links:
            found.append(manifest.parent.name)
    return found


def test_the_plugins_linking_both_libraries_are_the_known_set():
    # content-pipeline-kit and job-kit link llm_scripting_kit and bootstrap_lib;
    # llm-scripting-kit owns the former and links the latter.
    assert {"content-pipeline-kit", "job-kit"} <= set(_linking_both(PLUGINS))


@pytest.mark.parametrize("plugin", ["content-pipeline-kit", "job-kit", "llm-scripting-kit"])
def test_the_plugin_declares_katsoft_llm_kit(plugin):
    pyproject = tomllib.loads((PLUGINS / plugin / "pyproject.toml").read_text(encoding="utf-8"))
    assert _missing(pyproject) == []


def test_every_plugin_linking_both_libraries_declares_katsoft_llm_kit():
    for plugin in _linking_both(PLUGINS):
        pyproject = tomllib.loads((PLUGINS / plugin / "pyproject.toml").read_text(encoding="utf-8"))
        assert _missing(pyproject) == [], plugin


def test_the_check_goes_red_when_the_declaration_is_lost():
    pyproject = tomllib.loads((PLUGINS / "job-kit" / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]
    lost = {"project": {**project, "dependencies": [
        d for d in project["dependencies"] if not d.startswith("katsoft-llm-kit")]}}
    assert _missing(lost) == [f"dependencies lack {REQUIREMENT!r}"]


PACKAGE_BOOTSTRAP_FLOOR = (0, 160, 0)


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def _below_package_floor(plugins: Path) -> list[str]:
    """Plugins whose pyproject requires katsoft-llm-kit but whose bootstrap.json
    requires_bootstrap is below 0.160.0, the first bootstrap that holds the venv
    at the released package version and revokes undeclared .pth links."""
    offenders = []
    for pyproject in sorted(plugins.glob("*/pyproject.toml")):
        deps = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"].get("dependencies", [])
        if not any(d.startswith("katsoft-llm-kit") for d in deps):
            continue
        manifest = json.loads((pyproject.parent / "bootstrap.json").read_text(encoding="utf-8"))
        floor = manifest.get("requires_bootstrap", "0.0.0")
        if _version(floor) < PACKAGE_BOOTSTRAP_FLOOR:
            offenders.append(f"{pyproject.parent.name}: requires_bootstrap={floor}")
    return offenders


def test_every_plugin_requiring_katsoft_llm_kit_floors_bootstrap_at_0_160_0():
    assert _below_package_floor(PLUGINS) == []
