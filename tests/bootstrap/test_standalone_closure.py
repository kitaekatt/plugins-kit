"""The standalone interpreter never needs katsoft-contracts or katsoft-llm-kit.

The bootstrap engine, its hooks and bootstrap's scripts run under the
standalone interpreter, a Python that holds no plugin venv and therefore no
``katsoft-contracts`` distribution. ``bootstrap_lib.execution_event``,
``interrupt_contract`` and ``model_declaration`` are aliases of
``katsoft_contracts``, and ``bootstrap_lib.code_review`` imports them, so none
of those may sit in the import closure of anything the standalone interpreter
runs. ``bootstrap_lib.codex`` is likewise an alias, of ``llm_kit`` (the
``katsoft-llm-kit`` distribution), so it and ``llm_kit`` itself are forbidden
too; the engine's Codex question is answered by ``bootstrap_lib.codex_detection``. This test walks the static import graph (every ``import`` statement,
nested ones included, and constant ``importlib.import_module`` arguments) from
each entry point and fails on the first path that reaches one.

The entry points are derived, not listed: every ``.py`` under ``engine/`` and
``scripts/``, and every ``bootstrap_lib/<name>.py`` or ``engine/<name>.py`` a
hook script or ``hooks.json`` command names.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "bootstrap"
LIB = "bootstrap_lib"
CONTRACT_MODULES = ("execution_event", "interrupt_contract", "model_declaration")


def forbidden(dotted: str) -> bool:
    """True for a module the standalone interpreter must not import."""
    if dotted == "katsoft_contracts" or dotted.startswith("katsoft_contracts."):
        return True
    if dotted == "llm_kit" or dotted.startswith("llm_kit."):
        return True
    if dotted == f"{LIB}.code_review" or dotted.startswith(f"{LIB}.code_review."):
        return True
    if dotted in (f"{LIB}.codex", f"{LIB}._llm_kit_alias"):
        return True
    return any(dotted == f"{LIB}.{name}" for name in CONTRACT_MODULES) or \
        dotted == f"{LIB}._contracts_alias"


def _file_for(root: Path, dotted: str) -> Path | None:
    """The source file of a ``bootstrap_lib`` dotted name, if it is one."""
    if dotted != LIB and not dotted.startswith(LIB + "."):
        return None
    base = root.joinpath(*dotted.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _package_of(root: Path, path: Path) -> str | None:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return None
    parts = list(rel.with_suffix("").parts)
    if parts[-1] == "__init__":
        return ".".join(parts[:-1])
    return ".".join(parts[:-1])


def imports_of(root: Path, path: Path) -> set[str]:
    """Every dotted module name ``path`` may import, whether or not it exists."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = _package_of(root, path)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                if package is None:
                    continue
                parts = package.split(".")
                base = parts[: len(parts) - (node.level - 1)]
                module = ".".join(base + ([node.module] if node.module else []))
            else:
                module = node.module or ""
            found.add(module)
            for alias in node.names:
                found.add(f"{module}.{alias.name}")
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in ("import_module", "__import__") and node.args and \
                    isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                found.add(node.args[0].value)
    found.discard("")
    return found


def entry_points(plugin: Path) -> list[Path]:
    """Python files the standalone interpreter starts, derived from the plugin."""
    entries: set[Path] = set()
    for sub in ("engine", "scripts"):
        entries.update((plugin / sub).glob("*.py"))
    hook_texts = [p.read_text(encoding="utf-8")
                  for p in (plugin / "hooks").rglob("*") if p.is_file()]
    for text in hook_texts:
        for sub, name in re.findall(rf"\b({LIB}|engine)/(\w+)\.py", text):
            candidate = plugin / sub / f"{name}.py"
            if candidate.is_file():
                entries.add(candidate)
    return sorted(entries)


