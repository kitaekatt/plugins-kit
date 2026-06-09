"""Tests for bootstrap_lib/tool_paths.py."""

import json
import os
from unittest.mock import patch

import pytest

from bootstrap_lib import tool_paths


def _bootstrap_dir(tmp_path):
    """Return a path that satisfies tool_paths' canonical-dir heuristic.

    The module redirects writes to its canonical dir unless the supplied
    path's basename is ``bootstrap`` (so tests can scope writes to a
    temp dir).
    """
    d = tmp_path / "bootstrap"
    d.mkdir()
    return str(d)


class TestRecordAndResolve:
    def test_record_then_resolve_returns_path(self, tmp_path):
        d = _bootstrap_dir(tmp_path)
        tool_paths.record(d, "git", "/usr/bin/git")
        assert tool_paths.resolve(d, "git") == "/usr/bin/git"

    def test_resolve_missing_returns_none(self, tmp_path):
        d = _bootstrap_dir(tmp_path)
        assert tool_paths.resolve(d, "git") is None

    def test_record_idempotent_same_path(self, tmp_path):
        d = _bootstrap_dir(tmp_path)
        tool_paths.record(d, "git", "/usr/bin/git")
        mtime1 = os.path.getmtime(os.path.join(d, "tool_paths.json"))
        tool_paths.record(d, "git", "/usr/bin/git")
        mtime2 = os.path.getmtime(os.path.join(d, "tool_paths.json"))
        # Same path should be a no-op (file untouched).
        assert mtime1 == mtime2

    def test_record_updates_when_path_changes(self, tmp_path):
        d = _bootstrap_dir(tmp_path)
        tool_paths.record(d, "git", "/usr/bin/git")
        tool_paths.record(d, "git", "/opt/git/bin/git")
        assert tool_paths.resolve(d, "git") == "/opt/git/bin/git"

    def test_record_ignores_empty_name_or_path(self, tmp_path):
        d = _bootstrap_dir(tmp_path)
        tool_paths.record(d, "", "/usr/bin/git")
        tool_paths.record(d, "git", "")
        tool_paths.record(d, None, None)
        # File should not even be created.
        assert not os.path.exists(os.path.join(d, "tool_paths.json"))

    def test_all_paths(self, tmp_path):
        d = _bootstrap_dir(tmp_path)
        tool_paths.record(d, "git", "/usr/bin/git")
        tool_paths.record(d, "gh", "/usr/bin/gh")
        assert tool_paths.all_paths(d) == {
            "git": "/usr/bin/git",
            "gh": "/usr/bin/gh",
        }


class TestPersistence:
    def test_state_file_is_valid_json(self, tmp_path):
        d = _bootstrap_dir(tmp_path)
        tool_paths.record(d, "git", "/usr/bin/git")
        with open(os.path.join(d, "tool_paths.json")) as f:
            data = json.load(f)
        assert data["_schema_version"] == 1
        assert data["tools"]["git"]["path"] == "/usr/bin/git"
        assert "recorded_at" in data["tools"]["git"]

    def test_corrupt_file_is_treated_as_empty(self, tmp_path):
        d = _bootstrap_dir(tmp_path)
        with open(os.path.join(d, "tool_paths.json"), "w") as f:
            f.write("not valid json {")
        # Should not raise; should treat as empty.
        assert tool_paths.resolve(d, "git") is None
        assert tool_paths.all_paths(d) == {}
        # And the next record() should overwrite cleanly.
        tool_paths.record(d, "git", "/usr/bin/git")
        assert tool_paths.resolve(d, "git") == "/usr/bin/git"

    def test_atomic_write_no_temp_files_left_behind(self, tmp_path):
        d = _bootstrap_dir(tmp_path)
        tool_paths.record(d, "git", "/usr/bin/git")
        tool_paths.record(d, "gh", "/usr/bin/gh")
        leftovers = [f for f in os.listdir(d) if f.startswith(".tool_paths.")]
        assert leftovers == []


