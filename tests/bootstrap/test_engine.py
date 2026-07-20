"""Integration tests for bootstrap engine/bootstrap_engine.py."""

import json
import os
import subprocess
import sys

import pytest

from bootstrap.link_compat import link_tree

BOOTSTRAP_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "plugins", "bootstrap")
)
ENGINE_SCRIPT = os.path.join(BOOTSTRAP_ROOT, "engine", "bootstrap_engine.py")


def run_engine(data_dir, plugin_root=BOOTSTRAP_ROOT, home=None):
    """Run the bootstrap engine as a subprocess.

    When ``home`` is given, HOME/USERPROFILE are overridden for the subprocess
    so the engine resolves the production registry
    (``$HOME/.claude/plugins/installed_plugins.json``, see engine.py) under an
    isolated, empty home instead of the developer's real one. Without this, a
    populated dev box makes the engine enumerate and provision real installed
    plugins, so "silent on success" assertions fail off-CI even though the code
    is correct (CI passes either way because its home has no installed plugins).
    """
    env = dict(os.environ)
    if home is not None:
        env["HOME"] = home
        env["USERPROFILE"] = home
        # Pre-satisfy self_setup.path_entries (~/.local/bin) in the live PATH so
        # check_path_entry passes (a gated ok) instead of performing+reporting a
        # PATH add -- which would emit an action line AND mutate .bashrc / the
        # Windows User PATH registry on every run.
        local_bin = os.path.join(home, ".local", "bin")
        env["PATH"] = local_bin + os.pathsep + env.get("PATH", "")
    return subprocess.run(
        [sys.executable, ENGINE_SCRIPT, "--plugin-root", plugin_root, "--data-dir", data_dir],
        capture_output=True,
        text=True,
        env=env,
    )


