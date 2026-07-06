"""Tests for bootstrap_lib/env_manifest.py and the engine env pass (Step 3e).

Covers the env.json core (bootstrap-env-refactor spec 4.1/4.2/4.4, E1 step 3):
the 4-layer loader + merge, the machines registry (unknown-machine hard
error, os cross-check, hosts-filter typo validation), the env gate matrix
(no stamp / hash change in each layer / failed-last / clean skip / engine
bump / reset), and scripts/env-reset-cooldown.sh.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from bootstrap_lib.engine import _process_env_pass
from bootstrap_lib.env_manifest import (
    ENV_STATE_STAMP,
    canonical_manifest_hash,
    entry_applies,
    env_gate_reason,
    load_layered_env_manifests,
    read_env_state,
    resolve_machine,
    validate_entry_filters,
    write_env_state,
)
from bootstrap_lib.manifest_merge import merge_env_manifests

REPO_ROOT = Path(__file__).resolve().parents[2]
RESET_SCRIPT = (
    REPO_ROOT / "plugins" / "bootstrap" / "scripts" / "env-reset-cooldown.sh"
)

ENGINE_VERSION = "0.33.0"


def _find_bash() -> str | None:
    """Find a POSIX-compatible bash (Git Bash on Windows, never WSL/System32)."""
    candidates = []
    if os.name == "nt":
        candidates.extend([
            r"C:\Program Files\Git\usr\bin\bash.exe",
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files (x86)\Git\usr\bin\bash.exe",
        ])
    found = shutil.which("bash")
    if found:
        candidates.append(found)
    for c in candidates:
        if c and Path(c).exists() and "WindowsApps" not in c and "System32" not in c:
            return c
    return None


BASH = _find_bash()
needs_bash = pytest.mark.skipif(BASH is None, reason="bash not available on this platform")


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Point HOME at a tmp dir so user-layer env.json isolation is clean."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


def _write_json(path: Path, content: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content))


MACHINES_ONLY = {"machines": {"testhost": {"os": "ubuntu"}}}


class _Pass:
    """One env-pass invocation's outputs."""

    def __init__(self, failures, action_entries, ok_entries):
        self.failures = failures
        self.action_entries = action_entries
        self.ok_entries = ok_entries

    @property
    def ran(self) -> bool:
        return any(e.startswith("running (") for e in self.ok_entries)

    @property
    def skipped(self) -> bool:
        return any(e.startswith("up to date") for e in self.ok_entries)

    def run_reason(self) -> str:
        for e in self.ok_entries:
            if e.startswith("running ("):
                return e
        return ""


@pytest.fixture
def run_env_pass(isolated_home, tmp_path):
    """Run the engine env pass against the isolated home. Returns _Pass."""
    data_dir = tmp_path / "data"
    plugin_root = tmp_path / "plugin"
    data_dir.mkdir(exist_ok=True)
    plugin_root.mkdir(exist_ok=True)

    def _run(project_dir=None, hostname="testhost", current_os="ubuntu",
             engine_version=ENGINE_VERSION):
        action_entries: list = []
        ok_entries: list = []
        failures = _process_env_pass(
            str(project_dir) if project_dir else None, current_os,
            str(data_dir), str(plugin_root),
            action_entries, ok_entries,
            engine_version=engine_version, hostname=hostname,
        )
        return _Pass(failures, action_entries, ok_entries)

    _run.data_dir = data_dir
    return _run


