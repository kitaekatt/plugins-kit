"""Tests for venv_check.py — Python venv validation."""

import os
import shlex
import stat
import subprocess
from unittest.mock import patch

import pytest

from bootstrap_lib.venv_check import (
    check_venv,
    ensure_venv,
    export_venv_env_var,
    find_uv,
    venv_env_var_name,
)


def _sourced_value(env_file, var):
    """Return the value of `export <var>=...` from an env file, parsed the way a
    POSIX shell would (shlex implements shell quote-removal/word-splitting).

    Used instead of spawning `bash -c 'source ...'` because the `bash` on PATH
    may be WSL, which cannot source a Windows-path file or see a Windows venv --
    so a real-shell round-trip is unreliable here. shlex.split tests the same
    property the round-trip did: a correctly quoted path does not split on spaces.
    """
    prefix = f"export {var}="
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line.startswith(prefix):
            parts = shlex.split(line[len(prefix):])
            return parts[0] if parts else ""
    return None


class TestCheckVenv:
    def test_missing_venv_dir(self, tmp_path):
        """Returns failure when venv directory doesn't exist."""
        result = check_venv(str(tmp_path / "data"), str(tmp_path / "plugin"), ["yaml"])

        assert not result.passed
        assert "not found" in result.message
        assert result.remediation_cmd is not None
        assert "uv sync" in result.remediation_cmd

    def test_no_python_binary(self, tmp_path):
        """Returns failure when venv exists but has no python binary."""
        venv_dir = tmp_path / "data" / ".venv"
        venv_dir.mkdir(parents=True)

        result = check_venv(str(tmp_path / "data"), str(tmp_path / "plugin"), ["yaml"])

        assert not result.passed
        assert "no python binary" in result.message

    def test_working_venv_with_imports(self, tmp_path):
        """Passes when venv has working python and all imports succeed."""
        # Create a real venv using the current Python
        venv_dir = tmp_path / "data" / ".venv"
        subprocess.run(
            ["uv", "venv", str(venv_dir)],
            check=True, capture_output=True,
        )

        # sys and os are always available
        result = check_venv(str(tmp_path / "data"), str(tmp_path / "plugin"), ["sys", "os"])

        assert result.passed
        assert "2 imports verified" in result.message
        assert result.remediation_cmd is None

    def test_import_failure(self, tmp_path):
        """Returns failure when an import doesn't work in the venv."""
        venv_dir = tmp_path / "data" / ".venv"
        subprocess.run(
            ["uv", "venv", str(venv_dir)],
            check=True, capture_output=True,
        )

        result = check_venv(
            str(tmp_path / "data"), str(tmp_path / "plugin"),
            ["nonexistent_module_xyz_abc_123"],
        )

        assert not result.passed
        assert "import nonexistent_module_xyz_abc_123 failed" in result.message
        assert result.remediation_cmd is not None

    def test_empty_imports_list(self, tmp_path):
        """Passes when no imports to check."""
        venv_dir = tmp_path / "data" / ".venv"
        subprocess.run(
            ["uv", "venv", str(venv_dir)],
            check=True, capture_output=True,
        )

        result = check_venv(str(tmp_path / "data"), str(tmp_path / "plugin"), [])

        assert result.passed
        assert "0 imports verified" in result.message

    def test_python_nonzero_exit_code_fails(self, tmp_path):
        """A python binary that launches but exits non-zero is not functional (B4).

        The "python works" probe used to ignore returncode entirely, so a
        broken interpreter (missing DLLs, bad shebang target) passed the check
        and failed later in confusing ways.
        """
        venv_dir = tmp_path / "data" / ".venv"
        bin_dir = venv_dir / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "python").write_text("")  # exists so _find_python returns it

        class _Proc:
            returncode = 1
            stdout = b""
            stderr = b""

        with patch("bootstrap_lib.venv_check.subprocess.run", return_value=_Proc()):
            result = check_venv(str(tmp_path / "data"), str(tmp_path / "plugin"), [])

        assert not result.passed
        assert "not functional" in result.message
        assert result.remediation_cmd is not None

    def test_remediation_includes_plugin_root(self, tmp_path):
        """Remediation command references the plugin root for uv sync."""
        plugin_root = str(tmp_path / "my-plugin")
        result = check_venv(str(tmp_path / "data"), plugin_root, ["yaml"])

        assert not result.passed
        assert plugin_root in result.remediation_cmd


