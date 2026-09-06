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
    scan_editable_installs,
    site_packages_dirs,
    venv_env_var_name,
    _project_content_hash,
    _venv_sync_stamp_path,
)


def _site_packages(venv_dir):
    """Create and return the Windows-layout site-packages dir of a fake venv."""
    site = venv_dir / "Lib" / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    return site


def _write_direct_pth(site, dist, version, source_dir):
    """Write the bare-path editable shape (setuptools' simple strategy)."""
    pth = site / f"__editable__.{dist}-{version}.pth"
    pth.write_text(f"{source_dir}\n", encoding="utf-8")
    return pth


def _write_finder_pth(site, dist, version, package, source_dir):
    """Write the finder-module editable shape (setuptools' import strategy)."""
    token = f"{dist}_{version.replace('.', '_')}"
    finder = f"__editable___{token}_finder"
    pth = site / f"__editable__.{dist}-{version}.pth"
    pth.write_text(f"import {finder}; {finder}.install()\n", encoding="utf-8")
    (site / f"{finder}.py").write_text(
        "from __future__ import annotations\n"
        f"MAPPING: dict[str, str] = {{{package!r}: {str(source_dir)!r}}}\n"
        "NAMESPACES: dict[str, list[str]] = {}\n"
        "def install():\n    pass\n",
        encoding="utf-8",
    )
    return pth


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