class TestLayeredLoading:
    """The four env.json layers merge with bootstrap.json's discipline."""

    def test_no_files_returns_empty(self, isolated_home, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        merged, errors = load_layered_env_manifests(str(project))
        assert merged == {}
        assert errors == []

    def test_four_layers_merge_lowest_to_highest(self, isolated_home, tmp_path):
        project = tmp_path / "project"
        _write_json(isolated_home / ".claude" / "env.json", {
            "machines": {"h1": {"os": "macos"}},
            "shell_rc": [{"name": "a", "content": "user"}],
        })
        _write_json(isolated_home / ".claude" / "env.local.json", {
            "shell_rc": [{"name": "a", "content": "user-local"}],
        })
        _write_json(project / ".claude" / "env.json", {
            "machines": {"h2": {"os": "ubuntu"}},
            "shell_rc": [{"name": "b", "content": "project"}],
        })
        _write_json(project / ".claude" / "env.local.json", {
            "shell_rc": [{"name": "a", "content": "project-local"}],
        })

        merged, errors = load_layered_env_manifests(str(project))

        assert errors == []
        # machines deep-merge across layers (dict keyed by hostname)
        assert set(merged["machines"]) == {"h1", "h2"}
        # identity-keyed union: highest layer wins for entry "a", "b" appended
        by_name = {e["name"]: e["content"] for e in merged["shell_rc"]}
        assert by_name == {"a": "project-local", "b": "project"}

    def test_user_layer_alone(self, isolated_home):
        _write_json(isolated_home / ".claude" / "env.json", MACHINES_ONLY)
        merged, errors = load_layered_env_manifests(None)
        assert errors == []
        assert merged == MACHINES_ONLY

    def test_malformed_layer_surfaces_error_and_merge_continues(
        self, isolated_home, tmp_path
    ):
        project = tmp_path / "project"
        _write_json(isolated_home / ".claude" / "env.json", MACHINES_ONLY)
        bad = project / ".claude" / "env.json"
        bad.parent.mkdir(parents=True)
        bad.write_text("{not json")

        merged, errors = load_layered_env_manifests(str(project))

        assert merged == MACHINES_ONLY
        assert len(errors) == 1
        assert errors[0]["path"] == str(bad)
        assert "JSON parse error" in errors[0]["error"]


class TestEnvMerge:
    """merge_env_manifests: env.json's identity keys."""

    def test_symlinks_merge_by_name(self):
        base = {"symlinks": [{"name": "starship", "target": "~/a"}]}
        override = {"symlinks": [{"name": "starship", "target": "~/b"},
                                 {"name": "kitty", "target": "~/c"}]}
        merged = merge_env_manifests(base, override)
        by_name = {e["name"]: e["target"] for e in merged["symlinks"]}
        assert by_name == {"starship": "~/b", "kitty": "~/c"}

    def test_macos_defaults_merge_by_domain_plus_key(self):
        base = {"macos_defaults": [
            {"domain": "NSGlobalDomain", "key": "KeyRepeat", "value": 6},
            {"domain": "NSGlobalDomain", "key": "InitialKeyRepeat", "value": 25},
        ]}
        override = {"macos_defaults": [
            {"domain": "NSGlobalDomain", "key": "KeyRepeat", "value": 2},
        ]}
        merged = merge_env_manifests(base, override)
        assert len(merged["macos_defaults"]) == 2
        by_key = {(e["domain"], e["key"]): e["value"] for e in merged["macos_defaults"]}
        assert by_key[("NSGlobalDomain", "KeyRepeat")] == 2
        assert by_key[("NSGlobalDomain", "InitialKeyRepeat")] == 25

    def test_macos_hotkeys_merge_by_id(self):
        base = {"macos_hotkeys": [{"id": 28, "enabled": True}]}
        override = {"macos_hotkeys": [{"id": 28, "enabled": False},
                                      {"id": 29, "enabled": True}]}
        merged = merge_env_manifests(base, override)
        by_id = {e["id"]: e["enabled"] for e in merged["macos_hotkeys"]}
        assert by_id == {28: False, 29: True}

    def test_machines_deep_merge_per_host_fields(self):
        base = {"machines": {"h": {"os": "ubuntu", "skip_repos": ["a"]}}}
        override = {"machines": {"h": {"kitty_shortcuts": "cmd+2"}}}
        merged = merge_env_manifests(base, override)
        assert merged["machines"]["h"] == {
            "os": "ubuntu", "skip_repos": ["a"], "kitty_shortcuts": "cmd+2",
        }


class TestMachineResolution:
    def test_exact_match_wins(self):
        machines = {"host.local": {"os": "macos"}, "host": {"os": "ubuntu"}}
        assert resolve_machine(machines, "host.local") == "host.local"

    def test_short_form_fallback(self):
        machines = {"host": {"os": "ubuntu"}}
        assert resolve_machine(machines, "host.fritz.box") == "host"

    def test_unknown_returns_none(self):
        assert resolve_machine({"other": {"os": "macos"}}, "host") is None


class TestEntryApplies:
    def test_no_filters_applies(self):
        assert entry_applies({"name": "x"}, "ubuntu", "host") is True

    def test_os_filter(self):
        assert entry_applies({"os": ["macos"]}, "ubuntu", "host") is False
        assert entry_applies({"os": ["macos", "ubuntu"]}, "ubuntu", "host") is True

    def test_hosts_filter(self):
        assert entry_applies({"hosts": ["other"]}, "ubuntu", "host") is False
        assert entry_applies({"hosts": ["host"]}, "ubuntu", "host") is True

    def test_both_filters_intersect(self):
        entry = {"os": ["ubuntu"], "hosts": ["host"]}
        assert entry_applies(entry, "ubuntu", "host") is True
        assert entry_applies(entry, "macos", "host") is False
        assert entry_applies(entry, "ubuntu", "other") is False


class TestMachinesValidation:
    """Registry gatekeeping through the engine env pass."""

    def test_machines_only_manifest_processes_green(
        self, isolated_home, run_env_pass
    ):
        _write_json(isolated_home / ".claude" / "env.json", MACHINES_ONLY)

        result = run_env_pass()

        assert result.failures == []
        assert result.ran
        assert any("machine 'testhost' identified" in e for e in result.ok_entries)
        state = read_env_state(str(run_env_pass.data_dir))
        assert state["last_result"] == "clean"
        assert state["engine_version"] == ENGINE_VERSION

    def test_short_form_hostname_resolves(self, isolated_home, run_env_pass):
        _write_json(isolated_home / ".claude" / "env.json", MACHINES_ONLY)
        result = run_env_pass(hostname="testhost.fritz.box")
        assert result.failures == []
        assert any("machine 'testhost' identified" in e for e in result.ok_entries)

    def test_unknown_machine_is_hard_error(self, isolated_home, run_env_pass):
        _write_json(isolated_home / ".claude" / "env.json", MACHINES_ONLY)

        result = run_env_pass(hostname="stranger")

        assert len(result.failures) == 1
        failure = result.failures[0]
        assert failure["type"] == "env_manifest"
        assert failure["persist_across_sessions"] is True
        assert "Unknown machine 'stranger'" in failure["message"]
        assert "testhost" in failure["message"]  # known machines listed
        assert "Add it to ~/.claude/env.json" in failure["message"]
        assert read_env_state(str(run_env_pass.data_dir))["last_result"] == "failed"

    def test_os_mismatch_is_hard_error(self, isolated_home, run_env_pass):
        _write_json(isolated_home / ".claude" / "env.json",
                    {"machines": {"testhost": {"os": "windows"}}})

        result = run_env_pass(current_os="ubuntu")

        assert len(result.failures) == 1
        msg = result.failures[0]["message"]
        assert "windows" in msg
        assert "ubuntu" in msg
        assert read_env_state(str(run_env_pass.data_dir))["last_result"] == "failed"

    def test_missing_declared_os_is_hard_error(self, isolated_home, run_env_pass):
        _write_json(isolated_home / ".claude" / "env.json",
                    {"machines": {"testhost": {}}})
        result = run_env_pass()
        assert len(result.failures) == 1
        assert "declares no 'os'" in result.failures[0]["message"]

    def test_hosts_filter_typo_is_validation_error(
        self, isolated_home, run_env_pass
    ):
        _write_json(isolated_home / ".claude" / "env.json", {
            "machines": {"testhost": {"os": "ubuntu"}},
            "env_checks": [
                {"name": "gpu-stack", "check": "true", "hosts": ["testh0st"]},
            ],
        })

        result = run_env_pass()

        assert len(result.failures) == 1
        msg = result.failures[0]["message"]
        assert "gpu-stack" in msg
        assert "testh0st" in msg  # the typo, named
        assert "Known machines: testhost" in msg  # the registry, listed
        assert read_env_state(str(run_env_pass.data_dir))["last_result"] == "failed"

    def test_valid_hosts_filter_passes(self, isolated_home, run_env_pass):
        _write_json(isolated_home / ".claude" / "env.json", {
            "machines": {"testhost": {"os": "ubuntu"}},
            "env_checks": [
                {"name": "gpu-stack", "check": "true", "hosts": ["testhost"]},
            ],
        })
        result = run_env_pass()
        assert result.failures == []

    def test_entries_without_machines_registry_is_hard_error(
        self, isolated_home, run_env_pass
    ):
        _write_json(isolated_home / ".claude" / "env.json",
                    {"shell_rc": [{"name": "a", "content": "x"}]})

        result = run_env_pass()

        assert len(result.failures) == 1
        assert "machines" in result.failures[0]["message"]
        assert read_env_state(str(run_env_pass.data_dir))["last_result"] == "failed"

    def test_validate_entry_filters_walks_all_sections(self):
        machines = {"a": {"os": "macos"}}
        merged = {
            "machines": machines,
            "symlinks": [{"name": "s", "hosts": ["b"]}],
            "future_section": [{"name": "f", "hosts": ["c"]}],
        }
        errors = validate_entry_filters(merged, machines)
        assert len(errors) == 2
        assert any("symlinks entry 's'" in e for e in errors)
        assert any("future_section entry 'f'" in e for e in errors)

    @pytest.mark.parametrize("filt,value", [
        ("os", "ubuntu"),           # scalar string: 'in' would substring-match
        ("hosts", "testhost"),      # scalar string: would iterate characters
        ("os", {"ubuntu": True}),   # wrong container type
    ])
    def test_non_list_filter_is_a_validation_error(self, filt, value):
        machines = {"testhost": {"os": "ubuntu"}}
        merged = {
            "machines": machines,
            "symlinks": [{"name": "s", filt: value}],
        }
        errors = validate_entry_filters(merged, machines)
        assert len(errors) == 1
        assert f"'{filt}' filter must be a list" in errors[0]
        assert "symlinks entry 's'" in errors[0]

    def test_scalar_filter_fails_the_env_pass(
        self, isolated_home, run_env_pass
    ):
        """A scalar os filter is a persistent failure item; the entry never
        runs (scalar 'in' substring semantics must not silently apply it)."""
        target = isolated_home / "t"
        source = isolated_home / "src"
        source.write_text("x")
        _write_json(isolated_home / ".claude" / "env.json", {
            "machines": {"testhost": {"os": "ubuntu"}},
            "symlinks": [{"name": "s", "source": str(source),
                          "target": str(target), "os": "ubuntu"}],
        })

        result = run_env_pass()

        assert len(result.failures) == 1
        failure = result.failures[0]
        assert failure["type"] == "env_manifest"
        assert failure["name"] == "entry_filter"
        assert failure["persist_across_sessions"] is True
        assert "'os' filter must be a list" in failure["message"]
        assert not target.exists()
        assert read_env_state(str(run_env_pass.data_dir))["last_result"] == "failed"


class TestUnknownSections:
    def test_unknown_section_ignored_with_verbose_line(
        self, isolated_home, run_env_pass
    ):
        _write_json(isolated_home / ".claude" / "env.json", {
            "machines": {"testhost": {"os": "ubuntu"}},
            "frobnicators": [{"name": "x"}],
        })

        result = run_env_pass()

        assert result.failures == []
        assert any(
            "section 'frobnicators' ignored" in e for e in result.ok_entries
        )


class TestEnvGate:
    """The full gate matrix (spec 4.4)."""

    def test_no_env_json_anywhere_is_a_silent_noop(self, run_env_pass):
        result = run_env_pass()
        assert result.failures == []
        assert result.action_entries == []
        assert result.ok_entries == []
        assert read_env_state(str(run_env_pass.data_dir)) is None

    def test_first_run_no_stamp(self, isolated_home, run_env_pass):
        _write_json(isolated_home / ".claude" / "env.json", MACHINES_ONLY)
        result = run_env_pass()
        assert result.ran
        assert "first run (no env stamp)" in result.run_reason()

    def test_clean_pass_then_skip(self, isolated_home, run_env_pass):
        _write_json(isolated_home / ".claude" / "env.json", MACHINES_ONLY)
        first = run_env_pass()
        assert first.ran

        second = run_env_pass()

        assert second.failures == []
        assert not second.ran
        assert second.skipped
        assert second.action_entries == []

    @pytest.mark.parametrize("layer", [
        "user_env", "user_local", "project_env", "project_local",
    ])
    def test_hash_change_in_each_layer_reopens_gate(
        self, isolated_home, tmp_path, run_env_pass, layer
    ):
        project = tmp_path / "project"
        (project / ".claude").mkdir(parents=True)
        _write_json(isolated_home / ".claude" / "env.json", MACHINES_ONLY)
        assert run_env_pass(project_dir=project).ran
        assert run_env_pass(project_dir=project).skipped

        edit = {"machines": {"testhost": {"os": "ubuntu", "skip_repos": ["x"]}}}
        target = {
            "user_env": isolated_home / ".claude" / "env.json",
            "user_local": isolated_home / ".claude" / "env.local.json",
            "project_env": project / ".claude" / "env.json",
            "project_local": project / ".claude" / "env.local.json",
        }[layer]
        _write_json(target, edit)

        result = run_env_pass(project_dir=project)

        assert result.ran
        assert "env.json modified" in result.run_reason()
        assert result.failures == []
        # ...and once re-stamped clean, the gate closes again.
        assert run_env_pass(project_dir=project).skipped

    def test_failed_last_reruns_until_green(self, isolated_home, run_env_pass):
        _write_json(isolated_home / ".claude" / "env.json", MACHINES_ONLY)
        first = run_env_pass(hostname="stranger")
        assert first.failures  # unknown machine -> stamped failed

        # Same manifest, same everything: the failed stamp reopens the gate.
        second = run_env_pass(hostname="stranger")
        assert second.ran
        assert "last pass result was 'failed'" in second.run_reason()
        assert second.failures  # still failing

        # Out-of-band fix (hostname now resolves): re-check goes green...
        third = run_env_pass(hostname="testhost")
        assert third.ran
        assert third.failures == []
        # ...and the gate closes.
        assert run_env_pass(hostname="testhost").skipped

    def test_engine_version_bump_reopens_gate(self, isolated_home, run_env_pass):
        _write_json(isolated_home / ".claude" / "env.json", MACHINES_ONLY)
        assert run_env_pass().ran
        assert run_env_pass().skipped

        result = run_env_pass(engine_version="9.9.9")

        assert result.ran
        assert f"engine updated ({ENGINE_VERSION} -> 9.9.9)" in result.run_reason()

    def test_reset_deleted_stamp_reopens_gate(self, isolated_home, run_env_pass):
        _write_json(isolated_home / ".claude" / "env.json", MACHINES_ONLY)
        assert run_env_pass().ran
        assert run_env_pass().skipped

        (run_env_pass.data_dir / ENV_STATE_STAMP).unlink()

        result = run_env_pass()
        assert result.ran
        assert "first run (no env stamp)" in result.run_reason()

    def test_parse_error_forces_run_and_stamps_failed(
        self, isolated_home, run_env_pass
    ):
        _write_json(isolated_home / ".claude" / "env.json", MACHINES_ONLY)
        assert run_env_pass().ran
        assert run_env_pass().skipped

        # Break the user.local layer WITHOUT changing the merged content:
        # invalid JSON never reaches the merge, so only the parse-error path
        # can reopen the gate here.
        (isolated_home / ".claude" / "env.local.json").write_text("{not json")

        result = run_env_pass()

        assert result.ran
        assert "manifest parse error" in result.run_reason()
        assert len(result.failures) == 1
        assert result.failures[0]["type"] == "manifest_parse"
        assert read_env_state(str(run_env_pass.data_dir))["last_result"] == "failed"

    def test_gate_reason_matrix_unit(self):
        h = "abc123"
        v = "1.0.0"
        clean = {"manifest_sha256": h, "engine_version": v, "last_result": "clean"}
        assert env_gate_reason(None, h, v) == "first run (no env stamp)"
        assert "modified" in env_gate_reason(dict(clean, manifest_sha256="zzz"), h, v)
        assert "last pass result" in env_gate_reason(
            dict(clean, last_result="failed"), h, v)
        assert "engine updated" in env_gate_reason(
            dict(clean, engine_version="0.9"), h, v)
        assert env_gate_reason(clean, h, v) is None

    def test_corrupt_stamp_treated_as_absent(self, tmp_path):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / ENV_STATE_STAMP).write_text("{not json")
        assert read_env_state(str(data_dir)) is None

    def test_state_roundtrip(self, tmp_path):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        write_env_state(str(data_dir), "hash1", "1.2.3", "clean")
        state = read_env_state(str(data_dir))
        assert state == {
            "manifest_sha256": "hash1",
            "engine_version": "1.2.3",
            "last_result": "clean",
        }

    def test_canonical_hash_is_order_independent(self):
        a = {"machines": {"h": {"os": "macos"}}, "shell_rc": []}
        b = {"shell_rc": [], "machines": {"h": {"os": "macos"}}}
        assert canonical_manifest_hash(a) == canonical_manifest_hash(b)
        c = {"machines": {"h": {"os": "ubuntu"}}, "shell_rc": []}
        assert canonical_manifest_hash(a) != canonical_manifest_hash(c)


