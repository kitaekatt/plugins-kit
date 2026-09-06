"""Tests for atomic stale-lock removal."""

import os
from pathlib import Path
from typing import Optional

import pytest

from bootstrap_lib import proc_lock


def test_stale_removal_preserves_lock_replaced_after_pid_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path = tmp_path / proc_lock.LOCK_FILENAME
    replacement_path = tmp_path / "replacement.lock"
    stale_pid = 999997
    fresh_pid = os.getpid()
    lock_path.write_text(f"{stale_pid}\n123.0\n", encoding="utf-8")

    real_read_lock_pid = proc_lock._read_lock_pid
    replaced = False

    def read_lock_pid(path: str) -> Optional[int]:
        nonlocal replaced
        owner = real_read_lock_pid(path)
        if path == str(lock_path) and not replaced:
            replacement_path.write_text(
                f"{fresh_pid}\n456.0\n", encoding="utf-8"
            )
            os.replace(replacement_path, lock_path)
            replaced = True
        return owner

    monkeypatch.setattr(proc_lock, "_read_lock_pid", read_lock_pid)

    proc_lock._remove_if_owned(str(lock_path), stale_pid)

    assert lock_path.exists()
    assert lock_path.read_text(encoding="utf-8").splitlines()[0] == str(fresh_pid)
