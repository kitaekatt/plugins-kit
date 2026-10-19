"""The dashboard never interferes with the task system the harness uses.

The binding ruling: the user keeps using the task system in Claude Code,
uninterrupted, while the dashboard runs beside it. Three red checks hold that:

1. The import graph: ``task.py`` and every ``task_system`` module outside the
   dashboard package import nothing from the dashboard (or from llm-kit), so a
   task verb behaves identically whether or not a dashboard exists.
2. A dashboard refresh leaves every task folder byte-identical.
3. Existing verbs' stdout, stderr and exit code are byte-identical with a
   dashboard server running and being polled.
"""

from __future__ import annotations

import ast
import threading
import time
from pathlib import Path

import pytest
from dashboard_support import (
    SCRIPTS_DIR,
    TASK_CLI,
    Running,
    make_task,
    run_task_cli,
    tree_digest,
)

SYSTEM = SCRIPTS_DIR / "task_system"
FORBIDDEN_ROOTS = ("llm_kit",)


def forbidden_imports(source: str, *, package: tuple[str, ...]) -> list[str]:
    """Imports of the dashboard package or llm-kit in ``source``.

    ``package`` is the importing module's package path (``()`` for task.py,
    ``("task_system",)`` for a sibling module), which resolves relative imports.
    """
    hits = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] in FORBIDDEN_ROOTS or parts[:2] == ["task_system", "dashboard"] or parts[0] == "dashboard":
                    hits.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = list(package[: len(package) - node.level + 1]) if package else []
                parts = base + (node.module.split(".") if node.module else [])
            else:
                parts = node.module.split(".") if node.module else []
            names = [a.name for a in node.names]
            shown = "." * node.level + (node.module or "")
            if parts and parts[0] in FORBIDDEN_ROOTS:
                hits.append(f"from {shown} import ...")
            elif parts[:2] == ["task_system", "dashboard"] or parts[:1] == ["dashboard"]:
                hits.append(f"from {shown} import ...")
            elif parts == ["task_system"] and "dashboard" in names:
                hits.append(f"from {shown} import dashboard")
    return hits


def _non_dashboard_sources() -> list[tuple[Path, tuple[str, ...]]]:
    sources = [(TASK_CLI, ())]
    for path in sorted(SYSTEM.rglob("*.py")):
        rel = path.relative_to(SYSTEM)
        if rel.parts[0] == "dashboard":
            continue
        sources.append((path, ("task_system",) + rel.parts[:-1]))
    return sources


# The ONE llm_kit import that exists in task_system independent of the dashboard:
# review's lazy summary-repair backend. Named exactly (file and import), never
# a blanket allowance for llm_kit.
PRE_EXISTING_LLM_KIT_SITE = ("task_system/summary_ops.py", "from llm_kit.completion.types import ...")


def _offenders(sources) -> dict[str, list[str]]:
    found = {}
    for path, package in sources:
        rel = path.relative_to(SCRIPTS_DIR).as_posix()
        hits = [
            h for h in forbidden_imports(path.read_text(encoding="utf-8"), package=package)
            if (rel, h) != PRE_EXISTING_LLM_KIT_SITE
        ]
        if hits:
            found[rel] = hits
    return found


def test_task_py_and_task_system_import_nothing_from_the_dashboard_and_only_the_named_llm_kit_site():
    """RED CHECK 1: no dashboard import anywhere outside the dashboard package, and
    llm_kit only at the single pre-existing summary-repair site."""
    sources = _non_dashboard_sources()
    assert len(sources) > 15  # the scan really covers task.py and the whole package
    assert not _offenders(sources)


def test_the_named_llm_kit_site_still_exists_so_the_exception_is_not_stale():
    rel, hit = PRE_EXISTING_LLM_KIT_SITE
    path = SCRIPTS_DIR / rel
    assert hit in forbidden_imports(path.read_text(encoding="utf-8"), package=("task_system",))


EDGE_MODULE = "jobs_edge.py"


