"""Tests for tools[] machine-property targeting via `requires`.

Covers the `requires` gate: a bootstrap.json tools[] entry may declare
{"requires": {"dev": true, ...}} and applies iff EVERY pair is satisfied by
the current machine's entry in the env.json `machines` registry. The gate
short-circuits BEFORE resolve (like the skip sentinel), resolves identity
LAZILY (a manifest with no `requires` anywhere never reads env.json -- the
no-requires default must stay byte-for-byte unchanged), and hard-errors on
an unknown machine (Environment Awareness doctrine: no fallbacks).

Split across the two seams:
- env_manifest.requires_satisfied / MachineRequiresResolver (pure logic +
  lazy identity), and
- engine._strategy_requires via _process_tool_entry / _process_manifest
  (dispatch behavior, failure shape, laziness end to end).
"""

import json
import os
from pathlib import Path

import pytest

import bootstrap_lib.engine as engine
import bootstrap_lib.env_manifest as env_manifest
import bootstrap_lib.path_check as path_check
import bootstrap_lib.tool_check as tool_check
import bootstrap_lib.tool_paths as tool_paths
from bootstrap_lib.env_manifest import MachineRequiresResolver, requires_satisfied


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Point HOME at a tmp dir so user-layer env.json isolation is clean."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


def _write_env_json(home: Path, machines: dict) -> None:
    (home / ".claude" / "env.json").write_text(
        json.dumps({"machines": machines}))


def _stub(monkeypatch):
    """Neutralize side effects: PATH writes, tool_paths state."""
    monkeypatch.setattr(path_check, "add_path_to_shell_config", lambda d: (True, "stub"))
    monkeypatch.setattr(tool_paths, "record", lambda *a, **k: None)
    # Register PATH for restore: the resolve strategy prepends the tool dir
    # to the live process PATH when the tool is on disk but not on PATH.
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))


def _resolvable_tool(tmp_path, name="code"):
    """A tool entry that resolves via installPath (no install ever runs)."""
    exe = tmp_path / name
    exe.write_text("#!/bin/sh\n")
    return {"name": name, "installPath": str(tmp_path)}


class TestRequiresSatisfied:
    def test_empty_mapping_is_trivially_satisfied(self):
        assert requires_satisfied({}, {"os": "windows"})

    def test_true_needs_present_and_truthy(self):
        assert requires_satisfied({"dev": True}, {"dev": True})
        assert not requires_satisfied({"dev": True}, {})
        assert not requires_satisfied({"dev": True}, {"dev": False})
        assert not requires_satisfied({"dev": True}, {"dev": 0})
        # Truthy non-boolean values count as "has the property".
        assert requires_satisfied({"dev": True}, {"dev": "yes"})

    def test_false_needs_absent_or_falsy(self):
        assert requires_satisfied({"dev": False}, {})
        assert requires_satisfied({"dev": False}, {"dev": False})
        assert requires_satisfied({"dev": False}, {"dev": 0})
        assert not requires_satisfied({"dev": False}, {"dev": True})
        assert not requires_satisfied({"dev": False}, {"dev": "yes"})

    def test_other_values_compare_by_equality(self):
        assert requires_satisfied({"purpose": "gaming"}, {"purpose": "gaming"})
        assert not requires_satisfied({"purpose": "gaming"}, {"purpose": "dev"})
        assert not requires_satisfied({"purpose": "gaming"}, {})

    def test_conjunction_all_pairs_must_hold(self):
        machine = {"dev": True, "gpu": "blackwell"}
        assert requires_satisfied({"dev": True, "gpu": "blackwell"}, machine)
        assert not requires_satisfied({"dev": True, "gpu": "hopper"}, machine)

    def test_boolean_true_is_not_the_integer_one(self):
        # JSON true means presence+truth, never bool/int coercion tricks in
        # the equality branch: an expected value of 1 is an equality require.
        assert requires_satisfied({"slot": 1}, {"slot": 1})
        assert not requires_satisfied({"slot": 1}, {"slot": 2})


