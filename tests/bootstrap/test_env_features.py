"""Tests for the five declarative env.json feature sections (E1 step 4).

Covers bootstrap_lib/env_features.py and the engine's _ENV_PHASES handlers
(bootstrap-env-refactor spec 3.1/4.3): symlinks, shell_rc (ensure +
forbid), macos_defaults, macos_hotkeys, login_items. Per feature: the
check/fix/re-check pipeline through the real engine env pass (isolated
HOME), os/hosts filters, idempotency (second pass = no writes), and the
macOS-only skip on other platforms. macOS system surfaces (defaults,
symbolic hotkeys, System Events) are faked at the env_features subprocess
seam so checks stay side-effect free on the test machine.

Ends with the full-engine env.json e2e (subprocess --console, isolated
HOME) that step 3's audit carried forward.
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import bootstrap_lib.env_features as env_features
from bootstrap_lib.engine import _process_env_pass
from bootstrap_lib.env_features import (
    check_shell_ensure,
    check_shell_forbid,
    check_symlink,
    defaults_expected_string,
    expand_env_path,
    fix_symlink,
    hotkey_state,
    render_shell_content,
)
from bootstrap_lib.env_manifest import ENV_STATE_STAMP, read_env_state

ENGINE_VERSION = "0.34.0"


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Point HOME at a tmp dir so rc files and env.json layers are isolated."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


def _write_json(path: Path, content: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content))


def _manifest(os_: str = "ubuntu", **sections) -> dict:
    return {"machines": {"testhost": {"os": os_}}, **sections}


class _Pass:
    def __init__(self, failures, action_entries, ok_entries):
        self.failures = failures
        self.action_entries = action_entries
        self.ok_entries = ok_entries


@pytest.fixture
def run_env_pass(isolated_home, tmp_path):
    """Run the engine env pass against the isolated home. Returns _Pass.

    Deletes the env stamp before each invocation so repeated runs exercise
    the handlers (feature idempotency) rather than the gate skip -- the
    gate matrix itself is test_env_manifest.py's subject.
    """
    data_dir = tmp_path / "data"
    plugin_root = tmp_path / "plugin"
    data_dir.mkdir(exist_ok=True)
    plugin_root.mkdir(exist_ok=True)

    def _run(current_os="ubuntu", hostname="testhost"):
        stamp = data_dir / ENV_STATE_STAMP
        if stamp.exists():
            stamp.unlink()
        action_entries: list = []
        ok_entries: list = []
        failures = _process_env_pass(
            None, current_os, str(data_dir), str(plugin_root),
            action_entries, ok_entries,
            engine_version=ENGINE_VERSION, hostname=hostname,
        )
        return _Pass(failures, action_entries, ok_entries)

    _run.data_dir = data_dir
    return _run


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------

class TestExpandEnvPath:
    def test_tilde_expands(self, isolated_home):
        assert expand_env_path("~/x.txt") == str(isolated_home / "x.txt")

    def test_variable_expands(self, isolated_home, monkeypatch):
        monkeypatch.setenv("DEVROOT", str(isolated_home / "Dev"))
        assert expand_env_path("$DEVROOT/update.sh") == str(
            isolated_home / "Dev" / "update.sh")

    def test_unresolved_variable_is_an_error(self, monkeypatch):
        monkeypatch.delenv("NO_SUCH_VAR_XYZ", raising=False)
        with pytest.raises(ValueError, match="unresolved variable"):
            expand_env_path("$NO_SUCH_VAR_XYZ/f")


class TestShellRendering:
    def test_shell_name_renders_per_rc_file(self):
        content = 'eval "$(starship init SHELL_NAME)"'
        assert "starship init bash" in render_shell_content(content, "/h/.bashrc")
        assert "starship init zsh" in render_shell_content(content, "/h/.zshrc")


class TestDefaultsExpectedString:
    def test_type_mapping(self):
        assert defaults_expected_string(True) == "1"
        assert defaults_expected_string(False) == "0"
        assert defaults_expected_string(25) == "25"
        assert defaults_expected_string("abc") == "abc"
        assert defaults_expected_string([1]) is None
        assert defaults_expected_string(None) is None
        assert defaults_expected_string(1.5) is None


# ---------------------------------------------------------------------------
# symlinks
# ---------------------------------------------------------------------------

class TestSymlinks:
    def _entry(self, home, **overrides):
        source = home / "src.toml"
        target = home / ".config" / "starship.toml"
        entry = {
            "name": "starship-config",
            "source": str(source),
            "target": str(target),
        }
        entry.update(overrides)
        return source, target, entry

    def test_creates_missing_symlink(self, isolated_home, run_env_pass):
        source, target, entry = self._entry(isolated_home)
        source.write_text("data")
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(symlinks=[entry]))

        result = run_env_pass()

        assert result.failures == []
        assert target.is_symlink()
        assert os.path.realpath(target) == os.path.realpath(source)
        assert any("symlink starship-config: linked" in e
                   for e in result.action_entries)

    def test_correct_symlink_is_idempotent(self, isolated_home, run_env_pass):
        source, target, entry = self._entry(isolated_home)
        source.write_text("data")
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(symlinks=[entry]))
        assert run_env_pass().failures == []
        mtime = os.lstat(target).st_mtime_ns

        second = run_env_pass()

        assert second.failures == []
        assert second.action_entries == []
        assert any("symlink starship-config: ok" in e for e in second.ok_entries)
        assert os.lstat(target).st_mtime_ns == mtime

    def test_wrong_symlink_is_relinked(self, isolated_home, run_env_pass):
        source, target, entry = self._entry(isolated_home)
        source.write_text("data")
        other = isolated_home / "other.toml"
        other.write_text("other")
        target.parent.mkdir(parents=True)
        target.symlink_to(other)
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(symlinks=[entry]))

        result = run_env_pass()

        assert result.failures == []
        assert os.path.realpath(target) == os.path.realpath(source)

    def test_real_file_backed_up_when_backup_true(
        self, isolated_home, run_env_pass
    ):
        source, target, entry = self._entry(isolated_home, backup=True)
        source.write_text("tracked")
        target.parent.mkdir(parents=True)
        target.write_text("precious local edits")
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(symlinks=[entry]))

        result = run_env_pass()

        assert result.failures == []
        assert target.is_symlink()
        backups = list(target.parent.glob("starship.toml.backup_*"))
        assert len(backups) == 1
        assert backups[0].read_text() == "precious local edits"
        assert any("backed up" in e for e in result.action_entries)

    def test_real_file_replaced_without_backup(
        self, isolated_home, run_env_pass
    ):
        source, target, entry = self._entry(isolated_home)
        source.write_text("tracked")
        target.parent.mkdir(parents=True)
        target.write_text("stale")
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(symlinks=[entry]))

        result = run_env_pass()

        assert result.failures == []
        assert target.is_symlink()
        assert list(target.parent.glob("*.backup_*")) == []

    def test_directory_target_is_a_failure(self, isolated_home, run_env_pass):
        source, target, entry = self._entry(isolated_home)
        source.write_text("data")
        target.mkdir(parents=True)
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(symlinks=[entry]))

        result = run_env_pass()

        assert len(result.failures) == 1
        assert "directory" in result.failures[0]["message"]
        assert result.failures[0]["type"] == "env_symlink"
        assert target.is_dir() and not target.is_symlink()

    def test_missing_source_is_a_failure(self, isolated_home, run_env_pass):
        _source, target, entry = self._entry(isolated_home)
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(symlinks=[entry]))

        result = run_env_pass()

        assert len(result.failures) == 1
        assert "source does not exist" in result.failures[0]["message"]
        assert not target.exists()

    def test_unresolved_variable_is_a_failure(
        self, isolated_home, run_env_pass, monkeypatch
    ):
        monkeypatch.delenv("DEVROOT", raising=False)
        entry = {"name": "update-shortcut",
                 "source": "$DEVROOT/env-config/bin/update.sh",
                 "target": "$DEVROOT/update.sh"}
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(symlinks=[entry]))

        result = run_env_pass()

        assert len(result.failures) == 1
        assert "unresolved variable" in result.failures[0]["message"]

    def test_os_filter_skips_entry(self, isolated_home, run_env_pass):
        source, target, entry = self._entry(isolated_home)
        source.write_text("data")
        entry["os"] = ["windows"]
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(symlinks=[entry]))

        result = run_env_pass(current_os="ubuntu")

        assert result.failures == []
        assert not target.exists()
        assert any("skipped (os/hosts filter)" in e for e in result.ok_entries)

    def test_invalid_entry_is_a_failure(self, isolated_home, run_env_pass):
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(symlinks=[{"name": "broken"}]))
        result = run_env_pass()
        assert len(result.failures) == 1
        assert "invalid symlinks entry" in result.failures[0]["message"]
        assert result.failures[0]["name"] == "broken"

    def test_unnamed_invalid_entry_uses_placeholder(
        self, isolated_home, run_env_pass
    ):
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(symlinks=[{"source": "~/x"}]))
        result = run_env_pass()
        assert len(result.failures) == 1
        assert result.failures[0]["name"] == "(unnamed)"

    def test_devroot_expansion_via_env_var(
        self, isolated_home, run_env_pass, monkeypatch
    ):
        devroot = isolated_home / "Dev"
        (devroot / "env-config" / "bin").mkdir(parents=True)
        script = devroot / "env-config" / "bin" / "update.sh"
        script.write_text("#!/bin/sh\n")
        monkeypatch.setenv("DEVROOT", str(devroot))
        entry = {"name": "update-shortcut",
                 "source": "$DEVROOT/env-config/bin/update.sh",
                 "target": "$DEVROOT/update.sh"}
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(symlinks=[entry]))

        result = run_env_pass()

        assert result.failures == []
        assert (devroot / "update.sh").is_symlink()


class TestCheckSymlinkUnit:
    def test_dangling_link_to_missing_source_fails(self, tmp_path):
        source = tmp_path / "missing"
        target = tmp_path / "link"
        target.symlink_to(source)
        result = check_symlink(str(source), str(target))
        assert not result.passed
        assert "source does not exist" in result.message

    def test_fix_refuses_directory(self, tmp_path):
        source = tmp_path / "src"
        source.write_text("x")
        target = tmp_path / "d"
        target.mkdir()
        res = fix_symlink(str(source), str(target), backup=True)
        assert not res.ok
        assert "directory" in res.message

    def test_fix_refuses_source_equal_target(self, tmp_path):
        """source == target must never destroy the user's file (R2)."""
        path = tmp_path / "f.toml"
        path.write_text("precious")
        res = fix_symlink(str(path), str(path), backup=False)
        assert not res.ok
        assert "same path" in res.message
        assert path.is_file() and not path.is_symlink()
        assert path.read_text() == "precious"

    def test_fix_refuses_directory_symlink_alias(self, tmp_path):
        """Textually distinct paths that are the same file (target reaches
        the source through a symlinked ancestor dir) must never destroy the
        source -- the realpath guard, not just the abspath one."""
        real = tmp_path / "real"
        real.mkdir()
        source = real / "f.toml"
        source.write_text("precious")
        alias = tmp_path / "alias"
        alias.symlink_to(real)
        target = alias / "f.toml"

        res = fix_symlink(str(source), str(target), backup=False)

        assert not res.ok
        assert "resolve to the same file" in res.message
        assert source.is_file() and not source.is_symlink()
        assert source.read_text() == "precious"

    def test_fix_creates_link_under_symlinked_ancestor_to_other_file(
        self, tmp_path
    ):
        """The realpath guard must not refuse the normal create case: a
        missing target under a symlinked ancestor pointing at a DIFFERENT
        file still gets linked."""
        real = tmp_path / "real"
        real.mkdir()
        source = tmp_path / "src.toml"
        source.write_text("x")
        alias = tmp_path / "alias"
        alias.symlink_to(real)
        target = alias / "link.toml"

        res = fix_symlink(str(source), str(target), backup=False)

        assert res.ok
        assert target.is_symlink()
        assert check_symlink(str(source), str(target)).passed