def test_only_the_jobs_edge_touches_llm_kit_and_only_lazily():
    """The optional llm-kit edge is ONE module: no other dashboard file or launcher
    imports llm_kit, not even through a string-built import."""
    files = sorted((SYSTEM / "dashboard").rglob("*.py")) + sorted((SCRIPTS_DIR.parents[2] / "bin").glob("*.py"))
    assert len(files) > 8
    assert any(p.name == EDGE_MODULE for p in files)
    for path in files:
        text = path.read_text(encoding="utf-8")
        if path.name == EDGE_MODULE:
            continue
        assert not any(h.startswith(("import llm_kit", "from llm_kit")) for h in
                       forbidden_imports(text, package=("task_system", "dashboard"))), path
        assert "llm_kit" not in text, path
    edge = (SYSTEM / "dashboard" / EDGE_MODULE).read_text(encoding="utf-8")
    top_level = [n for n in ast.parse(edge).body if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert not any("llm_kit" in ast.dump(n) for n in top_level), "the edge imports llm_kit only inside functions"


def test_the_one_site_exception_does_not_excuse_any_other_llm_kit_import():
    """The narrowed guard is shown to still see an llm_kit import anywhere else."""
    other = "def f():" + chr(10) + "    from llm_kit.jobs import run" + chr(10)
    assert forbidden_imports(other, package=("task_system",))
    assert _offenders([(SYSTEM / "init.py", ("task_system",))]) == {}
    assert (SYSTEM / "summary_ops.py").relative_to(SCRIPTS_DIR).as_posix() == PRE_EXISTING_LLM_KIT_SITE[0]


@pytest.mark.parametrize("source,package", [
    ("from task_system.dashboard import launcher", ()),
    ("from task_system.dashboard.server import run", ("task_system",)),
    ("import task_system.dashboard.state", ()),
    ("from .dashboard import inventory", ("task_system",)),
    ("from . import dashboard", ("task_system",)),
    ("from task_system import dashboard", ()),
    ("import dashboard", ()),
    ("from llm_kit.jobs import run", ()),
    ("import llm_kit", ("task_system",)),
    ("def f():\n    from task_system.dashboard import state\n", ("task_system",)),
])
def test_the_import_scanner_catches_every_spelling(source, package):
    """The guard is shown to see what it forbids, including lazy imports."""
    assert forbidden_imports(source, package=package)


@pytest.mark.parametrize("source,package", [
    ("from .discovery import discover", ("task_system",)),
    ("from task_system import resolve", ()),
    ("import yaml", ()),
])
def test_the_import_scanner_leaves_ordinary_imports_alone(source, package):
    assert not forbidden_imports(source, package=package)


def test_the_dashboard_package_does_import_the_task_system():
    """The edge runs one way: dashboard -> task_system, never back."""
    text = (SYSTEM / "dashboard" / "inventory.py").read_text(encoding="utf-8")
    assert "from ..discovery import" in text and "from ..validate import" in text


def world(tmp_path: Path) -> tuple[Path, Path]:
    devroot = tmp_path / "devroot"
    project = devroot / "proj"
    project.mkdir(parents=True)
    make_task(project, "dev/tasks", "alpha", title="Alpha", summary="Alpha does a thing.",
              items=[{"id": "do-it", "title": "Do it", "state": "available", "priority": "P1"},
                     {"id": "wait", "title": "Wait", "state": "blocked-user", "note": "n"}],
              sections={"do-it": "Body."}, log="- 2026-10-01 09:00: began\n")
    make_task(project, "tmp", "beta", title="Beta", status="deferred")
    make_task(project, "dev/tasks", "gamma", title="Gamma", status="closed")
    return devroot, project


def test_refreshing_leaves_every_task_folder_byte_identical(tmp_path):
    """RED CHECK 2: through the real server, every kind of refresh and read."""
    devroot, _project = world(tmp_path)
    before = tree_digest(devroot)
    srv = Running(tmp_path / "data", (devroot,))
    try:
        pid = srv.json("GET", "/api/v1/dashboard")[2]["projects"][0]["id"]
        for path in ("/api/v1/dashboard", "/api/v1/dashboard?refresh=1", "/api/v1/dashboard?needs=1&q=do",
                     f"/api/v1/dashboard?project={pid}"):
            for _ in range(3):
                assert srv.json("GET", path)[0] == 200
        task = "dev%2Ftasks%2Falpha"
        assert srv.json("GET", f"/api/v1/items/{pid}/{task}/do-it")[0] == 200
        assert srv.json("GET", f"/api/v1/items/{pid}/{task}/wait/handoff")[0] == 200
    finally:
        srv.stop()
    assert tree_digest(devroot) == before


VERBS = (
    ["list", "--scope", "project"],
    ["list", "--scope", "project", "--format", "json"],
    ["list", "--scope", "project", "--status", "closed"],
    ["items", "dev/tasks/alpha"],
    ["show", "dev/tasks/alpha"],
    ["validate", "dev/tasks/alpha"],
    ["validate", "tmp/beta"],
    ["status", "dev/tasks/alpha"],
    ["show", "dev/tasks/does-not-exist"],
)


def test_task_verbs_are_byte_identical_with_a_dashboard_server_running(tmp_path):
    """RED CHECK 3: stdout, stderr and exit code, with the server up and being polled."""
    devroot, project = world(tmp_path)
    without = [run_task_cli(args + ["--root", str(project)], cwd=project) for args in VERBS]
    assert any(r.returncode == 0 for r in without) and any(r.returncode != 0 for r in without)

    srv = Running(tmp_path / "data", (devroot,))
    stop = threading.Event()

    def poll():
        while not stop.is_set():
            srv.request("GET", "/api/v1/dashboard?refresh=1")
            time.sleep(0.05)

    poller = threading.Thread(target=poll, daemon=True)
    poller.start()
    try:
        with_server = [run_task_cli(args + ["--root", str(project)], cwd=project) for args in VERBS]
    finally:
        stop.set()
        poller.join(timeout=10)
        srv.stop()
    for args, a, b in zip(VERBS, without, with_server):
        assert (a.returncode, a.stdout, a.stderr) == (b.returncode, b.stdout, b.stderr), args
