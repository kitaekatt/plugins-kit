"""Tests for bootstrap_lib/atomic_write.py — the single shared atomic-write
implementation (B13).

engine.py used to carry its own fixed-name ``path + ".tmp"`` copy (concurrent
sessions collide on the temp name; a failed os.replace left the temp behind).
These tests pin the unified mkstemp-based implementation and that the former
duplicate call sites now delegate to it.
"""

import os

import pytest

from bootstrap_lib import atomic_write
from bootstrap_lib.atomic_write import write_atomic


class TestWriteAtomic:
    def test_writes_content(self, tmp_path):
        target = tmp_path / "out.json"
        write_atomic(str(target), '{"a": 1}')
        assert target.read_text() == '{"a": 1}'

    def test_replaces_existing_file(self, tmp_path):
        target = tmp_path / "out.json"
        target.write_text("old")
        write_atomic(str(target), "new")
        assert target.read_text() == "new"

    def test_creates_parent_directory(self, tmp_path):
        target = tmp_path / "deep" / "nested" / "out.json"
        write_atomic(str(target), "x")
        assert target.read_text() == "x"

    def test_no_temp_leftovers_on_success(self, tmp_path):
        target = tmp_path / "out.json"
        write_atomic(str(target), "x")
        assert sorted(os.listdir(tmp_path)) == ["out.json"]

    def test_failure_cleans_temp_and_preserves_target(self, tmp_path, monkeypatch):
        """On a failed replace the temp file is removed and the destination
        keeps its previous content (all-or-nothing)."""
        target = tmp_path / "out.json"
        target.write_text("old")

        def _boom(src, dst):
            raise OSError("replace denied")

        monkeypatch.setattr(atomic_write.os, "replace", _boom)
        with pytest.raises(OSError, match="replace denied"):
            write_atomic(str(target), "new")
        monkeypatch.undo()

        assert target.read_text() == "old"
        assert sorted(os.listdir(tmp_path)) == ["out.json"]


class TestSingleImplementation:
    """Pin the unification: the former duplicate call sites delegate here."""

    def test_engine_uses_shared_impl(self):
        from bootstrap_lib import engine
        assert engine._write_atomic is atomic_write.write_atomic, (
            "engine.py must not carry its own atomic-write copy (B13)"
        )

    def test_tool_paths_record_uses_shared_impl(self, tmp_path, monkeypatch):
        """tool_paths.record routes its state write through write_atomic."""
        from bootstrap_lib import tool_paths

        calls = []
        real = atomic_write.write_atomic

        def _spy(path, content):
            calls.append(path)
            real(path, content)

        monkeypatch.setattr(tool_paths, "write_atomic", _spy)
        data_dir = tmp_path / "bootstrap"
        data_dir.mkdir()
        tool_paths.record(str(data_dir), "jq", str(tmp_path / "jq"))
        assert calls == [os.path.join(str(data_dir), "tool_paths.json")]
        assert tool_paths.resolve(str(data_dir), "jq") == str(tmp_path / "jq")