class _WinPrivilegeError(OSError):
    """OSError double carrying WinError 1314 on any test platform.

    ``winerror`` is a real attribute only on Windows Python; a subclass
    setting it explicitly exercises fix_symlink's ``getattr`` detection
    identically everywhere.
    """

    def __init__(self, msg="[WinError 1314] A required privilege is not held "
                           "by the client"):
        super().__init__(msg)
        self.winerror = 1314


class TestSymlinkNeedsElevation:
    """WinError 1314 (unelevated Windows symlink creation) routes into the
    elevated-deferral mechanism instead of surfacing as a raw failure."""

    def test_fix_symlink_reports_needs_elevation_on_1314(
        self, tmp_path, monkeypatch
    ):
        source = tmp_path / "src.toml"
        source.write_text("x")
        target = tmp_path / "link.toml"
        monkeypatch.setattr(
            env_features.os, "symlink",
            lambda s, t: (_ for _ in ()).throw(_WinPrivilegeError()))

        res = fix_symlink(str(source), str(target), backup=False)

        assert not res.ok
        assert res.needs_elevation is True
        assert "1314" in res.message

    def test_other_oserror_is_not_needs_elevation(self, tmp_path, monkeypatch):
        source = tmp_path / "src.toml"
        source.write_text("x")
        target = tmp_path / "link.toml"
        monkeypatch.setattr(
            env_features.os, "symlink",
            lambda s, t: (_ for _ in ()).throw(OSError("disk full")))

        res = fix_symlink(str(source), str(target), backup=False)

        assert not res.ok
        assert res.needs_elevation is False

    def test_engine_defers_1314_into_elevation_queue(
        self, isolated_home, run_env_pass, monkeypatch
    ):
        """The env pass converts the needs_elevation fix result into a
        persistent env_symlink failure carrying the standard
        {method: "command"} descriptor, so the pass's elevation-queue
        harvest lands the creation in the remediation script."""
        from bootstrap_lib.elevation import queue_from_failures

        source = isolated_home / "src.toml"
        source.write_text("x")
        target = isolated_home / ".config" / "starship.toml"
        entry = {"name": "starship-config", "source": str(source),
                 "target": str(target)}
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(os_="windows", symlinks=[entry]))
        monkeypatch.setattr(
            env_features.os, "symlink",
            lambda s, t: (_ for _ in ()).throw(_WinPrivilegeError()))

        result = run_env_pass(current_os="windows")

        assert len(result.failures) == 1
        failure = result.failures[0]
        assert failure["type"] == "env_symlink"
        assert failure["persist_across_sessions"] is True
        expected_cmd = (
            f"MSYS=winsymlinks:nativestrict ln -sfn '{source}' '{target}'"
        )
        assert failure["elevation"] == {
            "method": "command", "command": expected_cmd, "os": "windows"}
        assert "Developer Mode" in failure["agent_msg"]
        assert any("needs elevation" in a for a in result.action_entries)
        # The pass-level harvest picks the command up like any deferred op.
        queue = queue_from_failures(result.failures, "windows")
        assert queue.commands == [expected_cmd]

    def test_non_1314_failure_stays_a_raw_failure(
        self, isolated_home, run_env_pass, monkeypatch
    ):
        source = isolated_home / "src.toml"
        source.write_text("x")
        target = isolated_home / ".config" / "starship.toml"
        entry = {"name": "starship-config", "source": str(source),
                 "target": str(target)}
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(symlinks=[entry]))
        monkeypatch.setattr(
            env_features.os, "symlink",
            lambda s, t: (_ for _ in ()).throw(OSError("disk full")))

        result = run_env_pass()

        assert len(result.failures) == 1
        assert "elevation" not in result.failures[0]
        assert any("FAILED" in a for a in result.action_entries)


