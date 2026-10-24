"""Shared sys.path setup for the task-kit test suite.

Mirrors the tests/skills-kit / tests/workflow-kit pattern: put the plugin code
directories on sys.path so tests import plugin modules directly when run under
the repo-root pytest. plugins/skills-kit is also on pyproject pythonpath; it
is added here too so this suite stays self-contained.
"""

import importlib
import importlib.abc
import importlib.machinery
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

# Scripts that re-exec into the plugin venv (task.py)
# call os.execv at import time, which abandons the pytest process itself and
# yields a false green. This env var makes the re-exec a no-op, matching how the
# real script behaves once the guard has already fired. See plugins/CLAUDE.md.
os.environ.setdefault("_BOOTSTRAP_GUARD_SKIP_REEXEC", "1")

# A test run inside a Claude Code session inherits that session's identity. A test that registered a working
# session (or took a hold) under it would write the real session's records and publish them, so both variables
# are removed at import. CLAUDE_BOOTSTRAP_DATA_ROOT is NOT redirected here: bootstrap_guard refuses to run
# task.py under a redirected data root without a provisioned venv there, which fails every CLI test.
# Guard: test_task_env_sanitized.py runs a child pytest with both set.
for _name in ("CLAUDE_PID", "CLAUDE_CODE_SESSION_ID"):
    os.environ.pop(_name, None)

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


def _is_llm_kit_module(name: str) -> bool:
    return name == "llm_kit" or name.startswith("llm_kit.")


class _AbsentInstalledLlmKit(importlib.abc.MetaPathFinder):
    """Hide the installed library while allowing a test's explicit synthetic path."""

    def __init__(self, initial_path: list[str]) -> None:
        self.initial_path = set(initial_path)

    def find_spec(
        self, fullname: str, path: object = None, target: object = None
    ) -> importlib.machinery.ModuleSpec | None:
        if fullname != "llm_kit":
            return None
        synthetic_paths = [entry for entry in sys.path if entry not in self.initial_path]
        spec = importlib.machinery.PathFinder.find_spec(fullname, synthetic_paths)
        if spec is not None:
            return spec
        raise ModuleNotFoundError("No module named 'llm_kit'", name="llm_kit")


@pytest.fixture(autouse=True)
def llm_kit_is_absent_unless_a_test_adds_it(request: pytest.FixtureRequest) -> Iterator[None]:
    """Default to an absent library even when the project venv installs it.

    The explicit ``llm`` fixture opts into the installed package. Other tests
    can still supply fake modules or prepend a synthetic package directory.
    Clear prior imports and restore the worker's import state after each test.
    """
    saved_path = list(sys.path)
    saved_meta_path = list(sys.meta_path)
    hidden = {name: module for name, module in sys.modules.items() if _is_llm_kit_module(name)}
    for name in hidden:
        del sys.modules[name]
    if "llm" not in request.fixturenames:
        sys.meta_path.insert(0, _AbsentInstalledLlmKit(saved_path))
    importlib.invalidate_caches()
    try:
        yield
    finally:
        for name in [n for n in sys.modules if _is_llm_kit_module(n)]:
            del sys.modules[name]
        sys.modules.update(hidden)
        sys.path[:] = saved_path
        sys.meta_path[:] = saved_meta_path
        importlib.invalidate_caches()
