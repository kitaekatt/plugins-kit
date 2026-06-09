"""Tests for venv_check.py — Python venv validation."""

import os
import shlex
import stat
import subprocess
from unittest.mock import patch

import pytest

from bootstrap_lib.venv_check import (
    VenvCheckResult,
    check_venv,
    export_venv_env_var,
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