class TestScanEditableInstalls:
    """A plugin venv outlives the version-keyed cache dir it was built against.

    The superseded directory stays on disk, so the stale editable imports
    cleanly and no behavioral check can see it.
    """

    def test_direct_shape_current_is_clean(self, tmp_path):
        cache = tmp_path / "cache" / "0.2.0"
        (cache / "lib").mkdir(parents=True)
        site = _site_packages(tmp_path / ".venv")
        _write_direct_pth(site, "demo_kit", "0.2.0", cache / "lib")

        stale, unreadable = scan_editable_installs(str(tmp_path / ".venv"), str(cache))

        assert stale == []
        assert unreadable == []

    def test_direct_shape_stale_version(self, tmp_path):
        old = tmp_path / "cache" / "0.1.0"
        new = tmp_path / "cache" / "0.2.0"
        (old / "lib").mkdir(parents=True)
        (new / "lib").mkdir(parents=True)
        site = _site_packages(tmp_path / ".venv")
        _write_direct_pth(site, "demo_kit", "0.1.0", old / "lib")

        stale, unreadable = scan_editable_installs(str(tmp_path / ".venv"), str(new))

        assert unreadable == []
        assert len(stale) == 1
        name, detail = stale[0]
        assert name == "__editable__.demo_kit-0.1.0.pth"
        assert "0.1.0" in detail and str(new) in detail

    def test_finder_shape_current_is_clean(self, tmp_path):
        cache = tmp_path / "cache" / "0.2.0"
        (cache / "demo_lib").mkdir(parents=True)
        site = _site_packages(tmp_path / ".venv")
        _write_finder_pth(site, "demo", "0.2.0", "demo_lib", cache / "demo_lib")

        stale, unreadable = scan_editable_installs(str(tmp_path / ".venv"), str(cache))

        assert stale == []
        assert unreadable == []

    def test_finder_shape_stale_version(self, tmp_path):
        old = tmp_path / "cache" / "0.1.0"
        new = tmp_path / "cache" / "0.2.0"
        (old / "demo_lib").mkdir(parents=True)
        (new / "demo_lib").mkdir(parents=True)
        site = _site_packages(tmp_path / ".venv")
        _write_finder_pth(site, "demo", "0.1.0", "demo_lib", old / "demo_lib")

        stale, unreadable = scan_editable_installs(str(tmp_path / ".venv"), str(new))

        assert unreadable == []
        assert [n for n, _ in stale] == ["__editable__.demo-0.1.0.pth"]

    def test_posix_layout_is_scanned(self, tmp_path):
        old = tmp_path / "cache" / "0.1.0"
        new = tmp_path / "cache" / "0.2.0"
        (old / "lib").mkdir(parents=True)
        new.mkdir(parents=True)
        site = tmp_path / ".venv" / "lib" / "python3.12" / "site-packages"
        site.mkdir(parents=True)
        _write_direct_pth(site, "demo_kit", "0.1.0", old / "lib")

        stale, _unreadable = scan_editable_installs(str(tmp_path / ".venv"), str(new))

        assert len(stale) == 1

    def test_equivalent_spelling_is_not_stale(self, tmp_path):
        """Case and separator differences must not read as a moved directory.

        The real-world driver is a linked ``~/.claude``: the same cache dir is
        recorded under several spellings, and a string compare would report
        every plugin on such a machine as stale.
        """
        cache = tmp_path / "cache" / "0.2.0"
        (cache / "lib").mkdir(parents=True)
        site = _site_packages(tmp_path / ".venv")
        recorded = os.path.join(str(cache), ".", "lib")
        _write_direct_pth(site, "demo_kit", "0.2.0", recorded)

        stale, unreadable = scan_editable_installs(str(tmp_path / ".venv"), str(cache))

        assert stale == []
        assert unreadable == []

    def test_unparseable_pth_is_left_alone(self, tmp_path):
        """Fail closed: an unfamiliar shape is a note, never a staleness verdict."""
        cache = tmp_path / "cache" / "0.2.0"
        cache.mkdir(parents=True)
        site = _site_packages(tmp_path / ".venv")
        (site / "__editable__.demo-0.1.0.pth").write_text(
            "import some_unknown_hook; some_unknown_hook.go()\n", encoding="utf-8"
        )

        stale, unreadable = scan_editable_installs(str(tmp_path / ".venv"), str(cache))

        assert stale == []
        assert [n for n, _ in unreadable] == ["__editable__.demo-0.1.0.pth"]

    def test_missing_finder_module_is_unreadable(self, tmp_path):
        cache = tmp_path / "cache" / "0.2.0"
        cache.mkdir(parents=True)
        site = _site_packages(tmp_path / ".venv")
        (site / "__editable__.demo-0.1.0.pth").write_text(
            "import __editable___demo_0_1_0_finder; __editable___demo_0_1_0_finder.install()\n",
            encoding="utf-8",
        )

        stale, unreadable = scan_editable_installs(str(tmp_path / ".venv"), str(cache))

        assert stale == []
        assert "finder module" in unreadable[0][1]

    def test_finder_without_mapping_is_unreadable(self, tmp_path):
        cache = tmp_path / "cache" / "0.2.0"
        cache.mkdir(parents=True)
        site = _site_packages(tmp_path / ".venv")
        (site / "__editable__.demo-0.1.0.pth").write_text(
            "import __editable___demo_0_1_0_finder; __editable___demo_0_1_0_finder.install()\n",
            encoding="utf-8",
        )
        (site / "__editable___demo_0_1_0_finder.py").write_text(
            "def install():\n    pass\n", encoding="utf-8"
        )

        stale, unreadable = scan_editable_installs(str(tmp_path / ".venv"), str(cache))

        assert stale == []
        assert "MAPPING" in unreadable[0][1]

    def test_empty_pth_is_unreadable_not_stale(self, tmp_path):
        cache = tmp_path / "cache" / "0.2.0"
        cache.mkdir(parents=True)
        site = _site_packages(tmp_path / ".venv")
        (site / "__editable__.demo-0.1.0.pth").write_text("\n# comment\n", encoding="utf-8")

        stale, unreadable = scan_editable_installs(str(tmp_path / ".venv"), str(cache))

        assert stale == []
        assert unreadable[0][1] == "no paths recorded"

    def test_shared_lib_pth_is_ignored(self, tmp_path):
        """Only ``__editable__*`` files are in scope; shared-lib links are not."""
        cache = tmp_path / "cache" / "0.2.0"
        cache.mkdir(parents=True)
        site = _site_packages(tmp_path / ".venv")
        (site / "bootstrap_lib.pth").write_text(
            'import sys; sys.path.insert(0, r"D:/elsewhere/bootstrap_lib")\n', encoding="utf-8"
        )

        assert scan_editable_installs(str(tmp_path / ".venv"), str(cache)) == ([], [])

    def test_no_venv_yields_nothing(self, tmp_path):
        assert site_packages_dirs(str(tmp_path / "absent")) == []
        assert scan_editable_installs(str(tmp_path / "absent"), str(tmp_path)) == ([], [])