class TestVenvEnvVarName:
    def test_kebab_to_upper_underscore(self):
        assert venv_env_var_name("unreal-kit") == "UNREAL_KIT_VENV"

    def test_single_word(self):
        assert venv_env_var_name("bootstrap") == "BOOTSTRAP_VENV"

    def test_multi_hyphen(self):
        assert venv_env_var_name("multi-word-plugin") == "MULTI_WORD_PLUGIN_VENV"

    def test_already_upper_preserved(self):
        # Not kebab, but just in case — upper + replace is idempotent.
        assert venv_env_var_name("Foo-Bar") == "FOO_BAR_VENV"

    def test_dot_sanitized_to_underscore(self):
        # Any char that isn't a valid shell identifier char must become an
        # underscore so the resulting export line is valid.
        assert venv_env_var_name("foo.bar") == "FOO_BAR_VENV"


class TestExportVenvEnvVar:
    def _make_venv(self, data_dir):
        venv_dir = os.path.join(data_dir, ".venv")
        subprocess.run(
            ["uv", "venv", venv_dir],
            check=True, capture_output=True,
        )
        return venv_dir

    def test_no_op_when_claude_env_file_unset(self, tmp_path, monkeypatch):
        """Returns None and writes nothing when CLAUDE_ENV_FILE is absent."""
        monkeypatch.delenv("CLAUDE_ENV_FILE", raising=False)
        data_dir = str(tmp_path / "data")
        self._make_venv(data_dir)

        assert export_venv_env_var("unreal-kit", data_dir) is None

    def test_no_op_when_claude_env_file_empty(self, tmp_path, monkeypatch):
        """Returns None when CLAUDE_ENV_FILE is set but empty."""
        monkeypatch.setenv("CLAUDE_ENV_FILE", "")
        data_dir = str(tmp_path / "data")
        self._make_venv(data_dir)

        assert export_venv_env_var("unreal-kit", data_dir) is None

    def test_no_op_when_venv_missing(self, tmp_path, monkeypatch):
        """Returns None when the venv python binary does not exist."""
        env_file = tmp_path / "env"
        env_file.write_text("")
        monkeypatch.setenv("CLAUDE_ENV_FILE", str(env_file))

        assert export_venv_env_var("unreal-kit", str(tmp_path / "no-data")) is None
        assert env_file.read_text() == ""  # nothing written

    def test_writes_export_when_venv_exists(self, tmp_path, monkeypatch):
        """Appends a correct export line for the venv python binary."""
        env_file = tmp_path / "env"
        env_file.write_text("")
        monkeypatch.setenv("CLAUDE_ENV_FILE", str(env_file))

        data_dir = str(tmp_path / "data")
        venv_dir = self._make_venv(data_dir)

        var_name = export_venv_env_var("unreal-kit", data_dir)

        assert var_name == "UNREAL_KIT_VENV"
        contents = env_file.read_text()
        assert "export UNREAL_KIT_VENV=" in contents
        # path should point inside the venv and be a real file
        # (extract the quoted path from the export line)
        line = contents.strip().splitlines()[-1]
        value = line.split("=", 1)[1].strip("'\"")
        assert os.path.isfile(value)
        assert venv_dir in value

    def test_appends_preserves_existing_content(self, tmp_path, monkeypatch):
        """Existing export lines are preserved; new line appended."""
        env_file = tmp_path / "env"
        env_file.write_text("export EXISTING=1\n")
        monkeypatch.setenv("CLAUDE_ENV_FILE", str(env_file))

        data_dir = str(tmp_path / "data")
        self._make_venv(data_dir)

        export_venv_env_var("plugins-kit", data_dir)

        contents = env_file.read_text()
        assert "export EXISTING=1" in contents
        assert "export PLUGINS_KIT_VENV=" in contents

    def test_multiple_plugins_produce_multiple_exports(self, tmp_path, monkeypatch):
        """Each plugin produces its own export line."""
        env_file = tmp_path / "env"
        env_file.write_text("")
        monkeypatch.setenv("CLAUDE_ENV_FILE", str(env_file))

        data_a = str(tmp_path / "a")
        data_b = str(tmp_path / "b")
        self._make_venv(data_a)
        self._make_venv(data_b)

        assert export_venv_env_var("alpha", data_a) == "ALPHA_VENV"
        assert export_venv_env_var("beta-kit", data_b) == "BETA_KIT_VENV"

        contents = env_file.read_text()
        assert "export ALPHA_VENV=" in contents
        assert "export BETA_KIT_VENV=" in contents

    def test_path_with_spaces_is_shell_quoted(self, tmp_path, monkeypatch):
        """Paths with spaces are safely quoted so shells don't split them."""
        env_file = tmp_path / "env"
        env_file.write_text("")
        monkeypatch.setenv("CLAUDE_ENV_FILE", str(env_file))

        data_dir = tmp_path / "has space"
        data_dir.mkdir()
        self._make_venv(str(data_dir))

        export_venv_env_var("space-plugin", str(data_dir))

        line = env_file.read_text().strip()
        # shlex.quote wraps in single quotes when spaces are present
        assert "'" in line
        # Parse the export the way a POSIX shell would: the quoted path must come
        # back as ONE token (space preserved), resolving to the real venv python.
        value = _sourced_value(env_file, "SPACE_PLUGIN_VENV")
        assert value is not None
        assert "has space" in value
        assert os.path.isfile(value)


