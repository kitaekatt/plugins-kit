"""Tests for the plugins/job-kit/bin/ launcher shims.

Filesystem-level only: assert the shims exist, are shaped correctly, and
name a real target -- do not try to spawn a real session.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BIN_DIR = REPO_ROOT / "plugins" / "job-kit" / "bin"
POSIX_SHIM = BIN_DIR / "job-kit"
CMD_SHIM = BIN_DIR / "job-kit.cmd"


def test_posix_shim_exists() -> None:
    """The documented `job-kit` CLI resolves to a real launcher."""
    assert POSIX_SHIM.is_file()


@pytest.mark.skipif(
    os.name != "posix",
    reason=(
        "CPython on Windows derives st_mode execute bits from the file "
        "extension, so an extensionless shim never carries them there. The "
        "bit is a POSIX fact about a POSIX launcher."
    ),
)
def test_posix_shim_is_executable() -> None:
    """The shim carries the execute bit git tracks, so PATH can run it."""
    mode = POSIX_SHIM.stat().st_mode
    assert mode & stat.S_IXUSR
    assert mode & stat.S_IXGRP
    assert mode & stat.S_IXOTH


def test_cmd_shim_exists() -> None:
    """The Windows launcher ships alongside the POSIX one."""
    assert CMD_SHIM.is_file()


def test_both_shims_reference_the_job_kit_data_dir_venv() -> None:
    """Both shims prefer the bootstrap-provisioned job-kit venv."""
    posix_text = POSIX_SHIM.read_text(encoding="utf-8")
    cmd_text = CMD_SHIM.read_text(encoding="utf-8")
    for text in (posix_text, cmd_text):
        assert "plugins/data/plugins-kit/job-kit" in text or (
            "plugins\\data\\plugins-kit\\job-kit" in text
        )
        assert ".venv" in text


def test_posix_shim_invokes_the_job_kit_entrypoint_module() -> None:
    """The shim execs job_kit_entrypoint.py -- it re-execs under the plugin
    venv on its own, so it is the safe target even from a fallback
    interpreter that never resolved the venv."""
    posix_text = POSIX_SHIM.read_text(encoding="utf-8")
    assert "job_kit_entrypoint.py" in posix_text
    entrypoint = REPO_ROOT / "plugins" / "job-kit" / "lib" / "job_kit_entrypoint.py"
    assert entrypoint.is_file()


def test_cmd_shim_invokes_the_job_kit_entrypoint_module() -> None:
    """The Windows shim targets the same entrypoint as the POSIX one."""
    cmd_text = CMD_SHIM.read_text(encoding="utf-8")
    assert "job_kit_entrypoint.py" in cmd_text