class TestSymlinkSourceEqualsTarget:
    def test_entry_fails_and_preserves_file(self, isolated_home, run_env_pass):
        path = isolated_home / "starship.toml"
        path.write_text("precious")
        entry = {"name": "self-link", "source": str(path),
                 "target": str(path)}
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(symlinks=[entry]))

        result = run_env_pass()

        assert len(result.failures) == 1
        assert "same path" in result.failures[0]["message"]
        assert path.is_file() and not path.is_symlink()
        assert path.read_text() == "precious"


# ---------------------------------------------------------------------------
# shell_rc
# ---------------------------------------------------------------------------

STARSHIP = {"name": "starship-init",
            "content": 'eval "$(starship init SHELL_NAME)"'}
NO_TERM = {"name": "no-term-override",
           "forbid": "^\\s*(export\\s+)?TERM="}


class TestShellRcEnsure:
    def test_appends_to_existing_bashrc(self, isolated_home, run_env_pass):
        bashrc = isolated_home / ".bashrc"
        bashrc.write_text("# existing\n")
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(shell_rc=[STARSHIP]))

        result = run_env_pass()

        assert result.failures == []
        assert 'eval "$(starship init bash)"' in bashrc.read_text()
        assert "# existing" in bashrc.read_text()
        assert any("shell_rc starship-init: appended to .bashrc" in e
                   for e in result.action_entries)

    def test_renders_per_shell_in_both_rc_files(
        self, isolated_home, run_env_pass
    ):
        (isolated_home / ".bashrc").write_text("")
        (isolated_home / ".zshrc").write_text("")
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(shell_rc=[STARSHIP]))

        result = run_env_pass()

        assert result.failures == []
        assert "starship init bash" in (isolated_home / ".bashrc").read_text()
        assert "starship init zsh" in (isolated_home / ".zshrc").read_text()

    def test_fresh_machine_creates_platform_default_rc_macos(
        self, isolated_home, run_env_pass
    ):
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(os_="macos", shell_rc=[STARSHIP]))

        result = run_env_pass(current_os="macos")

        assert result.failures == []
        zshrc = isolated_home / ".zshrc"
        assert zshrc.exists()
        assert "starship init zsh" in zshrc.read_text()
        assert not (isolated_home / ".bashrc").exists()
        assert any("created .zshrc" in e for e in result.action_entries)

    def test_fresh_machine_creates_platform_default_rc_ubuntu(
        self, isolated_home, run_env_pass
    ):
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(shell_rc=[STARSHIP]))

        result = run_env_pass(current_os="ubuntu")

        assert result.failures == []
        assert "starship init bash" in (isolated_home / ".bashrc").read_text()
        assert not (isolated_home / ".zshrc").exists()

    def test_second_pass_is_idempotent(self, isolated_home, run_env_pass):
        (isolated_home / ".bashrc").write_text("")
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(shell_rc=[STARSHIP]))
        assert run_env_pass().failures == []
        content = (isolated_home / ".bashrc").read_text()

        second = run_env_pass()

        assert second.failures == []
        assert second.action_entries == []
        assert (isolated_home / ".bashrc").read_text() == content
        assert content.count("starship init bash") == 1

    def test_block_added_to_rc_file_where_missing(
        self, isolated_home, run_env_pass
    ):
        # .bashrc already carries the block, .zshrc does not: the check
        # requires EVERY existing rc file, so the fix completes .zshrc.
        (isolated_home / ".bashrc").write_text(
            '\neval "$(starship init bash)"\n')
        (isolated_home / ".zshrc").write_text("")
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(shell_rc=[STARSHIP]))

        result = run_env_pass()

        assert result.failures == []
        assert "starship init zsh" in (isolated_home / ".zshrc").read_text()
        bash_text = (isolated_home / ".bashrc").read_text()
        assert bash_text.count("starship init bash") == 1  # not duplicated


