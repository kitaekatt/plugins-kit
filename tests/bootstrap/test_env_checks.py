"""Tests for the env_checks check/fix contract (E1 step 5).

Covers bootstrap_lib/env_features.py::run_env_command and the engine's
_env_phase_env_checks handler (bootstrap-env-refactor spec section 5):
dispatch order (check -> fix -> authoritative re-check, NO trust
exceptions), check-only manual-attention items, per-entry timeouts with
R1-style exception containment, elevation-queue integration (the
{method: "command"} descriptor + golden remediation-script content),
message extraction (last non-empty stdout/stderr line), os/hosts filters,
entry validation, gate stamping, and the engine-level no-wedge guarantee.

Commands run through the REAL bash shim against an isolated HOME (the
test_env_features.py pattern); the subprocess seam is faked only for the
containment tests.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import bootstrap_lib.engine as engine
import bootstrap_lib.env_features as env_features
from bootstrap_lib.elevation import queue_from_failures, write_or_clear_script
from bootstrap_lib.engine import _ENV_PHASES, _env_phase_env_checks, _process_env_pass
from bootstrap_lib.env_features import ENV_CHECK_DEFAULT_TIMEOUT, run_env_command
from bootstrap_lib.env_manifest import ENV_STATE_STAMP, read_env_state

ENGINE_VERSION = "0.34.0"


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Point HOME at a tmp dir so env.json layers and ~ expansion are isolated."""
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
    the handler rather than the gate skip (the gate matrix is
    test_env_manifest.py's subject).
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


def _fake_subprocess(run):
    """A subprocess stand-in: fake `run`, real exception classes."""
    return SimpleNamespace(
        run=run,
        SubprocessError=subprocess.SubprocessError,
        TimeoutExpired=subprocess.TimeoutExpired,
    )


# ---------------------------------------------------------------------------
# run_env_command (the bash-shim runner): exit codes, message extraction,
# timeout containment
# ---------------------------------------------------------------------------

class TestRunEnvCommand:
    def test_exit_zero_with_last_line(self):
        rc, detail = run_env_command("echo first; echo last", timeout=10)
        assert rc == 0
        assert detail == "last"

    def test_nonzero_exit_code_reported(self):
        rc, detail = run_env_command("exit 3", timeout=10)
        assert rc == 3
        assert detail == "exit code 3"

    def test_stderr_preferred_over_stdout(self):
        rc, detail = run_env_command(
            "echo out; echo err 1>&2; exit 1", timeout=10)
        assert rc == 1
        assert detail == "err"

    def test_last_nonempty_line_extracted(self):
        rc, detail = run_env_command(
            "printf 'one\\ntwo\\n\\n\\n'; exit 1", timeout=10)
        assert rc == 1
        assert detail == "two"

    def test_tilde_expands_via_shell(self, isolated_home):
        (isolated_home / "flag").write_text("")
        rc, _ = run_env_command("test -f ~/flag", timeout=10)
        assert rc == 0

    def test_timeout_is_contained(self):
        rc, detail = run_env_command("sleep 3", timeout=1)
        assert rc is None
        assert detail == "timed out after 1s"

    def test_oserror_is_contained(self, monkeypatch):
        def raising_run(cmd, **kwargs):
            raise FileNotFoundError(2, "No such file or directory", "bash")

        monkeypatch.setattr(
            env_features, "subprocess", _fake_subprocess(raising_run))
        rc, detail = run_env_command("true", timeout=10)
        assert rc is None
        assert "could not run" in detail

    def test_default_timeout_is_600(self):
        assert ENV_CHECK_DEFAULT_TIMEOUT == 600


# ---------------------------------------------------------------------------
# Dispatch order: check -> fix -> authoritative re-check
# ---------------------------------------------------------------------------

class TestDispatchOrder:
    def _entry(self, home, tmp_path):
        log = tmp_path / "call.log"
        flag = home / "flag"
        return log, flag, {
            "name": "flag-check",
            "check": f"echo check >> {log}; test -f {flag}",
            "fix": f"echo fix >> {log}; touch {flag}; echo created flag",
        }

    def test_passing_check_runs_nothing_else(
        self, isolated_home, run_env_pass, tmp_path
    ):
        log, flag, entry = self._entry(isolated_home, tmp_path)
        flag.write_text("")
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(env_checks=[entry]))

        result = run_env_pass()

        assert result.failures == []
        assert result.action_entries == []
        assert any("env_check flag-check: ok" in e for e in result.ok_entries)
        assert log.read_text().splitlines() == ["check"]

    def test_check_fix_recheck_order(
        self, isolated_home, run_env_pass, tmp_path
    ):
        log, flag, entry = self._entry(isolated_home, tmp_path)
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(env_checks=[entry]))

        result = run_env_pass()

        assert result.failures == []
        assert log.read_text().splitlines() == ["check", "fix", "check"]
        assert flag.exists()
        # The action message carries the fix's last output line.
        assert any("env_check flag-check: fixed - created flag" in e
                   for e in result.action_entries)
        assert read_env_state(str(run_env_pass.data_dir))["last_result"] == "clean"

    def test_second_pass_is_idempotent(
        self, isolated_home, run_env_pass, tmp_path
    ):
        log, flag, entry = self._entry(isolated_home, tmp_path)
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(env_checks=[entry]))
        assert run_env_pass().failures == []
        log.unlink()

        second = run_env_pass()

        assert second.failures == []
        assert second.action_entries == []
        assert log.read_text().splitlines() == ["check"]

    def test_entries_process_in_array_order(
        self, isolated_home, run_env_pass, tmp_path
    ):
        log = tmp_path / "order.log"
        entries = [
            {"name": "first", "check": f"echo first >> {log}; true"},
            {"name": "second", "check": f"echo second >> {log}; true"},
        ]
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(env_checks=entries))

        result = run_env_pass()

        assert result.failures == []
        assert log.read_text().splitlines() == ["first", "second"]

    def test_env_checks_is_the_last_env_phase(self):
        assert _ENV_PHASES[-1] == (("env_checks",), _env_phase_env_checks)


