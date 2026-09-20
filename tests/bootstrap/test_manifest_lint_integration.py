"""The engine's bare-Python lint and failure hint, observed through the engine.

manifest_lint's functions are pinned in test_manifest_lint.py; these tests
drive ``_process_manifest``, ``_process_env_pass``, ``_main``, and the
install-command strategy, so they go red when the WIRING is removed even
though every helper still works (docs/reference/vacuous-checks.md). Severity
by boundary (plan v3 D21): a shipped plugin manifest's hit is a displayed
action entry; a layered manifest's and an env.json's hit is a quiet
(log-only) entry; a command that fails with a python-not-found shape carries
the hint.

Each test names the revert that turns it red.
"""

from __future__ import annotations

import json

import pytest

import bootstrap_lib.engine as engine
import bootstrap_lib.path_check as path_check
import bootstrap_lib.path_repair as path_repair
import bootstrap_lib.tool_check as tool_check
import bootstrap_lib.tool_paths as tool_paths
from bootstrap_lib import session_env
from bootstrap_lib.env_manifest import ENV_STATE_STAMP, current_hostname
from bootstrap_lib.interpreter_env import (
    CALL_SITE_EXPR,
    FACT_ID,
    PLUGIN_CALL_SITE_EXPR,
    REFERENCE_DOC,
)
from bootstrap_lib.messages import numbered
from bootstrap_lib.platform_detect import detect_os

from bootstrap.test_interpreter_env import git_bash_or_skip

LINTED = "python3 -c 1"
NOT_FOUND = "bash: python3: command not found"
HINT = "; hint: python was not found"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """HOME, the session env file, and every tool-path side effect stay local."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for name in ("CLAUDE_ENV_FILE", "CLAUDE_BOOTSTRAP_DATA_ROOT", "VIRTUAL_ENV",
                 "UV_PROJECT_ENVIRONMENT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(path_check, "add_path_to_shell_config", lambda d: (True, "stub"))
    monkeypatch.setattr(tool_paths, "record", lambda *a, **k: None)
    session_env.reset()
    yield home
    session_env.reset()


@pytest.fixture
def no_path_repair(monkeypatch):
    """The install strategies merge the registry PATH; keep this process's."""
    monkeypatch.setattr(path_repair, "repair_path", lambda: None)


def _entries_text(entries) -> str:
    return "\n".join(str(e) for e in entries)


def _run_manifest(tmp_path, manifest, plugin_name):
    action, ok, quiet = [], [], []
    failures = engine._process_manifest(
        manifest, "linux", str(tmp_path / "data"), str(tmp_path / "plugin"),
        action, ok, plugin_name=plugin_name, quiet_entries=quiet,
    )
    return failures, action, ok, quiet


@pytest.fixture
def no_phases(monkeypatch):
    """Only the lint runs: no phase may execute a manifest command."""
    monkeypatch.setattr(engine, "_MANIFEST_PHASES", ())


# The only bare-python command is another OS's install, so the host never
# runs it; the tools phase still resolves `true` for real in
# test_t9a_plugin_manifest_with_real_phases.
MACOS_ONLY = {"tools": [{"name": "probe", "check": "true",
                         "install": {"macos": LINTED}}]}


