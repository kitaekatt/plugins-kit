"""Tests for venv_check.py — Python venv validation."""

import os
import stat
import subprocess
import time
from unittest.mock import patch

import pytest

from bootstrap_lib import session_env
from bootstrap_lib.venv_check import (
    check_venv,
    ensure_venv,
    find_uv,
    installed_version,
    load_released_ledger,
    package_targets,
    parse_restricted_requirement,
    scan_editable_installs,
    site_packages_dirs,
    _project_content_hash,
    _venv_sync_stamp_path,
)


@pytest.fixture(autouse=True)
def _clean_session_env_buffer():
    """session_env buffers across calls, so no test may inherit another's."""
    session_env.reset()
    yield
    session_env.reset()


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

    def test_engine_logs_sync_mechanics_but_displays_only_summary(
        self, tmp_path, monkeypatch
    ):
        from bootstrap_lib.engine import _process_venv_def
        from bootstrap_lib.log import write_log_block
        from bootstrap_lib.messages import ITEM_MAX, numbered
        from bootstrap_lib.result import Result

        detail = (
            "not ready, running `uv sync --project C:\\Users\\truff\\cache\\job-kit` - "
            "stale editable install: __editable__.job_kit-0.4.0.pth points at "
            "D:\\Dev\\cache\\job-kit\\0.4.0\\lib, expected under "
            "C:\\Users\\truff\\cache\\job-kit\\0.4.1"
        )
        passed = Result(
            passed=True,
            subject=str(tmp_path / ".venv"),
            message="venv ok (1 import verified)",
        )
        monkeypatch.setattr(
            "bootstrap_lib.venv_check.ensure_venv",
            lambda *args, **kwargs: (passed, [detail, "re-synced"]),
        )
        actions, quiet, oks, failures = [], [], [], []

        _process_venv_def(
            {"check_imports": ["job_kit"]},
            str(tmp_path / "data"),
            str(tmp_path / "plugin"),
            "",
            "venv",
            actions,
            oks,
            failures,
            plugin_name="job-kit",
            quiet_entries=quiet,
        )
        write_log_block(str(tmp_path / "log"), "job-kit", actions + quiet)
        display = numbered(actions)
        log_text = (tmp_path / "log" / "bootstrap.log").read_text()

        assert detail in log_text
        assert "uv sync --project" not in display
        assert "stale editable install" not in display
        assert display == "venv: re-synced"
        assert len(display) <= ITEM_MAX
        assert failures == []


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
                             always_sync=False, released_ledger=None):
            seen["extras"] = list(extras)
            seen["released_ledger"] = released_ledger
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
            marketplace = "plugins-kit"
            action_entries: list = []
            ok_entries: list = []
            quiet_entries: list = []
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
            marketplace = "plugins-kit"
            action_entries: list = []
            ok_entries: list = []
            quiet_entries: list = []
            failures: list = []

        engine._phase_venv(Ctx())
        assert seen["extras"] == []

    def test_reported_remediation_includes_extras(self, tmp_path):
        from bootstrap_lib.venv_check import check_venv

        result = check_venv(str(tmp_path / "data"), str(tmp_path / "root"),
                            ["openai"], extras=["sdk"])
        assert not result.passed
        assert "--extra sdk" in result.remediation_cmd


# --- released-package targets (U6) ----------------------------------------

_LLM = "katsoft-llm-kit"


def _ledger(path, **dists):
    """Write a released.json: dists maps distribution -> [version, ...]."""
    import json
    data = {d: {v: {"version": v} for v in versions} for d, versions in dists.items()}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


def _project(tmp_path, *requirements, optional=None):
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    deps = ", ".join(repr(r) for r in requirements)
    text = f"[project]\nname = 'demo'\nversion = '1.0.0'\ndependencies = [{deps}]\n"
    for extra, reqs in (optional or {}).items():
        text += f"[project.optional-dependencies]\n{extra} = [{', '.join(repr(r) for r in reqs)}]\n"
    (project / "pyproject.toml").write_text(text, encoding="utf-8")
    return project


def _install(venv_path, dist, version):
    """Record `dist` as installed the way the installer does (a dist-info dir)."""
    site = os.path.join(venv_path, "Lib", "site-packages")
    for stale in [e for e in (os.listdir(site) if os.path.isdir(site) else [])
                  if e.startswith(dist.replace("-", "_") + "-")]:
        os.rmdir(os.path.join(site, stale))
    os.makedirs(os.path.join(site, f"{dist.replace('-', '_')}-{version}.dist-info"))


