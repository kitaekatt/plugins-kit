"""Tests for bootstrap_lib/tool_paths.py."""

import json
import os
from unittest.mock import patch

import pytest
from pathlib import Path

from bootstrap_lib import session_env, tool_paths


# record() stores absolute paths; on Windows a POSIX-looking literal gains a
# drive letter, so expectations are built the same way.
_GIT = str(Path("/usr/bin/git").absolute())
_GH = str(Path("/usr/bin/gh").absolute())
_OPT_GIT = str(Path("/opt/git/bin/git").absolute())


@pytest.fixture(autouse=True)
def _clean_session_env_buffer():
    """session_env buffers across calls, so no test may inherit another's."""
    session_env.reset()
    yield
    session_env.reset()


def _bootstrap_dir(tmp_path):
    """Return an isolated data dir for tool_paths state.

    An explicit data_dir is used exactly as given (only ``None`` resolves to
    the canonical production location), so any temp dir is fully isolated.
    """
    d = tmp_path / "bootstrap"
    d.mkdir()
    return str(d)


class TestRecordAndResolve:
    def test_record_then_resolve_returns_path(self, tmp_path):
        d = _bootstrap_dir(tmp_path)
        tool_paths.record(d, "git", _GIT)
        assert tool_paths.resolve(d, "git") == _GIT

    def test_resolve_missing_returns_none(self, tmp_path):
        d = _bootstrap_dir(tmp_path)
        assert tool_paths.resolve(d, "git") is None

    def test_record_idempotent_same_path(self, tmp_path):
        d = _bootstrap_dir(tmp_path)
        tool_paths.record(d, "git", _GIT)
        mtime1 = os.path.getmtime(os.path.join(d, "tool_paths.json"))
        tool_paths.record(d, "git", _GIT)
        mtime2 = os.path.getmtime(os.path.join(d, "tool_paths.json"))
        # Same path should be a no-op (file untouched).
        assert mtime1 == mtime2

    def test_record_updates_when_path_changes(self, tmp_path):
        d = _bootstrap_dir(tmp_path)
        tool_paths.record(d, "git", _GIT)
        tool_paths.record(d, "git", _OPT_GIT)
        assert tool_paths.resolve(d, "git") == _OPT_GIT

    def test_record_stores_relative_path_as_absolute(self, tmp_path, monkeypatch):
        d = _bootstrap_dir(tmp_path)
        monkeypatch.chdir(tmp_path)

        tool_paths.record(d, "git", "bin/git")

        assert tool_paths.resolve(d, "git") == str((tmp_path / "bin/git").absolute())

    def test_record_ignores_empty_name_or_path(self, tmp_path):
        d = _bootstrap_dir(tmp_path)
        tool_paths.record(d, "", _GIT)
        tool_paths.record(d, "git", "")
        tool_paths.record(d, None, None)
        # File should not even be created.
        assert not os.path.exists(os.path.join(d, "tool_paths.json"))

    def test_all_paths(self, tmp_path):
        d = _bootstrap_dir(tmp_path)
        tool_paths.record(d, "git", _GIT)
        tool_paths.record(d, "gh", _GH)
        assert tool_paths.all_paths(d) == {
            "git": _GIT,
            "gh": _GH,
        }


class TestPersistence:
    def test_state_file_is_valid_json(self, tmp_path):
        d = _bootstrap_dir(tmp_path)
        tool_paths.record(d, "git", _GIT)
        with open(os.path.join(d, "tool_paths.json")) as f:
            data = json.load(f)
        assert data["_schema_version"] == 1
        assert data["tools"]["git"]["path"] == _GIT
        assert "recorded_at" in data["tools"]["git"]

    def test_corrupt_file_is_treated_as_empty(self, tmp_path):
        d = _bootstrap_dir(tmp_path)
        with open(os.path.join(d, "tool_paths.json"), "w") as f:
            f.write("not valid json {")
        # Should not raise; should treat as empty.
        assert tool_paths.resolve(d, "git") is None
        assert tool_paths.all_paths(d) == {}
        # And the next record() should overwrite cleanly.
        tool_paths.record(d, "git", _GIT)
        assert tool_paths.resolve(d, "git") == _GIT

    def test_atomic_write_no_temp_files_left_behind(self, tmp_path):
        d = _bootstrap_dir(tmp_path)
        tool_paths.record(d, "git", _GIT)
        tool_paths.record(d, "gh", _GH)
        leftovers = [f for f in os.listdir(d) if f.startswith(".tool_paths.")]
        assert leftovers == []


class TestDataDirContract:
    """data_dir=None -> canonical location; explicit dir -> exactly that dir (B15).

    The old basename-sniffing redirect ("anything not named bootstrap goes to
    the canonical production file") is gone — a test passing a generic tmp dir
    must never pollute the user's real tool_paths.json.
    """

    def test_none_resolves_to_canonical(self, tmp_path, monkeypatch):
        canonical = tmp_path / "fake_canonical" / "bootstrap"
        monkeypatch.setattr(tool_paths, "canonical_data_dir", lambda: str(canonical))

        tool_paths.record(None, "git", _GIT)

        assert (canonical / "tool_paths.json").exists()
        assert tool_paths.resolve(None, "git") == _GIT

    def test_explicit_dir_writes_in_place_regardless_of_basename(self, tmp_path, monkeypatch):
        canonical = tmp_path / "fake_canonical" / "bootstrap"
        monkeypatch.setattr(tool_paths, "canonical_data_dir", lambda: str(canonical))

        plugin_dir = tmp_path / "some-plugin"
        plugin_dir.mkdir()
        tool_paths.record(str(plugin_dir), "git", _GIT)

        # The explicit dir holds the file; canonical is untouched.
        assert (plugin_dir / "tool_paths.json").exists()
        assert not canonical.exists()

    def test_bootstrap_basename_writes_in_place(self, tmp_path):
        d = tmp_path / "bootstrap"
        d.mkdir()
        tool_paths.record(str(d), "git", _GIT)
        assert (d / "tool_paths.json").exists()
