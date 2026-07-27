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
    read_path_version,
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


class TestLaunchLogsActualPathVersion:
    """The harvest launches an installPath, not a version number. When the code
    sitting at that path declares a DIFFERENT version than the registry claims,
    the log must say so -- otherwise a mismatch is invisible and the status line
    reports a version that never ran."""

    def _install_path(self, tmp_path, version):
        ip = tmp_path / "cache" / "bootstrap" / "ON-DISK"
        (ip / ".claude-plugin").mkdir(parents=True)
        (ip / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "bootstrap", "version": version}), encoding="utf-8"
        )
        return str(ip)

    def _run(self, tmp_path, monkeypatch, installed, path_version):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        global_stamp(str(data_dir), "engine_ran_version").write("0.61.0")
        install_path = self._install_path(tmp_path, path_version)
        reg = _registry(tmp_path, {
            "bootstrap@plugins-kit": [{"version": installed, "installPath": install_path}],
        })
        monkeypatch.setattr(harvest, "launch_new_engine", lambda ip, pd, dd: True)
        return run_harvest(str(data_dir), "/proj", reg, "plugins-kit")

    def test_mismatch_is_named_in_the_status(self, tmp_path, monkeypatch):
        status = self._run(tmp_path, monkeypatch, "0.63.0", "0.62.0")
        assert status is not None
        assert "0.63.0" in status, "the registry-claimed version still leads the line"
        assert "0.62.0" in status, "the version actually at installPath must be named"

    def test_match_adds_no_noise(self, tmp_path, monkeypatch):
        status = self._run(tmp_path, monkeypatch, "0.63.0", "0.63.0")
        assert status is not None
        assert "installPath is" not in status

    def test_unreadable_path_manifest_adds_no_noise(self, tmp_path, monkeypatch):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        global_stamp(str(data_dir), "engine_ran_version").write("0.61.0")
        reg = _registry(tmp_path, {
            "bootstrap@plugins-kit": [
                {"version": "0.63.0", "installPath": str(tmp_path / "nope")},
            ],
        })
        monkeypatch.setattr(harvest, "launch_new_engine", lambda ip, pd, dd: True)
        status = run_harvest(str(data_dir), "/proj", reg, "plugins-kit")
        assert status is not None and "installPath is" not in status


class TestReadPathVersion:
    def test_reads_version_from_plugin_json(self, tmp_path):
        ip = tmp_path / "ip"
        (ip / ".claude-plugin").mkdir(parents=True)
        (ip / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"version": "1.2.3"}), encoding="utf-8"
        )
        assert read_path_version(str(ip)) == "1.2.3"

    def test_missing_path_is_empty(self, tmp_path):
        assert read_path_version(str(tmp_path / "nope")) == ""

    def test_empty_path_is_empty(self):
        assert read_path_version("") == ""

    def test_malformed_json_is_empty(self, tmp_path):
        ip = tmp_path / "ip"
        (ip / ".claude-plugin").mkdir(parents=True)
        (ip / ".claude-plugin" / "plugin.json").write_text("{ not json", encoding="utf-8")
        assert read_path_version(str(ip)) == ""


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