class TestRestrictedRequirementForm:
    def test_restricted_form_parser(self):
        assert parse_restricted_requirement("katsoft-llm-kit>=1.1,<2") == (
            "katsoft-llm-kit", (1, 1, 0), 2)
        assert parse_restricted_requirement("katsoft_llm_kit[sdk, jobs] >= 1.2.3 , < 3") == (
            "katsoft_llm_kit", (1, 2, 3), 3)
        for bad in (
            "katsoft-llm-kit",                    # no range
            "katsoft-llm-kit>=1",                 # lower bound needs A.B
            "katsoft-llm-kit>=1.1",               # no upper bound
            "katsoft-llm-kit>=1.1,<2.0",          # upper bound is a bare major
            "katsoft-llm-kit<2,>=1.1",            # fixed order
            "katsoft-llm-kit==1.1.0",
            "katsoft-llm-kit~=1.1",
            "katsoft-llm-kit>=1.1,<2; python_version>'3.8'",
            "katsoft-llm-kit @ https://example.invalid/x.whl",
            "katsoft-llm-kit>=1.1rc1,<2",
            "",
        ):
            assert parse_restricted_requirement(bad) is None, bad

    def test_targets_are_the_highest_admitted_release(self, tmp_path):
        project = _project(tmp_path, f"{_LLM}[sdk]>=1.1,<2", "openai>=1.0")
        ledger = _ledger(tmp_path / "m" / "released.json",
                         **{_LLM: ["1.0.0", "1.1.0", "1.4.2", "1.10.0", "2.0.0", "1.5.0rc1"]})
        targets, errors = package_targets(str(project), ledger)
        assert errors == []
        assert targets == [(_LLM, "1.10.0")]   # numeric order; 2.0.0 and the rc excluded

    def test_extras_requirements_count_only_when_the_extra_is_installed(self, tmp_path):
        project = _project(tmp_path, "openai>=1.0",
                           optional={"sdk": [f"{_LLM}>=1.1,<2"]})
        ledger = _ledger(tmp_path / "m" / "released.json", **{_LLM: ["1.1.0", "1.2.0"]})
        assert package_targets(str(project), ledger) == ([], [])
        assert package_targets(str(project), ledger, ["sdk"]) == ([(_LLM, "1.2.0")], [])

    def test_no_ledger_or_no_registered_requirement_means_no_targets(self, tmp_path):
        project = _project(tmp_path, "openai>=1.0")
        assert package_targets(str(project), str(tmp_path / "absent.json")) == ([], [])
        ledger = _ledger(tmp_path / "m" / "released.json", **{_LLM: ["1.1.0"]})
        assert package_targets(str(project), ledger) == ([], [])

    def test_unparseable_requirement_fails_loudly(self, tmp_path):
        """Loosening the parser to skip an unrecognized form turns this red: the
        requirement would silently get no target."""
        ledger = _ledger(tmp_path / "m" / "released.json", **{_LLM: ["1.1.0"]})
        for text in (_LLM, f"{_LLM}~=1.1", f"{_LLM}>=1.1", f"{_LLM}==1.1.0"):
            project = _project(tmp_path, text)
            targets, errors = package_targets(str(project), ledger)
            assert targets == [], text
            assert len(errors) == 1 and repr(text) in errors[0], (text, errors)
            assert "name[extras]>=A.B[.C],<N" in errors[0]

    def test_range_no_release_satisfies_is_an_error(self, tmp_path):
        project = _project(tmp_path, f"{_LLM}>=1.5,<2")
        ledger = _ledger(tmp_path / "m" / "released.json", **{_LLM: ["1.0.0", "1.4.0"]})
        targets, errors = package_targets(str(project), ledger)
        assert targets == [] and "no released version of katsoft-llm-kit" in errors[0]

    def test_unreadable_ledger_is_an_error_not_a_skip(self, tmp_path):
        bad = tmp_path / "released.json"
        bad.write_text("{not json", encoding="utf-8")
        assert load_released_ledger(str(bad))[1].startswith("released-package ledger")
        bad.write_text('{"d": {"1.0.0": "not an entry"}}', encoding="utf-8")
        assert "distribution -> version -> entry" in load_released_ledger(str(bad))[1]
        project = _project(tmp_path, f"{_LLM}>=1.1,<2")
        assert package_targets(str(project), str(bad))[1]

    def test_installed_version_reads_the_dist_info(self, tmp_path):
        venv = str(tmp_path / ".venv")
        assert installed_version(venv, _LLM) is None
        _install(venv, _LLM, "1.3.0")
        assert installed_version(venv, _LLM) == "1.3.0"
        assert installed_version(venv, "Katsoft.LLM_kit") == "1.3.0"   # normalized name
        assert installed_version(venv, "katsoft-contracts") is None