class TestEngineIntegration:
    @staticmethod
    def _make_minimal_root(tmp_path):
        """Create a fake bootstrap root with minimal ecosystem manifest."""
        fake_root = tmp_path / "bootstrap_minimal"
        fake_root.mkdir()
        link_tree(fake_root / "bootstrap_lib", os.path.join(BOOTSTRAP_ROOT, "bootstrap_lib"))
        link_tree(fake_root / "engine", os.path.join(BOOTSTRAP_ROOT, "engine"))
        # Custom defaults with self_setup containing tools
        defaults = fake_root / "defaults"
        defaults.mkdir()
        config = {
            "schema_version": 5,
            "no_bootstrap": [],
            "bootstrap_cache": [],
            "log_success_shell": False,
            "log_success_checks": False,
            "self_setup": {
                "tools": [{"name": "git", "install": {"macos": "brew install git"}}],
                "path_entries": ["~/.local/bin"],
            },
        }
        (defaults / "config.json").write_text(json.dumps(config))
        manifest = {}
        (fake_root / "bootstrap.json").write_text(json.dumps(manifest))
        return str(fake_root)

    @staticmethod
    def _isolated_home(tmp_path):
        """An empty home so the engine sees no production installed_plugins.json."""
        home = tmp_path / "home"
        home.mkdir()
        return str(home)

    def test_first_run_silent_on_success(self, data_dir, tmp_path):
        """With log_success disabled, successful runs produce no output."""
        fake_root = self._make_minimal_root(tmp_path)
        result = run_engine(data_dir, plugin_root=fake_root, home=self._isolated_home(tmp_path))
        assert result.returncode == 0
        # No stdout when everything succeeds and success logging is off
        assert result.stdout.strip() == ""

    def test_cached_run_silent(self, data_dir, tmp_path):
        """Second run should hit cache — no output with success logging off."""
        fake_root = self._make_minimal_root(tmp_path)
        home = self._isolated_home(tmp_path)
        run_engine(data_dir, plugin_root=fake_root, home=home)  # First run populates cache
        result = run_engine(data_dir, plugin_root=fake_root, home=home)  # Second run hits cache
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_failure_emits_json(self, data_dir, tmp_path):
        """A self_setup with a fake tool should produce JSON failure output."""
        fake_root = tmp_path / "fake_plugin"
        fake_root.mkdir()
        link_tree(fake_root / "bootstrap_lib", os.path.join(BOOTSTRAP_ROOT, "bootstrap_lib"))
        link_tree(fake_root / "engine", os.path.join(BOOTSTRAP_ROOT, "engine"))
        defaults = fake_root / "defaults"
        defaults.mkdir()
        config = {
            "schema_version": 5,
            "no_bootstrap": [],
            "bootstrap_cache": [],
            "log_success_shell": False,
            "log_success_checks": False,
            "self_setup": {
                "tools": [{"name": "nonexistent_tool_xyz_abc", "install": {"macos": "brew install fake"}}],
            },
        }
        (defaults / "config.json").write_text(json.dumps(config))
        (fake_root / "bootstrap.json").write_text(json.dumps({}))

        result = run_engine(data_dir, plugin_root=str(fake_root),
                            home=self._isolated_home(tmp_path))
        assert result.returncode == 0
        assert result.stdout.strip() != ""

        response = json.loads(result.stdout)
        assert response["continue"] is True
        assert "hookSpecificOutput" in response
        assert "nonexistent_tool_xyz_abc" in response["hookSpecificOutput"]["additionalContext"]

    def test_remediation_attempted_but_still_fails(self, data_dir, tmp_path):
        """When install command runs but tool is still missing, failure JSON is emitted."""
        fake_root = tmp_path / "fake_plugin"
        fake_root.mkdir()
        link_tree(fake_root / "bootstrap_lib", os.path.join(BOOTSTRAP_ROOT, "bootstrap_lib"))
        link_tree(fake_root / "engine", os.path.join(BOOTSTRAP_ROOT, "engine"))
        defaults = fake_root / "defaults"
        defaults.mkdir()
        config = {
            "schema_version": 5,
            "no_bootstrap": [],
            "bootstrap_cache": [],
            "log_success_shell": False,
            "log_success_checks": False,
            "self_setup": {
                "tools": [{
                    "name": "nonexistent_tool_xyz_abc",
                    "install": {
                        "macos": f"{sys.executable} -c 'pass'",
                        "windows": f"{sys.executable} -c 'pass'",
                        "ubuntu": f"{sys.executable} -c 'pass'",
                    },
                }],
            },
        }
        (defaults / "config.json").write_text(json.dumps(config))
        (fake_root / "bootstrap.json").write_text(json.dumps({}))

        result = run_engine(data_dir, plugin_root=str(fake_root),
                            home=self._isolated_home(tmp_path))
        assert result.returncode == 0
        assert result.stdout.strip() != ""

        response = json.loads(result.stdout)
        assert response["continue"] is True
        assert "nonexistent_tool_xyz_abc" in response["hookSpecificOutput"]["additionalContext"]

    def test_config_migration_on_run(self, data_dir, tmp_path):
        """Engine should migrate v0 config to current version on first run."""
        # Pre-create a v0 config
        os.makedirs(data_dir, exist_ok=True)
        v0_config = {"some_setting": True}
        with open(os.path.join(data_dir, "config.json"), "w") as f:
            json.dump(v0_config, f)

        result = run_engine(data_dir, home=self._isolated_home(tmp_path))
        assert result.returncode == 0

        # Config should now be current version
        with open(os.path.join(data_dir, "config.json")) as f:
            config = json.load(f)
        assert config["schema_version"] == 6
        assert config["some_setting"] is True
        assert config["log_success_shell"] is False
        assert config["log_success_checks"] is False


