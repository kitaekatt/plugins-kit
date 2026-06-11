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
import os
from pathlib import Path

import pytest

import install_statusline


WINDOWS = os.name == "nt"
windows_only = pytest.mark.skipif(not WINDOWS, reason="Windows-specific behavior")
posix_only = pytest.mark.skipif(WINDOWS, reason="POSIX-specific behavior")


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
    """FakeCtx with a data_dir containing the synced statusline script.

    The data_dir includes a `claude-ui-kit` path segment to mirror the real
    install layout (~/.claude/plugins/data/<mkt>/claude-ui-kit/), so that
    _is_ours() recognizes commands pointing into it -- which is what drives the
    refresh / self-heal branch.
    """
    data_dir = tmp_path / "data" / "claude-ui-kit"
    script = data_dir / "scripts" / "statusline.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\necho hi\n")
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    return FakeCtx(data_dir, project_dir)


def _expected_command(ctx):
    """The command install() should emit -- platform-aware.

    Mirrors install_statusline._build_command: bare posix path off Windows,
    Git-Bash-wrapped on Windows.
    """
    return install_statusline._build_command(
        Path(ctx.data_dir) / "scripts" / "statusline.sh"
    )


def _bare_command(ctx):
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
        # On posix this is a plain path refresh; on Windows a stale bare path is
        # also un-wrapped, so it takes the remediation branch. Either way the
        # command is rewritten to expected.
        assert any(("refreshed path" in m) or ("remediated broken" in m) for m in ctx.logs)


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


class TestCommandBuilder:
    """_build_command is the platform-conditional core of both the installer
    fix and the self-heal: posix -> bare path, Windows -> Git-Bash-wrapped."""

    @posix_only
    def test_posix_emits_bare_path(self):
        cmd = install_statusline._build_command(Path("/x/claude-ui-kit/scripts/statusline.sh"))
        assert cmd == "/x/claude-ui-kit/scripts/statusline.sh"
        assert "bash" not in cmd

    @windows_only
    def test_windows_wraps_with_git_bash(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_GIT_BASH_PATH", r"C:\Program Files\Git\bin\bash.exe")
        cmd = install_statusline._build_command(Path("C:/x/claude-ui-kit/scripts/statusline.sh"))
        assert cmd == '"C:/Program Files/Git/bin/bash.exe" "C:/x/claude-ui-kit/scripts/statusline.sh"'

    @windows_only
    def test_windows_is_idempotent_not_double_wrapped(self, monkeypatch):
        """Building twice yields the same string; building from an already-built
        command's script path never nests a second interpreter."""
        monkeypatch.setenv("CLAUDE_CODE_GIT_BASH_PATH", r"C:\Program Files\Git\bin\bash.exe")
        script = Path("C:/x/claude-ui-kit/scripts/statusline.sh")
        once = install_statusline._build_command(script)
        twice = install_statusline._build_command(script)
        assert once == twice
        assert once.count("bash.exe") == 1


class TestBrokenDetection:
    @windows_only
    def test_bare_sh_path_is_broken(self):
        assert install_statusline._is_broken_command("C:/x/claude-ui-kit/scripts/statusline.sh")
        assert install_statusline._is_broken_command('"C:/x/claude-ui-kit/scripts/statusline.sh"')

    @windows_only
    def test_wrapped_command_is_not_broken(self):
        wrapped = '"C:/Program Files/Git/bin/bash.exe" "C:/x/claude-ui-kit/scripts/statusline.sh"'
        assert not install_statusline._is_broken_command(wrapped)

    @posix_only
    def test_never_broken_on_posix(self):
        assert not install_statusline._is_broken_command("/x/claude-ui-kit/scripts/statusline.sh")


@windows_only
class TestWindowsRemediation:
    """The detect-and-remediate self-heal: a previously installed bare-path
    (broken) command for OUR plugin is rewritten to the wrapped form, and an
    already-wrapped command is a no-op."""

    def test_broken_bare_path_is_remediated(self, ctx, fake_home, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_GIT_BASH_PATH", r"C:\Program Files\Git\bin\bash.exe")
        broken = {
            "statusLine": {"type": "command", "command": _bare_command(ctx)},
            "model": "opus",
        }
        _user_settings(fake_home).write_text(json.dumps(broken))

        install_statusline.install(ctx)

        data = json.loads(_user_settings(fake_home).read_text())
        assert data["statusLine"]["command"] == _expected_command(ctx)
        assert "bash.exe" in data["statusLine"]["command"]
        assert data["model"] == "opus"  # other keys preserved
        assert ctx.failures == []
        assert any("remediated broken" in m for m in ctx.logs)

    def test_already_wrapped_is_noop(self, ctx, fake_home, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_GIT_BASH_PATH", r"C:\Program Files\Git\bin\bash.exe")
        wrapped = {"statusLine": {"type": "command", "command": _expected_command(ctx)}}
        _user_settings(fake_home).write_text(json.dumps(wrapped))

        install_statusline.install(ctx)

        data = json.loads(_user_settings(fake_home).read_text())
        assert data["statusLine"]["command"] == _expected_command(ctx)
        assert data["statusLine"]["command"].count("bash.exe") == 1  # not double-wrapped
        assert ctx.failures == []
        assert any("no-op" in m for m in ctx.ok_logs)
