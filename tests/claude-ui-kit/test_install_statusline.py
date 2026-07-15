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


# No windows_only / posix_only markers by design: the emitted command is now a
# single machine-independent string, so every test below runs on every platform.
# A platform-conditional command is the bug this module was fixed to avoid.


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
def ctx(tmp_path, fake_home):
    """FakeCtx with a data_dir containing the synced statusline script.

    The data_dir mirrors the real install layout UNDER HOME
    (~/.claude/plugins/data/<mkt>/claude-ui-kit/) for two reasons: the
    `claude-ui-kit` segment is what _is_ours() keys on to drive the refresh /
    self-heal branch, and living under home is what lets _tilde_path emit the
    portable `~/...` form. A data_dir outside home would silently fall back to
    an absolute command and the portability tests would pass vacuously.
    """
    data_dir = (fake_home / ".claude" / "plugins" / "data"
                / "plugins-kit" / "claude-ui-kit")
    script = data_dir / "scripts" / "statusline.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\necho hi\n")
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    return FakeCtx(data_dir, project_dir)


def _expected_command(ctx):
    """The command install() should emit -- the same on every platform."""
    return install_statusline._build_command(
        Path(ctx.data_dir) / "scripts" / "statusline.sh"
    )


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
        # The stale command is an absolute path, so this is the migration
        # branch; TestLegacyMigration covers the real legacy shapes.
        assert any("migrated machine-specific" in m for m in ctx.logs)


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
    """_build_command emits ONE spelling for every platform: a PATH-resolved
    interpreter plus a `~`-relative script path. There is deliberately no
    platform branch -- an OS-conditional command is what made settings.json
    machine-specific in the first place.
    """

    def test_emits_portable_form_on_every_platform(self, fake_home):
        script = (fake_home / ".claude/plugins/data/plugins-kit/claude-ui-kit"
                  / "scripts/statusline.sh")
        assert install_statusline._build_command(script) == (
            "bash ~/.claude/plugins/data/plugins-kit/claude-ui-kit/scripts/statusline.sh"
        )

    def test_command_leaks_no_machine_specific_value(self, fake_home):
        """The load-bearing invariant. settings.json may be shared across
        machines, so the emitted string must contain no home path, no drive
        letter, and no absolute interpreter -- otherwise every machine rewrites
        the line to its own and they clobber each other forever.
        """
        script = (fake_home / ".claude/plugins/data/plugins-kit/claude-ui-kit"
                  / "scripts/statusline.sh")
        cmd = install_statusline._build_command(script)

        assert str(fake_home).replace("\\", "/") not in cmd
        assert "\\" not in cmd
        assert ":" not in cmd          # no C:/ drive letter
        assert ".exe" not in cmd       # no absolute interpreter
        assert cmd.startswith("bash ~/")

    def test_identical_across_different_homes(self, tmp_path, monkeypatch):
        """Two machines with different home dirs must produce the SAME string.
        This is the whole point; assert it directly rather than by proxy."""
        commands = set()
        for home_name in ("home_windows", "home_macos"):
            home = tmp_path / home_name
            script = (home / ".claude/plugins/data/plugins-kit/claude-ui-kit"
                      / "scripts/statusline.sh")
            monkeypatch.setattr(Path, "home", classmethod(lambda cls, h=home: h))
            commands.add(install_statusline._build_command(script))
        assert len(commands) == 1

    def test_is_idempotent(self, fake_home):
        script = (fake_home / ".claude/plugins/data/plugins-kit/claude-ui-kit"
                  / "scripts/statusline.sh")
        once = install_statusline._build_command(script)
        assert install_statusline._build_command(script) == once
        assert once.count("bash") == 1  # never nests a second interpreter

    def test_falls_back_to_absolute_outside_home(self, fake_home):
        """Not under home -> nothing portable is possible; a working absolute
        command beats a broken relative one."""
        cmd = install_statusline._build_command(
            Path("/opt/x/claude-ui-kit/scripts/statusline.sh"))
        assert cmd == "bash /opt/x/claude-ui-kit/scripts/statusline.sh"


class TestPortabilityDetection:
    def test_portable_form_recognized(self):
        assert install_statusline._is_portable(
            "bash ~/.claude/plugins/data/plugins-kit/claude-ui-kit/scripts/statusline.sh")

    def test_legacy_absolute_forms_are_not_portable(self):
        """Both shapes the old installer wrote -- Windows Git-Bash-wrapped and
        the POSIX bare path -- must read as non-portable so they migrate."""
        assert not install_statusline._is_portable(
            '"C:/Program Files/Git/bin/bash.exe" '
            '"C:/Users/truff/.claude/plugins/data/plugins-kit/claude-ui-kit/scripts/statusline.sh"')
        assert not install_statusline._is_portable(
            "/Users/christina/.claude/plugins/data/plugins-kit/claude-ui-kit/scripts/statusline.sh")


class TestLegacyMigration:
    """The one-time self-heal that ends the churn: a legacy machine-specific
    command for OUR plugin is rewritten to the portable form on the next
    SessionStart, on every machine, with no manual edit.
    """

    LEGACY_WINDOWS = ('"C:/Program Files/Git/bin/bash.exe" '
                      '"C:/Users/truff/.claude/plugins/data/plugins-kit/'
                      'claude-ui-kit/scripts/statusline.sh"')
    LEGACY_POSIX = ("/Users/christina/.claude/plugins/data/plugins-kit/"
                    "claude-ui-kit/scripts/statusline.sh")

    @pytest.mark.parametrize("legacy", [LEGACY_WINDOWS, LEGACY_POSIX])
    def test_legacy_command_is_migrated(self, ctx, fake_home, legacy):
        _user_settings(fake_home).write_text(json.dumps({
            "statusLine": {"type": "command", "command": legacy},
            "model": "opus",
        }))

        install_statusline.install(ctx)

        data = json.loads(_user_settings(fake_home).read_text())
        assert data["statusLine"]["command"] == _expected_command(ctx)
        assert install_statusline._is_portable(data["statusLine"]["command"])
        assert data["model"] == "opus"  # other keys preserved
        assert ctx.failures == []
        assert any("migrated machine-specific" in m for m in ctx.logs)

    def test_already_portable_is_noop(self, ctx, fake_home):
        settings = {"statusLine": {"type": "command", "command": _expected_command(ctx)}}
        _user_settings(fake_home).write_text(json.dumps(settings))

        install_statusline.install(ctx)

        data = json.loads(_user_settings(fake_home).read_text())
        assert data["statusLine"]["command"] == _expected_command(ctx)
        assert ctx.failures == []
        assert any("no-op" in m for m in ctx.ok_logs)

    def test_converged_fleet_leaves_settings_byte_identical(self, ctx, fake_home):
        """The end state that matters: once converged, a SessionStart must not
        rewrite the file at all -- that is what stops it going dirty."""
        _user_settings(fake_home).write_text(json.dumps(
            {"statusLine": {"type": "command", "command": _expected_command(ctx)},
             "model": "opus"}, indent=2))
        before = _user_settings(fake_home).read_bytes()

        install_statusline.install(ctx)

        assert _user_settings(fake_home).read_bytes() == before
