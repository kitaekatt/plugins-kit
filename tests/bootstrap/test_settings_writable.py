"""Tests for bootstrap_lib/settings_writable.py.

Covers the read-only-target guard that keeps a project-scope
`claude plugin install` from dying with EPERM on a Perforce-controlled
settings.json, and the line-ending preservation that keeps the CLI's
whole-file reserialisation from churning a shared source-controlled file.
"""

import os
import stat

import pytest

from bootstrap_lib import settings_writable
from bootstrap_lib.settings_writable import (
    ensure_writable,
    preserve_line_endings,
    settings_path_for_scope,
)


def _make_read_only(path):
    os.chmod(path, stat.S_IREAD)


def _is_read_only(path):
    return not (os.stat(path).st_mode & stat.S_IWRITE)


class TestSettingsPathForScope:
    def test_user_scope_uses_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        assert settings_path_for_scope("user", None) == os.path.join(
            str(tmp_path), ".claude", "settings.json"
        )

    def test_project_scope_uses_project_dir(self, tmp_path):
        assert settings_path_for_scope("project", str(tmp_path)) == os.path.join(
            str(tmp_path), ".claude", "settings.json"
        )

    def test_local_scope_uses_settings_local(self, tmp_path):
        assert settings_path_for_scope("local", str(tmp_path)) == os.path.join(
            str(tmp_path), ".claude", "settings.local.json"
        )

    def test_project_scope_without_project_dir_is_none(self):
        assert settings_path_for_scope("project", None) is None

    def test_unknown_scope_is_none(self, tmp_path):
        assert settings_path_for_scope("bogus", str(tmp_path)) is None


class TestP4WorkspaceDetection:
    """p4 must not be touched at all outside a Perforce workspace."""

    def test_no_marker_is_not_a_workspace(self, tmp_path):
        target = tmp_path / "settings.json"
        target.write_text("{}")
        assert not settings_writable._in_p4_workspace(str(target))

    @pytest.mark.parametrize(
        "marker", [".p4config.txt", ".p4config", ".p4ignore.txt", ".p4ignore"]
    )
    def test_marker_beside_file_is_a_workspace(self, tmp_path, marker):
        (tmp_path / marker).write_text("")
        target = tmp_path / "settings.json"
        target.write_text("{}")
        assert settings_writable._in_p4_workspace(str(target))

    def test_marker_at_workspace_root_is_found_from_below(self, tmp_path):
        (tmp_path / ".p4config.txt").write_text("")
        nested = tmp_path / ".claude" / "deeper"
        nested.mkdir(parents=True)
        target = nested / "settings.json"
        target.write_text("{}")
        assert settings_writable._in_p4_workspace(str(target))

    def test_p4_is_never_invoked_outside_a_workspace(self, tmp_path, monkeypatch):
        """The whole point of the gate: no p4 subprocess in a git checkout."""
        target = tmp_path / "settings.json"
        target.write_text("{}")
        _make_read_only(str(target))

        def explode(*args, **kwargs):
            raise AssertionError("p4 must not be invoked outside a p4 workspace")

        monkeypatch.setattr(settings_writable, "_p4", explode)

        result = ensure_writable(str(target))

        assert result.ok and result.method == "chmod"

    def test_missing_p4_binary_falls_back_to_chmod(self, tmp_path, monkeypatch):
        (tmp_path / ".p4config.txt").write_text("")
        target = tmp_path / "settings.json"
        target.write_text("{}")
        _make_read_only(str(target))
        monkeypatch.setattr(settings_writable.shutil, "which", lambda name: None)

        result = ensure_writable(str(target))

        assert result.ok and result.method == "chmod"


