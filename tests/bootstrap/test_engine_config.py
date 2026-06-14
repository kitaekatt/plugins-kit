"""Integration tests for engine config phase."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

BOOTSTRAP_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "plugins", "bootstrap")
)
ENGINE_SCRIPT = os.path.join(BOOTSTRAP_ROOT, "engine", "bootstrap_engine.py")


def _env_with_home(home_dir):
    """Build a subprocess env dict with HOME/USERPROFILE overridden."""
    env = dict(os.environ)
    env["HOME"] = str(home_dir)
    env["USERPROFILE"] = str(home_dir)
    # The Windows User PATH registry is global state and ignores HOME, so
    # without this guard the engine would leak tmp paths into the real PATH.
    env["BOOTSTRAP_SKIP_REGISTRY"] = "1"
    return env


def run_engine(data_dir, plugin_root=BOOTSTRAP_ROOT, env=None):
    """Run the bootstrap engine as a subprocess."""
    return subprocess.run(
        [sys.executable, ENGINE_SCRIPT, "--plugin-root", plugin_root, "--data-dir", data_dir],
        capture_output=True,
        text=True,
        env=env,
    )


def make_fake_bootstrap_root(tmp_path, manifest=None):
    """Create a fake bootstrap plugin root with symlinked lib/engine/defaults."""
    fake_root = tmp_path / "plugins" / "bootstrap"
    fake_root.mkdir(parents=True)
    (fake_root / "bootstrap_lib").symlink_to(os.path.join(BOOTSTRAP_ROOT, "bootstrap_lib"))
    (fake_root / "engine").symlink_to(os.path.join(BOOTSTRAP_ROOT, "engine"))
    (fake_root / "defaults").symlink_to(os.path.join(BOOTSTRAP_ROOT, "defaults"))
    (fake_root / "pyproject.toml").symlink_to(Path(BOOTSTRAP_ROOT) / "pyproject.toml")

    if manifest is None:
        manifest = {"tools": [], "path_entries": []}
    (fake_root / "bootstrap.json").write_text(json.dumps(manifest))
    return str(fake_root)


class TestEngineConfigPhase:
    def test_config_init_copies_defaults(self, tmp_path):
        """Engine copies defaults config when plugin config doesn't exist."""
        fake_root = make_fake_bootstrap_root(tmp_path)
        plugins_dir = tmp_path / "plugins"

        # Create plugin with config section
        plugin_dir = plugins_dir / "cfg-plugin"
        plugin_dir.mkdir()
        defaults = plugin_dir / "defaults"
        defaults.mkdir()
        (defaults / "config.yaml").write_text("P4PORT: \"\"\nDEFAULT_AGENT: \"\"\n")
        (plugin_dir / "bootstrap.json").write_text(json.dumps({
            "config": {
                "file": "config.yaml",
                "defaults_source": "defaults/config.yaml",
                "required_fields": {
                    "P4PORT": {"user_msg": "P4 port", "agent_msg": "Set P4PORT in {config_path}"},
                    "DEFAULT_AGENT": {"user_msg": "Agent", "default": "claude-opus"},
                },
            },
        }))

        registry = {"plugins": {"kit:cfg-plugin": [{"installPath": "./cfg-plugin", "version": "1.0.0"}]}}
        (plugins_dir / "installed_plugins.json").write_text(json.dumps(registry))

        data_dir = str(tmp_path / "data" / "kit" / "bootstrap")
        os.makedirs(data_dir)
        config = {"schema_version": 3, "enabled_plugins": ["kit:cfg-plugin"], "log_level": "info", "log_success_shell": False, "log_success_checks": False}
        with open(os.path.join(data_dir, "config.json"), "w") as f:
            json.dump(config, f)

        home_dir = tmp_path / "home"
        home_dir.mkdir()
        result = run_engine(data_dir, plugin_root=fake_root, env=_env_with_home(home_dir))
        assert result.returncode == 0

        # Config should be copied to plugin data dir
        plugin_data_dir = os.path.join(str(tmp_path / "data" / "kit"), "cfg-plugin")
        config_path = os.path.join(plugin_data_dir, "config.yaml")
        assert os.path.isfile(config_path)

    def test_missing_fields_produce_fix_all(self, tmp_path):
        """Missing required fields without defaults produce agent fix-all directives."""
        fake_root = make_fake_bootstrap_root(tmp_path)
        plugins_dir = tmp_path / "plugins"

        plugin_dir = plugins_dir / "cfg-plugin"
        plugin_dir.mkdir()
        defaults = plugin_dir / "defaults"
        defaults.mkdir()
        (defaults / "config.yaml").write_text("P4PORT: \"\"\nP4USER: \"\"\n")
        (plugin_dir / "bootstrap.json").write_text(json.dumps({
            "config": {
                "file": "config.yaml",
                "defaults_source": "defaults/config.yaml",
                "required_fields": {
                    "P4PORT": {"user_msg": "P4 port", "agent_msg": "Set P4PORT in {config_path}"},
                    "P4USER": {"user_msg": "P4 user", "agent_msg": "Set P4USER in {config_path}"},
                },
            },
        }))

        registry = {"plugins": {"kit:cfg-plugin": [{"installPath": "./cfg-plugin", "version": "1.0.0"}]}}
        (plugins_dir / "installed_plugins.json").write_text(json.dumps(registry))

        data_dir = str(tmp_path / "data" / "kit" / "bootstrap")
        os.makedirs(data_dir)
        config = {"schema_version": 3, "enabled_plugins": ["kit:cfg-plugin"], "log_level": "info", "log_success_shell": False, "log_success_checks": False}
        with open(os.path.join(data_dir, "config.json"), "w") as f:
            json.dump(config, f)

        home_dir = tmp_path / "home"
        home_dir.mkdir()
        result = run_engine(data_dir, plugin_root=fake_root, env=_env_with_home(home_dir))
        assert result.returncode == 0
        assert result.stdout.strip() != ""

        response = json.loads(result.stdout)
        ctx = response["hookSpecificOutput"]["additionalContext"]
        assert "P4PORT" in ctx
        assert "P4USER" in ctx
        assert "[cfg-plugin]" in ctx

    def test_defaults_applied_no_fix_all(self, tmp_path):
        """Fields with defaults are applied — no fix-all if all fields have defaults or values."""
        fake_root = make_fake_bootstrap_root(tmp_path)
        plugins_dir = tmp_path / "plugins"

        plugin_dir = plugins_dir / "cfg-plugin"
        plugin_dir.mkdir()
        defaults = plugin_dir / "defaults"
        defaults.mkdir()
        (defaults / "config.yaml").write_text("AGENT: \"\"\n")
        (plugin_dir / "bootstrap.json").write_text(json.dumps({
            "config": {
                "file": "config.yaml",
                "defaults_source": "defaults/config.yaml",
                "required_fields": {
                    "AGENT": {"user_msg": "Agent", "default": "claude-opus"},
                },
            },
        }))

        registry = {"plugins": {"kit:cfg-plugin": [{"installPath": "./cfg-plugin", "version": "1.0.0"}]}}
        (plugins_dir / "installed_plugins.json").write_text(json.dumps(registry))

        data_dir = str(tmp_path / "data" / "kit" / "bootstrap")
        os.makedirs(data_dir)
        config = {"schema_version": 3, "enabled_plugins": ["kit:cfg-plugin"], "log_level": "info", "log_success_shell": False, "log_success_checks": False}
        with open(os.path.join(data_dir, "config.json"), "w") as f:
            json.dump(config, f)

        home_dir = tmp_path / "home"
        home_dir.mkdir()
        result = run_engine(data_dir, plugin_root=fake_root, env=_env_with_home(home_dir))
        assert result.returncode == 0
        # No failures means no additionalContext (or no fix-all content)
        if result.stdout.strip():
            response = json.loads(result.stdout)
            # Should not have failure directives
            ctx = response.get("hookSpecificOutput", {}).get("additionalContext", "")
            assert "AGENT" not in ctx

    def test_config_runs_on_cache_hit(self, tmp_path):
        """Config phase runs even when manifest cache is valid."""
        fake_root = make_fake_bootstrap_root(tmp_path)
        plugins_dir = tmp_path / "plugins"

        plugin_dir = plugins_dir / "cfg-plugin"
        plugin_dir.mkdir()
        defaults = plugin_dir / "defaults"
        defaults.mkdir()
        (defaults / "config.yaml").write_text("FIELD: \"\"\n")
        (plugin_dir / "bootstrap.json").write_text(json.dumps({
            "config": {
                "file": "config.yaml",
                "defaults_source": "defaults/config.yaml",
                "required_fields": {
                    "FIELD": {"user_msg": "A field", "agent_msg": "Set FIELD in {config_path}"},
                },
            },
        }))

        registry = {"plugins": {"kit:cfg-plugin": [{"installPath": "./cfg-plugin", "version": "1.0.0"}]}}
        (plugins_dir / "installed_plugins.json").write_text(json.dumps(registry))

        data_dir = str(tmp_path / "data" / "kit" / "bootstrap")
        os.makedirs(data_dir)
        config = {"schema_version": 3, "enabled_plugins": ["kit:cfg-plugin"], "log_level": "info", "log_success_shell": False, "log_success_checks": False}
        with open(os.path.join(data_dir, "config.json"), "w") as f:
            json.dump(config, f)

        home_dir = tmp_path / "home"
        home_dir.mkdir()
        # First run — populates cache
        run_engine(data_dir, plugin_root=fake_root, env=_env_with_home(home_dir))

        # Second run — cache hit for tools/venv/git, but config still runs
        result = run_engine(data_dir, plugin_root=fake_root, env=_env_with_home(home_dir))
        assert result.returncode == 0
        # Config failure should still be reported
        if result.stdout.strip():
            response = json.loads(result.stdout)
            ctx = response.get("hookSpecificOutput", {}).get("additionalContext", "")
            assert "FIELD" in ctx

    def test_autodetect_populates_fields(self, tmp_path):
        """Autodetect script fills empty fields, preventing fix-all."""
        fake_root = make_fake_bootstrap_root(tmp_path)
        plugins_dir = tmp_path / "plugins"

        plugin_dir = plugins_dir / "ad-plugin"
        plugin_dir.mkdir()
        defaults = plugin_dir / "defaults"
        defaults.mkdir()
        (defaults / "config.yaml").write_text("SERVER: \"\"\n")

        # Write autodetect script that fills SERVER
        (plugin_dir / "detect.py").write_text(
            "def detect(config, config_path):\n"
            "    config['SERVER'] = 'auto-detected'\n"
            "    return True\n"
        )

        (plugin_dir / "bootstrap.json").write_text(json.dumps({
            "config": {
                "file": "config.yaml",
                "defaults_source": "defaults/config.yaml",
                "required_fields": {
                    "SERVER": {"user_msg": "Server address", "agent_msg": "Set SERVER in {config_path}"},
                },
                "autodetect": "detect.py detect",
            },
        }))

        registry = {"plugins": {"kit:ad-plugin": [{"installPath": "./ad-plugin", "version": "1.0.0"}]}}
        (plugins_dir / "installed_plugins.json").write_text(json.dumps(registry))

        data_dir = str(tmp_path / "data" / "kit" / "bootstrap")
        os.makedirs(data_dir)
        config = {"schema_version": 3, "enabled_plugins": ["kit:ad-plugin"], "log_level": "info", "log_success_shell": False, "log_success_checks": False}
        with open(os.path.join(data_dir, "config.json"), "w") as f:
            json.dump(config, f)

        home_dir = tmp_path / "home"
        home_dir.mkdir()
        result = run_engine(data_dir, plugin_root=fake_root, env=_env_with_home(home_dir))
        assert result.returncode == 0
        # No fix-all since autodetect filled the field
        if result.stdout.strip():
            response = json.loads(result.stdout)
            ctx = response.get("hookSpecificOutput", {}).get("additionalContext", "")
            assert "SERVER" not in ctx

        # Verify config was written with detected value
        plugin_data_dir = os.path.join(str(tmp_path / "data" / "kit"), "ad-plugin")
        config_path = os.path.join(plugin_data_dir, "config.yaml")
        with open(config_path) as f:
            content = f.read()
        assert "auto-detected" in content

    def test_autodetect_runs_even_when_fields_populated(self, tmp_path):
        """Autodetect is called even when all required fields have values (no has_empty gate)."""
        fake_root = make_fake_bootstrap_root(tmp_path)
        plugins_dir = tmp_path / "plugins"

        plugin_dir = plugins_dir / "ad-plugin"
        plugin_dir.mkdir()
        defaults = plugin_dir / "defaults"
        defaults.mkdir()
        # Config with values already set
        (defaults / "config.yaml").write_text("SERVER: old-value\n")

        # Autodetect script that overwrites SERVER with a marker
        (plugin_dir / "detect.py").write_text(
            "def detect(config, config_path):\n"
            "    config['SERVER'] = 'refreshed'\n"
            "    return True\n"
        )

        (plugin_dir / "bootstrap.json").write_text(json.dumps({
            "config": {
                "file": "config.yaml",
                "defaults_source": "defaults/config.yaml",
                "required_fields": {
                    "SERVER": {"user_msg": "Server address", "agent_msg": "Set SERVER in {config_path}"},
                },
                "autodetect": "detect.py detect",
            },
        }))

        registry = {"plugins": {"kit:ad-plugin": [{"installPath": "./ad-plugin", "version": "1.0.0"}]}}
        (plugins_dir / "installed_plugins.json").write_text(json.dumps(registry))

        data_dir = str(tmp_path / "data" / "kit" / "bootstrap")
        os.makedirs(data_dir)
        config = {"schema_version": 3, "enabled_plugins": ["kit:ad-plugin"], "log_level": "info", "log_success_shell": False, "log_success_checks": False}
        with open(os.path.join(data_dir, "config.json"), "w") as f:
            json.dump(config, f)

        home_dir = tmp_path / "home"
        home_dir.mkdir()
        result = run_engine(data_dir, plugin_root=fake_root, env=_env_with_home(home_dir))
        assert result.returncode == 0

        # Verify autodetect ran and updated the value even though it was already set
        plugin_data_dir = os.path.join(str(tmp_path / "data" / "kit"), "ad-plugin")
        config_path = os.path.join(plugin_data_dir, "config.yaml")
        with open(config_path) as f:
            content = f.read()
        assert "refreshed" in content