class TestRunRegistryRelaunch:
    """The mid-session install relaunch (third trigger) with launch_new_engine
    mocked — assert when a launch fires and the once-per-change dedup."""

    def _setup(self, tmp_path, monkeypatch, plugins, enabled, seed_hash="auto"):
        from bootstrap_lib.plugins_snapshot import STATE_STAMP, plugins_state_hash

        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)
        reg = _registry(tmp_path, plugins)
        st = tmp_path / "settings.json"
        st.write_text(json.dumps({"enabledPlugins": enabled}), encoding="utf-8")
        if seed_hash == "auto":
            seed_hash = plugins_state_hash(reg, str(st))
        if seed_hash:
            global_stamp(str(data_dir), STATE_STAMP).write(seed_hash)
        calls = []
        monkeypatch.setattr(
            harvest, "launch_new_engine",
            lambda ip, pd, dd: calls.append((ip, pd, dd)) or True,
        )
        return str(data_dir), reg, str(st), calls

    def _relaunch(self, data_dir, reg, st):
        return harvest.run_registry_relaunch(
            data_dir, "/proj", reg, "plugins-kit", settings_path=st,
        )

    def test_no_seed_never_launches(self, tmp_path, monkeypatch):
        # Unseeded stamp = no completed pass has absorbed state; the engine
        # seeds it. Launching here would fire a spurious pass on every machine
        # that adopts this version.
        data_dir, reg, st, calls = self._setup(
            tmp_path, monkeypatch, {}, {"hue-kit@plugins-kit": True}, seed_hash="",
        )
        assert self._relaunch(data_dir, reg, st) is None
        assert calls == []

    def test_unchanged_state_does_not_launch(self, tmp_path, monkeypatch):
        data_dir, reg, st, calls = self._setup(
            tmp_path, monkeypatch,
            {"hue-kit@plugins-kit": [{"version": "0.5.1", "installPath": "/h"}]},
            {"hue-kit@plugins-kit": True},
        )
        assert self._relaunch(data_dir, reg, st) is None
        assert calls == []

    def test_registry_change_launches_installed_engine(self, tmp_path, monkeypatch):
        # Seed the pre-install state, then "install" hue-kit into the registry.
        data_dir, _, st, calls = self._setup(
            tmp_path, monkeypatch,
            {"bootstrap@plugins-kit": [{"version": "1.0.0", "installPath": "/cache/bootstrap/1.0.0"}]},
            {},
        )
        reg2 = _registry(tmp_path, {
            "bootstrap@plugins-kit": [{"version": "1.0.0", "installPath": "/cache/bootstrap/1.0.0"}],
            "hue-kit@plugins-kit": [{"version": "0.5.1", "installPath": "/cache/hue-kit/0.5.1"}],
        })
        status = self._relaunch(data_dir, reg2, st)
        assert status is not None and "registry-change" in status
        assert len(calls) == 1
        # Relaunched via the installed bootstrap's installPath.
        assert calls[0][0] == "/cache/bootstrap/1.0.0"

    def test_enabled_only_change_launches(self, tmp_path, monkeypatch):
        # The registry-v2-EMPTY machine: installs never touch the registry,
        # only settings.json's enabledPlugins.
        data_dir, reg, _, calls = self._setup(tmp_path, monkeypatch, {}, {})
        st2 = tmp_path / "settings2.json"
        st2.write_text(
            json.dumps({"enabledPlugins": {"hue-kit@plugins-kit": True}}), encoding="utf-8",
        )
        status = self._relaunch(data_dir, reg, str(st2))
        assert status is not None
        assert len(calls) == 1
        # No registry installPath -> falls back to the running plugin root.
        assert calls[0][0] == harvest._PLUGIN_ROOT

    def test_dedup_blocks_second_launch_for_same_state(self, tmp_path, monkeypatch):
        from bootstrap_lib.plugins_snapshot import LAUNCHED_STAMP

        data_dir, reg, _, calls = self._setup(tmp_path, monkeypatch, {}, {})
        st2 = tmp_path / "settings2.json"
        st2.write_text(
            json.dumps({"enabledPlugins": {"hue-kit@plugins-kit": True}}), encoding="utf-8",
        )
        self._relaunch(data_dir, reg, str(st2))
        assert self._relaunch(data_dir, reg, str(st2)) is None
        assert len(calls) == 1, "must not relaunch for the same plugin-set state"
        assert global_stamp(data_dir, LAUNCHED_STAMP).exists()

    def test_engine_absorb_resets_trigger(self, tmp_path, monkeypatch):
        # After the launched pass completes (stamp_plugins_state), the same
        # state no longer triggers — and a FURTHER change triggers again.
        from bootstrap_lib.plugins_snapshot import stamp_plugins_state

        data_dir, reg, _, calls = self._setup(tmp_path, monkeypatch, {}, {})
        st2 = tmp_path / "settings2.json"
        st2.write_text(
            json.dumps({"enabledPlugins": {"hue-kit@plugins-kit": True}}), encoding="utf-8",
        )
        self._relaunch(data_dir, reg, str(st2))
        stamp_plugins_state(data_dir, reg, str(st2))  # what the engine does at completion
        assert self._relaunch(data_dir, reg, str(st2)) is None
        st3 = tmp_path / "settings3.json"
        st3.write_text(
            json.dumps({"enabledPlugins": {"hue-kit@plugins-kit": False}}), encoding="utf-8",
        )
        assert self._relaunch(data_dir, reg, str(st3)) is not None
        assert len(calls) == 2

    def test_launch_failure_returns_none(self, tmp_path, monkeypatch):
        data_dir, reg, _, calls = self._setup(tmp_path, monkeypatch, {}, {})
        monkeypatch.setattr(harvest, "launch_new_engine", lambda ip, pd, dd: False)
        st2 = tmp_path / "settings2.json"
        st2.write_text(
            json.dumps({"enabledPlugins": {"hue-kit@plugins-kit": True}}), encoding="utf-8",
        )
        assert self._relaunch(data_dir, reg, str(st2)) is None