class TestT9aSeverityByBoundary:
    def test_t9a_plugin_manifest_is_a_displayed_action(self, tmp_path, no_phases):
        # Revert: delete `_lint_manifest_python(ctx)` from _process_manifest.
        failures, action, ok, quiet = _run_manifest(tmp_path, MACOS_ONLY, "demo-kit")
        assert failures == []
        assert len(action) == 1
        text = str(action[0])
        assert text.startswith(
            "python: demo-kit bootstrap.json probe.install.macos calls bare python3")
        assert PLUGIN_CALL_SITE_EXPR in text and CALL_SITE_EXPR in text
        assert f"/bootstrap fact {FACT_ID}" in text and REFERENCE_DOC in text
        # The collated display line shows the authored short label.
        assert numbered(action) == "probe.install.macos: bare python3 (fact python_interpreter)"
        assert quiet == [] and "python3" not in _entries_text(ok)

    def test_t9a_layered_manifest_is_quiet_only(self, tmp_path, no_phases):
        # Revert: route the layered branch of _lint_manifest_python to
        # ctx.action instead of ctx.quiet.
        failures, action, ok, quiet = _run_manifest(tmp_path, MACOS_ONLY, "config")
        assert failures == []
        assert action == []
        assert "calls bare" not in _entries_text(ok)
        assert len(quiet) == 1
        assert quiet[0].startswith(
            "python: layered manifest probe.install.macos calls bare python3")
        assert PLUGIN_CALL_SITE_EXPR in quiet[0]

    def test_t9a_plugin_manifest_with_real_phases(self, tmp_path, no_path_repair):
        """The lint never turns a passing tool into a failure."""
        git_bash_or_skip()
        failures, action, _ok, _quiet = _run_manifest(tmp_path, MACOS_ONLY, "demo-kit")
        assert failures == []
        assert len(action) == 1 and "calls bare python3" in str(action[0])

    def test_absolute_path_and_variable_commands_are_never_linted(self, tmp_path, no_phases):
        # Revert: delete the `_names_own_location` early return in lint_command.
        manifest = {"tools": [
            {"name": "abs", "check": "python3 /opt/tool/check.py"},
            {"name": "var", "check": 'python3 -c 1 && test -d "$HOME/x"'},
            {"name": "interp", "check": "/usr/bin/python3 -c 1"},
            {"name": "forced", "check": PLUGIN_CALL_SITE_EXPR + " -c 1"},
        ]}
        failures, action, _ok, quiet = _run_manifest(tmp_path, manifest, "demo-kit")
        assert (failures, action, quiet) == ([], [], [])


class TestT9bEnvChecks:
    @pytest.fixture
    def env_pass(self, tmp_path, _isolated):
        home = _isolated
        data = tmp_path / "data"
        plugin = tmp_path / "plugin"
        data.mkdir()
        plugin.mkdir()

        def run(entries):
            (home / ".claude" / "env.json").write_text(json.dumps({
                "machines": {"testhost": {"os": "ubuntu"}},
                "env_checks": entries,
            }))
            stamp = data / ENV_STATE_STAMP
            if stamp.exists():
                stamp.unlink()
            action, ok, quiet = [], [], []
            failures = engine._process_env_pass(
                None, "ubuntu", str(data), str(plugin), action, ok,
                engine_version="0.0.0", hostname="testhost", quiet_entries=quiet)
            return failures, action, ok, quiet

        return run

    def test_t9b_env_checks_bare_python_is_quiet(self, env_pass):
        # Revert: delete the lint loop in _env_phase_env_checks (no quiet
        # entry), or drop `quiet_entries=quiet_entries` from the
        # _EnvManifestContext call in _process_env_pass (entry lost).
        git_bash_or_skip()
        failures, action, ok, quiet = env_pass(
            [{"name": "probe", "check": "exit 0; " + LINTED}])
        assert failures == []
        assert "calls bare" not in _entries_text(action) + _entries_text(ok)
        assert len(quiet) == 1
        assert quiet[0].startswith("python: env.json probe.check calls bare python3")

    def test_t9b_main_logs_env_quiet_entries(self, tmp_path, monkeypatch):
        """_main carries the env pass's quiet entries into bootstrap.log and
        never into the display."""
        # Revert: drop the `bootstrap_quiet_entries.extend(... env_quiet_entries)`
        # line in _main.
        git_bash_or_skip()
        from bootstrap.test_engine_python_export import _home, _project, _run_main
        home = _home(tmp_path)
        (home / ".claude" / "env.json").write_text(json.dumps({
            "machines": {current_hostname(): {"os": detect_os()}},
            "env_checks": [{"name": "probe", "check": "exit 0; " + LINTED}],
        }))
        project = _project(tmp_path, {}, pyproject=False)
        log = _run_main(tmp_path, monkeypatch, project)
        assert "env: python: env.json probe.check calls bare python3" in log, log
        pending = tmp_path / "data" / "bootstrap_display.pending"
        if pending.exists():
            assert "calls bare" not in pending.read_text(encoding="utf-8")


