"""Shared sys.path setup for the awesome-kit test suite.

Mirrors the tests/skills-kit / tests/workflow-kit pattern: put the plugin code
directories on sys.path so tests import plugin modules directly when run under
the repo-root pytest. plugins/skills-kit is also on pyproject pythonpath; it
is added here too so this suite stays self-contained.
"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SYS_PATHS = (
    _REPO_ROOT / "plugins" / "skills-kit",  # skills_kit_lib
    # task_system package + vendored bootstrap_guard
    _REPO_ROOT / "plugins" / "awesome-kit" / "skills" / "task" / "scripts",
)
for _p in _SYS_PATHS:
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