class TestMainTriggerSequencing:
    def test_relaunch_skipped_when_harvest_launches(self, tmp_path, monkeypatch):
        # One prompt, at most one pass: a version-harvest launch suppresses the
        # registry-change trigger for that prompt.
        monkeypatch.setattr(harvest, "run_harvest", lambda *a, **k: "harvest: launched")
        relaunch_calls = []
        monkeypatch.setattr(
            harvest, "run_registry_relaunch",
            lambda *a, **k: relaunch_calls.append(1) or "registry-change: relaunched",
        )
        monkeypatch.setattr(harvest, "_log_launch", lambda *a, **k: None)
        rc = harvest.main([
            "--data-dir", str(tmp_path), "--project-dir", "/proj",
            "--marketplace", "plugins-kit", "--registry", str(tmp_path / "r.json"),
        ])
        assert rc == 0
        assert relaunch_calls == []

    def test_relaunch_runs_when_harvest_quiet(self, tmp_path, monkeypatch):
        monkeypatch.setattr(harvest, "run_harvest", lambda *a, **k: None)
        relaunch_calls = []
        monkeypatch.setattr(
            harvest, "run_registry_relaunch",
            lambda *a, **k: relaunch_calls.append(1) or None,
        )
        rc = harvest.main([
            "--data-dir", str(tmp_path), "--project-dir", "/proj",
            "--marketplace", "plugins-kit", "--registry", str(tmp_path / "r.json"),
        ])
        assert rc == 0
        assert relaunch_calls == [1]