class TestShellRcForbid:
    def test_uncommented_match_is_commented_out(
        self, isolated_home, run_env_pass
    ):
        bashrc = isolated_home / ".bashrc"
        bashrc.write_text("export TERM=xterm-256color\nalias ll='ls -l'\n")
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(shell_rc=[NO_TERM]))

        result = run_env_pass()

        assert result.failures == []
        text = bashrc.read_text()
        assert "# export TERM=xterm-256color" in text
        assert "alias ll='ls -l'" in text
        assert any("commented out 1 line(s) in .bashrc" in e
                   for e in result.action_entries)

    def test_commented_match_passes_untouched(
        self, isolated_home, run_env_pass
    ):
        bashrc = isolated_home / ".bashrc"
        original = "# export TERM=xterm\n"
        bashrc.write_text(original)
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(shell_rc=[NO_TERM]))

        result = run_env_pass()

        assert result.failures == []
        assert result.action_entries == []
        assert bashrc.read_text() == original

    def test_no_rc_files_is_trivially_clean(self, isolated_home, run_env_pass):
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(shell_rc=[NO_TERM]))
        result = run_env_pass()
        assert result.failures == []
        assert result.action_entries == []

    def test_second_pass_is_idempotent(self, isolated_home, run_env_pass):
        bashrc = isolated_home / ".bashrc"
        bashrc.write_text("TERM=dumb\n")
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(shell_rc=[NO_TERM]))
        assert run_env_pass().failures == []
        text = bashrc.read_text()
        assert text == "# TERM=dumb\n"

        second = run_env_pass()

        assert second.failures == []
        assert second.action_entries == []
        assert bashrc.read_text() == text

    def test_invalid_regex_is_a_failure(self, isolated_home, run_env_pass):
        entry = {"name": "bad", "forbid": "["}
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(shell_rc=[entry]))
        result = run_env_pass()
        assert len(result.failures) == 1
        assert "invalid forbid regex" in result.failures[0]["message"]


class TestShellRcValidation:
    @pytest.mark.parametrize("entry", [
        {"name": "both", "content": "x", "forbid": "y"},
        {"name": "neither"},
        {"content": "no name"},
    ])
    def test_invalid_entries_fail(self, isolated_home, run_env_pass, entry):
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(shell_rc=[entry]))
        result = run_env_pass()
        assert len(result.failures) == 1
        assert "invalid shell_rc entry" in result.failures[0]["message"]

    def test_os_filter_skips_entry(self, isolated_home, run_env_pass):
        entry = {"name": "shell-var",
                 "content": 'export SHELL="${SHELL:-/usr/bin/bash}"',
                 "os": ["windows"]}
        (isolated_home / ".bashrc").write_text("")
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(shell_rc=[entry]))

        result = run_env_pass(current_os="ubuntu")

        assert result.failures == []
        assert (isolated_home / ".bashrc").read_text() == ""


class TestShellChecksUnit:
    def test_ensure_requires_every_existing_rc(self, isolated_home):
        (isolated_home / ".bashrc").write_text("eval \"$(starship init bash)\"")
        (isolated_home / ".zshrc").write_text("")
        result = check_shell_ensure(
            "starship-init", 'eval "$(starship init SHELL_NAME)"')
        assert not result.passed
        assert ".zshrc" in result.message

    def test_forbid_reports_file_and_line(self, isolated_home):
        (isolated_home / ".bashrc").write_text("a\nexport TERM=foo\n")
        result = check_shell_forbid(
            "no-term-override", "^\\s*(export\\s+)?TERM=")
        assert not result.passed
        assert ".bashrc:2" in result.message


# ---------------------------------------------------------------------------
# Fake macOS system surface (defaults / symbolic hotkeys / System Events)
# ---------------------------------------------------------------------------

class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _fake_subprocess(run):
    """A subprocess stand-in: fake `run`, real exception classes.

    env_features' except clauses reference subprocess.SubprocessError /
    TimeoutExpired from its module-level `subprocess` name, so the stub
    must carry them for the failure-path tests.
    """
    return SimpleNamespace(
        run=run,
        SubprocessError=subprocess.SubprocessError,
        TimeoutExpired=subprocess.TimeoutExpired,
    )


def _hotkeys_plist(entries):
    """Build an AppleSymbolicHotKeys plist dict from {id: (params, enabled)}."""
    return {
        "AppleSymbolicHotKeys": {
            str(hid): {
                "enabled": enabled,
                "value": {"parameters": list(params), "type": "standard"},
            }
            for hid, (params, enabled) in entries.items()
        }
    }


class FakeMac:
    """Stateful stand-in for the macOS commands env_features shells out to."""

    def __init__(self, defaults=None, hotkeys=None, login_items=None):
        self.defaults = dict(defaults or {})     # (domain, key) -> read output
        self.hotkeys = hotkeys                    # plist dict or None
        self.login_items = list(login_items or [])
        self.calls = []

    def install(self, monkeypatch):
        monkeypatch.setattr(
            env_features, "subprocess", _fake_subprocess(self.run))
        return self

    def write_calls(self):
        return [c for c in self.calls if c[:2] == ["defaults", "write"]
                or c[:2] == ["defaults", "import"]
                or (c[0] == "osascript" and "make login item" in c[2])]

    def run(self, cmd, **kwargs):
        # Engine idiom: every subprocess call is bounded (R1). Asserting it
        # here covers every call site the feature tests exercise.
        assert "timeout" in kwargs, f"unbounded subprocess call: {cmd}"
        cmd = [str(c) for c in cmd]
        self.calls.append(cmd)
        prog = os.path.basename(cmd[0])
        if prog == "defaults":
            return self._defaults(cmd)
        if prog == "killall" or prog == "activateSettings":
            return FakeProc(0)
        if prog == "osascript":
            return self._osascript(cmd)
        raise AssertionError(f"unexpected command: {cmd}")

    def _defaults(self, cmd):
        sub = cmd[1]
        if sub == "read":
            domain, key = cmd[2], cmd[3]
            if (domain, key) in self.defaults:
                return FakeProc(0, stdout=self.defaults[(domain, key)] + "\n")
            return FakeProc(1, stderr=f"The domain/default pair of ({domain}, {key}) does not exist")
        if sub == "write":
            domain, key, flag, raw = cmd[2], cmd[3], cmd[4], cmd[5]
            if flag == "-bool":
                self.defaults[(domain, key)] = "1" if raw == "true" else "0"
            else:
                self.defaults[(domain, key)] = raw
            return FakeProc(0)
        if sub == "export":
            assert cmd[2] == env_features.HOTKEYS_DOMAIN
            if self.hotkeys is None:
                return FakeProc(1, stderr=b"export failed")
            return FakeProc(0, stdout=plistlib.dumps(self.hotkeys))
        if sub == "import":
            assert cmd[2] == env_features.HOTKEYS_DOMAIN
            self.hotkeys = plistlib.loads(Path(cmd[3]).read_bytes())
            return FakeProc(0)
        raise AssertionError(f"unexpected defaults subcommand: {cmd}")

    def _osascript(self, cmd):
        script = cmd[2]
        if "get the name of every login item" in script:
            return FakeProc(0, stdout=", ".join(self.login_items) + "\n")
        if "make login item" in script:
            m = re.search(r'path:"([^"]+)"', script)
            name = os.path.basename(m.group(1))
            if name.endswith(".app"):
                name = name[:-4]
            self.login_items.append(name)
            return FakeProc(0)
        raise AssertionError(f"unexpected osascript: {script}")


