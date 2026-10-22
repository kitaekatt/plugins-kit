"""The code identity of a dashboard server.

The launcher restarts a running server when the code it would start differs
from the code the server is running. "The code" is an explicit manifest, so a
change that cannot alter server behaviour (a task edit, runtime data, a docs
page) never restarts anything:

- every ``*.py`` file and every static asset under ``task_system/dashboard/``;
- the ``task_system`` modules the dashboard imports in place
  (:data:`IMPORTED_TASK_SYSTEM_MODULES`) -- a change to discovery or
  validation changes what the server reports, so it must restart the server.

Line endings are normalized before hashing, so a CRLF working-copy checkout
and an LF plugin-cache copy of the same source hash alike. The plugin version
is not hashed: the bundle copies the code, not the manifest, and byte-equal
code must hash equal wherever it is read from.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

DASHBOARD_PACKAGE = ("task_system", "dashboard")

# task_system modules the dashboard package imports (directly or through
# another module listed here). tests/task-kit/test_dashboard_code_hash.py
# derives this set from the dashboard's real imports and fails on any drift.
IMPORTED_TASK_SYSTEM_MODULES = (
    "datadir.py",
    "discovery.py",
    "harness.py",
    "init.py",
    "machine.py",
    "oslock.py",
    "plan_sections.py",
    "procspawn.py",
    "procstamp.py",
    "relocate.py",
    "repo_identity.py",
    "resolve.py",
    "schemas.py",
    "session_publish.py",
    "sessions.py",
    "sessions_config.py",
    "task_items.py",
    "types.py",
    "validate.py",
    "vcs.py",
)

_ASSET_SUFFIXES = {".html", ".css", ".js", ".svg", ".png", ".ico"}
_SKIP_DIRS = {"__pycache__"}


def code_manifest(scripts_dir: Path) -> list[Path]:
    """Sorted absolute paths of every file the code hash covers."""
    scripts_dir = Path(scripts_dir)
    package = scripts_dir.joinpath(*DASHBOARD_PACKAGE)
    if not package.is_dir():
        raise FileNotFoundError(f"dashboard package not found at {package}")
    files: list[Path] = []
    for path in package.rglob("*"):
        if not path.is_file() or any(part in _SKIP_DIRS for part in path.parts):
            continue
        if path.suffix == ".py" or path.suffix in _ASSET_SUFFIXES:
            files.append(path)
    system = scripts_dir / "task_system"
    for name in IMPORTED_TASK_SYSTEM_MODULES:
        module = system / name
        if not module.is_file():
            raise FileNotFoundError(f"imported task_system module not found: {module}")
        files.append(module)
    return sorted(files)


def code_hash(scripts_dir: Path) -> str:
    """SHA-256 over the manifest: relative POSIX path and normalized content."""
    scripts_dir = Path(scripts_dir)
    digest = hashlib.sha256()
    for path in code_manifest(scripts_dir):
        rel = path.relative_to(scripts_dir).as_posix().encode("utf-8")
        content = path.read_bytes().replace(b"\r\n", b"\n")
        digest.update(len(rel).to_bytes(4, "big"))
        digest.update(rel)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()
