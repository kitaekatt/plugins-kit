"""Project-local Git configuration checks and remediation."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import NamedTuple


class GitConfigResult(NamedTuple):
    passed: bool
    key: str
    message: str


def _run_git(project_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(
        ["git", "-C", str(project_dir), "config", "--local", *args],
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )


def check_git_config(project_dir: Path, key: str, expected: str) -> GitConfigResult:
    """Return whether one project-local Git key has exactly the expected value."""
    try:
        result = _run_git(project_dir, "--get-all", key)
    except (OSError, subprocess.SubprocessError) as exc:
        return GitConfigResult(False, key, f"check failed: {exc}")
    values = result.stdout.splitlines() if result.returncode == 0 else []
    if values == [expected]:
        return GitConfigResult(True, key, f"set to {expected}")
    if values:
        return GitConfigResult(False, key, f"found {values!r}, expected {expected!r}")
    return GitConfigResult(False, key, f"not set, expected {expected!r}")


def write_git_config(project_dir: Path, key: str, value: str) -> GitConfigResult:
    """Set one project-local Git key and verify the resulting value."""
    try:
        result = _run_git(project_dir, "--replace-all", key, value)
    except (OSError, subprocess.SubprocessError) as exc:
        return GitConfigResult(False, key, f"write failed: {exc}")
    if result.returncode != 0:
        detail = result.stderr.strip() or "git config failed"
        return GitConfigResult(False, key, detail)
    return check_git_config(project_dir, key, value)
