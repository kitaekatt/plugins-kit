"""Step 3c-mkt: every marketplace refresh runs before any plugin version check.

Regression: bootstrap's own ``alwaysUpdate`` marketplace entry is declared in
plugins/bootstrap/bootstrap.json and was processed in Step 4, AFTER the
layered manifest's plugins phase (Step 3c) had compared the installed version
with the stale local clone. A published update was therefore reported "up to
date" and applied one pass late -- by the SessionStart hook, `bootstrap
codex-hook` and `bootstrap run` alike. The barrier settles every declared
marketplace first, in the same manifest order, so the same pass that
refreshes the clone also updates the plugin.
"""

import json
import os
import sys
from types import SimpleNamespace

import pytest

from bootstrap_lib import engine, marketplace_lifecycle, plugin_resolve
from bootstrap_lib.marketplace_lifecycle import (
    LifecycleResult, PinResult, ScopeSyncResult, VersionCheckResult,
)
from bootstrap_lib.plugin_resolve import PluginInfo


MKT = "mk"
ALWAYS_UPDATE = {"marketplaces": [
    {"name": MKT, "source": "https://example.invalid/mk.git", "alwaysUpdate": True},
]}


def _ok(ref="", message="stub"):
    return LifecycleResult(passed=True, ref=ref, message=message)


class _Remote:
    """A marketplace whose remote carries foo 1.1 while the clone says 1.0."""

    def __init__(self):
        self.refreshed = False
        self.calls = []

    def current(self, name):
        self.calls.append(("current", name))
        return LifecycleResult(passed=self.refreshed, ref=name, message="behind")

    def update(self, name=""):
        self.calls.append(("update_marketplace", name))
        self.refreshed = True
        return _ok(name, "updated")

    def version(self, ref):
        self.calls.append(("check_plugin_version", ref))
        latest = "1.1" if self.refreshed else "1.0"
        return VersionCheckResult(
            up_to_date=latest == "1.0", ref=ref, installed_version="1.0",
            latest_version=latest, message="")

    def update_plugin(self, ref, scope="user", project_dir=None):
        self.calls.append(("update_plugin", ref))
        return _ok(ref, "updated")


def _no_spawn(name):
    def refuse(*_a, **_k):
        pytest.fail(f"{name} must not run in this test")
    return refuse


@pytest.fixture
def remote(monkeypatch):
    fake = _Remote()
    ml = marketplace_lifecycle
    monkeypatch.setattr(ml, "resolve_claude_cli", lambda: "/fake/claude")
    monkeypatch.setattr(ml, "check_marketplace_exists", lambda name: _ok(name))
    monkeypatch.setattr(ml, "check_marketplace_current", fake.current)
    monkeypatch.setattr(ml, "update_marketplace", fake.update)
    monkeypatch.setattr(ml, "load_pin_markers", lambda *a, **k: {})
    monkeypatch.setattr(ml, "pinned_marketplace_sha", lambda *a, **k: "")
    monkeypatch.setattr(ml, "check_plugin_installed", lambda ref: _ok(ref))
    monkeypatch.setattr(ml, "check_plugin_enabled_at_scope",
                        lambda ref, scope, project_dir: _ok(ref))
    monkeypatch.setattr(ml, "ensure_registry_scope", lambda ref, scope: ScopeSyncResult(
        passed=True, ref=ref, added=False, refused=False, message="ok"))
    monkeypatch.setattr(ml, "check_plugin_version", fake.version)
    monkeypatch.setattr(ml, "update_plugin", fake.update_plugin)
    for name in ("add_marketplace", "remove_marketplace", "install_plugin",
                 "enable_plugin_in_claude", "apply_marketplace_pin",
                 "release_marketplace_pin"):
        monkeypatch.setattr(ml, name, _no_spawn(name))
    return fake


