"""Shared sys.path setup for the task-kit test suite.

Mirrors the tests/skills-kit / tests/workflow-kit pattern: put the plugin code
directories on sys.path so tests import plugin modules directly when run under
the repo-root pytest. plugins/skills-kit is also on pyproject pythonpath; it
is added here too so this suite stays self-contained.
"""

import importlib
import os
import sys
from pathlib import Path

import pytest

# Scripts that re-exec into the plugin venv (task.py)
# call os.execv at import time, which abandons the pytest process itself and
# yields a false green. This env var makes the re-exec a no-op, matching how the
# real script behaves once the guard has already fired. See plugins/CLAUDE.md.
os.environ.setdefault("_BOOTSTRAP_GUARD_SKIP_REEXEC", "1")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SYS_PATHS = (
    _REPO_ROOT / "plugins" / "skills-kit",  # skills_kit_lib
    # task_system package + vendored bootstrap_guard
    _REPO_ROOT / "plugins" / "task-kit" / "skills" / "task" / "scripts",
)
for _p in _SYS_PATHS:
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


@pytest.fixture
def symlinked_root(tmp_path: Path) -> tuple[Path, Path]:
    """A project root whose ``dev/tasks`` is a REAL symlink to an external
    directory -- the standard multi-repo task-folder topology (a task repo
    linked in at ``dev/tasks``). Yields ``(project_root, link_target)``.

    A real symlink is the only way to reproduce the topology; creating one
    needs no elevated privilege on macOS/Linux (``os.symlink`` works as an
    unprivileged user there), so this is skipped only on Windows, where it
    does.
    """
    if os.name == "nt":
        pytest.skip(
            "real symlinks need an elevated privilege (SeCreateSymbolicLinkPrivilege) "
            "on Windows; this fixture is exercised on macOS/Linux"
        )
    project_root = tmp_path / "project"
    link_target = tmp_path / "tasks-repo"
    (project_root / "dev").mkdir(parents=True)
    link_target.mkdir()
    os.symlink(link_target, project_root / "dev" / "tasks", target_is_directory=True)
    return project_root, link_target


@pytest.fixture
def outside_a_run(monkeypatch):
    """A test that calls the task-kit launcher in-process runs as if outside any dashboard run.

    A worker that runs these very tests inherits ``TASK_KIT_AGENT_DEPTH`` from its dashboard run, and the launcher
    refuses on it. The variables are removed for THIS test only; nothing is deleted globally. A test that builds a
    subprocess environment uses ``dashboard_support.clean_env`` instead.
    """
    for name in ("TASK_KIT_AGENT_DEPTH", "TASK_KIT_RUN_TOKEN"):
        monkeypatch.delenv(name, raising=False)


_LLM_KIT_LIB = (_REPO_ROOT / "plugins" / "llm-kit" / "lib").resolve()


def _is_llm_kit_module(name: str) -> bool:
    return name == "llm_kit" or name.startswith("llm_kit.")


@pytest.fixture(autouse=True)
def llm_kit_is_absent_unless_a_test_adds_it():
    """Every task-kit test starts with ``llm_kit`` unimportable, whatever ran before it on the worker.

    llm-kit is an OPTIONAL edge of task-kit, and the dashboard tests assert what it reports when the
    package is absent. Another suite's conftest (``tests/llm-kit/conftest.py``) puts
    ``plugins/llm-kit/lib`` on ``sys.path`` while pytest collects, and any test may leave ``llm_kit``
    modules in ``sys.modules``; both are process-global, so under a full run (collection loads every
    conftest in every xdist worker) the "absent" probes found the real package. A test that wants
    llm-kit adds the library itself (``monkeypatch.syspath_prepend``) and purges what it imported;
    this fixture hides the rest for the test and puts back exactly what it found.
    """
    saved_path = list(sys.path)
    hidden = {name: module for name, module in sys.modules.items() if _is_llm_kit_module(name)}
    sys.path[:] = [entry for entry in saved_path if not (entry and Path(entry).resolve() == _LLM_KIT_LIB)]
    for name in hidden:
        del sys.modules[name]
    importlib.invalidate_caches()
    try:
        yield
    finally:
        for name in [n for n in sys.modules if _is_llm_kit_module(n)]:
            del sys.modules[name]
        sys.modules.update(hidden)
        sys.path[:] = saved_path
        importlib.invalidate_caches()
