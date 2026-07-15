"""Integration tests for bootstrap engine --background mode."""

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


def run_engine(data_dir, plugin_root=BOOTSTRAP_ROOT, extra_args=None, env=None):
    """Run the bootstrap engine as a subprocess."""
    cmd = [sys.executable, ENGINE_SCRIPT, "--plugin-root", plugin_root, "--data-dir", data_dir]
    if extra_args:
        cmd.extend(extra_args)
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


def _isolated_env(tmp_path):
    """Return an env dict with HOME set to an empty temp dir.

    Without this the engine reads the developer's real installed-plugin registry
    and bootstraps their actual plugins, polluting stdout / the display file that
    the silence-expecting tests assert on.

    The isolated home's ~/.local/bin is pre-seeded onto PATH: the minimal
    self_setup declares path_entries ["~/.local/bin"], which expands under the
    fake HOME to a dir that is never on the inherited PATH — without the seed,
    every run emits a "PATH added" action entry and the silence-expecting
    tests fail on any machine.
    """
    home = str(tmp_path / "_home")
    local_bin = os.path.join(home, ".local", "bin")
    os.makedirs(local_bin, exist_ok=True)
    path = local_bin + os.pathsep + os.environ.get("PATH", "")
    return {**os.environ, "HOME": home, "PATH": path}


def _make_minimal_root(tmp_path, config_overrides=None, with_version=False):
    """Create a fake bootstrap root with minimal ecosystem manifest.

    When with_version=True, writes a .claude-plugin/plugin.json with a version
    so the first run produces an "installed: X" action entry — needed by tests
    that expect non-empty display output (oks alone no longer surface to the user).
    """
    fake_root = tmp_path / "bootstrap_bg"
    fake_root.mkdir(exist_ok=True)
    link_tree(fake_root / "bootstrap_lib", os.path.join(BOOTSTRAP_ROOT, "bootstrap_lib"))
    link_tree(fake_root / "engine", os.path.join(BOOTSTRAP_ROOT, "engine"))
    defaults = fake_root / "defaults"
    defaults.mkdir(exist_ok=True)
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
    if config_overrides:
        config.update(config_overrides)
    (defaults / "config.json").write_text(json.dumps(config))
    (fake_root / "bootstrap.json").write_text(json.dumps({}))
    if with_version:
        plugin_dir = fake_root / ".claude-plugin"
        plugin_dir.mkdir(exist_ok=True)
        (plugin_dir / "plugin.json").write_text(json.dumps({"name": "bootstrap", "version": "0.0.1"}))
    return str(fake_root)


class TestEngineBackground:
    def test_background_writes_display_file(self, data_dir, tmp_path):
        """Engine with --background creates bootstrap_display.pending when there's output."""
        fake_root = _make_minimal_root(tmp_path, with_version=True)
        result = run_engine(data_dir, plugin_root=fake_root, extra_args=["--background"],
                            env=_isolated_env(tmp_path))
        assert result.returncode == 0
        display_file = os.path.join(data_dir, "bootstrap_display.pending")
        assert os.path.isfile(display_file)

    def test_background_no_stdout(self, data_dir, tmp_path):
        """In background mode, stdout is empty (output goes to file)."""
        fake_root = _make_minimal_root(tmp_path, with_version=True)
        result = run_engine(data_dir, plugin_root=fake_root, extra_args=["--background"],
                            env=_isolated_env(tmp_path))
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_background_success_format(self, data_dir, tmp_path):
        """Display file has UserPromptSubmit-compliant JSON with additionalContext for Claude."""
        fake_root = _make_minimal_root(tmp_path, with_version=True)
        run_engine(data_dir, plugin_root=fake_root, extra_args=["--background"],
                   env=_isolated_env(tmp_path))
        display_file = os.path.join(data_dir, "bootstrap_display.pending")
        with open(display_file) as f:
            response = json.load(f)
        assert response["continue"] is True
        assert response["suppressOutput"] is False
        assert "systemMessage" in response
        assert "bootstrap complete" in response["systemMessage"]
        # hookSpecificOutput with hookEventName for UserPromptSubmit validation
        assert response["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
        assert "bootstrap complete" in response["hookSpecificOutput"]["additionalContext"]

    def test_background_failure_format(self, data_dir, tmp_path):
        """Display file includes remediation for Claude via additionalContext."""
        fake_root = tmp_path / "fake_plugin_bg"
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

        result = run_engine(data_dir, plugin_root=str(fake_root), extra_args=["--background"],
                            env=_isolated_env(tmp_path))
        assert result.returncode == 0
        assert result.stdout.strip() == ""

        display_file = os.path.join(data_dir, "bootstrap_display.pending")
        assert os.path.isfile(display_file)
        with open(display_file) as f:
            response = json.load(f)
        # Failure JSON carries the same universal fields as success (B21)
        assert response["continue"] is True
        assert response["suppressOutput"] is False
        # User sees log + fix instructions in systemMessage
        assert "nonexistent_tool_xyz_abc" in response["systemMessage"]
        # hookSpecificOutput with hookEventName for UserPromptSubmit validation
        assert response["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
        # Claude sees log + fix directives via additionalContext
        assert "nonexistent_tool_xyz_abc" in response["hookSpecificOutput"]["additionalContext"]

    def test_foreground_has_hook_specific_output(self, data_dir, tmp_path):
        """Non-background (SessionStart) output retains hookSpecificOutput."""
        fake_root = _make_minimal_root(tmp_path, with_version=True)
        result = run_engine(data_dir, plugin_root=fake_root, env=_isolated_env(tmp_path))
        assert result.returncode == 0
        response = json.loads(result.stdout.strip())
        assert response["hookSpecificOutput"]["hookEventName"] == "SessionStart"

    def test_background_silent_no_file(self, data_dir, tmp_path):
        """When everything is ok and log_success is false, no display file is created."""
        fake_root = _make_minimal_root(tmp_path)
        result = run_engine(data_dir, plugin_root=fake_root, extra_args=["--background"],
                            env=_isolated_env(tmp_path))
        assert result.returncode == 0
        display_file = os.path.join(data_dir, "bootstrap_display.pending")
        assert not os.path.isfile(display_file)

    def test_ok_entries_not_shown_on_second_run(self, data_dir, tmp_path):
        """Ok entries written to the log on run N must not appear in run N+1's display.

        Regression: the log-reader used by the engine read back ok entries from
        the previous run (because they were unconditionally written to the log),
        bypassing the log_success filter and showing them via shell_content.
        """
        fake_root = _make_minimal_root(tmp_path)
        env = _isolated_env(tmp_path)
        # First run: silent (no display file)
        run_engine(data_dir, plugin_root=fake_root, extra_args=["--background"], env=env)
        display_file = os.path.join(data_dir, "bootstrap_display.pending")
        assert not os.path.isfile(display_file), "first run should be silent"

        # Second run: ok entries from run 1 must not leak into run 2's display
        run_engine(data_dir, plugin_root=fake_root, extra_args=["--background"], env=env)
        assert not os.path.isfile(display_file), "second run must also be silent (no ok-entry leak)"

    def test_console_mode_unaffected(self, data_dir, tmp_path):
        """--console still prints to stdout regardless of --background."""
        fake_root = _make_minimal_root(tmp_path)
        result = run_engine(data_dir, plugin_root=fake_root, extra_args=["--console"],
                            env=_isolated_env(tmp_path))
        assert result.returncode == 0
        # Console mode prints plain text to stdout (verbose by default)
        # No display file should be created
        display_file = os.path.join(data_dir, "bootstrap_display.pending")
        assert not os.path.isfile(display_file)