def _fake_root(tmp_path):
    root = tmp_path / "plugin_root"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "bootstrap", "version": "0.130.0"}), encoding="utf-8")
    (root / "defaults").mkdir()
    (root / "defaults" / "config.json").write_text(json.dumps({
        "schema_version": 5, "no_bootstrap": [], "bootstrap_cache": [],
        "log_success_shell": False, "log_success_checks": False,
        "self_setup": {}, "notify_reload_needed": False,
    }), encoding="utf-8")
    (root / "bootstrap.json").write_text("{}", encoding="utf-8")
    return root


def _bootstrap_plugin(tmp_path):
    """The installed bootstrap plugin, carrying its own alwaysUpdate entry."""
    install = tmp_path / "installed_bootstrap"
    install.mkdir()
    (install / "bootstrap.json").write_text(json.dumps(ALWAYS_UPDATE), encoding="utf-8")
    return PluginInfo(name="bootstrap", install_path=str(install),
                      version="0.130.0", marketplace=MKT)


def _run_pass(tmp_path, monkeypatch, *mode):
    root = _fake_root(tmp_path)
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    # The user layer declares the plugin, so its version check runs in the
    # layered plugins phase (Step 3c) -- the phase that used to read the clone
    # before Step 4 refreshed it.
    (home / ".claude" / "bootstrap.json").write_text(
        json.dumps({"plugins": [{"ref": f"{MKT}:foo"}]}), encoding="utf-8")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    installed = [_bootstrap_plugin(tmp_path)]
    monkeypatch.setattr(plugin_resolve, "list_enabled_plugins",
                        lambda *a, **k: (list(installed), False))
    monkeypatch.setattr(sys, "argv", [
        "bootstrap_engine.py", "--plugin-root", str(root),
        "--data-dir", str(data_dir), *mode,
    ])
    engine._main()
    return data_dir


@pytest.mark.parametrize("mode", [
    ("--console", "--project-key", "_global_"),   # bootstrap run
    ("--background",),                            # the SessionStart hook
], ids=["run", "hook"])
def test_one_pass_refreshes_the_clone_and_updates_the_plugin(
        tmp_path, monkeypatch, capsys, remote, mode):
    _run_pass(tmp_path, monkeypatch, *mode)
    calls = [c[0] for c in remote.calls]
    assert calls.count("update_marketplace") == 1, remote.calls
    # The refresh happens before the first version check, so that check sees
    # the published version and the update lands in this same pass.
    assert calls.index("update_marketplace") < calls.index("check_plugin_version")
    assert ("update_plugin", f"{MKT}:foo") in remote.calls
    # The marketplace is fetched once per pass, not again by Step 4.
    assert calls.count("current") == 1
    capsys.readouterr()


def test_without_the_barrier_the_update_was_one_pass_late(
        tmp_path, monkeypatch, capsys, remote):
    """The same fixture with the barrier disabled reproduces the defect, so
    the test above fails when the barrier is removed."""
    monkeypatch.setattr(engine, "_marketplace_barrier", lambda *a, **k: [])
    _run_pass(tmp_path, monkeypatch, "--console", "--project-key", "_global_")
    calls = [c[0] for c in remote.calls]
    assert calls.index("check_plugin_version") < calls.index("update_marketplace")
    assert ("update_plugin", f"{MKT}:foo") not in remote.calls
    capsys.readouterr()


def test_pass_state_does_not_outlive_the_pass(tmp_path, monkeypatch, capsys, remote):
    _run_pass(tmp_path, monkeypatch, "--console")
    assert engine._marketplace_pass == {"settled": set(), "unusable": set()}
    capsys.readouterr()


class _Ctx(SimpleNamespace):
    def __init__(self, manifest, name="config"):
        super().__init__(manifest=manifest, plugin_name=name, project_dir=None,
                         actions=[], oks=[], failures=[],
                         unusable_marketplaces=set())

    def action(self, message, display=None, detail=None):
        self.actions.append(message)

    def ok(self, message):
        self.oks.append(message)

    def quiet(self, message):
        pass

    def fail(self, entry, display=None, detail=None, **failure):
        self.actions.append(entry)
        self.failures.append(failure)