# ---------------------------------------------------------------------------
# Re-check authority: NO trust exceptions
# ---------------------------------------------------------------------------

class TestRecheckAuthority:
    def test_fix_lies_success_but_recheck_fails(
        self, isolated_home, run_env_pass
    ):
        entry = {
            "name": "liar",
            "check": f"test -f {isolated_home / 'flag'}",
            "fix": "echo pretended to fix it; true",
        }
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(env_checks=[entry]))

        result = run_env_pass()

        assert len(result.failures) == 1
        failure = result.failures[0]
        assert failure["type"] == "env_check"
        assert failure["persist_across_sessions"] is True
        # The message is the fix's last non-empty output line (spec step 4).
        assert failure["message"] == "liar: pretended to fix it"
        assert read_env_state(str(run_env_pass.data_dir))["last_result"] == "failed"

    def test_fix_exit_code_is_advisory_when_recheck_passes(
        self, isolated_home, run_env_pass
    ):
        flag = isolated_home / "flag"
        entry = {
            "name": "grumpy-fixer",
            "check": f"test -f {flag}",
            "fix": f"touch {flag}; echo done anyway; exit 7",
        }
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(env_checks=[entry]))

        result = run_env_pass()

        assert result.failures == []
        assert any("env_check grumpy-fixer: fixed - done anyway" in e
                   for e in result.action_entries)
        assert read_env_state(str(run_env_pass.data_dir))["last_result"] == "clean"

    def test_failing_fix_message_uses_fix_output(
        self, isolated_home, run_env_pass
    ):
        entry = {
            "name": "broken",
            "check": "false",
            "fix": "echo error: no network 1>&2; exit 1",
        }
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(env_checks=[entry]))

        result = run_env_pass()

        assert len(result.failures) == 1
        assert result.failures[0]["message"] == "broken: error: no network"
        assert any("env_check broken: FAILED - error: no network" in e
                   for e in result.action_entries)