KEY_REPEAT = [
    {"domain": "NSGlobalDomain", "key": "ApplePressAndHoldEnabled",
     "value": False},
    {"domain": "NSGlobalDomain", "key": "InitialKeyRepeat", "value": 25},
    {"domain": "NSGlobalDomain", "key": "KeyRepeat", "value": 6},
]

CONVERGED_DEFAULTS = {
    ("NSGlobalDomain", "ApplePressAndHoldEnabled"): "0",
    ("NSGlobalDomain", "InitialKeyRepeat"): "25",
    ("NSGlobalDomain", "KeyRepeat"): "6",
}


# ---------------------------------------------------------------------------
# macos_defaults
# ---------------------------------------------------------------------------

class TestMacosDefaults:
    def _write_manifest(self, home, entries=KEY_REPEAT, os_="macos"):
        _write_json(home / ".claude" / "env.json",
                    _manifest(os_=os_, macos_defaults=entries))

    def test_skipped_on_non_macos(self, isolated_home, run_env_pass, monkeypatch):
        mac = FakeMac().install(monkeypatch)
        self._write_manifest(isolated_home, os_="ubuntu")

        result = run_env_pass(current_os="ubuntu")

        assert result.failures == []
        assert any("macos_defaults: skipped (not macOS)" in e
                   for e in result.ok_entries)
        assert mac.calls == []

    def test_converged_state_only_reads(
        self, isolated_home, run_env_pass, monkeypatch
    ):
        mac = FakeMac(defaults=CONVERGED_DEFAULTS).install(monkeypatch)
        self._write_manifest(isolated_home)

        result = run_env_pass(current_os="macos")

        assert result.failures == []
        assert result.action_entries == []
        assert mac.write_calls() == []
        assert all(c[:2] == ["defaults", "read"] for c in mac.calls)

    def test_missing_keys_written_with_typed_flags_and_flushed(
        self, isolated_home, run_env_pass, monkeypatch
    ):
        mac = FakeMac().install(monkeypatch)
        self._write_manifest(isolated_home)

        result = run_env_pass(current_os="macos")

        assert result.failures == []
        writes = [c for c in mac.calls if c[:2] == ["defaults", "write"]]
        assert ["defaults", "write", "NSGlobalDomain",
                "ApplePressAndHoldEnabled", "-bool", "false"] in writes
        assert ["defaults", "write", "NSGlobalDomain",
                "InitialKeyRepeat", "-int", "25"] in writes
        assert ["defaults", "write", "NSGlobalDomain",
                "KeyRepeat", "-int", "6"] in writes
        assert mac.defaults == CONVERGED_DEFAULTS  # re-check saw fixed state
        assert ["killall", "cfprefsd"] in mac.calls
        assert ["killall", "SystemUIServer"] in mac.calls
        assert len(result.action_entries) == 3

    def test_wrong_value_is_rewritten(
        self, isolated_home, run_env_pass, monkeypatch
    ):
        wrong = dict(CONVERGED_DEFAULTS)
        wrong[("NSGlobalDomain", "KeyRepeat")] = "2"
        mac = FakeMac(defaults=wrong).install(monkeypatch)
        self._write_manifest(isolated_home)

        result = run_env_pass(current_os="macos")

        assert result.failures == []
        assert mac.defaults[("NSGlobalDomain", "KeyRepeat")] == "6"
        assert len(result.action_entries) == 1

    def test_second_pass_is_idempotent(
        self, isolated_home, run_env_pass, monkeypatch
    ):
        mac = FakeMac().install(monkeypatch)
        self._write_manifest(isolated_home)
        assert run_env_pass(current_os="macos").failures == []
        mac.calls.clear()

        second = run_env_pass(current_os="macos")

        assert second.failures == []
        assert second.action_entries == []
        assert mac.write_calls() == []

    def test_string_value_written_as_string(
        self, isolated_home, run_env_pass, monkeypatch
    ):
        mac = FakeMac().install(monkeypatch)
        entries = [{"domain": "com.apple.dock", "key": "orientation",
                    "value": "left"}]
        self._write_manifest(isolated_home, entries=entries)

        result = run_env_pass(current_os="macos")

        assert result.failures == []
        assert ["defaults", "write", "com.apple.dock", "orientation",
                "-string", "left"] in mac.calls

    def test_failed_write_is_a_failure(
        self, isolated_home, run_env_pass, monkeypatch
    ):
        mac = FakeMac().install(monkeypatch)
        original = mac._defaults

        def failing(cmd):
            if cmd[1] == "write":
                return FakeProc(1, stderr="Could not write domain")
            return original(cmd)

        mac._defaults = failing
        self._write_manifest(isolated_home,
                             entries=[KEY_REPEAT[1]])

        result = run_env_pass(current_os="macos")

        assert len(result.failures) == 1
        assert "defaults write failed" in result.failures[0]["message"]
        assert result.failures[0]["type"] == "env_macos_default"

    def test_invalid_value_type_is_a_failure(
        self, isolated_home, run_env_pass, monkeypatch
    ):
        FakeMac().install(monkeypatch)
        entries = [{"domain": "NSGlobalDomain", "key": "K", "value": [1, 2]}]
        self._write_manifest(isolated_home, entries=entries)
        result = run_env_pass(current_os="macos")
        assert len(result.failures) == 1
        assert "invalid macos_defaults entry" in result.failures[0]["message"]


# ---------------------------------------------------------------------------
# macos_hotkeys
# ---------------------------------------------------------------------------

SCREENSHOT_HOTKEYS = [
    {"id": 28, "parameters": [48, 29, 1179648], "enabled": True,
     "description": "Screenshot: save screen to file (cmd+shift+0)"},
    {"id": 29, "parameters": [48, 29, 1441792], "enabled": True,
     "description": "Screenshot: screen to clipboard"},
]

FACTORY_HOTKEYS = _hotkeys_plist({
    28: ([51, 20, 1179648], True),
    29: ([51, 20, 1441792], True),
    30: ([52, 21, 1179648], True),
})

CONVERGED_HOTKEYS = _hotkeys_plist({
    28: ([48, 29, 1179648], True),
    29: ([48, 29, 1441792], True),
    30: ([52, 21, 1179648], True),
})


