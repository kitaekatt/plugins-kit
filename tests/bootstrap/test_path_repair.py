"""Tests for bootstrap_lib/path_repair.py and its vendored copies."""

import filecmp
import os
import sys
from pathlib import Path
from unittest.mock import patch

from bootstrap_lib.path_repair import PathRepairResult, repair_path
from test_support.fake_winreg import FakeWinreg


_REPO_ROOT = Path(__file__).resolve().parents[2]
_CANON = _REPO_ROOT / "plugins" / "bootstrap" / "bootstrap_lib" / "path_repair.py"


def _vendored_copies():
    """Every path_repair.py under plugins/ except the canonical and any that
    live inside a virtualenv / site-packages / cache dir. Glob discovery (the
    test_bootstrap_guard.py pattern) so a new vendored copy is auto-covered
    instead of silently escaping a hardcoded list."""
    skip = {".venv", "site-packages", "__pycache__", "node_modules"}
    out = []
    for p in _REPO_ROOT.glob("plugins/**/path_repair.py"):
        if p.resolve() == _CANON.resolve():
            continue
        if any(part in skip for part in p.parts):
            continue
        out.append(p)
    return out


class TestRepairPath:
    def test_dedups_inherited_path(self):
        # Three duplicates of the same entry should collapse to one
        env = {"PATH": os.pathsep.join(["/a", "/b", "/A", "/b", "/c"])}
        with patch.dict(os.environ, env, clear=True), \
             patch("bootstrap_lib.path_repair.sys") as mock_sys:
            mock_sys.platform = "linux"
            result = repair_path()
            entries = [p for p in os.environ["PATH"].split(os.pathsep) if p]
            assert len(entries) == 3
        assert result.before_entries == 5
        assert result.after_entries == 3
        assert result.deduped == 2
        assert result.restored == 0
        assert result.changed is True

    def test_no_change_when_already_clean(self):
        env = {"PATH": os.pathsep.join(["/a", "/b", "/c"])}
        with patch.dict(os.environ, env, clear=True), \
             patch("bootstrap_lib.path_repair.sys") as mock_sys:
            mock_sys.platform = "linux"
            result = repair_path()

        assert result.changed is False
        assert result.deduped == 0
        assert result.restored == 0

    def test_idempotent(self):
        env = {"PATH": os.pathsep.join(["/a", "/a", "/b"])}
        with patch.dict(os.environ, env, clear=True), \
             patch("bootstrap_lib.path_repair.sys") as mock_sys:
            mock_sys.platform = "linux"
            repair_path()
            second = repair_path()
        assert second.changed is False

    def test_handles_empty_path(self):
        env = {"PATH": ""}
        with patch.dict(os.environ, env, clear=True), \
             patch("bootstrap_lib.path_repair.sys") as mock_sys:
            mock_sys.platform = "linux"
            result = repair_path()
        assert result.before_entries == 0
        assert result.after_entries == 0

    def test_skips_registry_on_non_windows(self):
        # On non-Windows the function must not touch winreg
        env = {"PATH": "/a"}
        with patch.dict(os.environ, env, clear=True), \
             patch("bootstrap_lib.path_repair.sys") as mock_sys:
            mock_sys.platform = "linux"
            result = repair_path()
        assert result.restored == 0

    def test_merges_system_and_user_registry_paths_in_order(self):
        fake = FakeWinreg()
        system_key = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
        fake.set_value(fake.HKEY_LOCAL_MACHINE, system_key, "Path",
                       "/system;/shared")
        fake.set_value(fake.HKEY_CURRENT_USER, "Environment", "Path",
                       "/user;/shared;/system")
        env = {"PATH": "/inherited"}

        with patch.dict(os.environ, env, clear=True), \
             patch.dict(sys.modules, {"winreg": fake}), \
             patch("bootstrap_lib.path_repair.os.pathsep", ";"), \
            patch("bootstrap_lib.path_repair.sys") as mock_sys:
            mock_sys.platform = "win32"
            result = repair_path()
            actual_path = os.environ["PATH"]

        assert actual_path == ";".join(
            ["/inherited", "/system", "/shared", "/user"]
        )
        assert result.before_entries == 1
        assert result.after_entries == 4
        assert result.restored == 3
        assert result.changed is True

    def test_registry_merge_is_idempotent(self):
        fake = FakeWinreg()
        system_key = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
        fake.set_value(fake.HKEY_LOCAL_MACHINE, system_key, "Path", "/system")
        fake.set_value(fake.HKEY_CURRENT_USER, "Environment", "Path", "/user")

        with patch.dict(os.environ, {"PATH": "/inherited"}, clear=True), \
             patch.dict(sys.modules, {"winreg": fake}), \
             patch("bootstrap_lib.path_repair.sys") as mock_sys:
            mock_sys.platform = "win32"
            repair_path()
            second = repair_path()

        assert second.changed is False
        assert second.restored == 0


class TestVendoredCopiesInSync:
    """Vendored path_repair.py copies must be byte-identical to canon.

    Each consumer plugin keeps its own copy so it can run without
    depending on bootstrap being importable. Drift would mean different
    behavior across plugins for the same symptom.
    """

    def test_canon_exists(self):
        assert _CANON.is_file(), f"Canonical path_repair missing: {_CANON}"

    def test_at_least_one_vendored_copy_discovered(self):
        # Guards the glob itself: unreal-kit vendors a copy; if discovery ever
        # returns nothing, the sync assertion below would pass vacuously.
        assert _vendored_copies(), "glob discovered no vendored path_repair.py copies"

    def test_vendored_copies_match_canon(self):
        diffs = []
        for vendored in _vendored_copies():
            if not filecmp.cmp(_CANON, vendored, shallow=False):
                diffs.append(
                    f"diverged: {vendored}\n"
                    f"  fix: cp {_CANON} {vendored}"
                )
        assert not diffs, (
            "Vendored path_repair.py copies must match "
            f"{_CANON.relative_to(_REPO_ROOT)} (edit the canonical, then run "
            "the cp command(s) below):\n" + "\n".join(diffs)
        )