# ---------------------------------------------------------------------------
# Check-only entries (no fix): persistent manual-attention items
# ---------------------------------------------------------------------------

class TestCheckOnly:
    def test_failing_check_only_is_manual_attention(
        self, isolated_home, run_env_pass
    ):
        entry = {
            "name": "cuda-toolkit",
            "check": "echo nvcc not found; false",
            "description": "Install CUDA Toolkit via the NVIDIA installer (manual)",
        }
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(env_checks=[entry]))

        result = run_env_pass()

        assert len(result.failures) == 1
        failure = result.failures[0]
        assert failure["type"] == "env_check"
        assert failure["persist_across_sessions"] is True
        # name + description + last output line (spec step 2).
        assert failure["message"] == (
            "cuda-toolkit: Install CUDA Toolkit via the NVIDIA installer "
            "(manual); nvcc not found"
        )
        assert "elevation" not in failure
        assert read_env_state(str(run_env_pass.data_dir))["last_result"] == "failed"

    def test_check_only_without_description(self, isolated_home, run_env_pass):
        entry = {"name": "reboot-flag",
                 "check": "echo reboot required; false"}
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(env_checks=[entry]))

        result = run_env_pass()

        assert len(result.failures) == 1
        assert result.failures[0]["message"] == "reboot-flag: reboot required"

    def test_check_only_never_fixes(self, isolated_home, run_env_pass, tmp_path):
        # A check-only entry must never run anything beyond the check --
        # exactly one check invocation, no second (re-check) run either.
        log = tmp_path / "calls.log"
        entry = {"name": "probe", "check": f"echo ran >> {log}; false",
                 "description": "manual thing"}
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(env_checks=[entry]))

        result = run_env_pass()

        assert len(result.failures) == 1
        assert log.read_text().splitlines() == ["ran"]

    def test_passing_check_only_is_ok(self, isolated_home, run_env_pass):
        entry = {"name": "probe", "check": "true",
                 "description": "manual thing"}
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(env_checks=[entry]))
        result = run_env_pass()
        assert result.failures == []
        assert any("env_check probe: ok" in e for e in result.ok_entries)


# ---------------------------------------------------------------------------
# Timeouts: per-entry field, honored and contained
# ---------------------------------------------------------------------------

class TestTimeouts:
    def test_check_timeout_is_contained_and_fix_never_runs(
        self, isolated_home, run_env_pass
    ):
        marker = isolated_home / "fixed"
        entry = {"name": "slow-check", "check": "sleep 3",
                 "fix": f"touch {marker}", "timeout": 1}
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(env_checks=[entry]))

        result = run_env_pass()

        assert len(result.failures) == 1
        assert "check could not run" in result.failures[0]["message"]
        assert "timed out after 1s" in result.failures[0]["message"]
        # Unknown state: the fix must NOT have been attempted.
        assert not marker.exists()
        assert read_env_state(str(run_env_pass.data_dir))["last_result"] == "failed"

    def test_fix_timeout_is_contained(self, isolated_home, run_env_pass):
        entry = {"name": "slow-fix", "check": "false",
                 "fix": "sleep 3", "timeout": 1}
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(env_checks=[entry]))

        result = run_env_pass()

        assert len(result.failures) == 1
        assert result.failures[0]["message"] == "slow-fix: timed out after 1s"

    def test_default_timeout_used_when_absent(
        self, isolated_home, run_env_pass, monkeypatch
    ):
        seen = []

        def recording(command, timeout):
            seen.append((command, timeout))
            return 0, "ok"

        monkeypatch.setattr(env_features, "run_env_command", recording)
        entry = {"name": "probe", "check": "true"}
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(env_checks=[entry]))

        assert run_env_pass().failures == []
        assert seen == [("true", ENV_CHECK_DEFAULT_TIMEOUT)]

    def test_entry_timeout_passed_to_every_command(
        self, isolated_home, run_env_pass, monkeypatch
    ):
        seen = []

        def recording(command, timeout):
            seen.append((command, timeout))
            return (1, "nope") if command == "check-cmd" and len(seen) == 1 \
                else (0, "done")

        monkeypatch.setattr(env_features, "run_env_command", recording)
        entry = {"name": "probe", "check": "check-cmd", "fix": "fix-cmd",
                 "timeout": 42}
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(env_checks=[entry]))

        assert run_env_pass().failures == []
        assert seen == [("check-cmd", 42), ("fix-cmd", 42), ("check-cmd", 42)]

    @pytest.mark.parametrize("timeout", [0, -5, "60", 1.5, True])
    def test_invalid_timeout_is_a_failure(
        self, isolated_home, run_env_pass, timeout
    ):
        entry = {"name": "probe", "check": "true", "timeout": timeout}
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(env_checks=[entry]))
        result = run_env_pass()
        assert len(result.failures) == 1
        assert "invalid timeout" in result.failures[0]["message"]


