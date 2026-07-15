"""Integration tests for cross-marketplace plugin refs in the bootstrap engine."""

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


def run_engine(data_dir, plugin_root=BOOTSTRAP_ROOT, env=None):
    """Run the bootstrap engine as a subprocess."""
    return subprocess.run(
        [sys.executable, ENGINE_SCRIPT, "--plugin-root", plugin_root, "--data-dir", data_dir],
        capture_output=True,
        text=True,
        env=env,
    )


def make_fake_bootstrap_root(plugins_dir, manifest=None):
    """Create a fake bootstrap plugin root with symlinked lib/engine and custom defaults."""
    fake_root = plugins_dir / "bootstrap"
    fake_root.mkdir(parents=True, exist_ok=True)
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
        "self_setup": {},
    }
    (defaults / "config.json").write_text(json.dumps(config))

    if manifest is None:
        manifest = {}
    (fake_root / "bootstrap.json").write_text(json.dumps(manifest))
    return str(fake_root)


def _env_with_home(home_dir):
    """Build a subprocess env dict with HOME/USERPROFILE overridden."""
    env = dict(os.environ)
    env["HOME"] = str(home_dir)
    env["USERPROFILE"] = str(home_dir)
    return env


class TestCrossMarketplacePluginRefs:
    @pytest.mark.xfail(
        reason=(
            "Test scaffolding is stale — it writes a plugin manifest into a fake "
            "bootstrap plugin root, but the engine only processes a plugin's "
            "bootstrap.json when the plugin itself is registered in "
            "installed_plugins.json. The dev-layout enabled_refs filter added in "
            "900dd09 (`feat: filter bootstrap by enabled/installed plugins in dev "
            "layout`) further excludes any plugin not present in the real HOME's "
            "settings.json or production registry. Rewriting these tests requires "
            "committing to a scenario design for how cross-marketplace discovery "
            "should behave in fixture-based tests; the current behavior under test "
            "no longer matches a supported code path. Also fails on master."
        ),
        strict=False,
    )
    def test_same_marketplace_uses_local_registry(self, tmp_path):
        """Plugin ref with same marketplace resolves from local registry."""
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()
        fake_root = make_fake_bootstrap_root(plugins_dir)

        # Create a plugin in the local marketplace
        plugin_dir = plugins_dir / "my-plugin"
        plugin_dir.mkdir()

        # Local registry has the plugin
        registry = {"plugins": {"kit:my-plugin": [{"installPath": "./my-plugin", "version": "1.0.0"}]}}
        (plugins_dir / "installed_plugins.json").write_text(json.dumps(registry))

        # Bootstrap manifest references the plugin (same marketplace)
        (plugins_dir / "bootstrap" / "bootstrap.json").write_text(json.dumps({
            "tools": [],
            "plugins": [{"ref": "kit:my-plugin", "enabled": True}],
        }))

        data_dir = str(tmp_path / "data" / "bootstrap")
        os.makedirs(data_dir)
        config = {
            "schema_version": 3, "enabled_plugins": [], "log_level": "info",
            "log_success_shell": False, "log_success_checks": True,
        }
        with open(os.path.join(data_dir, "config.json"), "w") as f:
            json.dump(config, f)

        result = run_engine(data_dir, plugin_root=fake_root)
        assert result.returncode == 0

        # Should find the plugin in local registry and enable it
        if result.stdout.strip():
            response = json.loads(result.stdout)
            msg = response.get("systemMessage", "") + response.get("hookSpecificOutput", {}).get("additionalContext", "")
            # Plugin found — should either log ok or enable it
            assert "my-plugin" in msg

    @pytest.mark.xfail(
        reason=(
            "Test scaffolding is stale — it writes a plugin manifest into a fake "
            "bootstrap plugin root, but the engine only processes a plugin's "
            "bootstrap.json when the plugin itself is registered in "
            "installed_plugins.json. The dev-layout enabled_refs filter added in "
            "900dd09 (`feat: filter bootstrap by enabled/installed plugins in dev "
            "layout`) further excludes any local-registry plugin whose marketplace "
            "(update01) doesn't match the global-registry plugin (plugins-kit), so "
            "the bootstrap manifest with the cross-marketplace plugins block is "
            "never reached. Rewriting requires a scenario design for how "
            "cross-marketplace discovery should behave in fixture-based tests. Also "
            "fails on master."
        ),
        strict=False,
    )
    def test_cross_marketplace_uses_global_registry(self, tmp_path):
        """Plugin ref with different marketplace resolves from global registry."""
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()
        fake_root = make_fake_bootstrap_root(plugins_dir)

        # Local registry identifies this as 'update01' marketplace
        local_registry = {"plugins": {"update01:bootstrap": [{"installPath": "./bootstrap", "version": "1.0.0"}]}}
        (plugins_dir / "installed_plugins.json").write_text(json.dumps(local_registry))

        # Global registry has the cross-marketplace plugin
        global_home = tmp_path / "global"
        global_plugins_dir = global_home / ".claude" / "plugins"
        global_plugins_dir.mkdir(parents=True)
        global_registry = {
            "plugins": {
                "plugins-kit:bootstrap": [{"installPath": str(tmp_path / "cache" / "bootstrap"), "version": "1.0.0"}],
            }
        }
        (global_plugins_dir / "installed_plugins.json").write_text(json.dumps(global_registry))

        # Create the target plugin dir so the registration check works
        target_dir = tmp_path / "cache" / "bootstrap"
        target_dir.mkdir(parents=True)

        # Bootstrap manifest references a plugin from a DIFFERENT marketplace
        (plugins_dir / "bootstrap" / "bootstrap.json").write_text(json.dumps({
            "tools": [],
            "plugins": [{"ref": "plugins-kit:bootstrap", "enabled": True}],
        }))

        data_dir = str(tmp_path / "data" / "bootstrap")
        os.makedirs(data_dir)
        config = {
            "schema_version": 3, "enabled_plugins": [], "log_level": "info",
            "log_success_shell": False, "log_success_checks": True,
        }
        with open(os.path.join(data_dir, "config.json"), "w") as f:
            json.dump(config, f)

        # Pass env directly to subprocess so HOME override propagates
        result = run_engine(data_dir, plugin_root=fake_root, env=_env_with_home(global_home))
        assert result.returncode == 0

        # The engine should have used the global registry and found the plugin
        if result.stdout.strip():
            response = json.loads(result.stdout)
            msg = response.get("systemMessage", "") + response.get("hookSpecificOutput", {}).get("additionalContext", "")
            # Should reference the cross-marketplace plugin
            assert "plugins-kit:bootstrap" in msg


class TestMarketplaceNameFromPluginRoot:
    def test_dev_layout(self, tmp_path):
        """Dev layout: plugins-kit/plugins/bootstrap → marketplace = plugins-kit."""
        plugin_root = tmp_path / "plugins-kit" / "plugins" / "bootstrap"
        plugin_root.mkdir(parents=True)
        marketplace = os.path.basename(os.path.normpath(os.path.join(str(plugin_root), "..", "..")))
        assert marketplace == "plugins-kit"

    def test_cache_layout(self, tmp_path):
        """Cache layout: cache/my-market/bootstrap/0.5.0 → marketplace = my-market."""
        plugin_root = tmp_path / "cache" / "my-market" / "bootstrap" / "0.5.0"
        plugin_root.mkdir(parents=True)
        marketplace = os.path.basename(os.path.normpath(os.path.join(str(plugin_root), "..", "..")))
        assert marketplace == "my-market"
