"""Shared paths and helpers for the task-kit tests that run the plugin's scripts as subprocesses.

The module keeps its original name because several tests import it. Everything builds under pytest
``tmp_path``: the real repo's ``dev/tasks``, ``tmp`` and ``~/.claude`` are never read or written.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "task-kit"
SCRIPTS_DIR = PLUGIN_ROOT / "skills" / "task" / "scripts"
ENTRYPOINT = PLUGIN_ROOT / "bin" / "task_kit_entrypoint.py"
TASK_CLI = SCRIPTS_DIR / "task.py"

# Markers a dashboard run's command carried; an earlier dashboard's worker could leave them in the environment.
RUN_MARKERS = ("TASK_KIT_AGENT_DEPTH", "TASK_KIT_RUN_TOKEN", "TASK_KIT_DASHBOARD_ROOT")


def clean_env(extra: dict | None = None, *, base: dict | None = None) -> dict:
    """An environment for a subprocess the test builds: ``base`` (default: this process's) without the run markers."""
    env = dict(os.environ if base is None else base)
    for name in RUN_MARKERS:
        env.pop(name, None)
    env.update(extra or {})
    return env


def tree_digest(*roots: Path) -> str:
    """SHA-256 over every file (path and bytes) under the roots, order-independent."""
    digest = hashlib.sha256()
    entries = []
    for root in roots:
        for path in sorted(Path(root).rglob("*")):
            if path.is_file():
                entries.append((path.relative_to(root).as_posix(), path.read_bytes()))
    for rel, data in sorted(entries):
        digest.update(rel.encode())
        digest.update(hashlib.sha256(data).digest())
    return digest.hexdigest()