# ---------------------------------------------------------------------------
# Elevation: deferred via the queue, never self-elevated
# ---------------------------------------------------------------------------

SUDOERS_FIX = "bash ~/.claude/scripts/env/sudoers.sh fix"
GPU_FIX = "bash ~/.claude/scripts/env/gpu-stack.sh fix"


class TestElevation:
    def _elevated_entry(self, marker, name="sudoers", fix=SUDOERS_FIX):
        return {"name": name, "check": f"test -f {marker}",
                "fix": fix, "elevated": True}

    def test_missing_privileges_defers_with_descriptor(
        self, isolated_home, run_env_pass, monkeypatch
    ):
        monkeypatch.setattr(engine, "_privileges_available", lambda os_: False)
        marker = isolated_home / "configured"
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(env_checks=[self._elevated_entry(marker)]))

        result = run_env_pass()

        assert len(result.failures) == 1
        failure = result.failures[0]
        assert failure["type"] == "env_check"
        assert failure["persist_across_sessions"] is True
        assert failure["elevation"] == {
            "method": "command", "command": SUDOERS_FIX, "os": "ubuntu"}
        assert SUDOERS_FIX in failure["message"]
        assert SUDOERS_FIX in failure["agent_msg"]
        # NEVER attempted: nothing created the marker.
        assert not marker.exists()
        # Deferred elevation stamps the pass failed, keeping the gate open
        # so the next session re-checks out-of-band completion.
        assert read_env_state(str(run_env_pass.data_dir))["last_result"] == "failed"

    def test_privileges_available_runs_fix_directly(
        self, isolated_home, run_env_pass, monkeypatch
    ):
        monkeypatch.setattr(engine, "_privileges_available", lambda os_: True)
        marker = isolated_home / "configured"
        entry = self._elevated_entry(
            marker, fix=f"touch {marker}; echo sudoers configured")
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(env_checks=[entry]))

        result = run_env_pass()

        assert result.failures == []
        assert marker.exists()
        assert any("env_check sudoers: fixed - sudoers configured" in e
                   for e in result.action_entries)

    def test_passing_check_never_consults_privileges(
        self, isolated_home, run_env_pass, monkeypatch
    ):
        monkeypatch.setattr(
            engine, "_privileges_available",
            lambda os_: (_ for _ in ()).throw(
                AssertionError("must not probe privileges when configured")))
        marker = isolated_home / "configured"
        marker.write_text("")
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(env_checks=[self._elevated_entry(marker)]))

        result = run_env_pass()

        assert result.failures == []

    def test_non_elevated_entry_never_consults_privileges(
        self, isolated_home, run_env_pass, monkeypatch
    ):
        monkeypatch.setattr(
            engine, "_privileges_available",
            lambda os_: (_ for _ in ()).throw(
                AssertionError("must not probe privileges for unelevated fix")))
        flag = isolated_home / "flag"
        entry = {"name": "plain", "check": f"test -f {flag}",
                 "fix": f"touch {flag}"}
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(env_checks=[entry]))

        result = run_env_pass()

        assert result.failures == []
        assert flag.exists()

    def test_deferred_fixes_harvest_into_queue_in_order(
        self, isolated_home, run_env_pass, monkeypatch
    ):
        monkeypatch.setattr(engine, "_privileges_available", lambda os_: False)
        entries = [
            self._elevated_entry(isolated_home / "a", "sudoers", SUDOERS_FIX),
            self._elevated_entry(isolated_home / "b", "gpu-stack", GPU_FIX),
        ]
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(env_checks=entries))

        result = run_env_pass()

        queue = queue_from_failures(result.failures, "ubuntu")
        assert queue.commands == [SUDOERS_FIX, GPU_FIX]
        assert queue.apt_packages == []

    def test_golden_remediation_script_with_queued_env_fix(
        self, isolated_home, run_env_pass, monkeypatch, tmp_path
    ):
        """The queued env fix lands in the per-OS remediation script exactly
        as an elevated tool command would (spec step 5's named test)."""
        monkeypatch.setattr(engine, "_privileges_available", lambda os_: False)
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(env_checks=[
                        self._elevated_entry(isolated_home / "a")]))

        result = run_env_pass()

        queue = queue_from_failures(result.failures, "ubuntu")
        script = write_or_clear_script(queue, str(tmp_path / "elev"), "ubuntu")
        assert script is not None
        content = open(script).read()
        assert content.startswith("#!/usr/bin/env bash\n")
        assert "set -euo pipefail" in content
        assert "must never prompt for a sudo password" in content
        assert f'sudo bash "{script}"' in content
        # Comment label (zero execution surface) then the command itself,
        # with ~ pre-expanded to the invoking user's home: the script runs
        # under `sudo bash` (HOME=/root), so the verbatim ~ of SUDOERS_FIX
        # would resolve to root's home and abort the script.
        expanded_fix = f"bash {isolated_home}/.claude/scripts/env/sudoers.sh fix"
        assert f"# bootstrap-elevate: {expanded_fix}\n{expanded_fix}\n" in content
        assert "~/.claude" not in content
        # The queue itself keeps the verbatim command (expansion is render-time).
        assert queue.commands == [SUDOERS_FIX]
        # No apt section: the env fix is a plain deferred command.
        assert "apt-get" not in content

    def test_next_session_recheck_clears_queue_and_script(
        self, isolated_home, run_env_pass, monkeypatch, tmp_path
    ):
        """Out-of-band completion converges: the reopened gate re-checks,
        the queue empties, and the stale script is removed."""
        monkeypatch.setattr(engine, "_privileges_available", lambda os_: False)
        marker = isolated_home / "configured"
        elev_dir = tmp_path / "elev"
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(env_checks=[self._elevated_entry(marker)]))

        first = run_env_pass()
        script = write_or_clear_script(
            queue_from_failures(first.failures, "ubuntu"),
            str(elev_dir), "ubuntu")
        assert script and os.path.isfile(script)

        # The user ran the remediation script out of band...
        marker.write_text("")
        # ...and the next session's pass re-runs WITHOUT a reset: the failed
        # stamp alone reopens the gate.
        action_entries: list = []
        ok_entries: list = []
        failures = _process_env_pass(
            None, "ubuntu", str(run_env_pass.data_dir), str(tmp_path / "plugin"),
            action_entries, ok_entries,
            engine_version=ENGINE_VERSION, hostname="testhost",
        )

        assert failures == []
        assert read_env_state(str(run_env_pass.data_dir))["last_result"] == "clean"
        cleared = write_or_clear_script(
            queue_from_failures(failures, "ubuntu"), str(elev_dir), "ubuntu")
        assert cleared is None
        assert not os.path.exists(script)


