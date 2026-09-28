"""task.py's `except ImportError` guard must not discard the real exception.

Regression coverage for a real incident: a `PermissionError [WinError 5]
Access is denied` inside a dependency surfaced as
``ImportError: cannot import name 'schema_engine' from 'skills_kit_lib'
(unknown location)``, and the guard's ``except ImportError:`` handler threw
that message away, printing only the canonical
"missing: skills_kit_lib/pyyaml" text -- sending diagnosis toward a
nonexistent missing-pyyaml problem instead of the real cause.

This test forces task.py's module-level ``task_system`` import to fail with a
controlled ImportError and asserts the underlying exception's type and
message are printed to stderr, ahead of the canonical bootstrap message, and
that the exit code is unchanged (EXIT_BOOTSTRAP_MISSING).

The scenario is built in a scratch directory rather than by breaking the real
``task_system`` package: task.py is copied next to a stub ``task_system.py``
that raises the crafted ImportError, so Python's script-directory-first
sys.path rule (``sys.path[0]`` is the running script's own directory) shadows
the real package with no risk to shared repo/test state.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from bootstrap_guard import _REEXEC_GUARD_ENV, EXIT_BOOTSTRAP_MISSING

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TASK_SCRIPTS_DIR = (
    _REPO_ROOT / "plugins" / "awesome-kit" / "skills" / "task" / "scripts"
)
_TASK_CLI = _TASK_SCRIPTS_DIR / "task.py"
_BOOTSTRAP_GUARD = _TASK_SCRIPTS_DIR / "bootstrap_guard.py"

_UNDERLYING_MESSAGE = (
    "cannot import name 'schema_engine' from 'skills_kit_lib' (unknown location)"
)


def test_import_error_prints_underlying_exception_before_canonical_message(
    tmp_path,
):
    # Build a scratch script directory: real task.py + real bootstrap_guard.py,
    # plus a stub task_system module that fails with the exact ImportError text
    # observed in the real incident (a masked PermissionError).
    shutil.copy(_TASK_CLI, tmp_path / "task.py")
    shutil.copy(_BOOTSTRAP_GUARD, tmp_path / "bootstrap_guard.py")
    (tmp_path / "task_system.py").write_text(
        f"raise ImportError({_UNDERLYING_MESSAGE!r})\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env[_REEXEC_GUARD_ENV] = "1"
    # Keep this hermetic: no PYTHONPATH pointing at the real task_system/
    # skills_kit_lib, so nothing can accidentally resolve the real package
    # instead of the stub.
    env.pop("PYTHONPATH", None)

    res = subprocess.run(
        [sys.executable, str(tmp_path / "task.py")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
    )

    assert res.returncode == EXIT_BOOTSTRAP_MISSING, res.stderr
    stderr_lines = res.stderr.splitlines()

    underlying_idx = next(
        i for i, line in enumerate(stderr_lines) if "underlying error:" in line
    )
    assert f"ImportError: {_UNDERLYING_MESSAGE}" in stderr_lines[underlying_idx]

    canonical_idx = next(
        i
        for i, line in enumerate(stderr_lines)
        if "has not provisioned" in line
    )
    assert "missing: skills_kit_lib/pyyaml" in stderr_lines[canonical_idx]

    # The underlying (real) error must appear BEFORE the canonical message --
    # that ordering is what keeps the real cause from being discarded/hidden.
    assert underlying_idx < canonical_idx
