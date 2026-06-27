"""Tests for bootstrap_lib/harvest.py — the single-session update harvest.

Mocks all external effects (no real engine launch, no real plugin installs).
Pins the harvest DECISION logic (installed > ran triggers a launch; == / < do
not), the launch invocation (new installPath, cooldown cleared), and the
per-installed-version dedup guard.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from bootstrap_lib import harvest
from bootstrap_lib.harvest import (
    read_installed_bootstrap,
    run_harvest,
    should_harvest,
)
from bootstrap_lib.stamps import global_stamp, project_stamp


def _registry(tmp_path, plugins):
    p = tmp_path / "installed_plugins.json"
    p.write_text(json.dumps({"plugins": plugins}), encoding="utf-8")
    return str(p)


HARVEST_PY = Path(__file__).resolve().parents[2] / "plugins" / "bootstrap" / "bootstrap_lib" / "harvest.py"


class TestScriptInvocation:
    """harvest.py must work when EXECUTED AS A SCRIPT (`python harvest.py`) — the
    way the UserPromptSubmit hook invokes it — not only when imported as a module.

    Regression: in-function relative imports (`from .stamps import ...`) raise
    "attempted relative import with no known parent package" under script
    execution (no package context), which made run_harvest throw and the hook
    silently no-op — the harvest never fired in production despite the module-
    level unit tests passing. Run it as a real subprocess to catch that.
    """

    def test_runs_as_script_and_reaches_harvest_logic(self, tmp_path):
        dd = tmp_path / "data"
        dd.mkdir()
        (dd / "engine_ran_version").write_text("0.0.1")  # an old engine "ran"
        reg = tmp_path / "installed_plugins.json"
        reg.write_text(json.dumps({"plugins": {
            "bootstrap@plugins-kit": [
                {"version": "9.9.9", "installPath": str(tmp_path / "no-such-install")}
            ]
        }}), encoding="utf-8")

        result = subprocess.run(
            [sys.executable, str(HARVEST_PY),
             "--data-dir", str(dd),
             "--project-dir", str(tmp_path),
             "--marketplace", "plugins-kit",
             "--registry", str(reg)],
            capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stderr
        # run_harvest writes harvest_launched_version BEFORE attempting the launch
        # (which fails here — the fake installPath has no session-bootstrap.sh).
        # If a relative-import (or any top-level) error had no-op'd the script,
        # this marker is absent.
        marker = dd / "harvest_launched_version"
        assert marker.exists(), (
            "harvest.py run as a script did not reach the harvest logic — a "
            f"relative import or other error silently no-op'd it. stderr={result.stderr!r}"
        )
        assert marker.read_text().strip() == "9.9.9"


class TestReadInstalledBootstrap:
    def test_list_entry(self, tmp_path):
        reg = _registry(tmp_path, {
            "bootstrap@plugins-kit": [{"version": "0.22.0", "installPath": "/cache/bootstrap/0.22.0"}],
        })
        assert read_installed_bootstrap(reg, "plugins-kit") == ("0.22.0", "/cache/bootstrap/0.22.0")

    def test_dict_entry(self, tmp_path):
        reg = _registry(tmp_path, {
            "bootstrap@plugins-kit": {"version": "0.22.0", "installPath": "/cache/bootstrap/0.22.0"},
        })
        assert read_installed_bootstrap(reg, "plugins-kit") == ("0.22.0", "/cache/bootstrap/0.22.0")

    def test_fallback_to_name_when_marketplace_missing(self, tmp_path):
        reg = _registry(tmp_path, {
            "bootstrap@other-mkt": [{"version": "0.9.0", "installPath": "/x"}],
        })
        # Asked for plugins-kit (absent) -> fall back to any "bootstrap" key.
        assert read_installed_bootstrap(reg, "plugins-kit") == ("0.9.0", "/x")

    def test_missing_registry(self, tmp_path):
        assert read_installed_bootstrap(str(tmp_path / "nope.json"), "plugins-kit") == ("", "")

    def test_malformed_json(self, tmp_path):
        p = tmp_path / "installed_plugins.json"
        p.write_text("{not json", encoding="utf-8")
        assert read_installed_bootstrap(str(p), "plugins-kit") == ("", "")

    def test_no_bootstrap_entry(self, tmp_path):
        reg = _registry(tmp_path, {"git-kit@plugins-kit": [{"version": "1.0", "installPath": "/g"}]})
        assert read_installed_bootstrap(reg, "plugins-kit") == ("", "")


class TestShouldHarvest:
    def test_installed_strictly_newer(self):
        assert should_harvest("0.22.0", "0.21.0") is True

    def test_equal_is_false(self):
        assert should_harvest("0.22.0", "0.22.0") is False

    def test_installed_older_is_false(self):
        assert should_harvest("0.21.0", "0.22.0") is False

    def test_missing_installed_is_false(self):
        assert should_harvest("", "0.21.0") is False

    def test_missing_ran_treated_as_zero(self):
        assert should_harvest("0.21.0", "") is True

    def test_numeric_not_string_compare(self):
        # "0.9" vs "0.14": string compare would say 0.9 > 0.14; numeric says no.
        assert should_harvest("0.9.0", "0.14.0") is False
        assert should_harvest("0.14.0", "0.9.0") is True


class TestRunHarvestDecision:
    """run_harvest with launch_new_engine mocked — assert when a launch fires."""

    def _setup(self, tmp_path, monkeypatch, installed, ran, install_path="/cache/bootstrap/NEW"):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        if ran is not None:
            global_stamp(str(data_dir), "engine_ran_version").write(ran)
        reg = _registry(tmp_path, {
            "bootstrap@plugins-kit": [{"version": installed, "installPath": install_path}],
        })
        calls = []
        monkeypatch.setattr(
            harvest, "launch_new_engine",
            lambda ip, pd, dd: calls.append((ip, pd, dd)) or True,
        )
        return str(data_dir), reg, calls

    def test_installed_greater_triggers_launch(self, tmp_path, monkeypatch):
        data_dir, reg, calls = self._setup(tmp_path, monkeypatch, "0.22.0", "0.21.0")
        status = run_harvest(data_dir, "/proj", reg, "plugins-kit")
        assert len(calls) == 1
        # Launched by the NEW installPath.
        assert calls[0][0] == "/cache/bootstrap/NEW"
        assert calls[0][1] == "/proj"
        assert status is not None and "0.22.0" in status

    def test_equal_does_not_launch(self, tmp_path, monkeypatch):
        data_dir, reg, calls = self._setup(tmp_path, monkeypatch, "0.22.0", "0.22.0")
        assert run_harvest(data_dir, "/proj", reg, "plugins-kit") is None
        assert calls == []

    def test_older_does_not_launch(self, tmp_path, monkeypatch):
        data_dir, reg, calls = self._setup(tmp_path, monkeypatch, "0.21.0", "0.22.0")
        assert run_harvest(data_dir, "/proj", reg, "plugins-kit") is None
        assert calls == []

    def test_dedup_blocks_second_launch_for_same_version(self, tmp_path, monkeypatch):
        data_dir, reg, calls = self._setup(tmp_path, monkeypatch, "0.22.0", "0.21.0")
        run_harvest(data_dir, "/proj", reg, "plugins-kit")
        run_harvest(data_dir, "/proj", reg, "plugins-kit")  # same installed version
        assert len(calls) == 1, "must not relaunch for the same installed version"
        assert global_stamp(data_dir, "harvest_launched_version").read() == "0.22.0"

    def test_new_version_relaunches_after_dedup(self, tmp_path, monkeypatch):
        data_dir, reg, calls = self._setup(tmp_path, monkeypatch, "0.22.0", "0.21.0")
        run_harvest(data_dir, "/proj", reg, "plugins-kit")
        # A still-newer version arrives; dedup keyed on installed version lets it through.
        reg2 = _registry(tmp_path, {
            "bootstrap@plugins-kit": [{"version": "0.23.0", "installPath": "/cache/bootstrap/0.23.0"}],
        })
        run_harvest(data_dir, "/proj", reg2, "plugins-kit")
        assert len(calls) == 2
        assert calls[1][0] == "/cache/bootstrap/0.23.0"

    def test_no_ran_stamp_still_launches_when_install_path_present(self, tmp_path, monkeypatch):
        data_dir, reg, calls = self._setup(tmp_path, monkeypatch, "0.22.0", None)
        run_harvest(data_dir, "/proj", reg, "plugins-kit")
        assert len(calls) == 1


class TestLaunchNewEngine:
    """launch_new_engine with subprocess.Popen mocked — assert the invocation
    and the cooldown-clear side effect."""

    def _install(self, tmp_path, with_script=True):
        ip = tmp_path / "cache" / "bootstrap" / "0.22.0"
        (ip / "hooks" / "sessionstart").mkdir(parents=True)
        if with_script:
            (ip / "hooks" / "sessionstart" / "session-bootstrap.sh").write_text("#!/bin/bash\n")
        return str(ip)

    def test_launches_new_session_bootstrap(self, tmp_path, monkeypatch):
        install_path = self._install(tmp_path)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        popen_calls = []

        class _FakePopen:
            def __init__(self, args, **kwargs):
                popen_calls.append((args, kwargs))

        monkeypatch.setattr(harvest.subprocess, "Popen", _FakePopen)
        ok = harvest.launch_new_engine(install_path, "/proj", str(data_dir))
        assert ok is True
        assert len(popen_calls) == 1
        args, kwargs = popen_calls[0]
        # bash <new installPath>/hooks/sessionstart/session-bootstrap.sh
        assert args[0] == "bash"
        assert args[1].endswith("session-bootstrap.sh")
        assert install_path in args[1]
        assert kwargs["cwd"] == "/proj"
        # Detached + silent so it outlives the hook and never lands in the prompt.
        assert kwargs["stdin"] == harvest.subprocess.DEVNULL
        assert kwargs["stdout"] == harvest.subprocess.DEVNULL

    def test_clears_cooldown_before_launch(self, tmp_path, monkeypatch):
        install_path = self._install(tmp_path)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        # Seed a cooldown stamp for /proj; the launch must clear it so the new
        # session-bootstrap.sh's throttle gate doesn't skip the forced pass.
        cd = project_stamp(str(data_dir), "last_run_epoch", "/proj")
        cd.write("123456")
        assert cd.exists()
        monkeypatch.setattr(harvest.subprocess, "Popen", lambda *a, **k: None)
        harvest.launch_new_engine(install_path, "/proj", str(data_dir))
        assert not cd.exists(), "cooldown must be cleared to force the pass"

    def test_missing_script_returns_false(self, tmp_path, monkeypatch):
        install_path = self._install(tmp_path, with_script=False)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        popen_calls = []
        monkeypatch.setattr(harvest.subprocess, "Popen",
                            lambda *a, **k: popen_calls.append(1))
        assert harvest.launch_new_engine(install_path, "/proj", str(data_dir)) is False
        assert popen_calls == []

    def test_popen_oserror_returns_false(self, tmp_path, monkeypatch):
        install_path = self._install(tmp_path)
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        def _boom(*a, **k):
            raise OSError("no bash")

        monkeypatch.setattr(harvest.subprocess, "Popen", _boom)
        assert harvest.launch_new_engine(install_path, "/proj", str(data_dir)) is False


class TestMainNeverRaises:
    def test_main_swallows_errors(self, tmp_path, monkeypatch):
        # main() must return 0 even if run_harvest blows up — never break a prompt.
        def _boom(*a, **k):
            raise RuntimeError("boom")

        monkeypatch.setattr(harvest, "run_harvest", _boom)
        rc = harvest.main([
            "--data-dir", str(tmp_path), "--project-dir", "/proj",
            "--marketplace", "plugins-kit", "--registry", str(tmp_path / "r.json"),
        ])
        assert rc == 0