@pytest.fixture
def clean_pass_state():
    engine._reset_marketplace_pass()
    engine._pinned_marketplaces_this_run.clear()
    yield
    engine._reset_marketplace_pass()
    engine._pinned_marketplaces_this_run.clear()


def _barrier(layered, plugins=()):
    actions, oks, quiets = [], [], []
    failures = engine._marketplace_barrier(
        layered, list(plugins), "linux", "/data/mk/bootstrap", "/root", None,
        "0.130.0", actions, oks, quiets)
    return failures, actions, oks


def test_layered_pin_still_wins_over_a_plugin_always_update(
        tmp_path, monkeypatch, remote, clean_pass_state):
    pins = []
    monkeypatch.setattr(marketplace_lifecycle, "apply_marketplace_pin",
                        lambda name, pin: pins.append((name, pin)) or PinResult(
                            passed=True, ref=name, status="already_pinned",
                            sha="abcdef1234", message="ok"))
    layered = {"marketplaces": [{"name": MKT, "pin": "v1"}]}
    failures, _actions, _oks = _barrier(layered, [_bootstrap_plugin(tmp_path)])
    assert failures == []
    assert pins == [(MKT, "v1")]
    assert ("update_marketplace", MKT) not in remote.calls
    # The in-order phase for the plugin manifest then stands aside.
    ctx = _Ctx(ALWAYS_UPDATE, name="bootstrap")
    engine._phase_marketplaces(ctx)
    assert ctx.oks == [f"marketplace {MKT}: settled earlier this pass"]
    assert ("update_marketplace", MKT) not in remote.calls


def test_without_a_claude_cli_the_in_order_phases_run_as_before(
        tmp_path, monkeypatch, remote, clean_pass_state):
    """A fresh extension-only machine gets the CLI from bootstrap's own tools
    phase in Step 4; the barrier must leave every marketplace to that order."""
    monkeypatch.setattr(marketplace_lifecycle, "resolve_claude_cli", lambda: None)
    failures, actions, oks = _barrier(ALWAYS_UPDATE, [_bootstrap_plugin(tmp_path)])
    assert (failures, actions, oks) == ([], [], [])
    assert engine._marketplace_pass["settled"] == set()
    monkeypatch.setattr(marketplace_lifecycle, "resolve_claude_cli",
                        lambda: "/fake/claude")
    ctx = _Ctx(ALWAYS_UPDATE, name="bootstrap")
    engine._phase_marketplaces(ctx)
    assert ("update_marketplace", MKT) in remote.calls


def test_an_unusable_marketplace_stands_down_installs_in_another_manifest(
        tmp_path, monkeypatch, remote, clean_pass_state):
    ml = marketplace_lifecycle
    monkeypatch.setattr(ml, "check_marketplace_exists", lambda name: LifecycleResult(
        passed=False, ref=name, message="absent"))
    monkeypatch.setattr(ml, "add_marketplace", lambda source, name: LifecycleResult(
        passed=False, ref=name, message="access denied"))
    monkeypatch.setattr(ml, "check_plugin_installed", lambda ref: LifecycleResult(
        passed=False, ref=ref, message="absent"))
    failures, _actions, _oks = _barrier({}, [_bootstrap_plugin(tmp_path)])
    assert [f["type"] for f in failures] == ["marketplace"]
    assert engine._marketplace_pass["unusable"] == {MKT}
    # The layered manifest declares the plugin; its own ctx never saw the
    # failed add, yet it still does not attempt the install.
    ctx = _Ctx({"plugins": [{"ref": f"{MKT}:foo"}]})
    engine._phase_plugins(ctx)
    assert ctx.failures == []
    assert any("marketplace mk unavailable" in a for a in ctx.actions)
