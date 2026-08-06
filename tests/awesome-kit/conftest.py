"""Shared sys.path setup for the awesome-kit test suite.

Mirrors the tests/skills-kit / tests/workflow-kit pattern: put the plugin code
directories on sys.path so tests import plugin modules directly when run under
the repo-root pytest. plugins/skills-kit is also on pyproject pythonpath; it
is added here too so this suite stays self-contained.
"""

import os
import sys
from pathlib import Path

# Scripts that re-exec into the plugin venv (task.py, orchestration_guidance.py)
# call os.execv at import time, which abandons the pytest process itself and
# yields a false green. This env var makes the re-exec a no-op, matching how the
# real script behaves once the guard has already fired. See plugins/CLAUDE.md.
os.environ.setdefault("_BOOTSTRAP_GUARD_VENV_REEXEC", "1")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SYS_PATHS = (
    _REPO_ROOT / "plugins" / "skills-kit",  # skills_kit_lib
    # task_system package + vendored bootstrap_guard
    _REPO_ROOT / "plugins" / "awesome-kit" / "skills" / "task" / "scripts",
    # orchestration_guidance + its vendored bootstrap_guard
    _REPO_ROOT / "plugins" / "awesome-kit" / "skills" / "orchestrate" / "scripts",
)
for _p in _SYS_PATHS:
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