# ---------------------------------------------------------------------------
# os/hosts filters + entry validation
# ---------------------------------------------------------------------------

class TestFiltersAndValidation:
    def test_os_filter_skips_entry(self, isolated_home, run_env_pass):
        entry = {"name": "reboot-flag", "check": "false",
                 "os": ["windows"], "description": "reboot"}
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(env_checks=[entry]))

        result = run_env_pass(current_os="ubuntu")

        assert result.failures == []
        assert any("env_check reboot-flag: skipped (os/hosts filter)" in e
                   for e in result.ok_entries)

    def test_hosts_filter_skips_entry(self, isolated_home, run_env_pass):
        manifest = {
            "machines": {"testhost": {"os": "ubuntu"},
                         "5090RTX": {"os": "ubuntu"}},
            "env_checks": [{"name": "gpu-stack", "check": "false",
                            "fix": GPU_FIX, "hosts": ["5090RTX"]}],
        }
        _write_json(isolated_home / ".claude" / "env.json", manifest)

        result = run_env_pass()

        assert result.failures == []
        assert any("env_check gpu-stack: skipped (os/hosts filter)" in e
                   for e in result.ok_entries)

    @pytest.mark.parametrize("entry", [
        {"check": "true"},                          # no name
        {"name": "x"},                              # no check
        {"name": "x", "check": ""},                 # empty check
        {"name": "x", "check": "true", "fix": 7},   # non-string fix
        {"name": "x", "check": "true", "fix": ""},  # empty fix
        "just-a-string",                            # not a dict
    ])
    def test_invalid_entries_fail(self, isolated_home, run_env_pass, entry):
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(env_checks=[entry]))
        result = run_env_pass()
        assert len(result.failures) == 1
        assert "invalid env_checks entry" in result.failures[0]["message"]

    def test_unnamed_invalid_entry_uses_placeholder(
        self, isolated_home, run_env_pass
    ):
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(env_checks=[{"check": "true"}]))
        result = run_env_pass()
        assert len(result.failures) == 1
        assert result.failures[0]["name"] == "(unnamed)"

    def test_non_array_section_is_a_failure(self, isolated_home, run_env_pass):
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(env_checks={"name": "not-a-list"}))
        result = run_env_pass()
        assert len(result.failures) == 1
        assert "must be an array" in result.failures[0]["message"]
        # Section-shape errors carry the section's per-entry type (singular).
        assert result.failures[0]["type"] == "env_check"