class TestCheckVenvEditableStaleness:
    """check_venv is where the stale editable turns into a remediable failure."""

    def _fake_venv(self, tmp_path):
        venv_dir = tmp_path / "data" / ".venv"
        subprocess.run(["uv", "venv", str(venv_dir)], check=True, capture_output=True)
        return venv_dir

    def test_stale_editable_fails_the_check(self, tmp_path):
        venv_dir = self._fake_venv(tmp_path)
        old = tmp_path / "cache" / "0.1.0"
        new = tmp_path / "cache" / "0.2.0"
        (old / "lib").mkdir(parents=True)
        new.mkdir(parents=True)
        _write_direct_pth(_site_packages(venv_dir), "demo_kit", "0.1.0", old / "lib")

        result = check_venv(str(tmp_path / "data"), str(new), [])

        assert not result.passed
        assert "stale editable install" in result.message
        assert "uv sync" in result.remediation_cmd

    def test_current_editable_passes(self, tmp_path):
        venv_dir = self._fake_venv(tmp_path)
        cache = tmp_path / "cache" / "0.2.0"
        (cache / "lib").mkdir(parents=True)
        _write_direct_pth(_site_packages(venv_dir), "demo_kit", "0.2.0", cache / "lib")

        result = check_venv(str(tmp_path / "data"), str(cache), [])

        assert result.passed
        assert "0 imports verified" in result.message

    def test_unreadable_editable_passes_with_a_note(self, tmp_path):
        """The note rides the passing message, so it is logged verbose-only."""
        venv_dir = self._fake_venv(tmp_path)
        cache = tmp_path / "cache" / "0.2.0"
        cache.mkdir(parents=True)
        (_site_packages(venv_dir) / "__editable__.demo-0.1.0.pth").write_text(
            "import some_unknown_hook; some_unknown_hook.go()\n", encoding="utf-8"
        )

        result = check_venv(str(tmp_path / "data"), str(cache), [])

        assert result.passed
        assert "editable install left alone" in result.message


