"""Tests for bootstrap_lib/stamps.py — the single string-valued marker-file API.

Pins: one atomic-write convention, one missing-file (default) convention, the
three scopes (global / per-plugin / per-project sha1 path), and the EXPLICIT
mtime contract (read never touches mtime; write is the only thing that advances
it — the cooldown `-nt` gate relies on this).
"""

import hashlib
import os
import time

from bootstrap_lib import stamps
from bootstrap_lib.stamps import Stamp, global_stamp, plugin_stamp, project_stamp


class TestReadWriteClearExists:
    def test_write_then_read(self, tmp_path):
        s = Stamp(tmp_path / "v")
        s.write("0.21.0")
        assert s.read() == "0.21.0"

    def test_read_strips_whitespace(self, tmp_path):
        p = tmp_path / "v"
        p.write_text("  0.21.0\n", encoding="utf-8")
        assert Stamp(p).read() == "0.21.0"

    def test_read_missing_returns_default(self, tmp_path):
        s = Stamp(tmp_path / "nope")
        assert s.read() == ""
        assert s.read("fallback") == "fallback"

    def test_read_never_raises_on_missing(self, tmp_path):
        # The one missing-file convention: read returns the default, never throws.
        assert Stamp(tmp_path / "a" / "b" / "missing").read("d") == "d"

    def test_exists(self, tmp_path):
        s = Stamp(tmp_path / "v")
        assert s.exists() is False
        s.write("x")
        assert s.exists() is True

    def test_clear_removes(self, tmp_path):
        s = Stamp(tmp_path / "v")
        s.write("x")
        assert s.exists()
        s.clear()
        assert not s.exists()

    def test_clear_missing_is_idempotent(self, tmp_path):
        # No raise on a missing file — matches the read convention.
        Stamp(tmp_path / "ghost").clear()

    def test_write_creates_parent_dirs(self, tmp_path):
        s = Stamp(tmp_path / "deep" / "nested" / "v")
        s.write("x")
        assert s.read() == "x"

    def test_write_overwrites(self, tmp_path):
        s = Stamp(tmp_path / "v")
        s.write("old")
        s.write("new")
        assert s.read() == "new"

    def test_write_coerces_non_str(self, tmp_path):
        s = Stamp(tmp_path / "v")
        s.write(12345)  # epoch ints are a real caller pattern
        assert s.read() == "12345"


class TestAtomicWrite:
    def test_write_routes_through_write_atomic(self, tmp_path, monkeypatch):
        calls = []
        real = stamps.write_atomic

        def _spy(path, content):
            calls.append((path, content))
            real(path, content)

        monkeypatch.setattr(stamps, "write_atomic", _spy)
        target = tmp_path / "v"
        Stamp(target).write("0.1.0")
        assert calls == [(str(target), "0.1.0")]

    def test_no_temp_leftovers(self, tmp_path):
        Stamp(tmp_path / "v").write("x")
        assert sorted(os.listdir(tmp_path)) == ["v"]


class TestMtimeContract:
    def test_mtime_missing_is_none(self, tmp_path):
        assert Stamp(tmp_path / "nope").mtime() is None

    def test_mtime_returns_float(self, tmp_path):
        s = Stamp(tmp_path / "v")
        s.write("x")
        m = s.mtime()
        assert isinstance(m, float)

    def test_read_does_not_advance_mtime(self, tmp_path):
        # Load-bearing: a cooldown SKIP reads the stamp and must NOT touch mtime,
        # or the `-nt` registry-bypass gate would mis-fire.
        s = Stamp(tmp_path / "v")
        s.write("x")
        m1 = s.mtime()
        time.sleep(0.05)
        s.read()
        s.read("d")
        s.exists()
        s.mtime()
        assert s.mtime() == m1

    def test_write_advances_mtime(self, tmp_path):
        s = Stamp(tmp_path / "v")
        s.write("x")
        m1 = s.mtime()
        time.sleep(0.05)
        s.write("y")  # explicit touch
        assert s.mtime() >= m1


class TestScopes:
    def test_global_scope_path(self, tmp_path):
        s = global_stamp(tmp_path, "last_version")
        assert s.path == tmp_path / "last_version"

    def test_plugin_scope_path(self, tmp_path):
        s = plugin_stamp(tmp_path / "p4-kit", "last_version")
        assert s.path == tmp_path / "p4-kit" / "last_version"

    def test_project_scope_path_matches_shell_layout(self, tmp_path):
        # Must match session-bootstrap.sh: cooldowns/<name>.<sha1-of-cwd>.
        project = "/c/dev/some-project"
        key = hashlib.sha1(project.encode("utf-8")).hexdigest()
        s = project_stamp(tmp_path, "last_run_epoch", project)
        assert s.path == tmp_path / "cooldowns" / f"last_run_epoch.{key}"

    def test_project_scope_global_fallback(self, tmp_path):
        # No project_dir -> the shell's _global_ bucket.
        s = project_stamp(tmp_path, "last_run_epoch", None)
        assert s.path == tmp_path / "cooldowns" / "last_run_epoch._global_"
        s2 = project_stamp(tmp_path, "last_run_epoch", "")
        assert s2.path == tmp_path / "cooldowns" / "last_run_epoch._global_"

    def test_project_scope_distinct_per_cwd(self, tmp_path):
        a = project_stamp(tmp_path, "last_run_epoch", "/proj/a")
        b = project_stamp(tmp_path, "last_run_epoch", "/proj/b")
        assert a.path != b.path