class TestEnsureVenvReleasedTargets:
    """ensure_venv holds each registered package at its ledger target."""

    def _wire(self, tmp_path, monkeypatch, *, requirement=f"{_LLM}>=1.1,<2",
              versions=("1.1.0", "1.2.0"), sync_installs=None, sync_rc=0):
        """A project + ledger, a passing check, and a fake `uv sync`.

        `sync_installs` maps dist -> version the fake sync leaves installed
        (what a real uv would do for the -P pins it is given); None installs
        whatever `-P dist==v` asks for.
        """
        from bootstrap_lib.result import Result
        project = _project(tmp_path, requirement)
        ledger = _ledger(tmp_path / "mkt" / "packages" / "released.json", **{_LLM: list(versions)})
        venv_path = str(tmp_path / "data" / ".venv")
        os.makedirs(venv_path)
        monkeypatch.setattr(
            "bootstrap_lib.venv_check.check_venv",
            lambda d, r, i, extras=(), venv_path=None: Result(
                passed=True, subject=venv_path, message="venv ok"))
        monkeypatch.setattr("bootstrap_lib.venv_check.find_uv", lambda: "/fake/uv")
        ran = []

        class _Proc:
            returncode = sync_rc
            stderr = b"No solution found" if sync_rc else b""

        def _run(cmd, **kw):
            ran.append(cmd)
            if sync_rc == 0:
                pins = [c.split("==") for c in cmd if "==" in c]
                for dist, ver in pins:
                    _install(venv_path, dist,
                             (sync_installs or {}).get(dist, ver))
            return _Proc()

        monkeypatch.setattr("bootstrap_lib.venv_check.subprocess.run", _run)
        return project, ledger, venv_path, ran

    def test_target_change_changes_stamp(self, tmp_path):
        """Dropping the fold from `_project_content_hash` turns this red."""
        project = _project(tmp_path, f"{_LLM}>=1.1,<2")
        base = _project_content_hash(str(project))
        a = _project_content_hash(str(project), [(_LLM, "1.1.0")])
        b = _project_content_hash(str(project), [(_LLM, "1.2.0")])
        assert len({base, a, b}) == 3
        assert a == _project_content_hash(str(project), [(_LLM, "1.1.0")])

    def test_no_targets_hashes_exactly_as_before(self, tmp_path):
        """The fold must not re-sync a single venv that has no registered package."""
        import hashlib
        project = _project(tmp_path, "openai>=1.0")
        (project / "uv.lock").write_text("version = 1\n")
        legacy = hashlib.sha256()
        for name in ("pyproject.toml", "uv.lock"):
            legacy.update(name.encode() + b"\0")
            legacy.update((project / name).read_bytes() + b"\0")
        assert _project_content_hash(str(project)) == legacy.hexdigest()
        assert _project_content_hash(str(project), []) == legacy.hexdigest()

    def test_behind_venv_syncs_with_upgrade_package_pin(self, tmp_path, monkeypatch):
        """Without the `-P` pin, or without the `behind` trigger, this is red:
        the stamp is current and the venv passes, yet it holds 1.1.0."""
        project, ledger, venv_path, ran = self._wire(tmp_path, monkeypatch)
        _install(venv_path, _LLM, "1.1.0")
        # Stamp is current for the TARGET, so only the version gap can trigger a sync.
        stamp = _venv_sync_stamp_path(venv_path)
        stamp.write_text(_project_content_hash(str(project), [(_LLM, "1.2.0")]))

        result, entries = ensure_venv(str(project), venv_path, released_ledger=ledger)

        assert result.passed, result.message
        assert len(ran) == 1
        assert ran[0][:4] == ["/fake/uv", "sync", "--project", str(project)]
        assert ran[0][4:] == ["-P", f"{_LLM}==1.2.0"]
        assert installed_version(venv_path, _LLM) == "1.2.0"
        assert "re-synced" in entries
        assert any("installed 1.1.0, released target 1.2.0" in e for e in entries)
        assert stamp.read_text().strip() == _project_content_hash(
            str(project), [(_LLM, "1.2.0")])

    def test_venv_at_target_with_current_stamp_does_not_sync(self, tmp_path, monkeypatch):
        project, ledger, venv_path, ran = self._wire(tmp_path, monkeypatch)
        _install(venv_path, _LLM, "1.2.0")
        _venv_sync_stamp_path(venv_path).write_text(
            _project_content_hash(str(project), [(_LLM, "1.2.0")]))

        result, entries = ensure_venv(str(project), venv_path, released_ledger=ledger)

        assert result.passed and entries == [] and ran == []

    def test_new_release_alone_triggers_the_sync(self, tmp_path, monkeypatch):
        """A release inside the range changes nothing in pyproject.toml or uv.lock."""
        project, ledger, venv_path, ran = self._wire(tmp_path, monkeypatch)
        _install(venv_path, _LLM, "1.2.0")
        _venv_sync_stamp_path(venv_path).write_text(
            _project_content_hash(str(project), [(_LLM, "1.2.0")]))
        _ledger(tmp_path / "mkt" / "packages" / "released.json",
                **{_LLM: ["1.1.0", "1.2.0", "1.3.0"]})

        result, _entries = ensure_venv(str(project), venv_path, released_ledger=ledger)

        assert result.passed and ran and ran[0][-2:] == ["-P", f"{_LLM}==1.3.0"]
        assert installed_version(venv_path, _LLM) == "1.3.0"

    def test_stale_simple_index_resolution_is_retried_once(self, tmp_path, monkeypatch):
        project, ledger, venv_path, _ran = self._wire(
            tmp_path, monkeypatch, versions=("1.1.0", "1.2.0"))
        _install(venv_path, _LLM, "1.1.0")
        ran = []
        failures = [
            b"No solution found: only katsoft-llm-kit[config]<=1.1.0 is available",
            b"",
        ]

        class _Proc:
            def __init__(self, returncode, stderr):
                self.returncode = returncode
                self.stderr = stderr

        def _run(cmd, **kw):
            ran.append(cmd)
            stderr = failures.pop(0)
            if not stderr:
                _install(venv_path, _LLM, "1.2.0")
            return _Proc(1 if stderr else 0, stderr)

        monkeypatch.setattr("bootstrap_lib.venv_check.subprocess.run", _run)
        delays = []
        monkeypatch.setattr(time, "sleep", delays.append)

        result, entries = ensure_venv(str(project), venv_path, released_ledger=ledger)

        assert result.passed, result.message
        assert len(ran) == 2
        assert ran[0] == ran[1]
        assert delays == [2]
        assert installed_version(venv_path, _LLM) == "1.2.0"
        assert any("stale released-package index" in entry for entry in entries)

    def test_major_release_is_not_adopted(self, tmp_path, monkeypatch):
        project, ledger, venv_path, ran = self._wire(
            tmp_path, monkeypatch, versions=("1.1.0", "1.2.0", "2.0.0"))
        _install(venv_path, _LLM, "1.2.0")
        _venv_sync_stamp_path(venv_path).write_text(
            _project_content_hash(str(project), [(_LLM, "1.2.0")]))

        result, entries = ensure_venv(str(project), venv_path, released_ledger=ledger)

        assert result.passed and ran == [] and entries == []

    def test_target_miss_is_failed_no_stamp(self, tmp_path, monkeypatch):
        """Treating a failed pinned sync as the old passing venv turns this red."""
        project, ledger, venv_path, ran = self._wire(
            tmp_path, monkeypatch, sync_rc=1)
        _install(venv_path, _LLM, "1.1.0")

        result, entries = ensure_venv(str(project), venv_path, released_ledger=ledger)

        assert not result.passed
        assert "katsoft-llm-kit 1.2.0" in result.message
        assert result.remediation_cmd == "bootstrap run"
        assert any("uv sync failed (exit 1)" in e for e in entries)
        assert not _venv_sync_stamp_path(venv_path).exists()

    def test_sync_that_leaves_the_wrong_version_is_failed_no_stamp(self, tmp_path, monkeypatch):
        project, ledger, venv_path, ran = self._wire(
            tmp_path, monkeypatch, sync_installs={_LLM: "1.1.0"})
        _install(venv_path, _LLM, "1.1.0")

        result, entries = ensure_venv(str(project), venv_path, released_ledger=ledger)

        assert not result.passed
        assert "is 1.1.0, target 1.2.0" in result.message
        assert not _venv_sync_stamp_path(venv_path).exists()

    def test_unparseable_requirement_is_failed_even_when_the_venv_is_healthy(
            self, tmp_path, monkeypatch):
        project, ledger, venv_path, ran = self._wire(
            tmp_path, monkeypatch, requirement=f"{_LLM}~=1.1")
        stamp = _venv_sync_stamp_path(venv_path)
        stamp.write_text(_project_content_hash(str(project)) + "\n")   # current

        result, entries = ensure_venv(str(project), venv_path, released_ledger=ledger)

        assert not result.passed
        assert "name[extras]>=A.B[.C],<N" in result.message
        assert ran == []
        assert any("name[extras]>=A.B[.C],<N" in e for e in entries)

    def test_unparseable_requirement_never_writes_a_stamp_after_a_sync(
            self, tmp_path, monkeypatch):
        project, ledger, venv_path, ran = self._wire(
            tmp_path, monkeypatch, requirement=f"{_LLM}~=1.1")
        always = ensure_venv(str(project), venv_path, always_sync=True,
                             released_ledger=ledger)
        assert ran and not always[0].passed
        assert not _venv_sync_stamp_path(venv_path).exists()

    def test_unresolved_target_fails_when_uv_is_missing(self, tmp_path, monkeypatch):
        """Passing check, stale stamp, nothing behind, unresolved target, no uv:
        the `uv not found` return used to hand back the passing result."""
        project, ledger, venv_path, ran = self._wire(
            tmp_path, monkeypatch, requirement=f"{_LLM}~=1.1")
        monkeypatch.setattr("bootstrap_lib.venv_check.find_uv", lambda: None)
        assert not _venv_sync_stamp_path(venv_path).exists()   # stale: content_changed

        result, entries = ensure_venv(str(project), venv_path, released_ledger=ledger)

        assert not result.passed
        assert "name[extras]>=A.B[.C],<N" in result.message
        assert result.remediation_cmd == "bootstrap run"
        assert any("uv not found" in e for e in entries)
        assert any("name[extras]>=A.B[.C],<N" in e for e in entries)
        assert ran == []
        assert not _venv_sync_stamp_path(venv_path).exists()

    def test_unresolved_target_fails_when_uv_cannot_be_run(self, tmp_path, monkeypatch):
        """Same state, but launching uv raises: the `uv sync error` return."""
        project, ledger, venv_path, ran = self._wire(
            tmp_path, monkeypatch, requirement=f"{_LLM}~=1.1")

        def _boom(cmd, **kw):
            raise OSError("exec failed")

        monkeypatch.setattr("bootstrap_lib.venv_check.subprocess.run", _boom)

        result, entries = ensure_venv(str(project), venv_path, released_ledger=ledger)

        assert not result.passed
        assert result.remediation_cmd == "bootstrap run"
        assert any("uv sync error" in e for e in entries)
        assert not _venv_sync_stamp_path(venv_path).exists()

    def test_without_a_ledger_nothing_changes(self, tmp_path, monkeypatch):
        project, _ledger_path, venv_path, ran = self._wire(tmp_path, monkeypatch)
        _venv_sync_stamp_path(venv_path).write_text(_project_content_hash(str(project)))

        result, entries = ensure_venv(str(project), venv_path)

        assert result.passed and entries == [] and ran == []

    def test_real_uv_moves_a_behind_venv_to_the_ledger_target(self, tmp_path, monkeypatch):
        """End to end against the installed uv and two local wheels (no network):
        the plain venv holds 1.1.0, the ledger says 1.2.0, and `-P name==version`
        is what moves it."""
        import zipfile
        uv = find_uv()
        if not uv:
            pytest.skip("uv not available")
        wheels = tmp_path / "wheels"
        wheels.mkdir()
        for version in ("1.1.0", "1.2.0"):
            name = f"relprobe-{version}-py3-none-any.whl"
            info = f"relprobe-{version}.dist-info"
            with zipfile.ZipFile(wheels / name, "w") as z:
                z.writestr("relprobe/__init__.py", f"VERSION = '{version}'\n")
                z.writestr(f"{info}/METADATA",
                           f"Metadata-Version: 2.1\nName: relprobe\nVersion: {version}\n")
                z.writestr(f"{info}/WHEEL",
                           "Wheel-Version: 1.0\nGenerator: t\nRoot-Is-Purelib: true\n"
                           "Tag: py3-none-any\n")
                z.writestr(f"{info}/RECORD", "")
        project = tmp_path / "proj"
        project.mkdir()
        (project / "pyproject.toml").write_text(
            "[project]\nname = 'demo'\nversion = '0'\nrequires-python = '>=3.12'\n"
            "dependencies = ['relprobe>=1.1,<2']\n[tool.uv]\npackage = false\n",
            encoding="utf-8")
        monkeypatch.setenv("UV_NO_INDEX", "1")
        monkeypatch.setenv("UV_FIND_LINKS", str(wheels))
        venv_path = str(tmp_path / "data" / ".venv")
        # Install 1.1.0 first by hiding 1.2.0, then reveal it and publish it in the ledger.
        hidden = wheels / "relprobe-1.2.0-py3-none-any.whl"
        hidden.rename(tmp_path / "relprobe-1.2.0-py3-none-any.whl")
        first, entries = ensure_venv(str(project), venv_path)
        assert first.passed, entries
        assert installed_version(venv_path, "relprobe") == "1.1.0"
        (tmp_path / "relprobe-1.2.0-py3-none-any.whl").rename(hidden)
        ledger = _ledger(tmp_path / "mkt" / "released.json", relprobe=["1.1.0", "1.2.0"])

        result, entries = ensure_venv(str(project), venv_path, released_ledger=ledger)

        assert result.passed, entries
        assert installed_version(venv_path, "relprobe") == "1.2.0", entries
        assert "re-synced" in entries