class TestT9cFailureHint:
    @staticmethod
    def _install(tmp_path):
        tool = {"name": "ghost", "installPath": str(tmp_path / "nowhere"),
                "install": {"linux": LINTED}}
        action, ok = [], []
        failure = engine._process_tool_entry(
            tool, "linux", "/data", "", action, ok, [], plugin_name="config")
        return failure, action

    def test_t9c_install_failure_output_carries_hint(self, tmp_path, monkeypatch,
                                                     no_path_repair):
        # Revert: set `failure_message = result.message` (message red) or drop
        # the `_with_python_hint(` wrapper around the install_failed entry
        # (entry red).
        monkeypatch.setattr(tool_check, "run_install",
                            lambda cmd: (False, "some output\n" + NOT_FOUND + "\n"))
        failure, action = self._install(tmp_path)
        assert failure["install_state"] == "install_failed"
        text = _entries_text(action)
        assert "install command failed" in text
        assert HINT in text
        assert PLUGIN_CALL_SITE_EXPR in text
        assert HINT in failure["message"]
        assert f"/bootstrap fact {FACT_ID}" in failure["message"]
        # The collated display still shows only the subject and the verdict.
        assert numbered(action) == "ghost: install command failed"

    def test_t9c_unrelated_failure_has_no_hint(self, tmp_path, monkeypatch, no_path_repair):
        monkeypatch.setattr(tool_check, "run_install",
                            lambda cmd: (False, "error: disk full"))
        failure, action = self._install(tmp_path)
        assert failure["install_state"] == "install_failed"
        assert "hint:" not in _entries_text(action)
        assert "hint:" not in failure["message"]

    def test_t9c_env_check_fix_failure_carries_hint(self, tmp_path, monkeypatch,
                                                    no_path_repair):
        # Revert: delete `fix_detail = _with_python_hint(fix_detail, fix_detail)`.
        calls = []

        def fake_run(command, timeout):
            calls.append(command)
            if command == "fix-it":
                return 127, NOT_FOUND
            return 1, "not configured"

        monkeypatch.setattr("bootstrap_lib.env_features.run_env_command", fake_run)
        monkeypatch.setattr(engine, "_privileges_available", lambda os_: True)
        action, ok, quiet = [], [], []
        ctx = engine._EnvManifestContext(
            {"env_checks": [{"name": "probe", "check": "check-it", "fix": "fix-it"}]},
            "ubuntu", str(tmp_path), str(tmp_path), action, ok, None,
            "testhost", {"testhost": {"os": "ubuntu"}}, quiet_entries=quiet)
        engine._env_phase_env_checks(ctx)
        assert calls == ["check-it", "fix-it", "check-it"]
        assert len(ctx.failures) == 1
        failure = ctx.failures[0]
        for field in ("message", "user_msg"):
            assert HINT in failure[field], field
        assert HINT in _entries_text(action)
        assert numbered(action) == "env_check probe: FAILED"


class TestT9dUnrelatedShellSyntax:
    def test_t9d_version_case_check_is_untouched(self, tmp_path, no_phases):
        """A godot-style `case "$ver" in ${want}.*` check draws no lint entry
        and is not rewritten by variable resolution."""
        # A regression guard for acceptance check 4: the command holds no bare
        # python, so no single-line revert turns it red. Red run: drop the
        # `_names_own_location` early return AND add "case" to _BAD_EXACT
        # (two lines) -- the lint then flags the check.
        check = ('ver="$(godot --version 2>/dev/null)"; want=4.3; '
                 'case "$ver" in ${want}.*) exit 0;; esac; exit 1')
        manifest = {"tools": [{"name": "godot", "check": check}]}
        before = json.dumps(manifest, sort_keys=True)
        failures, action, _ok, quiet = _run_manifest(tmp_path, manifest, "demo-kit")
        assert (failures, action, quiet) == ([], [], [])
        assert json.dumps(manifest, sort_keys=True) == before
