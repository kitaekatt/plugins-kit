"""The server's code identity: what the hash covers, and that it cannot drift.

The launcher restarts a server whose code hash differs from the code on disk.
Too narrow a manifest leaves a stale server serving old behaviour; too wide
restarts it for changes that cannot matter. ``IMPORTED_TASK_SYSTEM_MODULES`` is
checked against the dashboard's REAL imports so it cannot be a stale survey.
"""

from __future__ import annotations

import ast
import shutil
from pathlib import Path

import pytest
from dashboard_support import DASHBOARD_DIR, SCRIPTS_DIR

from task_system.dashboard import codehash

SYSTEM = SCRIPTS_DIR / "task_system"


def _imported_task_system_modules(path: Path, package: tuple[str, ...]) -> set[str]:
    """Names of task_system modules a file imports, for relative and absolute forms."""
    found: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                base = list(package[: len(package) - node.level + 1])
                parts = base + (node.module.split(".") if node.module else [])
                names = [a.name for a in node.names]
            else:
                parts = node.module.split(".") if node.module else []
                names = [a.name for a in node.names]
            if parts[:1] != ["task_system"]:
                continue
            tail = parts[1:]
            if tail and tail[0] == "dashboard":
                continue
            if tail:
                found.add(tail[0])
            else:
                found.update(names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] == "task_system" and len(parts) > 1 and parts[1] != "dashboard":
                    found.add(parts[1])
    return found


def real_imports() -> set[str]:
    closure: set[str] = set()
    pending: list[tuple[Path, tuple[str, ...]]] = [
        (p, ("task_system", "dashboard")) for p in DASHBOARD_DIR.rglob("*.py")
    ]
    while pending:
        path, package = pending.pop()
        for name in _imported_task_system_modules(path, package):
            module = SYSTEM / f"{name}.py"
            if module.is_file() and name not in closure:
                closure.add(name)
                pending.append((module, ("task_system",)))
    return {f"{name}.py" for name in closure}


def test_the_manifest_lists_exactly_the_task_system_modules_the_dashboard_imports():
    assert real_imports() == set(codehash.IMPORTED_TASK_SYSTEM_MODULES)


def test_the_import_scanner_sees_relative_and_absolute_forms(tmp_path):
    """The scanner is shown able to find what it must (a blind scanner proves nothing)."""
    source = tmp_path / "m.py"
    source.write_text(
        "from ..discovery import x\nfrom .. import resolve, schemas\n"
        "from task_system.validate import y\nimport task_system.types\n"
        "from . import server\nfrom task_system.dashboard import state\n", encoding="utf-8")
    assert _imported_task_system_modules(source, ("task_system", "dashboard")) == {
        "discovery", "resolve", "schemas", "validate", "types"}


@pytest.fixture
def copy(tmp_path):
    scripts = tmp_path / "scripts"
    shutil.copytree(SCRIPTS_DIR, scripts, ignore=shutil.ignore_patterns("__pycache__"))
    return scripts


def test_hash_is_stable_and_equal_for_equal_trees(copy):
    assert codehash.code_hash(copy) == codehash.code_hash(copy) == codehash.code_hash(SCRIPTS_DIR)


@pytest.mark.parametrize("rel", [
    "task_system/dashboard/server.py",
    "task_system/dashboard/static/app.js",
    "task_system/dashboard/static/index.html",
    "task_system/validate.py",
    "task_system/discovery.py",
    "task_system/init.py",
    "task_system/relocate.py",
])
def test_a_change_to_covered_code_changes_the_hash(copy, rel):
    before = codehash.code_hash(copy)
    target = copy / rel
    target.write_text(target.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
    assert codehash.code_hash(copy) != before


@pytest.mark.parametrize("rel", ["task_system/review_html.py", "task.py", "bootstrap_guard.py"])
def test_a_change_to_uncovered_files_does_not_restart_the_server(copy, rel):
    before = codehash.code_hash(copy)
    target = copy / rel
    target.write_text(target.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
    assert codehash.code_hash(copy) == before


def test_line_endings_do_not_change_the_hash(copy):
    before = codehash.code_hash(copy)
    target = copy / "task_system" / "dashboard" / "server.py"
    target.write_bytes(target.read_bytes().replace(b"\n", b"\r\n"))
    assert codehash.code_hash(copy) == before


def test_a_missing_covered_module_is_an_error_not_a_smaller_hash(copy):
    (copy / "task_system" / "validate.py").unlink()
    with pytest.raises(FileNotFoundError, match="validate.py"):
        codehash.code_hash(copy)