class TestPhaseVenvLedgerPlumbing:
    def test_phase_venv_passes_the_marketplace_clone_ledger(self, tmp_path, monkeypatch):
        from bootstrap_lib import engine

        monkeypatch.delenv("CLAUDE_BOOTSTRAP_DATA_ROOT", raising=False)
        monkeypatch.setattr(
            "bootstrap_lib.marketplace_lifecycle.marketplace_install_location",
            lambda name: str(tmp_path / "clones" / name))
        seen = {}

        def fake_ensure_venv(project_dir, venv_path, extras=(), check_imports=(),
                             always_sync=False, released_ledger=None):
            seen["ledger"] = released_ledger
            from bootstrap_lib.venv_check import Result
            return Result(subject="venv", passed=True, message="ok"), []

        monkeypatch.setattr("bootstrap_lib.venv_check.ensure_venv", fake_ensure_venv)

        class Ctx:
            manifest = {"venv": {}}
            data_dir = str(tmp_path / "data")
            plugin_root = str(tmp_path / "root")
            plugin_name = "git-kit"
            marketplace = "plugins-kit"
            action_entries: list = []
            ok_entries: list = []
            quiet_entries: list = []
            failures: list = []

        engine._phase_venv(Ctx())
        assert seen["ledger"] == str(tmp_path / "clones" / "plugins-kit" / "packages" / "released.json")

    def test_redirected_pass_reads_the_working_tree_ledger(self, tmp_path, monkeypatch):
        from bootstrap_lib import engine

        monkeypatch.setenv("CLAUDE_BOOTSTRAP_DATA_ROOT", str(tmp_path / "redirected"))
        plugin_root = tmp_path / "repo" / "plugins" / "git-kit"
        assert engine._released_ledger_path("plugins-kit", str(plugin_root)) == str(
            tmp_path / "repo" / "packages" / "released.json")

    def test_layered_manifest_venv_is_not_held_to_a_ledger(self, tmp_path, monkeypatch):
        from bootstrap_lib import engine

        seen = {}

        def fake_ensure_venv(project_dir, venv_path, extras=(), check_imports=(),
                             always_sync=False, released_ledger=None):
            seen["ledger"] = released_ledger
            from bootstrap_lib.venv_check import Result
            return Result(subject="venv", passed=True, message="ok"), []

        monkeypatch.setattr("bootstrap_lib.venv_check.ensure_venv", fake_ensure_venv)

        class Ctx:
            manifest = {"venv": {}}
            data_dir = str(tmp_path / "data")
            plugin_root = str(tmp_path / "root")
            plugin_name = "config"
            marketplace = "data"
            action_entries: list = []
            ok_entries: list = []
            quiet_entries: list = []
            failures: list = []

        engine._phase_venv(Ctx())
        assert seen["ledger"] is None