class TestEnsureWritable:
    def test_missing_path_is_ok(self):
        assert ensure_writable(None).ok
        assert ensure_writable(None).method == "absent"

    def test_nonexistent_file_is_ok(self, tmp_path):
        result = ensure_writable(str(tmp_path / "nope.json"))
        assert result.ok and result.method == "absent"

    def test_already_writable_is_left_alone(self, tmp_path):
        target = tmp_path / "settings.json"
        target.write_text("{}")
        result = ensure_writable(str(target))
        assert result.ok and result.method == "already-writable"

    def test_read_only_untracked_file_falls_back_to_chmod(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings_writable, "_p4_tracked", lambda path: False)
        target = tmp_path / "settings.json"
        target.write_text("{}")
        _make_read_only(str(target))

        result = ensure_writable(str(target))

        assert result.ok and result.method == "chmod"
        assert not _is_read_only(str(target))

    def test_p4_edit_is_preferred_over_chmod(self, tmp_path, monkeypatch):
        """A tracked file goes through `p4 edit` so the change stays in a CL.

        A bare chmod would leave it writable-but-not-opened, and the next
        `p4 sync` would then refuse to clobber it.
        """
        target = tmp_path / "settings.json"
        target.write_text("{}")
        _make_read_only(str(target))
        calls = []

        def fake_edit(path):
            calls.append(path)
            os.chmod(path, stat.S_IWRITE)
            return True

        monkeypatch.setattr(settings_writable, "_p4_tracked", lambda path: True)
        monkeypatch.setattr(settings_writable, "_p4_edit", fake_edit)

        result = ensure_writable(str(target))

        assert result.ok and result.method == "p4-edit"
        assert calls == [str(target)]

    def test_failed_p4_edit_falls_back_to_chmod(self, tmp_path, monkeypatch):
        target = tmp_path / "settings.json"
        target.write_text("{}")
        _make_read_only(str(target))
        monkeypatch.setattr(settings_writable, "_p4_tracked", lambda path: True)
        monkeypatch.setattr(settings_writable, "_p4_edit", lambda path: False)

        result = ensure_writable(str(target))

        assert result.ok and result.method == "chmod"
        assert not _is_read_only(str(target))

    def test_unfixable_read_only_reports_failure(self, tmp_path, monkeypatch):
        target = tmp_path / "settings.json"
        target.write_text("{}")
        _make_read_only(str(target))
        monkeypatch.setattr(settings_writable, "_p4_tracked", lambda path: False)

        def boom(path, mode):
            raise OSError("denied")

        monkeypatch.setattr(settings_writable.os, "chmod", boom)

        result = ensure_writable(str(target))

        assert not result.ok and result.method == "failed"


class TestPreserveLineEndings:
    def test_crlf_file_rewritten_as_lf_is_restored(self, tmp_path):
        target = tmp_path / "settings.json"
        target.write_bytes(b'{\r\n  "a": 1\r\n}\r\n')

        with preserve_line_endings(str(target)):
            target.write_bytes(b'{\n  "a": 1,\n  "b": 2\n}\n')

        data = target.read_bytes()
        assert b"\r\n" in data
        assert data.count(b"\n") == data.count(b"\r\n")
        assert b'"b": 2' in data  # the semantic change survives

    def test_lf_file_stays_lf(self, tmp_path):
        target = tmp_path / "settings.json"
        target.write_bytes(b'{\n  "a": 1\n}\n')

        with preserve_line_endings(str(target)):
            target.write_bytes(b'{\n  "a": 2\n}\n')

        assert b"\r\n" not in target.read_bytes()

    def test_missing_file_is_a_noop(self, tmp_path):
        target = tmp_path / "settings.json"
        with preserve_line_endings(str(target)):
            target.write_bytes(b'{\n}\n')
        assert target.read_bytes() == b'{\n}\n'

    def test_body_exception_propagates(self, tmp_path):
        target = tmp_path / "settings.json"
        target.write_bytes(b'{\r\n}\r\n')
        with pytest.raises(ValueError):
            with preserve_line_endings(str(target)):
                raise ValueError("boom")