@needs_bash
class TestEnvResetScript:
    """Behavioral tests for scripts/env-reset-cooldown.sh."""

    def _run(self, *args: str, home: Path) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["HOME"] = str(home)
        return subprocess.run(
            [BASH, str(RESET_SCRIPT), *args],
            capture_output=True, text=True, env=env,
        )

    def _seed_stamp(self, home: Path) -> Path:
        data_dir = home / ".claude" / "plugins" / "data" / "plugins-kit" / "bootstrap"
        data_dir.mkdir(parents=True, exist_ok=True)
        stamp = data_dir / "env_state.json"
        stamp.write_text(json.dumps({
            "manifest_sha256": "abc", "engine_version": "0.33.0",
            "last_result": "clean",
        }))
        return stamp

    def test_default_deletes_stamp_and_clears_cooldown(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        stamp = self._seed_stamp(home)

        result = self._run(home=home)

        assert result.returncode == 0, result.stderr
        assert not stamp.exists()
        assert "reset env stamp" in result.stdout
        # Delegates to bootstrap-reset-cooldown.sh so the next SessionStart
        # actually runs (the cooldown gates the whole pass).
        assert "cooldown" in result.stdout

    def test_reset_without_stamp_reports_and_succeeds(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        result = self._run(home=home)
        assert result.returncode == 0, result.stderr
        assert "no env stamp to reset" in result.stdout

    def test_status_reports_without_writes(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        stamp = self._seed_stamp(home)

        result = self._run("--status", home=home)

        assert result.returncode == 0, result.stderr
        assert stamp.exists(), "--status must not delete the stamp"
        assert "env stamp at" in result.stdout
        assert "manifest_sha256" in result.stdout

    def test_help_prints_usage(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        result = self._run("--help", home=home)
        assert result.returncode == 0
        assert "env-reset-cooldown" in result.stdout

    def test_unknown_argument_errors(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        result = self._run("--bogus", home=home)
        assert result.returncode == 2
        assert "unknown argument" in result.stderr
