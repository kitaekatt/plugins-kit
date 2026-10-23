"""llm-kit's own manifests and README summary.

The three legacy plugins each pinned their bootstrap floor and their manifest
shape; those tests are mirrored from the legacy suites with named differences
(the equality with the highest call floor becomes at-least, because llm-kit's
floor is the release that first recognizes the llm_kit shared library). This
file asserts the llm-kit versions natively:

* ``requires_bootstrap`` is at least every ``*_BOOTSTRAP`` floor any llm_kit
  module names, found by scanning the source (a new floor constant is covered
  without editing this file), and each documented floor is pinned to its literal
  apart from the manifest;
* the manifest adds no edge: one shared library, one imported library, no
  ``plugins`` entry, and a plugin dependency on bootstrap alone;
* the README's exit-code paragraph names every exit constant of every CLI group.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PLUGIN = REPO / "plugins" / "llm-kit"
LIB = PLUGIN / "lib" / "llm_kit"
MANIFEST = json.loads((PLUGIN / "bootstrap.json").read_text(encoding="utf-8"))
PLUGIN_JSON = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def _version(text: str) -> tuple:
    return tuple(int(part) for part in text.split("."))


def bootstrap_floors(root: Path = LIB) -> dict[str, str]:
    """``path::NAME`` -> version for every module-level ``*_BOOTSTRAP = "x.y.z"`` in the tree."""
    found = {}
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                    and isinstance(node.value.value, str) and _SEMVER.match(node.value.value):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id.endswith("BOOTSTRAP"):
                        found[f"{path.relative_to(root).as_posix()}::{target.id}"] = node.value.value
    return found


def test_the_scan_finds_the_floors_the_code_names():
    floors = bootstrap_floors()
    assert len(floors) >= 10, floors
    assert floors["jobs/events.py::_EXECUTION_EVENT_BOOTSTRAP"] == "0.136.0"
    assert floors["adapters/backends.py::_CLAUDX_MIN_BOOTSTRAP"] == "0.154.1"


def test_requires_bootstrap_covers_every_bootstrap_floor_in_the_code():
    floors = bootstrap_floors()
    highest = max(floors.values(), key=_version)
    assert _version(MANIFEST["requires_bootstrap"]) >= _version(highest), (highest, MANIFEST["requires_bootstrap"])


def test_documented_floors_are_pinned_to_their_literals():
    """Each floor is pinned apart from the manifest, so moving a constant and the manifest back together stays red."""
    import llm_kit.completion.skill_context as skill_context
    import llm_kit.dispatch.declaration as declaration
    import llm_kit.jobs.events as job_events
    import llm_kit.jobs.interrupts as job_interrupts
    import llm_kit.pipeline.execution.events as pipeline_events
    import llm_kit.pipeline.execution.interrupts as pipeline_interrupts

    assert job_events._EXECUTION_EVENT_BOOTSTRAP == "0.136.0"
    assert job_interrupts._INTERRUPT_CONTRACT_BOOTSTRAP == "0.137.0"
    assert pipeline_interrupts.INTERRUPT_CONTRACT_BOOTSTRAP == "0.137.0"
    assert pipeline_events.EXECUTION_EVENT_BOOTSTRAP == "0.135.0"
    assert pipeline_events.EXECUTION_EVENT_V2_BOOTSTRAP == "0.136.0"
    assert declaration.EXECUTION_EVENT_BOOTSTRAP == "0.135.0"
    assert skill_context.SKILL_MATERIAL_BOOTSTRAP == "0.138.0"
    # the validator floor is the first llm-kit release that ships schema.json_schema
    assert job_interrupts._JSON_SCHEMA_LSK_VERSION == "0.1.2"
    assert pipeline_interrupts.JSON_SCHEMA_LSK_VERSION == "0.1.2"


def test_manifest_adds_no_edge():
    assert MANIFEST["shared_libs"] == [{"name": "llm_kit", "src": "lib"}]
    assert MANIFEST["shared_lib_imports"] == ["bootstrap_lib"]
    assert "plugins" not in MANIFEST, "llm-kit depends on no other plugin for the validator or the completion code"
    assert PLUGIN_JSON["dependencies"] == ["bootstrap"]
    assert PLUGIN_JSON.get("published", True) is True


def test_no_config_block_and_only_the_credential_check_script():
    """Decision 7: llm-kit seeds no user config and reads the legacy locations unchanged.

    The one custom script is the OpenRouter credential check; what it does and
    does not do is asserted in test_custom_bootstrap.py.
    """
    assert "config" not in MANIFEST
    assert "custom_script" not in MANIFEST
    assert MANIFEST["script"] == {"path": "custom_bootstrap.py", "entry_point": "bootstrap"}
    assert (PLUGIN / "custom_bootstrap.py").is_file()
    assert not list(PLUGIN.rglob("default_config.yaml"))


def test_venv_carries_every_extra_the_bundled_code_reaches():
    """The extras flow from the katsoft-llm-kit requirement, not from a manifest list."""
    import tomllib

    project = tomllib.loads((PLUGIN / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert "extras" not in MANIFEST["venv"]
    (requirement,) = [r for r in project["dependencies"] if r.startswith("katsoft-llm-kit")]
    extras = set(re.fullmatch(r"katsoft-llm-kit\[([^\]]*)\].*", requirement).group(1).split(","))
    assert extras == {"config", "sdk", "local", "frontdoor", "jobs", "pipeline"}
    assert "scripts" not in project, "the console script llm-kit is the distribution's"


def test_requires_bootstrap_is_the_package_consumer_floor():
    """0.160.0 revokes undeclared .pth links and holds the venv at the released package target."""
    assert MANIFEST["requires_bootstrap"] == "0.160.0"


def test_plugin_and_pyproject_versions_agree():
    pyproject = (PLUGIN / "pyproject.toml").read_text(encoding="utf-8")
    version = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.M).group(1)
    assert version == PLUGIN_JSON["version"]


# --- the README's exit-code paragraph ---------------------------------------

def _exit_constants() -> dict[str, int]:
    import llm_kit.cli._common as common
    import llm_kit.cli.jobs as jobs

    out = {}
    for module in (common, jobs):
        for name, value in vars(module).items():
            if name.startswith("EXIT_") and isinstance(value, int):
                out[f"{module.__name__.rsplit('.', 1)[1]}.{name}"] = value
    return out


def _exit_code_paragraph() -> str:
    text = (PLUGIN / "README.md").read_text(encoding="utf-8")
    start = text.index("Exit codes are")
    return text[start: text.index("\n\n", start)]


def test_readme_exit_code_summary_names_every_exit_constant():
    paragraph = _exit_code_paragraph()
    constants = _exit_constants()
    assert len(constants) >= 10, constants
    missing = [f"{name}={value}" for name, value in constants.items() if f"`{value}`" not in paragraph]
    assert not missing, f"README's exit-code paragraph is missing: {missing}\n\n{paragraph}"