class TestMacosHotkeys:
    def _write_manifest(self, home, entries=SCREENSHOT_HOTKEYS, os_="macos"):
        _write_json(home / ".claude" / "env.json",
                    _manifest(os_=os_, macos_hotkeys=entries))

    def test_skipped_on_non_macos(self, isolated_home, run_env_pass, monkeypatch):
        mac = FakeMac(hotkeys=FACTORY_HOTKEYS).install(monkeypatch)
        self._write_manifest(isolated_home, os_="ubuntu")

        result = run_env_pass(current_os="ubuntu")

        assert result.failures == []
        assert any("macos_hotkeys: skipped (not macOS)" in e
                   for e in result.ok_entries)
        assert mac.calls == []

    def test_converged_state_only_exports(
        self, isolated_home, run_env_pass, monkeypatch
    ):
        mac = FakeMac(hotkeys=CONVERGED_HOTKEYS).install(monkeypatch)
        self._write_manifest(isolated_home)

        result = run_env_pass(current_os="macos")

        assert result.failures == []
        assert result.action_entries == []
        assert mac.write_calls() == []
        assert mac.calls == [["defaults", "export",
                              env_features.HOTKEYS_DOMAIN, "-"]]

    def test_mismatch_imports_mutated_plist_and_flushes(
        self, isolated_home, run_env_pass, monkeypatch
    ):
        mac = FakeMac(hotkeys=FACTORY_HOTKEYS).install(monkeypatch)
        self._write_manifest(isolated_home)

        result = run_env_pass(current_os="macos")

        assert result.failures == []
        hot = mac.hotkeys["AppleSymbolicHotKeys"]
        assert hot["28"]["value"]["parameters"] == [48, 29, 1179648]
        assert hot["29"]["value"]["parameters"] == [48, 29, 1441792]
        assert hot["28"]["enabled"] is True
        # Untouched sibling hotkey and value.type survive the round-trip.
        assert hot["30"]["value"]["parameters"] == [52, 21, 1179648]
        assert hot["28"]["value"]["type"] == "standard"
        # ONE import for the whole batch, then the flush + re-export.
        imports = [c for c in mac.calls if c[:2] == ["defaults", "import"]]
        assert len(imports) == 1
        assert ["killall", "cfprefsd"] in mac.calls
        assert ["killall", "screencaptureui"] in mac.calls
        assert ["killall", "SystemUIServer"] in mac.calls
        assert any("activateSettings" in c[0] for c in mac.calls)
        assert len(result.action_entries) == 2
        assert any("Screenshot: save screen to file" in e
                   for e in result.action_entries)

    def test_enabled_false_is_applied_and_compared(
        self, isolated_home, run_env_pass, monkeypatch
    ):
        mac = FakeMac(hotkeys=CONVERGED_HOTKEYS).install(monkeypatch)
        entries = [{"id": 30, "parameters": [52, 21, 1179648],
                    "enabled": False}]
        self._write_manifest(isolated_home, entries=entries)

        result = run_env_pass(current_os="macos")

        assert result.failures == []
        assert len(result.action_entries) == 1
        assert mac.hotkeys["AppleSymbolicHotKeys"]["30"]["enabled"] is False

    def test_missing_id_is_a_failure_without_import(
        self, isolated_home, run_env_pass, monkeypatch
    ):
        mac = FakeMac(hotkeys=CONVERGED_HOTKEYS).install(monkeypatch)
        entries = [{"id": 99, "parameters": [1, 2, 3], "enabled": True}]
        self._write_manifest(isolated_home, entries=entries)

        result = run_env_pass(current_os="macos")

        assert len(result.failures) == 1
        assert "id 99 not present" in result.failures[0]["message"]
        assert mac.write_calls() == []

    def test_export_failure_is_a_failure(
        self, isolated_home, run_env_pass, monkeypatch
    ):
        FakeMac(hotkeys=None).install(monkeypatch)
        self._write_manifest(isolated_home)

        result = run_env_pass(current_os="macos")

        assert len(result.failures) == 1
        assert "defaults export" in result.failures[0]["message"]

    def test_second_pass_is_idempotent(
        self, isolated_home, run_env_pass, monkeypatch
    ):
        mac = FakeMac(hotkeys=FACTORY_HOTKEYS).install(monkeypatch)
        self._write_manifest(isolated_home)
        assert run_env_pass(current_os="macos").failures == []
        mac.calls.clear()

        second = run_env_pass(current_os="macos")

        assert second.failures == []
        assert second.action_entries == []
        assert mac.write_calls() == []

    def test_invalid_entry_is_a_failure(
        self, isolated_home, run_env_pass, monkeypatch
    ):
        FakeMac(hotkeys=CONVERGED_HOTKEYS).install(monkeypatch)
        self._write_manifest(
            isolated_home, entries=[{"id": "28", "parameters": [1]}])
        result = run_env_pass(current_os="macos")
        assert len(result.failures) == 1
        assert "invalid macos_hotkeys entry" in result.failures[0]["message"]


class TestHotkeyStateUnit:
    def test_enabled_int_compares_as_bool(self):
        data = {"AppleSymbolicHotKeys": {
            "28": {"enabled": 1,
                   "value": {"parameters": [48, 29, 1179648]}}}}
        status, _ = hotkey_state(data, 28, [48, 29, 1179648], True)
        assert status == "ok"

    def test_mismatch_detail_names_both_states(self):
        data = _hotkeys_plist({28: ([1, 2, 3], True)})
        status, detail = hotkey_state(data, 28, [4, 5, 6], True)
        assert status == "mismatch"
        assert "[4, 5, 6]" in detail and "[1, 2, 3]" in detail


# ---------------------------------------------------------------------------
# login_items
# ---------------------------------------------------------------------------