class TestMachineRequiresResolver:
    def test_resolves_known_machine(self, isolated_home, monkeypatch):
        _write_env_json(isolated_home, {"boxy": {"os": "windows", "dev": True}})
        monkeypatch.setattr(env_manifest, "current_hostname", lambda: "boxy")
        key, machine, err = MachineRequiresResolver(None).resolve()
        assert err is None
        assert key == "boxy"
        assert machine == {"os": "windows", "dev": True}

    def test_short_form_hostname_resolves(self, isolated_home, monkeypatch):
        _write_env_json(isolated_home, {"boxy": {"os": "ubuntu"}})
        monkeypatch.setattr(env_manifest, "current_hostname",
                            lambda: "boxy.local.lan")
        key, _machine, err = MachineRequiresResolver(None).resolve()
        assert err is None
        assert key == "boxy"

    def test_unknown_machine_reports_error(self, isolated_home, monkeypatch):
        _write_env_json(isolated_home, {"other": {"os": "macos"}})
        monkeypatch.setattr(env_manifest, "current_hostname", lambda: "boxy")
        key, machine, err = MachineRequiresResolver(None).resolve()
        assert key is None and machine is None
        assert "boxy" in err and "machines" in err and "other" in err

    def test_missing_registry_reports_error(self, isolated_home):
        # No env.json at all -> no machines registry.
        key, machine, err = MachineRequiresResolver(None).resolve()
        assert key is None and machine is None
        assert "machines" in err

    def test_construction_does_no_io_and_resolve_memoizes(
            self, isolated_home, monkeypatch):
        calls = []
        real_load = env_manifest.load_layered_env_manifests

        def counting_load(project_dir):
            calls.append(project_dir)
            return real_load(project_dir)

        monkeypatch.setattr(env_manifest, "load_layered_env_manifests",
                            counting_load)
        _write_env_json(isolated_home, {"boxy": {"os": "windows"}})
        monkeypatch.setattr(env_manifest, "current_hostname", lambda: "boxy")
        resolver = MachineRequiresResolver(None)
        assert calls == []          # lazy: nothing until first resolve()
        first = resolver.resolve()
        second = resolver.resolve()
        assert first == second == ("boxy", {"os": "windows"}, None)
        assert len(calls) == 1      # memoized: one load per pass


class TestStrategyRequires:
    def test_no_requires_applies_unchanged(self, tmp_path, isolated_home,
                                           monkeypatch):
        """(a) No `requires` -> the entry behaves exactly as before, and no
        identity machinery runs at all."""
        _stub(monkeypatch)

        def boom(*a, **k):
            raise AssertionError("identity must not resolve without `requires`")

        monkeypatch.setattr(env_manifest, "load_layered_env_manifests", boom)
        monkeypatch.setattr(env_manifest, "current_hostname", boom)

        ok_entries = []
        failure = engine._process_tool_entry(
            _resolvable_tool(tmp_path), "linux", "/data", "", [], ok_entries,
            [], plugin_name="config",
        )
        assert failure is None
        assert any("code: ok" in e for e in ok_entries)

    def test_requires_satisfied_applies(self, tmp_path, isolated_home,
                                        monkeypatch):
        """(b) requires {dev: true} + machine has dev: true -> the entry
        proceeds into the normal resolve path."""
        _stub(monkeypatch)
        _write_env_json(isolated_home, {"boxy": {"os": "ubuntu", "dev": True}})
        monkeypatch.setattr(env_manifest, "current_hostname", lambda: "boxy")

        tool = _resolvable_tool(tmp_path)
        tool["requires"] = {"dev": True}
        ok_entries = []
        failure = engine._process_tool_entry(
            tool, "linux", "/data", "", [], ok_entries, [],
            plugin_name="config",
        )
        assert failure is None
        assert any("code: ok" in e for e in ok_entries)

    def test_requires_unsatisfied_skips_before_resolve(
            self, isolated_home, monkeypatch):
        """(c) requires {dev: true} + machine omits dev -> skipped like the
        skip sentinel: no check subprocess, no failure, one verbose ok line."""
        _write_env_json(isolated_home, {"boxy": {"os": "ubuntu"}})
        monkeypatch.setattr(env_manifest, "current_hostname", lambda: "boxy")

        def boom_check(*a, **k):
            raise AssertionError("resolve must not run for an excluded tool")

        monkeypatch.setattr(tool_check, "check_tool", boom_check)

        action_entries, ok_entries = [], []
        failure = engine._process_tool_entry(
            {"name": "code", "requires": {"dev": True},
             "install": {"linux": "apt install code"}},
            "linux", "/data", "", action_entries, ok_entries, [],
            plugin_name="config",
        )
        assert failure is None
        assert action_entries == []
        assert any("code: skipped" in e and "boxy" in e for e in ok_entries)

    def test_requires_unknown_machine_hard_errors(self, isolated_home,
                                                  monkeypatch):
        """(d) `requires` present + unknown machine -> hard failure naming
        the tool and pointing at the env.json machines registry."""
        _write_env_json(isolated_home, {"other": {"os": "macos"}})
        monkeypatch.setattr(env_manifest, "current_hostname", lambda: "boxy")

        action_entries = []
        failure = engine._process_tool_entry(
            {"name": "code", "requires": {"dev": True},
             "install": {"linux": "apt install code"}},
            "linux", "/data", "", action_entries, [], [],
            plugin_name="config",
        )
        assert failure is not None
        assert failure["name"] == "code"
        assert failure["install_state"] == "requires_unresolved"
        assert failure["persist_across_sessions"] is True
        assert "env.json" in failure["agent_msg"]
        assert "machines" in failure["agent_msg"]
        assert any("code: FAILED" in e for e in action_entries)

    def test_requires_no_registry_hard_errors(self, isolated_home):
        """(d') `requires` present + no env.json at all -> same hard error
        (a targeted tool never installs on an unidentified machine)."""
        failure = engine._process_tool_entry(
            {"name": "code", "requires": {"dev": True},
             "install": {"linux": "apt install code"}},
            "linux", "/data", "", [], [], [],
            plugin_name="config",
        )
        assert failure is not None
        assert failure["install_state"] == "requires_unresolved"

    def test_invalid_requires_shape_hard_errors(self, isolated_home):
        """A non-object `requires` is a manifest bug, not a guess."""
        failure = engine._process_tool_entry(
            {"name": "code", "requires": ["dev"],
             "install": {"linux": "apt install code"}},
            "linux", "/data", "", [], [], [],
            plugin_name="config",
        )
        assert failure is not None
        assert failure["install_state"] == "requires_invalid"

    def test_os_skip_precedes_identity_resolution(self, isolated_home,
                                                  monkeypatch):
        """The skip sentinel fires from the entry alone: an OS the entry
        opted out of never triggers an env.json read even with `requires`."""
        def boom(*a, **k):
            raise AssertionError("identity must not resolve when skip fires")

        monkeypatch.setattr(env_manifest, "load_layered_env_manifests", boom)

        ok_entries = []
        failure = engine._process_tool_entry(
            {"name": "code", "requires": {"dev": True},
             "install": {"linux": "skip"}},
            "linux", "/data", "", [], ok_entries, [],
            plugin_name="config",
        )
        assert failure is None
        assert any("not applicable" in e for e in ok_entries)