class TestPythonStubIntegration:
    """Integration test: _process_self_setup with python_stub_check failing.

    Uses in-process invocation (not subprocess) so we can monkeypatch the
    check_python_stub detection function. This still exercises the engine
    code path that wires the manifest entry to the check + remediation.
    """

    def test_python_stub_fail_writes_bat_file(self, tmp_path, monkeypatch):
        # Make bootstrap_lib importable
        sys.path.insert(0, BOOTSTRAP_ROOT)
        try:
            from bootstrap_lib import python_stub_check as psc
            from bootstrap_lib.engine import _process_self_setup
        finally:
            pass

        # Force the check to report a stub-shadowing failure regardless of host state
        bad_python = r"C:\Users\fake\AppData\Local\Microsoft\WindowsApps\python.exe"
        good_dir = str(tmp_path / "standalone" / "python")

        def fake_check(good_python_dir, stub_markers):
            return psc._stub_result(
                False,
                f"stub python (WindowsApps) shadows standalone python: {bad_python}",
                bad_python,
                good_dir,
            )

        monkeypatch.setattr(
            "bootstrap_lib.python_stub_check.check_python_stub", fake_check
        )

        script_out = tmp_path / "fake_desktop"
        self_setup = {
            "python_stub_check": {
                "good_python_dir": good_dir,
                "stub_markers": ["WindowsApps"],
                "script_output_dir": str(script_out),
            }
        }

        action_entries = []
        ok_entries = []
        failures = _process_self_setup(
            self_setup,
            "windows",
            str(tmp_path / "data"),
            str(tmp_path / "plugin"),
            action_entries,
            ok_entries,
        )

        # The bat file should have been written to the tmp script_out dir
        bat_path = script_out / "fix_python_path.bat"
        assert bat_path.exists(), f"expected {bat_path} to exist"

        # action_entries should record the detection + script write (terse log line)
        assert any("python stub:" in e for e in action_entries)
        assert any(bad_python in e for e in action_entries)
        assert any(str(bat_path) in e for e in action_entries)

        # failures list should include a python_stub failure with the structured
        # user_msg / agent_msg fields and persist_across_sessions marker
        stub_failures = [f for f in failures if f.get("type") == "python_stub"]
        assert len(stub_failures) == 1
        sf = stub_failures[0]
        assert sf["plugin"] == "bootstrap"
        assert sf["persist_across_sessions"] is True
        assert sf["bad_python"] == bad_python
        assert sf["script_path"] == str(bat_path)
        assert "Claude needs your help" in sf["user_msg"]
        assert "fix_python_path" in sf["user_msg"]
        assert "Microsoft Store Python stub" in sf["agent_msg"]
        assert bad_python in sf["agent_msg"]
        assert str(bat_path) in sf["agent_msg"]

    def test_emit_failure_response_python_stub_only(self, tmp_path):
        """When only python_stub failures exist, emit_failure_response writes a
        focused JSON payload with user_msg in systemMessage and agent_msg in
        additionalContext, AND writes the same JSON to the persistent alert path."""
        sys.path.insert(0, BOOTSTRAP_ROOT)
        from bootstrap_lib.engine import emit_failure_response

        out_pending = tmp_path / "bootstrap_display.pending"
        out_alert = tmp_path / "bootstrap_alert.json"

        failures = [{
            "type": "python_stub",
            "name": "python_stub",
            "user_msg": "Claude needs your help! Run the fix_python_path script ...",
            "agent_msg": "A Microsoft Store Python stub at C:\\fake\\python.exe ...",
            "message": "Claude needs your help! ...",
            "bad_python": r"C:\fake\python.exe",
            "script_path": r"C:\Users\fake\Desktop\fix_python_path.bat",
            "plugin": "bootstrap",
            "persist_across_sessions": True,
        }]

        emit_failure_response(
            failures,
            current_os="windows",
            log_content="some log content (should NOT appear in focused message)",
            label="plugins-kit:bootstrap@test",
            output_file=str(out_pending),
            persistent_output_file=str(out_alert),
        )

        assert out_pending.exists()
        assert out_alert.exists()
        # Both files should contain the same JSON
        pending_text = out_pending.read_text()
        alert_text = out_alert.read_text()
        assert pending_text == alert_text

        payload = json.loads(pending_text)
        # Focused user message — no log_content noise, no fix-all boilerplate
        assert "Claude needs your help" in payload["systemMessage"]
        assert "log content" not in payload["systemMessage"]
        assert "fix-all" not in payload["systemMessage"]
        # Focused agent message in additionalContext
        ac = payload["hookSpecificOutput"]["additionalContext"]
        assert "Microsoft Store Python stub" in ac
        assert "fake" in ac
        # UserPromptSubmit hook event name (background mode)
        assert payload["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"

    def test_ask_user_msg_names_the_issues(self, tmp_path):
        """ASK failures (needing the user first): systemMessage NAMES each issue
        and says Claude will ask -- there is no vague 'work through it manually'
        third outcome; the two-outcome contract routes these through
        AskUserQuestion (agent side) and names them (user side)."""
        sys.path.insert(0, BOOTSTRAP_ROOT)
        from bootstrap_lib.engine import emit_failure_response

        out_pending = tmp_path / "bootstrap_display.pending"
        failures = [
            {"type": "env_check", "name": "repo-sync", "ask_reason": "action",
             "message": "repo-sync: still not in sync: env-config (dirty)",
             "plugin": "env", "persist_across_sessions": True},
            {"type": "env_check", "name": "sudoers", "ask_reason": "elevation",
             "message": "sudoers: NOPASSWD rule missing",
             "plugin": "env", "persist_across_sessions": True},
        ]

        emit_failure_response(
            failures, current_os="ubuntu",
            log_content="log noise",
            label="plugins-kit:bootstrap@test",
            output_file=str(out_pending),
        )

        payload = json.loads(out_pending.read_text())
        sysmsg = payload["systemMessage"]
        # Each ASK issue is named in the USER-facing message.
        assert "repo-sync: still not in sync: env-config (dirty)" in sysmsg
        assert "sudoers: NOPASSWD rule missing" in sysmsg
        # The two-outcome lead: Claude will ask, not "work through it manually".
        assert "Claude will ask" in sysmsg
        assert "manual attention" not in sysmsg
        # Agent side mandates AskUserQuestion.
        ac = payload["hookSpecificOutput"]["additionalContext"]
        assert "AskUserQuestion" in ac

    def test_engine_writes_and_clears_persistent_alert(self, data_dir, tmp_path, monkeypatch):
        """Engine writes bootstrap_alert.json on python_stub failure and
        deletes it on success."""
        sys.path.insert(0, BOOTSTRAP_ROOT)
        from bootstrap_lib import python_stub_check as psc

        # Isolate HOME so the in-process engine run discovers an EMPTY plugins
        # dir instead of the developer's real ~/.claude/plugins/installed_plugins.json
        # (which would run claude-ui-kit's install_statusline against the real
        # ~/.claude/settings.json). See run_engine's docstring.
        isolated_home = tmp_path / "home"
        isolated_home.mkdir()
        monkeypatch.setenv("HOME", str(isolated_home))
        monkeypatch.setenv("USERPROFILE", str(isolated_home))

        bad_python = r"C:\Users\fake\AppData\Local\Microsoft\WindowsApps\python.exe"
        good_dir = str(tmp_path / "standalone" / "python")
        script_out = tmp_path / "fake_desktop"

        # Build a fake plugin root that injects python_stub_check into self_setup
        fake_root = tmp_path / "bootstrap_minimal"
        fake_root.mkdir()
        link_tree(fake_root / "bootstrap_lib", os.path.join(BOOTSTRAP_ROOT, "bootstrap_lib"))
        link_tree(fake_root / "engine", os.path.join(BOOTSTRAP_ROOT, "engine"))
        link_tree(fake_root / "pyproject.toml", os.path.join(BOOTSTRAP_ROOT, "pyproject.toml"))
        defaults = fake_root / "defaults"
        defaults.mkdir()
        config = {
            "schema_version": 6,
            "no_bootstrap": [],
            "bootstrap_cache": [],
            "log_success_shell": False,
            "log_success_checks": False,
            "self_setup": {
                "python_stub_check": {
                    "good_python_dir": good_dir,
                    "stub_markers": ["WindowsApps"],
                    "script_output_dir": str(script_out),
                },
            },
        }
        (defaults / "config.json").write_text(json.dumps(config))
        (fake_root / "bootstrap.json").write_text("{}")

        # Patch the check to fail, then run engine
        def fake_fail(good_python_dir, stub_markers):
            return psc._stub_result(
                False,
                "stub python (WindowsApps) shadows standalone python",
                bad_python,
                good_dir,
            )

        monkeypatch.setenv("PYTHONPATH", os.pathsep.join(sys.path))
        # Run via subprocess so --background path is exercised
        import importlib
        sys.path.insert(0, str(fake_root))

        # In-process invocation of main() with --background
        from bootstrap_lib import engine as engine_mod
        importlib.reload(engine_mod)
        monkeypatch.setattr(
            "bootstrap_lib.python_stub_check.check_python_stub", fake_fail
        )

        old_argv = sys.argv
        try:
            sys.argv = [
                "bootstrap_engine",
                "--plugin-root", str(fake_root),
                "--data-dir", str(data_dir),
                "--background",
            ]
            engine_mod.main()
        finally:
            sys.argv = old_argv

        alert_path = os.path.join(data_dir, "bootstrap_alert.json")
        pending_path = os.path.join(data_dir, "bootstrap_display.pending")
        assert os.path.exists(alert_path), f"expected {alert_path} to exist"
        assert os.path.exists(pending_path), f"expected {pending_path} to exist"

        # Now patch to PASS and re-run; alert should be cleared
        def fake_pass(good_python_dir, stub_markers):
            return psc._stub_result(
                True,
                "good python first on persistent PATH",
                None,
                good_dir,
            )

        monkeypatch.setattr(
            "bootstrap_lib.python_stub_check.check_python_stub", fake_pass
        )
        try:
            sys.argv = [
                "bootstrap_engine",
                "--plugin-root", str(fake_root),
                "--data-dir", str(data_dir),
                "--background",
            ]
            engine_mod.main()
        finally:
            sys.argv = old_argv

        assert not os.path.exists(alert_path), (
            f"{alert_path} should have been deleted after successful re-check"
        )
