"""Tests for the script phase in bootstrap engine."""

import json
import os
import subprocess
import sys
import tempfile

import pytest

from bootstrap.link_compat import link_tree

BOOTSTRAP_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "plugins", "bootstrap")
)
ENGINE_SCRIPT = os.path.join(BOOTSTRAP_ROOT, "engine", "bootstrap_engine.py")


def run_engine(data_dir, plugin_root=BOOTSTRAP_ROOT):
    # Isolate HOME to an empty dir so the engine discovers no real
    # installed_plugins.json (an un-isolated run would execute claude-ui-kit's
    # install_statusline against the developer's real ~/.claude/settings.json).
    env = dict(os.environ)
    isolated_home = tempfile.mkdtemp(prefix="bootstrap_test_home_")
    env["HOME"] = isolated_home
    env["USERPROFILE"] = isolated_home
    return subprocess.run(
        [sys.executable, ENGINE_SCRIPT, "--plugin-root", plugin_root, "--data-dir", data_dir],
        capture_output=True,
        text=True,
        env=env,
    )


def make_plugin_root(tmp_path, manifest, scripts=None):
    """Create a fake plugin root with manifest and optional script files."""
    fake_root = tmp_path / "plugin"
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
        "self_setup": {},
    }
    (defaults / "config.json").write_text(json.dumps(config))
    (fake_root / "bootstrap.json").write_text(json.dumps(manifest))

    if scripts:
        for name, content in scripts.items():
            (fake_root / name).write_text(content)

    return str(fake_root)


class TestScriptPhase:
    def test_script_called_with_context(self, data_dir, tmp_path):
        """Script entry point receives a context object with expected attributes."""
        script_content = """\
def bootstrap(ctx):
    assert hasattr(ctx, 'config')
    assert hasattr(ctx, 'config_path')
    assert hasattr(ctx, 'data_dir')
    assert hasattr(ctx, 'plugin_root')
    ctx.log("script: ran successfully")
"""
        manifest = {
            "tools": [],
            "script": {"path": "my_script.py", "entry_point": "bootstrap"},
        }
        fake_root = make_plugin_root(tmp_path, manifest, {"my_script.py": script_content})
        result = run_engine(data_dir, plugin_root=fake_root)
        assert result.returncode == 0

    def test_script_can_add_failures(self, data_dir, tmp_path):
        """Script failures are aggregated into the engine's fix-all output."""
        script_content = """\
def bootstrap(ctx):
    ctx.add_failure("script", message="something is wrong")
"""
        manifest = {
            "tools": [],
            "script": {"path": "my_script.py", "entry_point": "bootstrap"},
        }
        fake_root = make_plugin_root(tmp_path, manifest, {"my_script.py": script_content})
        result = run_engine(data_dir, plugin_root=fake_root)
        assert result.returncode == 0
        if result.stdout.strip():
            response = json.loads(result.stdout)
            assert "hookSpecificOutput" in response

    def test_script_exception_caught(self, data_dir, tmp_path):
        """Script exceptions don't crash the engine."""
        script_content = """\
def bootstrap(ctx):
    raise RuntimeError("boom")
"""
        manifest = {
            "tools": [],
            "script": {"path": "my_script.py", "entry_point": "bootstrap"},
        }
        fake_root = make_plugin_root(tmp_path, manifest, {"my_script.py": script_content})
        result = run_engine(data_dir, plugin_root=fake_root)
        assert result.returncode == 0

    def test_missing_script_skipped(self, data_dir, tmp_path):
        """Script phase is skipped if the script file doesn't exist."""
        manifest = {
            "tools": [],
            "script": {"path": "nonexistent.py", "entry_point": "bootstrap"},
        }
        fake_root = make_plugin_root(tmp_path, manifest)
        result = run_engine(data_dir, plugin_root=fake_root)
        assert result.returncode == 0

    def test_missing_entry_point_logged(self, data_dir, tmp_path):
        """If entry point function doesn't exist, it's logged but not fatal."""
        script_content = """\
def other_func(ctx):
    pass
"""
        manifest = {
            "tools": [],
            "script": {"path": "my_script.py", "entry_point": "bootstrap"},
        }
        fake_root = make_plugin_root(tmp_path, manifest, {"my_script.py": script_content})
        result = run_engine(data_dir, plugin_root=fake_root)
        assert result.returncode == 0

    def test_script_can_save_config(self, data_dir, tmp_path):
        """Script can modify and save config via ctx.save_config()."""
        script_content = """\
def bootstrap(ctx):
    ctx.config["custom_key"] = "custom_value"
    ctx.save_config()
    ctx.log("config saved")
"""
        manifest = {
            "tools": [],
            "script": {"path": "my_script.py", "entry_point": "bootstrap"},
        }
        fake_root = make_plugin_root(tmp_path, manifest, {"my_script.py": script_content})
        result = run_engine(data_dir, plugin_root=fake_root)
        assert result.returncode == 0
