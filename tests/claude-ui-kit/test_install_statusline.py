"""Tests for install_statusline.py -- the bootstrap custom script that installs
claude-ui-kit's statusLine into settings.json.

Mirrors the engine's _ScriptContext contract (data_dir, project_dir, log,
log_ok, add_failure) with a local fake; install() is called directly.

The malformed-settings case is the load-bearing regression: _load_json returns
None for unparseable JSON, and the script previously replaced the whole file
with just {"statusLine": ...}, destroying every other user setting -- running
unattended at SessionStart.
"""

import json
from pathlib import Path

import pytest

import install_statusline


class FakeCtx:
    """Minimal stand-in for the bootstrap engine's _ScriptContext."""

    def __init__(self, data_dir, project_dir=None):
        self.data_dir = str(data_dir)
        self.project_dir = str(project_dir) if project_dir is not None else None
        self.failures = []
        self.logs = []
        self.ok_logs = []

    def add_failure(self, failure_type, **kwargs):
        failure = {"type": failure_type}
        failure.update(kwargs)
        self.failures.append(failure)

    def log(self, message):
        self.logs.append(message)

    def log_ok(self, message):
        self.ok_logs.append(message)


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Redirect Path.home() to an isolated directory."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


@pytest.fixture
def ctx(tmp_path):
    """FakeCtx with a data_dir containing the synced statusline script."""
    data_dir = tmp_path / "data"
    script = data_dir / "scripts" / "statusline.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\necho hi\n")
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    return FakeCtx(data_dir, project_dir)


def _expected_command(ctx):
    return str(Path(ctx.data_dir) / "scripts" / "statusline.sh").replace("\\", "/")


def _user_settings(fake_home):
    return fake_home / ".claude" / "settings.json"


class TestFreshInstall:
    def test_installs_into_user_settings_preserving_other_keys(self, ctx, fake_home):
        _user_settings(fake_home).write_text(json.dumps({"model": "opus"}))

        install_statusline.install(ctx)

        data = json.loads(_user_settings(fake_home).read_text())
        assert data["statusLine"] == {"type": "command", "command": _expected_command(ctx)}
        assert data["model"] == "opus"  # existing settings preserved
        assert ctx.failures == []
        assert any("installed to" in m for m in ctx.logs)


class TestRefresh:
    def test_refreshes_our_stale_path_in_place(self, ctx, fake_home):
        stale = {"statusLine": {"type": "command", "command": "/old/claude-ui-kit/scripts/statusline.sh"},
                 "model": "opus"}
        _user_settings(fake_home).write_text(json.dumps(stale))

        install_statusline.install(ctx)

        data = json.loads(_user_settings(fake_home).read_text())
        assert data["statusLine"]["command"] == _expected_command(ctx)
        assert data["model"] == "opus"
        assert ctx.failures == []
        assert any("refreshed path" in m for m in ctx.logs)


class TestConflict:
    def test_foreign_statusline_left_alone_and_failure_surfaced(self, ctx, fake_home):
        custom = {"statusLine": {"type": "command", "command": "/usr/local/bin/my-statusline"}}
        _user_settings(fake_home).write_text(json.dumps(custom))

        install_statusline.install(ctx)

        assert json.loads(_user_settings(fake_home).read_text()) == custom  # untouched
        assert len(ctx.failures) == 1
        assert ctx.failures[0]["type"] == "statusline_conflict"


class TestCustomizedFlag:
    def test_flag_skips_all_work(self, ctx, fake_home):
        (Path(ctx.data_dir) / install_statusline.CUSTOMIZED_FLAG).write_text("")

        install_statusline.install(ctx)

        assert not _user_settings(fake_home).exists()  # nothing written
        assert ctx.failures == []
        assert any("user customized" in m for m in ctx.ok_logs)


class TestMalformedSettings:
    def test_refuses_to_overwrite_unparseable_settings(self, ctx, fake_home):
        """A malformed ~/.claude/settings.json must NOT be replaced with just
        {"statusLine": ...} -- the script refuses and surfaces a fix-all failure."""
        malformed = '{"model": "opus", broken'
        _user_settings(fake_home).write_text(malformed)

        install_statusline.install(ctx)

        assert _user_settings(fake_home).read_text() == malformed  # byte-for-byte untouched
        assert len(ctx.failures) == 1
        assert ctx.failures[0]["type"] == "statusline_settings_unparseable"
        assert "will not modify" in ctx.failures[0]["user_msg"]
