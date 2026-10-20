"""Shared sys.path setup for the task-kit test suite.

Mirrors the tests/skills-kit / tests/workflow-kit pattern: put the plugin code
directories on sys.path so tests import plugin modules directly when run under
the repo-root pytest. plugins/skills-kit is also on pyproject pythonpath; it
is added here too so this suite stays self-contained.
"""

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