class TestNamingConvention:
    def test_tool_env_var_name_basic(self):
        assert tool_paths.tool_env_var_name("git") == "BOOTSTRAP_BIN_GIT"

    def test_tool_env_var_name_hyphen_to_underscore(self):
        assert tool_paths.tool_env_var_name("github-cli") == "BOOTSTRAP_BIN_GITHUB_CLI"

    def test_tool_env_var_name_already_uppercase(self):
        assert tool_paths.tool_env_var_name("UV") == "BOOTSTRAP_BIN_UV"

    def test_tool_env_var_name_dot_to_underscore(self):
        # Binary filenames can contain dots (e.g. draw.io). A bare dot in
        # the var name produces an invalid shell identifier that fails to
        # export, so it must be sanitized to an underscore.
        assert tool_paths.tool_env_var_name("draw.io") == "BOOTSTRAP_BIN_DRAW_IO"

    def test_tool_env_var_name_is_valid_shell_identifier(self):
        import re

        for raw in ("draw.io", "github-cli", "foo.bar.baz", "a+b", "x y"):
            var = tool_paths.tool_env_var_name(raw)
            assert re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", var), var


class TestExportToolEnvVars:
    def test_noop_when_claude_env_file_unset(self, tmp_path, monkeypatch):
        d = _bootstrap_dir(tmp_path)
        tool_paths.record(d, "git", "/usr/bin/git")
        monkeypatch.delenv("CLAUDE_ENV_FILE", raising=False)
        assert tool_paths.export_tool_env_vars(d) == []

    def test_writes_exports_when_paths_exist(self, tmp_path, monkeypatch):
        d = _bootstrap_dir(tmp_path)
        # Create a real file on disk that the path can resolve to.
        fake_git = tmp_path / "git"
        fake_git.write_text("#!/bin/sh\n")
        tool_paths.record(d, "git", str(fake_git))

        env_file = tmp_path / "env_file"
        env_file.touch()
        monkeypatch.setenv("CLAUDE_ENV_FILE", str(env_file))

        exported = tool_paths.export_tool_env_vars(d)
        assert exported == ["BOOTSTRAP_BIN_GIT"]
        content = env_file.read_text()
        assert "export BOOTSTRAP_BIN_GIT=" in content
        assert str(fake_git) in content

    def test_skips_tools_whose_path_no_longer_exists(self, tmp_path, monkeypatch):
        d = _bootstrap_dir(tmp_path)
        tool_paths.record(d, "ghost", "/nonexistent/path/to/ghost")

        env_file = tmp_path / "env_file"
        env_file.touch()
        monkeypatch.setenv("CLAUDE_ENV_FILE", str(env_file))

        exported = tool_paths.export_tool_env_vars(d)
        assert exported == []
        # Nothing should have been appended.
        assert env_file.read_text() == ""


class TestCanonicalRedirect:
    def test_non_bootstrap_data_dir_redirects_to_canonical(self, tmp_path, monkeypatch):
        """A caller passing a per-plugin data dir should still write to the
        canonical bootstrap data dir, not to the caller's dir."""
        plugin_dir = tmp_path / "some-plugin"
        plugin_dir.mkdir()

        canonical = tmp_path / "fake_canonical" / "bootstrap"
        monkeypatch.setattr(tool_paths, "canonical_data_dir", lambda: str(canonical))

        tool_paths.record(str(plugin_dir), "git", "/usr/bin/git")

        # Plugin dir should remain empty; canonical should hold the file.
        assert "tool_paths.json" not in os.listdir(plugin_dir)
        assert (canonical / "tool_paths.json").exists()

    def test_bootstrap_basename_writes_in_place(self, tmp_path):
        d = tmp_path / "bootstrap"
        d.mkdir()
        tool_paths.record(str(d), "git", "/usr/bin/git")
        assert (d / "tool_paths.json").exists()