class TestEnsureVenvEditableRemediation:
    """A stale editable must produce a visible action entry, not a silent rewrite."""

    def test_stale_editable_syncs_and_logs_an_action(self, tmp_path, monkeypatch):
        from bootstrap_lib.result import Result

        venv_path = str(tmp_path / ".venv")
        os.makedirs(venv_path)
        states = [
            Result(passed=False, subject=venv_path,
                   message="stale editable install: __editable__.demo-0.1.0.pth points at old",
                   remediation_cmd="uv sync --project p"),
            Result(passed=True, subject=venv_path, message="venv ok (0 imports verified)"),
        ]
        monkeypatch.setattr("bootstrap_lib.venv_check.check_venv",
                            lambda d, r, i, extras=(), venv_path=None: states.pop(0))
        monkeypatch.setattr("bootstrap_lib.venv_check.find_uv", lambda: "/fake/uv")

        class _Proc:
            returncode = 0
            stderr = b""

        monkeypatch.setattr("bootstrap_lib.venv_check.subprocess.run",
                            lambda cmd, **k: _Proc())

        result, entries = ensure_venv(str(tmp_path / "proj"), venv_path)

        assert result.passed
        assert any("stale editable install" in e for e in entries)
        assert any(e == "re-synced" for e in entries)


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
                            lambda d, r, i, extras=(), venv_path=None: self._passing_result(venv_path))
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
                            lambda d, r, i, extras=(), venv_path=None: states.pop(0))
        monkeypatch.setattr("bootstrap_lib.venv_check.find_uv", lambda: "/fake/uv")

        class _Proc:
            returncode = 0
            stderr = b""
        calls = []
        monkeypatch.setattr("bootstrap_lib.venv_check.subprocess.run",
                            lambda cmd, **k: calls.append((cmd, k)) or _Proc())

        result, entries = ensure_venv(str(tmp_path / "proj"), venv_path, extras=["dev"])
        assert result.passed
        assert any("not ready, running" in e and "venv not found" in e for e in entries)
        assert any(e == "created" for e in entries)
        cmd, kwargs = calls[0]
        assert cmd[:2] == ["/fake/uv", "sync"]
        assert "--extra" in cmd and "dev" in cmd
        assert kwargs["env"]["UV_PROJECT_ENVIRONMENT"] == venv_path

    def test_sync_failure_surfaces_stderr(self, tmp_path, monkeypatch):
        """uv sync errors are logged with exit code + stderr (B8: never swallowed)."""
        venv_path = str(tmp_path / ".venv")
        monkeypatch.setattr("bootstrap_lib.venv_check.check_venv",
                            lambda d, r, i, extras=(), venv_path=None: self._failing_result(venv_path))
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
                            lambda d, r, i, extras=(), venv_path=None: self._failing_result(venv_path))
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
                            lambda d, r, i, extras=(), venv_path=None: self._failing_result(venv_path))
        monkeypatch.setattr("bootstrap_lib.venv_check.find_uv", lambda: None)
        result, entries = ensure_venv(str(tmp_path / "proj"), venv_path)
        assert not result.passed
        assert any("uv not found" in e for e in entries)

    def test_always_sync_runs_even_when_passing(self, tmp_path, monkeypatch):
        """Self-setup mode: sync runs on a passing check, silently when clean."""
        venv_path = str(tmp_path / ".venv")
        monkeypatch.setattr("bootstrap_lib.venv_check.check_venv",
                            lambda d, r, i, extras=(), venv_path=None: self._passing_result(venv_path))
        monkeypatch.setattr("bootstrap_lib.venv_check.find_uv", lambda: "/fake/uv")

        class _Proc:
            returncode = 1
            stderr = b"sync failed"
        ran = []
        monkeypatch.setattr("bootstrap_lib.venv_check.subprocess.run",
                            lambda cmd, **k: ran.append(cmd) or _Proc())

        result, entries = ensure_venv(str(tmp_path / "proj"), venv_path, always_sync=True)
        assert not result.passed
        assert any("uv sync failed (exit 1)" in entry for entry in entries)
        assert ran  # but the sync did run

    def test_matching_dependency_stamp_skips_sync(self, tmp_path, monkeypatch):
        project = tmp_path / "proj"
        project.mkdir()
        (project / "pyproject.toml").write_text("[project]\nname = 'demo'\n")
        (project / "uv.lock").write_text("version = 1\n")
        venv_path = str(tmp_path / "data" / ".venv")
        monkeypatch.setattr("bootstrap_lib.venv_check.check_venv",
                            lambda d, r, i, extras=(), venv_path=None: self._passing_result(venv_path))
        monkeypatch.setattr("bootstrap_lib.venv_check.subprocess.run",
                            lambda *a, **k: pytest.fail("uv sync must not run"))
        stamp = _venv_sync_stamp_path(venv_path)
        stamp.parent.mkdir(parents=True)
        stamp.write_text(_project_content_hash(str(project)))

        result, entries = ensure_venv(str(project), venv_path)

        assert result.passed
        assert entries == []

    def test_changed_pyproject_runs_sync(self, tmp_path, monkeypatch):
        project = tmp_path / "proj"
        project.mkdir()
        pyproject = project / "pyproject.toml"
        pyproject.write_text("[project]\nname = 'demo'\n")
        venv_path = str(tmp_path / "data" / ".venv")
        stamp = _venv_sync_stamp_path(venv_path)
        stamp.parent.mkdir(parents=True)
        stamp.write_text(_project_content_hash(str(project)))
        pyproject.write_text("[project]\nname = 'changed'\n")
        monkeypatch.setattr("bootstrap_lib.venv_check.check_venv",
                            lambda d, r, i, extras=(), venv_path=None: self._passing_result(venv_path))
        monkeypatch.setattr("bootstrap_lib.venv_check.find_uv", lambda: "/fake/uv")
        class _Proc:
            returncode = 0
            stderr = b""
        ran = []
        monkeypatch.setattr("bootstrap_lib.venv_check.subprocess.run",
                            lambda cmd, **k: ran.append(cmd) or _Proc())

        result, _entries = ensure_venv(str(project), venv_path)

        assert result.passed
        assert ran
        assert stamp.read_text().strip() == _project_content_hash(str(project))

    def test_failed_dependency_sync_does_not_write_stamp(self, tmp_path, monkeypatch):
        project = tmp_path / "proj"
        project.mkdir()
        (project / "pyproject.toml").write_text("[project]\nname = 'demo'\n")
        venv_path = str(tmp_path / "data" / ".venv")
        monkeypatch.setattr("bootstrap_lib.venv_check.check_venv",
                            lambda d, r, i, extras=(), venv_path=None: self._passing_result(venv_path))
        monkeypatch.setattr("bootstrap_lib.venv_check.find_uv", lambda: "/fake/uv")
        class _Proc:
            returncode = 1
            stderr = b"resolution failed"
        monkeypatch.setattr("bootstrap_lib.venv_check.subprocess.run",
                            lambda *a, **k: _Proc())

        result, entries = ensure_venv(str(project), venv_path)

        assert result.passed
        assert any("uv sync failed" in entry for entry in entries)
        assert not _venv_sync_stamp_path(venv_path).exists()

    def test_custom_venv_path_is_checked_and_synced(self, tmp_path, monkeypatch):
        project = tmp_path / "proj"
        project.mkdir()
        venv_path = str(tmp_path / "data" / "env")
        seen = []

        def _fake_check(data, root, imports, extras=(), venv_path=None):
            seen.append(data)
            return self._passing_result(str(tmp_path / "data" / "env"))

        monkeypatch.setattr(
            "bootstrap_lib.venv_check.check_venv",
            _fake_check,
        )
        monkeypatch.setattr("bootstrap_lib.venv_check.find_uv", lambda: "/fake/uv")
        class _Proc:
            returncode = 0
            stderr = b""
        monkeypatch.setattr("bootstrap_lib.venv_check.subprocess.run", lambda *a, **k: _Proc())

        result, _entries = ensure_venv(str(project), venv_path, always_sync=True)

        assert result.passed
        assert seen == [str(tmp_path / "data"), str(tmp_path / "data")]