# ---------------------------------------------------------------------------
# Engine-level no-wedge: containment inside the full env pass
# ---------------------------------------------------------------------------

class TestEngineNoWedge:
    def test_broken_subprocess_seam_never_escapes_the_pass(
        self, isolated_home, run_env_pass, monkeypatch
    ):
        """A subprocess layer that cannot launch anything (the R1 scenario)
        lands as one contained env_check failure; sibling sections in the
        same pass still converge and the pass stamps failed, not wedged."""
        def raising_run(cmd, **kwargs):
            raise FileNotFoundError(2, "No such file or directory", "sh")

        monkeypatch.setattr(
            env_features, "subprocess", _fake_subprocess(raising_run))

        source = isolated_home / "src.toml"
        source.write_text("x")
        target = isolated_home / ".config" / "starship.toml"
        manifest = _manifest(
            symlinks=[{"name": "starship-config", "source": str(source),
                       "target": str(target)}],
            env_checks=[{"name": "probe", "check": "true", "fix": "true"}],
        )
        _write_json(isolated_home / ".claude" / "env.json", manifest)

        result = run_env_pass()

        assert len(result.failures) == 1
        assert result.failures[0]["type"] == "env_check"
        assert "check could not run" in result.failures[0]["message"]
        # The symlink section still converged in the same pass.
        assert target.is_symlink()
        assert read_env_state(str(run_env_pass.data_dir))["last_result"] == "failed"

    def test_one_failing_check_never_blocks_the_next(
        self, isolated_home, run_env_pass, tmp_path
    ):
        log = tmp_path / "order.log"
        entries = [
            {"name": "bad", "check": "echo broken; false"},
            {"name": "good", "check": f"echo good >> {log}; true"},
        ]
        _write_json(isolated_home / ".claude" / "env.json",
                    _manifest(env_checks=entries))

        result = run_env_pass()

        assert len(result.failures) == 1
        assert result.failures[0]["name"] == "bad"
        assert log.read_text().splitlines() == ["good"]