def closure_violations(plugin: Path, entries: list[Path]) -> list[str]:
    """One ``entry -> ... -> module`` chain per forbidden module reached."""
    seen: dict[Path, tuple[Path, ...]] = {}
    queue = [(e, (e,)) for e in entries]
    problems: list[str] = []
    while queue:
        path, chain = queue.pop(0)
        if path in seen:
            continue
        seen[path] = chain
        for dotted in sorted(imports_of(plugin, path)):
            if forbidden(dotted):
                shown = " -> ".join(p.relative_to(plugin).as_posix() for p in chain)
                problems.append(f"{shown} imports {dotted}")
                continue
            target = _file_for(plugin, dotted)
            if target is None:
                # A bare sibling import of an entry script (engine's ``config`` shim).
                sibling = path.parent / (dotted.split(".")[0] + ".py")
                if path.parent != plugin / LIB and "." not in dotted and sibling.is_file():
                    target = sibling
            if target is not None and target not in seen:
                queue.append((target, chain + (target,)))
                # Importing a package or submodule runs its parents' __init__ files.
                parent = target.parent
                init = parent / "__init__.py"
                if parent.is_relative_to(plugin / LIB) and init.is_file() and init not in seen:
                    queue.append((init, chain + (init,)))
    return problems


def test_standalone_import_closure_reaches_no_contract_or_code_review():
    entries = entry_points(PLUGIN)
    assert closure_violations(PLUGIN, entries) == []


def test_the_scan_is_not_vacuous():
    """The entries and the walk cover the real engine, not an empty set."""
    entries = {p.relative_to(PLUGIN).as_posix() for p in entry_points(PLUGIN)}
    assert {"engine/bootstrap_engine.py", "scripts/bootstrap_cli.py",
            f"{LIB}/harvest.py", f"{LIB}/display_relay.py"} <= entries
    reached = set()
    queue = list(entry_points(PLUGIN))
    while queue:
        path = queue.pop()
        if path in reached:
            continue
        reached.add(path)
        for dotted in imports_of(PLUGIN, path):
            target = _file_for(PLUGIN, dotted)
            if target is not None:
                queue.append(target)
    names = {p.relative_to(PLUGIN).as_posix() for p in reached}
    assert f"{LIB}/engine.py" in names and f"{LIB}/shared_lib.py" in names
    # The engine's Codex question reaches the stdlib probe, never the alias.
    assert f"{LIB}/codex_detection.py" in names and f"{LIB}/codex.py" not in names
    assert len(names) > 20


def _tree(tmp_path: Path, extra: str) -> tuple[Path, list[Path]]:
    plugin = tmp_path / "bootstrap"
    (plugin / LIB / "code_review").mkdir(parents=True)
    (plugin / "engine").mkdir()
    for name in ("__init__", "mid", *CONTRACT_MODULES):
        (plugin / LIB / f"{name}.py").write_text("")
    (plugin / LIB / "code_review" / "__init__.py").write_text("")
    (plugin / LIB / "code_review" / "lane.py").write_text("")
    (plugin / LIB / "mid.py").write_text(extra)
    entry = plugin / "engine" / "entry.py"
    entry.write_text("from bootstrap_lib import mid\n")
    return plugin, [entry]


@pytest.mark.parametrize("extra", [
    "from bootstrap_lib import model_declaration\n",
    "import bootstrap_lib.execution_event\n",
    "from . import interrupt_contract\n",
    "from .model_declaration import CORE_IDS\n",
    "def f():\n    from bootstrap_lib.code_review import lane\n",
    "from bootstrap_lib.code_review.lane import x\n",
    "import katsoft_contracts\n",
    "from bootstrap_lib import codex\n",
    "from .codex import detect_codex\n",
    "import bootstrap_lib.codex\n",
    "import bootstrap_lib._llm_kit_alias\n",
    "import llm_kit.adapters.codex_command\n",
    "def f():\n    from llm_kit.adapters import codex_command\n",
    "import importlib\nimportlib.import_module('llm_kit.adapters.codex_command')\n",
    "import importlib\nimportlib.import_module('bootstrap_lib.model_declaration')\n",
])
def test_a_planted_import_is_found(tmp_path, extra):
    plugin, entries = _tree(tmp_path, extra)
    assert closure_violations(plugin, entries), extra


def test_a_clean_tree_has_no_violation(tmp_path):
    plugin, entries = _tree(tmp_path, "import json\nfrom . import mid\n")
    assert closure_violations(plugin, entries) == []


def test_the_codex_detection_probe_is_not_the_codex_alias(tmp_path):
    plugin, entries = _tree(tmp_path, "from . import codex_detection\nfrom .codex_detection import detect_codex\n")
    (plugin / LIB / "codex_detection.py").write_text("import shutil\n")
    assert closure_violations(plugin, entries) == []
