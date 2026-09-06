"""Shared bounded subprocess capture for bootstrap installers."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from typing import Mapping


def run_captured(
    argv: str | Sequence[str],
    *,
    timeout: float,
    env: Mapping[str, str] | None = None,
    stdin_devnull: bool = True,
) -> tuple[int, str, str]:
    """Run a command with separate stdout/stderr and an optional closed stdin."""
    kwargs: dict[str, object] = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": timeout,
        "env": env,
    }
    if stdin_devnull:
        kwargs["stdin"] = subprocess.DEVNULL
    if isinstance(argv, str):
        kwargs["shell"] = True
    result = subprocess.run(argv, **kwargs)
    return result.returncode, result.stdout, result.stderr
