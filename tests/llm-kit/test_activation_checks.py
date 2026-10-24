"""Activation checks 2 and 3 of the llm-kit migration.

The signed design (docs/reference/llm-kit-architecture.md, "Skills, agents,
workflows, and CLIs") makes activation conditional on three machine-checkable
conditions. Condition 1, the maintained-reference scan, is
``tests/repo-scripts/test_maintained_references.py``. This module holds the
other two.

Condition 2: "Old-created resumable fixtures resume through both the old name
and the final llm-kit name."
Condition 3: "Rollback tests disable llm-kit registration and resume through the
old names."

Where qualified names are persisted
-----------------------------------

A qualified skill, agent or workflow name (``<plugin>:<name>``) is persisted
only by a store that records which registration was selected. Measured over
``packages/katsoft-llm-kit/src/llm_kit``:

* the job ledger (schema 12) and the pipeline execution store (schema 10) hold
  ids, states, tokens, endpoints, models, JSON payloads and event rows; no
  column, ``definition_json`` field, ``environment`` snapshot, dispatch row or
  event payload carries a skill, agent or workflow name;
* the background launcher builds ``claude --bg <extra_launch_args> -- <prompt>``
  per launch, persists neither the flags nor the prompt, and the prompt names a
  run, unit, worker, answer path, fencing token and CLI invocations, never a
  registration;
* the workflow lane's ``args`` object holds a run id, batch id and per-unit
  packs; the spend ledger, verdict cache, credentials and config hold none.

What does carry an old plugin-qualified string is the event-type and schema
vocabulary (``job-kit:run-created``, ``content-pipeline-kit:lease-renewed``,
``job-kit.interrupt-request/v1``). Those are persisted protocol identities, not
registrations, and the design preserves them byte for byte ("a branding rename
is not a data migration"); both implementations emit and read the same bytes,
so no old-name translator is needed or exists.

The records that DO name a registration are harness-owned, outside this
repository: a Claude Code background session launched with ``--agent
<plugin>:pipeline-worker`` and a resumable Workflow run of
``<plugin>:run-ready-wave``. The harness resolves those names against installed
plugins, so an llm-kit translator cannot intercept them; the frozen legacy
registrations keep resolving them through their own unchanged implementations,
and the retirement gate (zero nonterminal records needing an old identity) is an
operator check at deletion, not a repository fixture.

So conditions 2 and 3 reduce to:

* store records created by the legacy code resume through llm-kit (the final
  implementation) and through the legacy code (the old implementation), and
  records created by llm-kit resume through the legacy code: tests/llm-kit/
  test_cross_state_parity.py, in both directions, with each stretch run in its
  own interpreter whose ``sys.path`` holds only one side;
* a tripwire (:func:`test_no_registration_name_is_persistable_from_llm_kit_code`)
  that fails the moment llm-kit code holds a qualified registration name as a
  string constant, which is the precondition for persisting one, so a future
  change cannot start persisting names without adding the translator and its
  fixtures first;
* a rollback guard (:func:`test_legacy_trees_do_not_depend_on_llm_kit`): with
  llm-kit's registration disabled, the legacy trees must still work, which is
  true only while nothing in them imports, depends on, or ships text that
  names llm-kit.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Dict, Iterator, List

import pytest

REPO = Path(__file__).resolve().parents[2]
LLM_KIT = REPO / "plugins" / "llm-kit"
import llm_kit
LIB = Path(llm_kit.__file__).resolve().parent

# Spelled as fragments so that this file does not match the scan for old
# qualified references (tests/llm-kit/ is exempt from it, but keep it honest).
OLD_PLUGINS = ("llm-scripting-" + "kit", "content-pipeline-" + "kit", "job-" + "kit")
REGISTERED = (
    "openrouter-account",
    "content-pipeline-domain",
    "background-pipeline",
    "execute-work-unit",
    "workflow-pipeline",
    "pipeline-worker",
    "run-ready-wave",
)
_PLUGINS = "|".join(re.escape(p) for p in ("llm-kit", *OLD_PLUGINS))
_NAMES = "|".join(re.escape(n) for n in REGISTERED)
QUALIFIED = re.compile(rf"(?<![\w-])(?:{_PLUGINS}):(?:{_NAMES})(?![\w-])")


# --- condition 2: no registration name is persistable from llm-kit code -----

def _docstring_nodes(tree: ast.AST) -> set:
    nodes = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                nodes.add(id(body[0].value))
    return nodes


def qualified_string_constants(lib: Path) -> Iterator[str]:
    """``path:line: constant`` for each non-docstring string constant under
    ``lib`` that is a qualified registration name, old or final."""
    for path in sorted(lib.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        skip = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in skip
                and QUALIFIED.search(node.value)
            ):
                yield f"{path.relative_to(lib.parent).as_posix()}:{node.lineno}: {QUALIFIED.search(node.value).group(0)}"


def test_no_registration_name_is_persistable_from_llm_kit_code() -> None:
    hits = list(qualified_string_constants(LIB))
    assert not hits, (
        "llm-kit code holds a qualified registration name as a string constant, so it can "
        "persist one. Before activation, add an old-name translator inside llm-kit and "
        "old-created and rollback resume fixtures (design: 'Skills, agents, workflows, "
        "and CLIs'), then record the persisting store in this module's docstring:\n"
        + "\n".join(hits)
    )


@pytest.mark.parametrize(
    "literal",
    [
        "claude --bg --agent " + "content-pipeline-" + "kit:pipeline-worker",
        "llm-kit:execute-work-unit",
        "{}:run-ready-wave".format("content-pipeline-" + "kit"),
    ],
)
def test_the_tripwire_can_fail(tmp_path: Path, literal: str) -> None:
    lib = tmp_path / "llm_kit"
    lib.mkdir()
    module = lib / "persist.py"
    module.write_text(f"AGENT = {literal!r}\n", encoding="utf-8")
    assert list(qualified_string_constants(lib)), "a planted qualified name was not seen"
    module.write_text("AGENT = 'pipeline-worker'\n", encoding="utf-8")  # restored
    assert not list(qualified_string_constants(lib))


def test_the_tripwire_ignores_docstrings_and_event_identities(tmp_path: Path) -> None:
    lib = tmp_path / "llm_kit"
    lib.mkdir()
    (lib / "ok.py").write_text(
        '"""Mentions llm-kit:execute-work-unit in prose."""\n'
        "EVENT = 'job-kit:run-created'\n"
        "SCHEMA = 'job-kit.interrupt-request/v1'\n",
        encoding="utf-8",
    )
    assert not list(qualified_string_constants(lib))


def test_persisted_event_identities_are_not_registrations() -> None:
    """The old-namespaced event types are the preserved protocol vocabulary.

    They must stay recognizable as identities and distinct from registrations,
    otherwise the tripwire's exclusion above would hide a real persisted name.
    """
    for identity in ("job-kit:run-created", "content-pipeline-kit:run-created"):
        assert not QUALIFIED.search(identity)


# --- condition 3: rollback; the legacy trees must work without llm-kit ------

LEGACY = {name: REPO / "plugins" / name for name in OLD_PLUGINS}
REGISTRATION_DIRS = ("skills", "agents", "workflows", "hooks")
# The llm-kit PLUGIN as a whole token: "llm-kit", "plugins-kit:llm-kit", "plugins/llm-kit". Not the
# PyPI package "katsoft-llm-kit" nor the module "llm_kit": a legacy plugin legitimately installs
# the package (its bootstrap.json and pyproject name it), and the rollback this guard protects
# is the plugin registration. A legacy Python tree importing the library is the separate AST scan.
LLM_KIT_NAMES = re.compile(r"(?<![\w-])llm-kit(?![\w-])")


def _python_imports_llm_kit(path: Path) -> List[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names if a.name.split(".")[0] == "llm_kit"]
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] == "llm_kit":
                found.append(node.module)
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.split(".")[0] == "llm_kit"
        ):
            found.append(node.value)  # a literal dynamic import or probe
    return found


def legacy_dependencies_on_llm_kit(plugin: Path) -> List[str]:
    """Every way a legacy plugin tree can reach llm-kit that rollback would break."""
    problems: List[str] = []
    manifest = plugin / ".claude-plugin" / "plugin.json"
    if manifest.is_file():
        deps = json.loads(manifest.read_text(encoding="utf-8")).get("dependencies", [])
        problems += [f"plugin.json dependency {d!r}" for d in deps if LLM_KIT_NAMES.search(str(d))]
    boot = plugin / "bootstrap.json"
    if boot.is_file() and LLM_KIT_NAMES.search(boot.read_text(encoding="utf-8")):
        problems.append("bootstrap.json names llm-kit")
    for path in sorted(plugin.rglob("*.py")):
        if ".venv" in path.parts or "__pycache__" in path.parts:
            continue
        problems += [
            f"{path.relative_to(plugin).as_posix()} imports {name}"
            for name in _python_imports_llm_kit(path)
        ]
    for directory in REGISTRATION_DIRS:
        for path in sorted((plugin / directory).rglob("*")) if (plugin / directory).is_dir() else ():
            if path.is_file() and path.suffix in {".md", ".js", ".json", ".yaml", ".yml", ".sh", ".cmd"}:
                if LLM_KIT_NAMES.search(path.read_text(encoding="utf-8", errors="replace")):
                    problems.append(f"{path.relative_to(plugin).as_posix()} names llm-kit")
    return problems


@pytest.mark.parametrize("name", OLD_PLUGINS)
def test_legacy_trees_do_not_depend_on_llm_kit(name: str) -> None:
    plugin = LEGACY[name]
    if not plugin.is_dir():
        pytest.skip(f"{name} is deleted (migration increment M10); nothing to roll back to")
    problems = legacy_dependencies_on_llm_kit(plugin)
    assert not problems, (
        f"{name} must keep working with llm-kit's registration disabled, but it reaches "
        "llm-kit:\n" + "\n".join(problems)
    )


def _scratch_plugin(root: Path) -> Path:
    plugin = root / "legacy-kit"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / "lib").mkdir()
    (plugin / "skills" / "s").mkdir(parents=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "legacy-kit", "dependencies": ["bootstrap"]}), encoding="utf-8"
    )
    (plugin / "bootstrap.json").write_text("{}", encoding="utf-8")
    (plugin / "lib" / "mod.py").write_text("import os\n", encoding="utf-8")
    (plugin / "skills" / "s" / "SKILL.md").write_text("A skill.\n", encoding="utf-8")
    return plugin


def test_the_rollback_guard_can_fail(tmp_path: Path) -> None:
    plugin = _scratch_plugin(tmp_path)
    assert legacy_dependencies_on_llm_kit(plugin) == []
    plants = {
        "dependency": lambda: (plugin / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"dependencies": ["bootstrap", "llm-kit"]}), encoding="utf-8"
        ),
        "manifest": lambda: (plugin / "bootstrap.json").write_text('{"ref": "llm-kit"}', encoding="utf-8"),
        "import": lambda: (plugin / "lib" / "mod.py").write_text("import llm_kit.jobs\n", encoding="utf-8"),
        "from-import": lambda: (plugin / "lib" / "mod.py").write_text(
            "from llm_kit.jobs import run\n", encoding="utf-8"
        ),
        "dynamic": lambda: (plugin / "lib" / "mod.py").write_text(
            "import importlib\nimportlib.import_module('llm_kit.jobs')\n", encoding="utf-8"
        ),
        "skill text": lambda: (plugin / "skills" / "s" / "SKILL.md").write_text(
            "Run llm-kit jobs run.\n", encoding="utf-8"
        ),
    }
    restore = {
        "dependency": lambda: (plugin / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"dependencies": ["bootstrap"]}), encoding="utf-8"
        ),
        "manifest": lambda: (plugin / "bootstrap.json").write_text("{}", encoding="utf-8"),
        "import": lambda: (plugin / "lib" / "mod.py").write_text("import os\n", encoding="utf-8"),
        "from-import": lambda: (plugin / "lib" / "mod.py").write_text("import os\n", encoding="utf-8"),
        "dynamic": lambda: (plugin / "lib" / "mod.py").write_text("import os\n", encoding="utf-8"),
        "skill text": lambda: (plugin / "skills" / "s" / "SKILL.md").write_text("A skill.\n", encoding="utf-8"),
    }
    for label, plant in plants.items():
        plant()
        assert legacy_dependencies_on_llm_kit(plugin), f"a planted {label} dependency was not seen"
        restore[label]()
        assert legacy_dependencies_on_llm_kit(plugin) == [], f"restoring {label} did not clear it"


def test_a_frozen_pointer_in_readme_or_description_is_not_a_dependency(tmp_path: Path) -> None:
    """The freeze releases (migration plan, section 5) add README and description
    pointers to llm-kit; those must not trip the rollback guard."""
    plugin = _scratch_plugin(tmp_path)
    (plugin / "README.md").write_text("Frozen; see llm-kit.\n", encoding="utf-8")
    (plugin / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"description": "Frozen. Use llm-kit.", "dependencies": ["bootstrap"]}),
        encoding="utf-8",
    )
    assert legacy_dependencies_on_llm_kit(plugin) == []


@pytest.mark.parametrize("name", OLD_PLUGINS)
def test_the_real_legacy_manifests_name_the_package_not_the_plugin(name: str, tmp_path: Path) -> None:
    """The shipped bootstrap.json of each legacy plugin names katsoft-llm-kit (the package it
    installs), which is not a reference to the llm-kit plugin; a plugin reference added to that
    same real text is seen."""
    real = LEGACY[name] / "bootstrap.json"
    if not real.is_file():
        pytest.skip(f"{name} is deleted; nothing to read")
    text = real.read_text(encoding="utf-8")
    assert "katsoft-llm-kit" in text, "the premise of this test: the real manifest names the package"
    plugin = _scratch_plugin(tmp_path)
    (plugin / "bootstrap.json").write_text(text, encoding="utf-8")
    assert legacy_dependencies_on_llm_kit(plugin) == []
    for reference in ("plugins-kit:llm-kit", "llm-kit", "plugins/llm-kit/bin"):
        (plugin / "bootstrap.json").write_text(text + "\n" + reference, encoding="utf-8")
        assert legacy_dependencies_on_llm_kit(plugin) == ["bootstrap.json names llm-kit"], reference


def test_the_package_name_in_skill_text_or_dependencies_is_not_the_plugin(tmp_path: Path) -> None:
    plugin = _scratch_plugin(tmp_path)
    (plugin / "skills" / "s" / "SKILL.md").write_text("Needs katsoft-llm-kit>=1.3.\n", encoding="utf-8")
    (plugin / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"dependencies": ["bootstrap", "katsoft-llm-kit"]}), encoding="utf-8"
    )
    assert legacy_dependencies_on_llm_kit(plugin) == []
    (plugin / "skills" / "s" / "SKILL.md").write_text("Run plugins-kit:llm-kit jobs.\n", encoding="utf-8")
    assert legacy_dependencies_on_llm_kit(plugin)