class TestManifestExtrasReachEnsureVenv:
    """A plugin's declared venv extras must survive the engine's wiring.

    Regression: `_phase_venv` and bootstrap's self-setup call site both dropped
    `extras`, so a plugin declaring `"venv": {"extras": ["sdk"]}` in its
    bootstrap.json had that silently ignored and `uv sync` ran without it. The
    venv then failed its own `check_imports` forever, and the remediation the
    engine printed omitted the extras too, so running it by hand could not fix
    it either. Only the project-venv call site passed them through.
    """

    def _capture(self, monkeypatch):
        seen = {}

        def fake_ensure_venv(project_dir, venv_path, extras=(), check_imports=(),
                             always_sync=False):
            seen["extras"] = list(extras)
            from bootstrap_lib.venv_check import Result
            return Result(subject="venv", passed=True, message="ok"), []

        monkeypatch.setattr("bootstrap_lib.venv_check.ensure_venv", fake_ensure_venv)
        return seen

    def test_phase_venv_forwards_manifest_extras(self, tmp_path, monkeypatch):
        from bootstrap_lib import engine

        seen = self._capture(monkeypatch)

        class Ctx:
            manifest = {"venv": {"extras": ["sdk"], "check_imports": ["openai"]}}
            data_dir = str(tmp_path / "data")
            plugin_root = str(tmp_path / "root")
            prefix = ""
            plugin_name = "llm-scripting-kit"
            action_entries: list = []
            ok_entries: list = []
            failures: list = []

        engine._phase_venv(Ctx())
        assert seen["extras"] == ["sdk"]

    def test_phase_venv_without_extras_passes_empty(self, tmp_path, monkeypatch):
        from bootstrap_lib import engine

        seen = self._capture(monkeypatch)

        class Ctx:
            manifest = {"venv": {"check_imports": ["yaml"]}}
            data_dir = str(tmp_path / "data")
            plugin_root = str(tmp_path / "root")
            prefix = ""
            plugin_name = "some-plugin"
            action_entries: list = []
            ok_entries: list = []
            failures: list = []

        engine._phase_venv(Ctx())
        assert seen["extras"] == []

    def test_reported_remediation_includes_extras(self, tmp_path):
        from bootstrap_lib.venv_check import check_venv

        result = check_venv(str(tmp_path / "data"), str(tmp_path / "root"),
                            ["openai"], extras=["sdk"])
        assert not result.passed
        assert "--extra sdk" in result.remediation_cmd