class TestFindUv:
    def test_finds_uv_on_path(self):
        # uv is a hard prerequisite of this repo's test environment.
        assert find_uv() is not None

    def test_falls_back_to_local_bin(self, tmp_path, monkeypatch):
        local_bin = tmp_path / ".local" / "bin"
        local_bin.mkdir(parents=True)
        fake_uv = local_bin / "uv"
        fake_uv.write_text("#!/bin/sh\n")
        monkeypatch.setattr("bootstrap_lib.venv_check.shutil.which", lambda name: None)
        monkeypatch.setattr(
            "bootstrap_lib.venv_check.os.path.expanduser",
            lambda p: str(tmp_path / p[2:]) if p.startswith("~/") else p,
        )
        assert find_uv() == str(fake_uv)

    def test_none_when_absent(self, tmp_path, monkeypatch):
        monkeypatch.setattr("bootstrap_lib.venv_check.shutil.which", lambda name: None)
        monkeypatch.setattr(
            "bootstrap_lib.venv_check.os.path.expanduser",
            lambda p: str(tmp_path / p[2:]) if p.startswith("~/") else p,
        )
        assert find_uv() is None


class TestEnsureVenv:
    """The single shared venv remediation path (B9)."""

    def _passing_result(self, venv_path):
        from bootstrap_lib.result import Result
        return Result(passed=True, subject=venv_path, message="venv ok (0 imports verified)")

    def _failing_result(self, venv_path):
        from bootstrap_lib.result import Result
        return Result(passed=False, subject=venv_path, message="venv not found",
                      remediation_cmd="uv sync --project p")

    def test_passing_no_sync_no_entries(self, tmp_path, monkeypatch):
        """When the check passes and always_sync is off, nothing runs."""
        venv_path = str(tmp_path / ".venv")
        monkeypatch.setattr("bootstrap_lib.venv_check.check_venv",
                            lambda d, r, i: self._passing_result(venv_path))
        ran = []
        monkeypatch.setattr("bootstrap_lib.venv_check.subprocess.run",
                            lambda *a, **k: ran.append(a))
        result, entries = ensure_venv(str(tmp_path), venv_path)
        assert result.passed
        assert entries == []
        assert ran == []

    def test_failing_check_runs_sync_and_logs(self, tmp_path, monkeypatch):
        """A failing check triggers uv sync; remediation is logged."""
        venv_path = str(tmp_path / ".venv")
        states = [self._failing_result(venv_path), self._passing_result(venv_path)]
        monkeypatch.setattr("bootstrap_lib.venv_check.check_venv",
                            lambda d, r, i: states.pop(0))
        monkeypatch.setattr("bootstrap_lib.venv_check.find_uv", lambda: "/fake/uv")

        class _Proc:
            returncode = 0
            stderr = b""
        calls = []
        monkeypatch.setattr("bootstrap_lib.venv_check.subprocess.run",
                            lambda cmd, **k: calls.append((cmd, k)) or _Proc())

        result, entries = ensure_venv(str(tmp_path / "proj"), venv_path, extras=["dev"])
        assert result.passed
        assert any("not ready, running" in e for e in entries)
        assert any(e == "created" for e in entries)
        cmd, kwargs = calls[0]
        assert cmd[:2] == ["/fake/uv", "sync"]
        assert "--extra" in cmd and "dev" in cmd
        assert kwargs["env"]["UV_PROJECT_ENVIRONMENT"] == venv_path

    def test_sync_failure_surfaces_stderr(self, tmp_path, monkeypatch):
        """uv sync errors are logged with exit code + stderr (B8: never swallowed)."""
        venv_path = str(tmp_path / ".venv")
        monkeypatch.setattr("bootstrap_lib.venv_check.check_venv",
                            lambda d, r, i: self._failing_result(venv_path))
        monkeypatch.setattr("bootstrap_lib.venv_check.find_uv", lambda: "/fake/uv")

        class _Proc:
            returncode = 2
            stderr = b"No pyproject.toml found"
        monkeypatch.setattr("bootstrap_lib.venv_check.subprocess.run",
                            lambda cmd, **k: _Proc())

        result, entries = ensure_venv(str(tmp_path / "proj"), venv_path)
        assert not result.passed
        assert any("uv sync failed (exit 2)" in e and "No pyproject.toml" in e for e in entries)

    def test_sync_exception_surfaces(self, tmp_path, monkeypatch):
        """A subprocess exception is logged, not swallowed (B8)."""
        venv_path = str(tmp_path / ".venv")
        monkeypatch.setattr("bootstrap_lib.venv_check.check_venv",
                            lambda d, r, i: self._failing_result(venv_path))
        monkeypatch.setattr("bootstrap_lib.venv_check.find_uv", lambda: "/fake/uv")

        def _boom(cmd, **k):
            raise OSError("exec failed")
        monkeypatch.setattr("bootstrap_lib.venv_check.subprocess.run", _boom)

        result, entries = ensure_venv(str(tmp_path / "proj"), venv_path)
        assert not result.passed
        assert any("uv sync error" in e and "exec failed" in e for e in entries)

    def test_uv_missing_logged(self, tmp_path, monkeypatch):
        venv_path = str(tmp_path / ".venv")
        monkeypatch.setattr("bootstrap_lib.venv_check.check_venv",
                            lambda d, r, i: self._failing_result(venv_path))
        monkeypatch.setattr("bootstrap_lib.venv_check.find_uv", lambda: None)
        result, entries = ensure_venv(str(tmp_path / "proj"), venv_path)
        assert not result.passed
        assert any("uv not found" in e for e in entries)

    def test_always_sync_runs_even_when_passing(self, tmp_path, monkeypatch):
        """Self-setup mode: sync runs on a passing check, silently when clean."""
        venv_path = str(tmp_path / ".venv")
        monkeypatch.setattr("bootstrap_lib.venv_check.check_venv",
                            lambda d, r, i: self._passing_result(venv_path))
        monkeypatch.setattr("bootstrap_lib.venv_check.find_uv", lambda: "/fake/uv")

        class _Proc:
            returncode = 0
            stderr = b""
        ran = []
        monkeypatch.setattr("bootstrap_lib.venv_check.subprocess.run",
                            lambda cmd, **k: ran.append(cmd) or _Proc())

        result, entries = ensure_venv(str(tmp_path / "proj"), venv_path, always_sync=True)
        assert result.passed
        assert entries == []  # clean no-op sync stays silent
        assert ran  # but the sync did run