class TestLoginItems:
    def _write_manifest(self, home, app_dir, os_="macos", **overrides):
        entry = {"name": "Tailscale", "path": str(app_dir),
                 "hidden": False, "os": ["macos"]}
        entry.update(overrides)
        _write_json(home / ".claude" / "env.json",
                    _manifest(os_=os_, login_items=[entry]))

    def _app(self, home):
        app_dir = home / "Applications" / "Tailscale.app"
        app_dir.mkdir(parents=True)
        return app_dir

    def test_skipped_on_non_macos(self, isolated_home, run_env_pass, monkeypatch):
        mac = FakeMac().install(monkeypatch)
        self._write_manifest(isolated_home, self._app(isolated_home),
                             os_="ubuntu")

        result = run_env_pass(current_os="ubuntu")

        assert result.failures == []
        assert any("login_items: skipped (not macOS)" in e
                   for e in result.ok_entries)
        assert mac.calls == []

    def test_registered_item_passes(self, isolated_home, run_env_pass, monkeypatch):
        mac = FakeMac(login_items=["Tailscale", "Other"]).install(monkeypatch)
        self._write_manifest(isolated_home, self._app(isolated_home))

        result = run_env_pass(current_os="macos")

        assert result.failures == []
        assert result.action_entries == []
        assert mac.write_calls() == []

    def test_missing_item_is_added_and_rechecked(
        self, isolated_home, run_env_pass, monkeypatch
    ):
        mac = FakeMac(login_items=["Other"]).install(monkeypatch)
        app_dir = self._app(isolated_home)
        self._write_manifest(isolated_home, app_dir)

        result = run_env_pass(current_os="macos")

        assert result.failures == []
        assert "Tailscale" in mac.login_items
        makes = [c for c in mac.calls
                 if c[0] == "osascript" and "make login item" in c[2]]
        assert len(makes) == 1
        assert f'path:"{app_dir}"' in makes[0][2]
        assert "hidden:false" in makes[0][2]
        assert any("login_item Tailscale: added login item" in e
                   for e in result.action_entries)

    def test_hidden_true_propagates(self, isolated_home, run_env_pass, monkeypatch):
        mac = FakeMac().install(monkeypatch)
        self._write_manifest(isolated_home, self._app(isolated_home),
                             hidden=True)

        result = run_env_pass(current_os="macos")

        assert result.failures == []
        makes = [c for c in mac.calls if "make login item" in c[-1]]
        assert "hidden:true" in makes[0][2]

    def test_missing_app_is_a_persistent_failure(
        self, isolated_home, run_env_pass, monkeypatch
    ):
        mac = FakeMac().install(monkeypatch)
        self._write_manifest(
            isolated_home, isolated_home / "Applications" / "Tailscale.app")

        result = run_env_pass(current_os="macos")

        assert len(result.failures) == 1
        failure = result.failures[0]
        assert failure["type"] == "env_login_item"
        assert failure["persist_across_sessions"] is True
        assert "app not found" in failure["message"]
        assert mac.calls == []  # never queried System Events
        assert read_env_state(str(run_env_pass.data_dir))["last_result"] == "failed"

    def test_osascript_failure_is_a_failure(
        self, isolated_home, run_env_pass, monkeypatch
    ):
        def failing_run(cmd, **kwargs):
            return FakeProc(1, stderr="osascript: not authorized")

        monkeypatch.setattr(
            env_features, "subprocess", _fake_subprocess(failing_run))
        self._write_manifest(isolated_home, self._app(isolated_home))

        result = run_env_pass(current_os="macos")

        assert len(result.failures) == 1
        assert "not authorized" in result.failures[0]["message"]

    def test_second_pass_is_idempotent(
        self, isolated_home, run_env_pass, monkeypatch
    ):
        mac = FakeMac().install(monkeypatch)
        self._write_manifest(isolated_home, self._app(isolated_home))
        assert run_env_pass(current_os="macos").failures == []
        mac.calls.clear()

        second = run_env_pass(current_os="macos")

        assert second.failures == []
        assert second.action_entries == []
        assert mac.write_calls() == []


# ---------------------------------------------------------------------------
# Subprocess bounds + best-effort flushes (R1)
# ---------------------------------------------------------------------------

class TestSubprocessFailureContainment:
    """Timeouts and missing binaries become descriptive failures, and the
    best-effort flush helpers really are best-effort (never raise)."""

    def _install_raising(self, monkeypatch, exc_factory):
        calls = []

        def raising_run(cmd, **kwargs):
            calls.append([str(c) for c in cmd])
            raise exc_factory(cmd)

        monkeypatch.setattr(
            env_features, "subprocess", _fake_subprocess(raising_run))
        return calls

    @staticmethod
    def _timeout(cmd):
        return subprocess.TimeoutExpired(cmd, 10)

    @staticmethod
    def _missing(cmd):
        return FileNotFoundError(2, "No such file or directory", cmd[0])

    def test_defaults_read_timeout_is_a_failed_check(self, monkeypatch):
        self._install_raising(monkeypatch, self._timeout)
        result = env_features.check_macos_default("NSGlobalDomain", "KeyRepeat", 6)
        assert not result.passed
        assert "defaults read failed" in result.message

    def test_defaults_write_timeout_is_a_failed_fix(self, monkeypatch):
        self._install_raising(monkeypatch, self._timeout)
        ok, msg = env_features.fix_macos_default("NSGlobalDomain", "KeyRepeat", 6)
        assert not ok
        assert "defaults write failed" in msg

    def test_hotkey_export_timeout_is_an_error(self, monkeypatch):
        self._install_raising(monkeypatch, self._timeout)
        data, err = env_features.read_symbolic_hotkeys()
        assert data is None
        assert "failed to read symbolic hotkeys" in err

    def test_hotkey_import_failure_is_a_failed_fix(self, monkeypatch):
        self._install_raising(monkeypatch, self._missing)
        data = _hotkeys_plist({28: ([1, 2, 3], True)})
        ok, msg = env_features.apply_symbolic_hotkeys(
            data, [{"id": 28, "parameters": [4, 5, 6], "enabled": True}])
        assert not ok
        assert "failed to apply symbolic hotkeys" in msg

    def test_osascript_timeout_is_an_error(self, monkeypatch):
        self._install_raising(monkeypatch, self._timeout)
        items, err = env_features.list_login_items()
        assert items is None
        assert "login-item query failed" in err
        ok, msg = env_features.add_login_item("/Applications/X.app", False)
        assert not ok
        assert "make login item failed" in msg

    def test_flush_helpers_swallow_missing_binary(self, monkeypatch):
        calls = self._install_raising(monkeypatch, self._missing)
        env_features.flush_macos_defaults_cache()  # must not raise
        env_features._flush_hotkey_caches()        # must not raise
        assert len(calls) == 2 + 4  # 2 killall + 3 killall + activateSettings

    def test_flush_helpers_swallow_timeout(self, monkeypatch):
        self._install_raising(monkeypatch, self._timeout)
        env_features.flush_macos_defaults_cache()
        env_features._flush_hotkey_caches()

    def test_osascript_timeout_surfaces_as_engine_failure(
        self, isolated_home, run_env_pass, monkeypatch
    ):
        """The SessionStart-hang scenario: a blocked System Events call is
        bounded and lands as one persistent failure, not a wedged pass."""
        self._install_raising(monkeypatch, self._timeout)
        app_dir = isolated_home / "Applications" / "Tailscale.app"
        app_dir.mkdir(parents=True)
        entry = {"name": "Tailscale", "path": str(app_dir)}
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(os_="macos", login_items=[entry]))

        result = run_env_pass(current_os="macos")

        assert len(result.failures) == 1
        # The bounded timeout flows through check -> fix -> re-check and
        # surfaces as the fix's descriptive message.
        assert "osascript" in result.failures[0]["message"]
        assert "timed out" in result.failures[0]["message"]
        assert read_env_state(str(run_env_pass.data_dir))["last_result"] == "failed"

    def test_missing_activate_settings_does_not_abort_hotkey_fix(
        self, isolated_home, run_env_pass, monkeypatch
    ):
        """A macOS without the activateSettings private binary still
        converges: the flush is best-effort, the pass completes green."""
        mac = FakeMac(hotkeys=FACTORY_HOTKEYS)

        real_run = mac.run

        def run(cmd, **kwargs):
            if str(cmd[0]).endswith("activateSettings"):
                raise self._missing([str(c) for c in cmd])
            return real_run(cmd, **kwargs)

        monkeypatch.setattr(
            env_features, "subprocess", _fake_subprocess(run))
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(os_="macos", macos_hotkeys=SCREENSHOT_HOTKEYS))

        result = run_env_pass(current_os="macos")

        assert result.failures == []
        assert len(result.action_entries) == 2
        hot = mac.hotkeys["AppleSymbolicHotKeys"]
        assert hot["28"]["value"]["parameters"] == [48, 29, 1179648]
        assert read_env_state(str(run_env_pass.data_dir))["last_result"] == "clean"