class TestRelaunchScriptInvocation:
    """The relaunch trigger must also be reachable under SCRIPT execution
    (`python harvest.py`) — the regression class TestScriptInvocation exists
    for, extended to the third trigger."""

    def test_script_run_reaches_relaunch_logic(self, tmp_path):
        from bootstrap_lib.plugins_snapshot import (
            LAUNCHED_STAMP, STATE_STAMP, plugins_state_hash,
        )

        dd = tmp_path / "data"
        dd.mkdir()
        # A bootstrap entry with a FAKE installPath: keeps the version harvest
        # quiet (installed == engine_ran_version) and — critically — makes the
        # relaunch's launch attempt fail fast instead of falling back to the
        # real dev-tree plugin root and spawning a genuine pass.
        reg = _registry(tmp_path, {
            "bootstrap@plugins-kit": [
                {"version": "0.0.1", "installPath": str(tmp_path / "no-such-install")}
            ]
        })
        st = tmp_path / "settings.json"
        st.write_text(json.dumps({"enabledPlugins": {}}), encoding="utf-8")
        # Seed a DIFFERENT pre-change state so the script sees a change.
        global_stamp(str(dd), STATE_STAMP).write("old-state-hash")
        global_stamp(str(dd), "engine_ran_version").write("0.0.1")

        result = subprocess.run(
            [sys.executable, str(HARVEST_PY),
             "--data-dir", str(dd),
             "--project-dir", str(tmp_path),
             "--marketplace", "plugins-kit",
             "--registry", reg,
             "--settings", str(st)],
            capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stderr
        # run_registry_relaunch writes the dedup marker BEFORE the launch
        # attempt (which fails here — no session-bootstrap.sh anywhere useful);
        # its presence proves the trigger logic ran under script execution.
        marker = global_stamp(str(dd), LAUNCHED_STAMP)
        assert marker.exists(), (
            "harvest.py run as a script did not reach the relaunch logic. "
            f"stderr={result.stderr!r}"
        )
        assert marker.read() == plugins_state_hash(reg, str(st))


class TestCacheFallbackInstalledBootstrap:
    """Registry-v2 fallback for the harvest: with installed_plugins.json empty
    ({"plugins": {}}), the installed bootstrap version/installPath is derived
    from the highest version dir under <plugins>/cache/<mkt>/bootstrap/."""

    def _scaffold(self, tmp_path, versions, registry_plugins=None):
        root = tmp_path / "plugins-root"
        reg = root / "installed_plugins.json"
        root.mkdir()
        reg.write_text(
            json.dumps({"version": 2, "plugins": registry_plugins or {}}), encoding="utf-8"
        )
        for v in versions:
            (root / "cache" / "plugins-kit" / "bootstrap" / v).mkdir(parents=True)
        return str(reg)

    def test_empty_registry_falls_back_to_cache(self, tmp_path):
        reg = self._scaffold(tmp_path, ["0.46.0"])
        version, path = read_installed_bootstrap(reg, "plugins-kit")
        assert version == "0.46.0"
        assert path.endswith("0.46.0")

    def test_highest_version_wins_numerically(self, tmp_path):
        reg = self._scaffold(tmp_path, ["0.9.0", "0.10.0"])
        version, _ = read_installed_bootstrap(reg, "plugins-kit")
        assert version == "0.10.0"

    def test_registry_entry_takes_precedence(self, tmp_path):
        reg = self._scaffold(
            tmp_path, ["9.9.9"],
            registry_plugins={"bootstrap@plugins-kit": [{"version": "1.0.0", "installPath": "/x"}]},
        )
        version, path = read_installed_bootstrap(reg, "plugins-kit")
        assert version == "1.0.0"
        assert path == "/x"

    def test_no_cache_no_registry_is_miss(self, tmp_path):
        reg = tmp_path / "installed_plugins.json"
        reg.write_text(json.dumps({"version": 2, "plugins": {}}), encoding="utf-8")
        assert read_installed_bootstrap(str(reg), "plugins-kit") == ("", "")

    def test_unknown_marketplace_scans_all(self, tmp_path):
        reg = self._scaffold(tmp_path, ["0.46.0"])
        version, _ = read_installed_bootstrap(reg, "")
        assert version == "0.46.0"


class TestReadInstalledBootstrapDuplicateRecords:
    """The wedge scenario (claude-code#79892): a stale user-scope record
    carrying projectPath sits AHEAD of the healthy record; first-entry picks
    read the stale one forever."""

    def test_prefers_record_without_projectpath(self, tmp_path):
        reg = _registry(tmp_path, {
            "bootstrap@plugins-kit": [
                {"version": "0.45.0", "installPath": "/cache/bootstrap/0.45.0",
                 "projectPath": "D:/dev/env-config", "scope": "user"},
                {"version": "0.52.0", "installPath": "/cache/bootstrap/0.52.0",
                 "scope": "user"},
            ],
        })
        assert read_installed_bootstrap(reg, "plugins-kit") == (
            "0.52.0", "/cache/bootstrap/0.52.0")

    def test_healthy_record_wins_even_when_older(self, tmp_path):
        reg = _registry(tmp_path, {
            "bootstrap@plugins-kit": [
                {"version": "0.60.0", "installPath": "/stale",
                 "projectPath": "D:/dev/somewhere"},
                {"version": "0.52.0", "installPath": "/healthy"},
            ],
        })
        assert read_installed_bootstrap(reg, "plugins-kit") == ("0.52.0", "/healthy")

    def test_newest_wins_among_healthy_records(self, tmp_path):
        reg = _registry(tmp_path, {
            "bootstrap@plugins-kit": [
                {"version": "0.9.0", "installPath": "/a"},
                {"version": "0.10.0", "installPath": "/b"},
            ],
        })
        assert read_installed_bootstrap(reg, "plugins-kit") == ("0.10.0", "/b")