class TestPhaseToolsLaziness:
    def test_manifest_without_requires_never_resolves_identity(
            self, tmp_path, isolated_home, monkeypatch):
        """(e) No `requires` anywhere + no env.json -> the whole tools phase
        completes without any identity resolution attempt and no error."""
        _stub(monkeypatch)

        def boom(*a, **k):
            raise AssertionError("identity must not resolve without `requires`")

        monkeypatch.setattr(env_manifest, "load_layered_env_manifests", boom)
        monkeypatch.setattr(env_manifest, "current_hostname", boom)

        action_entries, ok_entries = [], []
        failures = engine._process_manifest(
            {"tools": [_resolvable_tool(tmp_path)]},
            "linux", str(tmp_path / "data"), str(tmp_path), action_entries,
            ok_entries, plugin_name="config", project_dir=None,
        )
        assert failures == []
        assert any("code: ok" in e for e in ok_entries)

    def test_identity_resolves_once_across_many_requires(
            self, tmp_path, isolated_home, monkeypatch):
        """The phase-shared resolver memoizes: N requires-bearing entries,
        one env.json load."""
        _stub(monkeypatch)
        calls = []
        real_load = env_manifest.load_layered_env_manifests

        def counting_load(project_dir):
            calls.append(project_dir)
            return real_load(project_dir)

        monkeypatch.setattr(env_manifest, "load_layered_env_manifests",
                            counting_load)
        _write_env_json(isolated_home, {"boxy": {"os": "ubuntu"}})
        monkeypatch.setattr(env_manifest, "current_hostname", lambda: "boxy")

        tools = [
            {"name": f"t{i}", "requires": {"dev": True},
             "install": {"linux": f"apt install t{i}"}}
            for i in range(3)
        ]
        ok_entries = []
        failures = engine._process_manifest(
            {"tools": tools}, "linux", str(tmp_path / "data"), str(tmp_path),
            [], ok_entries, plugin_name="config", project_dir=None,
        )
        assert failures == []
        assert len(calls) == 1
        assert sum("skipped" in e for e in ok_entries) == 3