# ---------------------------------------------------------------------------
# Section-shape validation + pass stamping
# ---------------------------------------------------------------------------

class TestSectionShape:
    def test_non_array_section_is_a_failure(self, isolated_home, run_env_pass):
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(symlinks={"name": "not-a-list"}))
        result = run_env_pass()
        assert len(result.failures) == 1
        assert "must be an array" in result.failures[0]["message"]
        # Section-shape errors carry the section's per-entry type (singular).
        assert result.failures[0]["type"] == "env_symlink"

    def test_feature_failure_stamps_pass_failed(
        self, isolated_home, run_env_pass
    ):
        entry = {"name": "s", "source": str(isolated_home / "missing"),
                 "target": str(isolated_home / "t")}
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(symlinks=[entry]))
        result = run_env_pass()
        assert result.failures
        assert read_env_state(str(run_env_pass.data_dir))["last_result"] == "failed"

    def test_clean_features_stamp_pass_clean(self, isolated_home, run_env_pass):
        source = isolated_home / "src"
        source.write_text("x")
        entry = {"name": "s", "source": str(source),
                 "target": str(isolated_home / "t")}
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(symlinks=[entry]))
        result = run_env_pass()
        assert result.failures == []
        assert read_env_state(str(run_env_pass.data_dir))["last_result"] == "clean"


# ---------------------------------------------------------------------------
# Full-engine e2e (subprocess --console, isolated HOME) -- the e2e step 3's
# audit carried forward, now that feature handlers exist.
# ---------------------------------------------------------------------------

BOOTSTRAP_ROOT = os.path.normpath(os.path.join(
    os.path.dirname(__file__), os.pardir, os.pardir, "plugins", "bootstrap"))
ENGINE_SCRIPT = os.path.join(BOOTSTRAP_ROOT, "engine", "bootstrap_engine.py")


class TestFullEngineEnvE2E:
    def _setup(self, tmp_path):
        """Fake bootstrap root + isolated HOME carrying a real env.json."""
        from bootstrap_lib.platform_detect import detect_os

        fake_root = tmp_path / "plugins" / "bootstrap"
        fake_root.mkdir(parents=True)
        (fake_root / "bootstrap_lib").symlink_to(
            os.path.join(BOOTSTRAP_ROOT, "bootstrap_lib"))
        (fake_root / "engine").symlink_to(
            os.path.join(BOOTSTRAP_ROOT, "engine"))
        defaults = fake_root / "defaults"
        defaults.mkdir()
        (defaults / "config.json").write_text(json.dumps({
            "schema_version": 5, "no_bootstrap": [], "bootstrap_cache": [],
            "log_success_shell": False, "log_success_checks": False,
            "self_setup": {},
        }))
        (fake_root / "bootstrap.json").write_text(json.dumps({}))

        home = tmp_path / "_home"
        (home / ".claude").mkdir(parents=True)
        current_os = detect_os()

        source = home / ".claude" / "dotfiles" / "starship.toml"
        source.parent.mkdir(parents=True)
        source.write_text("# tracked starship config\n")
        target = home / ".config" / "starship.toml"
        bashrc = home / ".bashrc"
        bashrc.write_text("# rc\nexport TERM=xterm\n")

        env_json = {
            "machines": {socket.gethostname(): {"os": current_os}},
            "symlinks": [
                {"name": "starship-config", "source": str(source),
                 "target": str(target)},
            ],
            "shell_rc": [
                {"name": "starship-init",
                 "content": 'eval "$(starship init SHELL_NAME)"'},
                {"name": "no-term-override",
                 "forbid": "^\\s*(export\\s+)?TERM="},
            ],
        }
        _write_json(home / ".claude" / "env.json", env_json)

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home)}
        env.pop("CLAUDE_ENV_FILE", None)
        return fake_root, data_dir, home, env, target, bashrc

    def _run(self, fake_root, data_dir, env):
        return subprocess.run(
            [sys.executable, ENGINE_SCRIPT,
             "--plugin-root", str(fake_root),
             "--data-dir", str(data_dir), "--console"],
            capture_output=True, text=True, env=env,
        )

    def test_console_pass_applies_features_then_gate_skips(self, tmp_path):
        fake_root, data_dir, home, env, target, bashrc = self._setup(tmp_path)

        first = self._run(fake_root, data_dir, env)

        assert first.returncode == 0, first.stderr
        assert "failure" not in first.stdout, first.stdout
        # Actions surfaced in console output under the env prefix.
        assert "env: symlink starship-config" in first.stdout
        assert "env: shell_rc starship-init" in first.stdout
        assert "env: shell_rc no-term-override" in first.stdout
        # ...and actually happened on disk.
        assert target.is_symlink()
        text = bashrc.read_text()
        assert 'eval "$(starship init bash)"' in text
        assert "# export TERM=xterm" in text
        assert "\nexport TERM=xterm" not in text
        # The env stamp recorded a clean pass.
        state = json.loads((data_dir / ENV_STATE_STAMP).read_text())
        assert state["last_result"] == "clean"

        second = self._run(fake_root, data_dir, env)

        assert second.returncode == 0, second.stderr
        # Gate closed: no env actions, nothing re-applied, files unchanged.
        assert "env: symlink" not in second.stdout
        assert "env: shell_rc" not in second.stdout
        assert bashrc.read_text() == text

    def test_console_manifest_edit_reopens_gate(self, tmp_path):
        fake_root, data_dir, home, env, target, bashrc = self._setup(tmp_path)
        assert self._run(fake_root, data_dir, env).returncode == 0

        manifest_path = home / ".claude" / "env.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["shell_rc"].append(
            {"name": "terminal-colors",
             "content": ". ~/.claude/scripts/terminalcolor-init.sh"})
        manifest_path.write_text(json.dumps(manifest))

        result = self._run(fake_root, data_dir, env)

        assert result.returncode == 0, result.stderr
        assert "env: shell_rc terminal-colors" in result.stdout
        assert "terminalcolor-init.sh" in bashrc.read_text()
