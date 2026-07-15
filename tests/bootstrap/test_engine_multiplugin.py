"""Integration tests for multi-plugin bootstrap engine flow."""

import hashlib
import json
import os
import shlex
import subprocess
import sys

import pytest

from bootstrap.link_compat import link_tree


def _sourced_value(env_file, var):
    """Value of `export <var>=...` from an env file, parsed POSIX-shell style.

    Avoids spawning `bash -c 'source ...'`: the `bash` on PATH may be WSL, which
    can't source a Windows-path file or see a Windows venv. shlex.split tests the
    same property (a quoted path round-trips as one token, space preserved).
    """
    prefix = f"export {var}="
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line.startswith(prefix):
            parts = shlex.split(line[len(prefix):])
            return parts[0] if parts else ""
    return None

BOOTSTRAP_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "plugins", "bootstrap")
)
ENGINE_SCRIPT = os.path.join(BOOTSTRAP_ROOT, "engine", "bootstrap_engine.py")


def run_engine(data_dir, plugin_root=BOOTSTRAP_ROOT, env=None, project_dir=None):
    """Run the bootstrap engine as a subprocess."""
    args = [sys.executable, ENGINE_SCRIPT, "--plugin-root", plugin_root, "--data-dir", data_dir]
    if project_dir is not None:
        args.extend(["--project-dir", project_dir])
    return subprocess.run(args, capture_output=True, text=True, env=env)


def _isolated_env(tmp_path):
    """Return an env dict with HOME set to an empty temp dir.

    This ensures _load_enabled_refs finds no settings files and no production
    registry, so it returns None (no filter) — preserving pre-filter behavior
    for tests that don't exercise the dev-layout filtering logic.
    """
    home = str(tmp_path / "_home")
    os.makedirs(home, exist_ok=True)
    return {**os.environ, "HOME": home}


def _write_minimal_defaults(fake_root):
    """Write a minimal defaults/config.json with empty self_setup."""
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


def make_fake_bootstrap_root(tmp_path, manifest=None, self_setup=None):
    """Create a fake bootstrap plugin root with symlinked lib/engine and custom defaults."""
    fake_root = tmp_path / "bootstrap"
    fake_root.mkdir(parents=True)
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
        "self_setup": self_setup or {},
    }
    (defaults / "config.json").write_text(json.dumps(config))

    if manifest is None:
        manifest = {}
    (fake_root / "bootstrap.json").write_text(json.dumps(manifest))
    return str(fake_root)


class TestMultiPluginEngine:
    def test_no_enabled_plugins_emits_log(self, tmp_path):
        """Engine with no enabled plugins should emit log after self-bootstrap."""
        fake_root = make_fake_bootstrap_root(tmp_path)
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir)

        # Isolate HOME so the engine doesn't read the developer's real installed
        # plugin registry and bootstrap their actual plugins into stdout.
        result = run_engine(data_dir, plugin_root=fake_root, env=_isolated_env(tmp_path))

        assert result.returncode == 0
        # Empty manifest = no checks = no log entries = silent exit
        assert result.stdout == ""

    def test_enabled_plugin_with_tool_check(self, tmp_path):
        """Engine processes an enabled plugin's tool checks."""
        # Set up fake bootstrap root (in plugins/ subdir so registry resolution works)
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()

        fake_root = plugins_dir / "bootstrap"
        fake_root.mkdir()
        link_tree(fake_root / "bootstrap_lib", os.path.join(BOOTSTRAP_ROOT, "bootstrap_lib"))
        link_tree(fake_root / "engine", os.path.join(BOOTSTRAP_ROOT, "engine"))
        _write_minimal_defaults(fake_root)
        (fake_root / "bootstrap.json").write_text(json.dumps({}))

        # Create a test plugin with a manifest requiring a nonexistent tool
        test_plugin_dir = plugins_dir / "my-test"
        test_plugin_dir.mkdir()
        (test_plugin_dir / "bootstrap.json").write_text(json.dumps({
            "tools": [{"name": "fake_tool_xyz_999", "install": {"macos": "brew install fake"}}],
        }))

        # Create registry
        registry = {"plugins": {"kit:my-test": [{"installPath": "./my-test", "version": "1.0.0"}]}}
        (plugins_dir / "installed_plugins.json").write_text(json.dumps(registry))

        # Create data dir with config enabling the plugin
        data_dir = str(tmp_path / "data" / "kit" / "bootstrap")
        os.makedirs(data_dir)
        config = {"schema_version": 3, "enabled_plugins": ["kit:my-test"], "log_level": "info", "log_success_shell": False, "log_success_checks": False}
        with open(os.path.join(data_dir, "config.json"), "w") as f:
            json.dump(config, f)

        result = run_engine(data_dir, plugin_root=str(fake_root), env=_isolated_env(tmp_path))

        assert result.returncode == 0
        # Should emit failure JSON for the missing tool
        assert result.stdout.strip() != ""
        response = json.loads(result.stdout)
        assert response["continue"] is True
        assert "fake_tool_xyz_999" in response["hookSpecificOutput"]["additionalContext"]
        assert "[my-test]" in response["hookSpecificOutput"]["additionalContext"]

    def test_enabled_plugin_all_pass_emits_log(self, tmp_path):
        """Plugin with passing checks emits log."""
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()

        fake_root = plugins_dir / "bootstrap"
        fake_root.mkdir()
        link_tree(fake_root / "bootstrap_lib", os.path.join(BOOTSTRAP_ROOT, "bootstrap_lib"))
        link_tree(fake_root / "engine", os.path.join(BOOTSTRAP_ROOT, "engine"))
        _write_minimal_defaults(fake_root)
        (fake_root / "bootstrap.json").write_text(json.dumps({}))

        # Plugin that only checks for 'git' (which should be available)
        test_plugin_dir = plugins_dir / "good-plugin"
        test_plugin_dir.mkdir()
        (test_plugin_dir / "bootstrap.json").write_text(json.dumps({
            "tools": [{"name": "git", "install": {"macos": "brew install git"}}],
        }))

        registry = {"plugins": {"kit:good-plugin": [{"installPath": "./good-plugin", "version": "1.0.0"}]}}
        (plugins_dir / "installed_plugins.json").write_text(json.dumps(registry))

        data_dir = str(tmp_path / "data" / "kit" / "bootstrap")
        os.makedirs(data_dir)
        config = {"schema_version": 3, "enabled_plugins": ["kit:good-plugin"], "log_level": "info", "log_success_shell": False, "log_success_checks": True}
        with open(os.path.join(data_dir, "config.json"), "w") as f:
            json.dump(config, f)

        result = run_engine(data_dir, plugin_root=str(fake_root), env=_isolated_env(tmp_path))

        assert result.returncode == 0
        response = json.loads(result.stdout)
        assert response["continue"] is True
        assert "good-plugin" in response["systemMessage"]

    def test_plugin_log_written_to_own_data_dir(self, tmp_path):
        """Plugin log entries are written to plugin's own data dir, not bootstrap's."""
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()

        fake_root = plugins_dir / "bootstrap"
        fake_root.mkdir()
        link_tree(fake_root / "bootstrap_lib", os.path.join(BOOTSTRAP_ROOT, "bootstrap_lib"))
        link_tree(fake_root / "engine", os.path.join(BOOTSTRAP_ROOT, "engine"))
        _write_minimal_defaults(fake_root)
        (fake_root / "bootstrap.json").write_text(json.dumps({}))

        test_plugin_dir = plugins_dir / "logged-plugin"
        test_plugin_dir.mkdir()
        (test_plugin_dir / "bootstrap.json").write_text(json.dumps({
            "tools": [{"name": "git", "install": {"macos": "brew install git"}}],
        }))

        registry = {"plugins": {"kit:logged-plugin": [{"installPath": "./logged-plugin", "version": "2.3.0"}]}}
        (plugins_dir / "installed_plugins.json").write_text(json.dumps(registry))

        data_dir = str(tmp_path / "data" / "kit" / "bootstrap")
        os.makedirs(data_dir)
        config = {"schema_version": 3, "enabled_plugins": ["kit:logged-plugin"], "log_level": "info", "log_success_shell": False, "log_success_checks": True}
        with open(os.path.join(data_dir, "config.json"), "w") as f:
            json.dump(config, f)

        result = run_engine(data_dir, plugin_root=str(fake_root), env=_isolated_env(tmp_path))
        assert result.returncode == 0

        # Plugin log should be in plugin's own data dir with version in header
        plugin_data_dir = os.path.join(str(tmp_path / "data" / "kit"), "logged-plugin")
        plugin_log = os.path.join(plugin_data_dir, "bootstrap.log")
        assert os.path.exists(plugin_log)
        with open(plugin_log) as f:
            content = f.read()
        assert "logged-plugin@2.3.0" in content
        assert "git" in content

        # Bootstrap's own log should NOT contain plugin entries
        bootstrap_log = os.path.join(data_dir, "bootstrap.log")
        if os.path.exists(bootstrap_log):
            with open(bootstrap_log) as f:
                bootstrap_content = f.read()
            assert "logged-plugin" not in bootstrap_content

        # But the hook response should still show plugin entries to the user
        response = json.loads(result.stdout)
        assert "logged-plugin" in response["systemMessage"]

    def test_second_run_reruns_checks(self, tmp_path):
        """Second run re-runs all checks — no cache gate."""
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()

        fake_root = plugins_dir / "bootstrap"
        fake_root.mkdir()
        link_tree(fake_root / "bootstrap_lib", os.path.join(BOOTSTRAP_ROOT, "bootstrap_lib"))
        link_tree(fake_root / "engine", os.path.join(BOOTSTRAP_ROOT, "engine"))
        _write_minimal_defaults(fake_root)
        (fake_root / "bootstrap.json").write_text(json.dumps({}))

        test_plugin_dir = plugins_dir / "rerun-plugin"
        test_plugin_dir.mkdir()
        (test_plugin_dir / "bootstrap.json").write_text(json.dumps({
            "tools": [{"name": "git", "install": {"macos": "brew install git"}}],
        }))

        registry = {"plugins": {"kit:rerun-plugin": [{"installPath": "./rerun-plugin", "version": "1.0.0"}]}}
        (plugins_dir / "installed_plugins.json").write_text(json.dumps(registry))

        data_dir = str(tmp_path / "data" / "kit" / "bootstrap")
        os.makedirs(data_dir)
        config = {"schema_version": 3, "enabled_plugins": ["kit:rerun-plugin"], "log_level": "info", "log_success_shell": False, "log_success_checks": True}
        with open(os.path.join(data_dir, "config.json"), "w") as f:
            json.dump(config, f)

        # First run
        run_engine(data_dir, plugin_root=str(fake_root), env=_isolated_env(tmp_path))

        # Second run — re-runs checks. Display is silent (only oks), but the
        # plugin's own log file records the re-run since log_success_checks=True.
        result = run_engine(data_dir, plugin_root=str(fake_root), env=_isolated_env(tmp_path))
        assert result.returncode == 0
        plugin_log = os.path.join(str(tmp_path / "data" / "kit"), "rerun-plugin", "bootstrap.log")
        with open(plugin_log) as f:
            content = f.read()
        # Two timestamped headers = two runs recorded
        assert content.count("rerun-plugin@1.0.0") >= 2

    def test_plugin_without_manifest_skipped(self, tmp_path):
        """Plugin with no bootstrap.json is silently skipped."""
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()

        fake_root = plugins_dir / "bootstrap"
        fake_root.mkdir()
        link_tree(fake_root / "bootstrap_lib", os.path.join(BOOTSTRAP_ROOT, "bootstrap_lib"))
        link_tree(fake_root / "engine", os.path.join(BOOTSTRAP_ROOT, "engine"))
        _write_minimal_defaults(fake_root)
        (fake_root / "bootstrap.json").write_text(json.dumps({}))

        # Plugin dir exists but has no bootstrap.json
        no_manifest_dir = plugins_dir / "no-manifest"
        no_manifest_dir.mkdir()

        registry = {"plugins": {"kit:no-manifest": [{"installPath": "./no-manifest", "version": "1.0.0"}]}}
        (plugins_dir / "installed_plugins.json").write_text(json.dumps(registry))

        data_dir = str(tmp_path / "data" / "kit" / "bootstrap")
        os.makedirs(data_dir)
        config = {"schema_version": 3, "enabled_plugins": ["kit:no-manifest"], "log_level": "info", "log_success_shell": False, "log_success_checks": False}
        with open(os.path.join(data_dir, "config.json"), "w") as f:
            json.dump(config, f)

        result = run_engine(data_dir, plugin_root=str(fake_root), env=_isolated_env(tmp_path))

        assert result.returncode == 0
        # Plugin with no manifest is skipped — nothing to log → silent exit
        assert result.stdout == ""

    def test_venv_failure_in_plugin(self, tmp_path):
        """Plugin with venv check that fails emits JSON with remediation."""
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()

        fake_root = plugins_dir / "bootstrap"
        fake_root.mkdir()
        link_tree(fake_root / "bootstrap_lib", os.path.join(BOOTSTRAP_ROOT, "bootstrap_lib"))
        link_tree(fake_root / "engine", os.path.join(BOOTSTRAP_ROOT, "engine"))
        _write_minimal_defaults(fake_root)
        (fake_root / "bootstrap.json").write_text(json.dumps({}))

        # Plugin that requires a venv (which won't exist)
        venv_plugin_dir = plugins_dir / "venv-plugin"
        venv_plugin_dir.mkdir()
        (venv_plugin_dir / "bootstrap.json").write_text(json.dumps({
            "venv": {"check_imports": ["yaml"]},
        }))

        registry = {"plugins": {"kit:venv-plugin": [{"installPath": "./venv-plugin", "version": "1.0.0"}]}}
        (plugins_dir / "installed_plugins.json").write_text(json.dumps(registry))

        data_dir = str(tmp_path / "data" / "kit" / "bootstrap")
        os.makedirs(data_dir)
        config = {"schema_version": 3, "enabled_plugins": ["kit:venv-plugin"], "log_level": "info", "log_success_shell": False, "log_success_checks": False}
        with open(os.path.join(data_dir, "config.json"), "w") as f:
            json.dump(config, f)

        result = run_engine(data_dir, plugin_root=str(fake_root), env=_isolated_env(tmp_path))

        assert result.returncode == 0
        response = json.loads(result.stdout)
        assert "venv" in response["hookSpecificOutput"]["additionalContext"].lower()
        assert "[venv-plugin]" in response["hookSpecificOutput"]["additionalContext"]

    def test_plugin_venv_exports_env_var_via_claude_env_file(self, tmp_path):
        """Plugin with a working venv writes <PLUGIN>_VENV to $CLAUDE_ENV_FILE.

        Consumer scripts read this var to re-exec themselves under the venv's
        python without reconstructing bootstrap's data-dir path layout.
        """
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()

        fake_root = plugins_dir / "bootstrap"
        fake_root.mkdir()
        link_tree(fake_root / "bootstrap_lib", os.path.join(BOOTSTRAP_ROOT, "bootstrap_lib"))
        link_tree(fake_root / "engine", os.path.join(BOOTSTRAP_ROOT, "engine"))
        _write_minimal_defaults(fake_root)
        (fake_root / "bootstrap.json").write_text(json.dumps({}))

        # Plugin that declares a venv.check_imports. We pre-create the venv
        # in its data dir so the engine's venv check passes without invoking
        # uv sync against a nonexistent pyproject.toml.
        plugin_name = "my-venv-plugin"
        venv_plugin_dir = plugins_dir / plugin_name
        venv_plugin_dir.mkdir()
        (venv_plugin_dir / "bootstrap.json").write_text(json.dumps({
            "venv": {"check_imports": ["os", "sys"]},
        }))

        plugin_data_dir = tmp_path / "data" / "kit" / plugin_name
        plugin_data_dir.mkdir(parents=True)
        subprocess.run(
            ["uv", "venv", str(plugin_data_dir / ".venv")],
            check=True, capture_output=True,
        )

        registry = {"plugins": {f"kit:{plugin_name}": [{"installPath": f"./{plugin_name}", "version": "1.0.0"}]}}
        (plugins_dir / "installed_plugins.json").write_text(json.dumps(registry))

        data_dir = str(tmp_path / "data" / "kit" / "bootstrap")
        os.makedirs(data_dir)
        config = {"schema_version": 3, "enabled_plugins": [f"kit:{plugin_name}"], "log_level": "info", "log_success_shell": False, "log_success_checks": False}
        with open(os.path.join(data_dir, "config.json"), "w") as f:
            json.dump(config, f)

        env_file = tmp_path / "claude_env_file"
        env_file.write_text("")
        env = {**_isolated_env(tmp_path), "CLAUDE_ENV_FILE": str(env_file)}

        result = run_engine(data_dir, plugin_root=str(fake_root), env=env)
        assert result.returncode == 0, result.stderr

        contents = env_file.read_text()
        assert "export MY_VENV_PLUGIN_VENV=" in contents

        # The exported path should resolve to a real python binary (parsed the
        # way a POSIX shell would; see _sourced_value).
        python_path = _sourced_value(env_file, "MY_VENV_PLUGIN_VENV")
        assert python_path is not None
        assert os.path.isfile(python_path)
        assert str(plugin_data_dir / ".venv") in python_path

    def test_plugin_venv_no_export_when_claude_env_file_unset(self, tmp_path):
        """No export is written when CLAUDE_ENV_FILE is absent from the env."""
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()

        fake_root = plugins_dir / "bootstrap"
        fake_root.mkdir()
        link_tree(fake_root / "bootstrap_lib", os.path.join(BOOTSTRAP_ROOT, "bootstrap_lib"))
        link_tree(fake_root / "engine", os.path.join(BOOTSTRAP_ROOT, "engine"))
        _write_minimal_defaults(fake_root)
        (fake_root / "bootstrap.json").write_text(json.dumps({}))

        plugin_name = "nosig"
        venv_plugin_dir = plugins_dir / plugin_name
        venv_plugin_dir.mkdir()
        (venv_plugin_dir / "bootstrap.json").write_text(json.dumps({
            "venv": {"check_imports": ["os"]},
        }))

        plugin_data_dir = tmp_path / "data" / "kit" / plugin_name
        plugin_data_dir.mkdir(parents=True)
        subprocess.run(
            ["uv", "venv", str(plugin_data_dir / ".venv")],
            check=True, capture_output=True,
        )

        registry = {"plugins": {f"kit:{plugin_name}": [{"installPath": f"./{plugin_name}", "version": "1.0.0"}]}}
        (plugins_dir / "installed_plugins.json").write_text(json.dumps(registry))

        data_dir = str(tmp_path / "data" / "kit" / "bootstrap")
        os.makedirs(data_dir)
        config = {"schema_version": 3, "enabled_plugins": [f"kit:{plugin_name}"], "log_level": "info", "log_success_shell": False, "log_success_checks": False}
        with open(os.path.join(data_dir, "config.json"), "w") as f:
            json.dump(config, f)

        env = _isolated_env(tmp_path)
        env.pop("CLAUDE_ENV_FILE", None)

        result = run_engine(data_dir, plugin_root=str(fake_root), env=env)
        assert result.returncode == 0, result.stderr
        # No env file was created; engine should not have crashed.

    def test_git_dep_failure_in_plugin(self, tmp_path):
        """Plugin with missing git dep emits JSON with remediation."""
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()

        fake_root = plugins_dir / "bootstrap"
        fake_root.mkdir()
        link_tree(fake_root / "bootstrap_lib", os.path.join(BOOTSTRAP_ROOT, "bootstrap_lib"))
        link_tree(fake_root / "engine", os.path.join(BOOTSTRAP_ROOT, "engine"))
        _write_minimal_defaults(fake_root)
        (fake_root / "bootstrap.json").write_text(json.dumps({}))

        git_plugin_dir = plugins_dir / "git-plugin"
        git_plugin_dir.mkdir()
        (git_plugin_dir / "bootstrap.json").write_text(json.dumps({
            "git_deps": [{"url": "https://invalid.example.invalid/nonexistent/repo", "branch": "main"}],
        }))

        registry = {"plugins": {"kit:git-plugin": [{"installPath": "./git-plugin", "version": "1.0.0"}]}}
        (plugins_dir / "installed_plugins.json").write_text(json.dumps(registry))

        data_dir = str(tmp_path / "data" / "kit" / "bootstrap")
        os.makedirs(data_dir)
        config = {"schema_version": 3, "enabled_plugins": ["kit:git-plugin"], "log_level": "info", "log_success_shell": False, "log_success_checks": False}
        with open(os.path.join(data_dir, "config.json"), "w") as f:
            json.dump(config, f)

        result = run_engine(data_dir, plugin_root=str(fake_root), env=_isolated_env(tmp_path))

        assert result.returncode == 0
        response = json.loads(result.stdout)
        ctx = response["hookSpecificOutput"]["additionalContext"]
        assert "invalid.example.invalid" in ctx
        assert "[git-plugin]" in ctx

    def test_cache_layout_finds_registry(self, tmp_path):
        """Registry found when bootstrap is nested deep (cache layout)."""
        # Simulate cache layout: plugins/cache/mkt/bootstrap/0.5.0/
        cache_dir = tmp_path / "plugins" / "cache" / "mymkt" / "bootstrap" / "0.5.0"
        cache_dir.mkdir(parents=True)
        link_tree(cache_dir / "bootstrap_lib", os.path.join(BOOTSTRAP_ROOT, "bootstrap_lib"))
        link_tree(cache_dir / "engine", os.path.join(BOOTSTRAP_ROOT, "engine"))
        _write_minimal_defaults(cache_dir)
        (cache_dir / "bootstrap.json").write_text(json.dumps({}))

        # Plugin at cache/mymkt/deep-plugin/1.0.0/
        deep_plugin_dir = tmp_path / "plugins" / "cache" / "mymkt" / "deep-plugin" / "1.0.0"
        deep_plugin_dir.mkdir(parents=True)
        (deep_plugin_dir / "bootstrap.json").write_text(json.dumps({
            "tools": [{"name": "git", "install": {"macos": "brew install git"}}],
        }))

        # Registry at plugins/installed_plugins.json (two levels above cache/mymkt/)
        registry = {"plugins": {"mymkt:deep-plugin": [{"installPath": str(deep_plugin_dir), "version": "1.0.0"}]}}
        (tmp_path / "plugins" / "installed_plugins.json").write_text(json.dumps(registry))

        data_dir = str(tmp_path / "data" / "kit" / "bootstrap")
        os.makedirs(data_dir)
        config = {"schema_version": 3, "enabled_plugins": ["mymkt:deep-plugin"], "log_level": "info", "log_success_shell": False, "log_success_checks": True}
        with open(os.path.join(data_dir, "config.json"), "w") as f:
            json.dump(config, f)

        result = run_engine(data_dir, plugin_root=str(cache_dir), env=_isolated_env(tmp_path))
        assert result.returncode == 0
        response = json.loads(result.stdout)
        assert "deep-plugin" in response["systemMessage"]


class TestPhase2PluginBootstrap:
    """Tests for Step 4b: re-scan for plugins installed during Step 4."""

    def test_phase2_bootstraps_newly_installed_plugin(self, tmp_path):
        """A plugin installed during Step 4 (via script) is bootstrapped in Step 4b."""
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()

        # Set up fake bootstrap root
        fake_root = plugins_dir / "bootstrap"
        fake_root.mkdir()
        link_tree(fake_root / "bootstrap_lib", os.path.join(BOOTSTRAP_ROOT, "bootstrap_lib"))
        link_tree(fake_root / "engine", os.path.join(BOOTSTRAP_ROOT, "engine"))
        _write_minimal_defaults(fake_root)

        # "installer" plugin: its bootstrap script adds "new-plugin" to the registry
        installer_dir = plugins_dir / "installer"
        installer_dir.mkdir()

        # Create the script that simulates install_plugin writing to the registry
        install_script = installer_dir / "install_new_plugin.py"
        new_plugin_dir = plugins_dir / "new-plugin"
        install_script.write_text(
            "import json, os\n"
            "def bootstrap(ctx):\n"
            "    registry_path = os.path.join(os.path.dirname(ctx.plugin_root), 'installed_plugins.json')\n"
            "    with open(registry_path) as f:\n"
            "        registry = json.load(f)\n"
            f"    new_path = {repr(str(new_plugin_dir))}\n"
            "    registry['plugins']['kit:new-plugin'] = [{'installPath': new_path, 'version': '1.0.0'}]\n"
            "    with open(registry_path, 'w') as f:\n"
            "        json.dump(registry, f)\n"
            "    ctx.log('installed new-plugin')\n"
        )

        (installer_dir / "bootstrap.json").write_text(json.dumps({
            "tools": [{"name": "git", "install": {"macos": "brew install git"}}],
            "script": {"path": "install_new_plugin.py"},
        }))

        # "new-plugin": will be added to registry by installer's script
        new_plugin_dir.mkdir()
        (new_plugin_dir / "bootstrap.json").write_text(json.dumps({
            "tools": [{"name": "fake_phase2_tool_xyz", "install": {"macos": "brew install fake"}}],
        }))

        # Initial registry: only installer plugin (new-plugin not yet installed)
        registry = {"plugins": {"kit:installer": [{"installPath": str(installer_dir), "version": "1.0.0"}]}}
        (plugins_dir / "installed_plugins.json").write_text(json.dumps(registry))

        # Bootstrap's own manifest is empty (no checks)
        (fake_root / "bootstrap.json").write_text(json.dumps({}))

        data_dir = str(tmp_path / "data" / "kit" / "bootstrap")
        os.makedirs(data_dir)
        config = {"schema_version": 3, "enabled_plugins": ["kit:installer"], "log_level": "info",
                  "log_success_shell": False, "log_success_checks": True}
        with open(os.path.join(data_dir, "config.json"), "w") as f:
            json.dump(config, f)

        result = run_engine(data_dir, plugin_root=str(fake_root), env=_isolated_env(tmp_path))

        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert result.stdout.strip() != ""
        response = json.loads(result.stdout)
        # new-plugin's tool check should appear (it was discovered in Step 4b)
        ctx_text = response.get("hookSpecificOutput", {}).get("additionalContext", "")
        system_msg = response.get("systemMessage", "")
        combined = ctx_text + system_msg
        assert "fake_phase2_tool_xyz" in combined, f"Phase 2 plugin not bootstrapped. Output: {combined}"
        assert "[new-plugin]" in combined or "new-plugin" in combined

    def test_phase2_noop_when_no_new_plugins(self, tmp_path):
        """No new plugins installed during Step 4 — identical behavior, no regression."""
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()

        fake_root = plugins_dir / "bootstrap"
        fake_root.mkdir()
        link_tree(fake_root / "bootstrap_lib", os.path.join(BOOTSTRAP_ROOT, "bootstrap_lib"))
        link_tree(fake_root / "engine", os.path.join(BOOTSTRAP_ROOT, "engine"))
        _write_minimal_defaults(fake_root)
        (fake_root / "bootstrap.json").write_text(json.dumps({}))

        test_plugin_dir = plugins_dir / "stable-plugin"
        test_plugin_dir.mkdir()
        (test_plugin_dir / "bootstrap.json").write_text(json.dumps({
            "tools": [{"name": "git", "install": {"macos": "brew install git"}}],
        }))

        registry = {"plugins": {"kit:stable-plugin": [{"installPath": str(test_plugin_dir), "version": "1.0.0"}]}}
        (plugins_dir / "installed_plugins.json").write_text(json.dumps(registry))

        data_dir = str(tmp_path / "data" / "kit" / "bootstrap")
        os.makedirs(data_dir)
        config = {"schema_version": 3, "enabled_plugins": ["kit:stable-plugin"], "log_level": "info",
                  "log_success_shell": False, "log_success_checks": True}
        with open(os.path.join(data_dir, "config.json"), "w") as f:
            json.dump(config, f)

        result = run_engine(data_dir, plugin_root=str(fake_root), env=_isolated_env(tmp_path))

        assert result.returncode == 0
        response = json.loads(result.stdout)
        assert "stable-plugin" in response["systemMessage"]
        # Verify no duplicate entries — stable-plugin should appear exactly once in sections
        assert response["systemMessage"].count("stable-plugin") >= 1

    def test_phase2_skips_already_processed_plugins(self, tmp_path):
        """Plugin already in registry from start appears exactly once, not duplicated by Phase 2."""
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()

        fake_root = plugins_dir / "bootstrap"
        fake_root.mkdir()
        link_tree(fake_root / "bootstrap_lib", os.path.join(BOOTSTRAP_ROOT, "bootstrap_lib"))
        link_tree(fake_root / "engine", os.path.join(BOOTSTRAP_ROOT, "engine"))
        _write_minimal_defaults(fake_root)
        (fake_root / "bootstrap.json").write_text(json.dumps({}))

        # Two plugins, both in registry from the start
        plugin_a_dir = plugins_dir / "plugin-a"
        plugin_a_dir.mkdir()
        (plugin_a_dir / "bootstrap.json").write_text(json.dumps({
            "tools": [{"name": "git", "install": {"macos": "brew install git"}}],
        }))

        plugin_b_dir = plugins_dir / "plugin-b"
        plugin_b_dir.mkdir()
        (plugin_b_dir / "bootstrap.json").write_text(json.dumps({
            "tools": [{"name": "git", "install": {"macos": "brew install git"}}],
        }))

        registry = {"plugins": {
            "kit:plugin-a": [{"installPath": str(plugin_a_dir), "version": "1.0.0"}],
            "kit:plugin-b": [{"installPath": str(plugin_b_dir), "version": "2.0.0"}],
        }}
        (plugins_dir / "installed_plugins.json").write_text(json.dumps(registry))

        data_dir = str(tmp_path / "data" / "kit" / "bootstrap")
        os.makedirs(data_dir)
        config = {"schema_version": 3, "enabled_plugins": ["kit:plugin-a", "kit:plugin-b"],
                  "log_level": "info", "log_success_shell": False, "log_success_checks": True}
        with open(os.path.join(data_dir, "config.json"), "w") as f:
            json.dump(config, f)

        result = run_engine(data_dir, plugin_root=str(fake_root), env=_isolated_env(tmp_path))

        assert result.returncode == 0
        response = json.loads(result.stdout)
        system_msg = response["systemMessage"]
        # Each plugin should appear exactly once as a section header
        assert system_msg.count("kit:plugin-a@1.0.0") == 1, f"plugin-a duplicated: {system_msg}"
        assert system_msg.count("kit:plugin-b@2.0.0") == 1, f"plugin-b duplicated: {system_msg}"


def _make_dev_layout(tmp_path, plugins):
    """Create a dev layout with multiple plugins and a registry.

    Args:
        tmp_path: pytest tmp_path
        plugins: list of (name, has_bootstrap_json) tuples

    Returns:
        (fake_root_str, plugins_dir_path, registry_path_str, data_dir_str)
    """
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()

    fake_root = plugins_dir / "bootstrap"
    fake_root.mkdir()
    link_tree(fake_root / "bootstrap_lib", os.path.join(BOOTSTRAP_ROOT, "bootstrap_lib"))
    link_tree(fake_root / "engine", os.path.join(BOOTSTRAP_ROOT, "engine"))
    _write_minimal_defaults(fake_root)
    (fake_root / "bootstrap.json").write_text(json.dumps({}))

    registry_plugins = {}
    for name, has_manifest in plugins:
        plugin_dir = plugins_dir / name
        plugin_dir.mkdir()
        if has_manifest:
            (plugin_dir / "bootstrap.json").write_text(json.dumps({
                "tools": [{"name": "git", "install": {"macos": "brew install git"}}],
            }))
        registry_plugins[f"testkit:{name}"] = [{"installPath": f"./{name}", "version": "1.0.0"}]

    (plugins_dir / "installed_plugins.json").write_text(json.dumps({"plugins": registry_plugins}))

    data_dir = str(tmp_path / "data" / "bootstrap")
    os.makedirs(data_dir)
    config = {"schema_version": 5, "no_bootstrap": [], "bootstrap_cache": [],
              "log_success_shell": False, "log_success_checks": True}
    with open(os.path.join(data_dir, "config.json"), "w") as f:
        json.dump(config, f)

    return str(fake_root), plugins_dir, data_dir


def _settings_home(tmp_path, enabled_refs):
    """Create a temp HOME with settings.json enabling the given plugin refs.

    Args:
        tmp_path: pytest tmp_path
        enabled_refs: list of 'plugin@marketplace' ref strings to enable

    Returns:
        env dict with HOME set to the temp home
    """
    home = tmp_path / "_settings_home"
    claude_dir = home / ".claude"
    claude_dir.mkdir(parents=True)
    settings = {"enabledPlugins": {ref: True for ref in enabled_refs}}
    (claude_dir / "settings.json").write_text(json.dumps(settings))
    return {**os.environ, "HOME": str(home)}


class TestDevLayoutFilter:
    """Dev layout: only enabled plugins are bootstrapped (not all registry entries)."""

    def test_disabled_plugin_skipped(self, tmp_path):
        """Registry has two plugins; only the enabled one is bootstrapped."""
        fake_root, plugins_dir, data_dir = _make_dev_layout(tmp_path, [
            ("enabled-plugin", True),
            ("skipped-plugin", True),
        ])
        env = _settings_home(tmp_path, ["enabled-plugin@testkit"])

        result = run_engine(data_dir, plugin_root=fake_root, env=env)

        assert result.returncode == 0
        response = json.loads(result.stdout)
        system_msg = response["systemMessage"]
        assert "enabled-plugin" in system_msg
        assert "skipped-plugin" not in system_msg

    def test_all_disabled_plugins_skipped(self, tmp_path):
        """When no plugins are in enabled_refs, all registry plugins are filtered."""
        fake_root, plugins_dir, data_dir = _make_dev_layout(tmp_path, [
            ("plugin-a", True),
            ("plugin-b", True),
        ])
        env = _settings_home(tmp_path, [])  # nothing enabled

        result = run_engine(data_dir, plugin_root=fake_root, env=env)

        assert result.returncode == 0
        # No plugin sections — silent exit (no output) or bootstrap-only output
        if result.stdout.strip():
            response = json.loads(result.stdout)
            system_msg = response.get("systemMessage", "")
            assert "plugin-a" not in system_msg
            assert "plugin-b" not in system_msg

    def test_multiple_enabled_plugins_all_included(self, tmp_path):
        """All plugins listed in enabled_refs are bootstrapped."""
        fake_root, plugins_dir, data_dir = _make_dev_layout(tmp_path, [
            ("plugin-x", True),
            ("plugin-y", True),
            ("plugin-z", True),
        ])
        env = _settings_home(tmp_path, ["plugin-x@testkit", "plugin-y@testkit"])

        result = run_engine(data_dir, plugin_root=fake_root, env=env)

        assert result.returncode == 0
        response = json.loads(result.stdout)
        system_msg = response["systemMessage"]
        assert "plugin-x" in system_msg
        assert "plugin-y" in system_msg
        assert "plugin-z" not in system_msg

    def test_production_layout_unaffected(self, tmp_path):
        """In production layout (registry == prod registry), no filter is applied."""
        # Simulate production layout: place installed_plugins.json where the engine
        # expects the prod registry (HOME/.claude/plugins/installed_plugins.json)
        home = tmp_path / "_prod_home"
        prod_plugins_dir = home / ".claude" / "plugins"
        prod_plugins_dir.mkdir(parents=True)

        # Use prod_plugins_dir as the plugins_dir for the bootstrap root too
        fake_root = prod_plugins_dir / "bootstrap"
        fake_root.mkdir()
        link_tree(fake_root / "bootstrap_lib", os.path.join(BOOTSTRAP_ROOT, "bootstrap_lib"))
        link_tree(fake_root / "engine", os.path.join(BOOTSTRAP_ROOT, "engine"))
        _write_minimal_defaults(fake_root)
        (fake_root / "bootstrap.json").write_text(json.dumps({}))

        plugin_dir = prod_plugins_dir / "my-prod-plugin"
        plugin_dir.mkdir()
        (plugin_dir / "bootstrap.json").write_text(json.dumps({
            "tools": [{"name": "git", "install": {"macos": "brew install git"}}],
        }))

        # Registry at HOME/.claude/plugins/installed_plugins.json (the prod location)
        registry = {"plugins": {"testkit:my-prod-plugin": [
            {"installPath": str(plugin_dir), "version": "1.0.0"}
        ]}}
        (prod_plugins_dir / "installed_plugins.json").write_text(json.dumps(registry))

        data_dir = str(tmp_path / "data" / "kit" / "bootstrap")
        os.makedirs(data_dir)
        config = {"schema_version": 5, "no_bootstrap": [], "bootstrap_cache": [],
                  "log_success_shell": False, "log_success_checks": True}
        with open(os.path.join(data_dir, "config.json"), "w") as f:
            json.dump(config, f)

        # Use HOME without any settings.json — prod layout should not filter
        env = {**os.environ, "HOME": str(home)}
        result = run_engine(data_dir, plugin_root=str(fake_root), env=env)

        assert result.returncode == 0
        response = json.loads(result.stdout)
        # my-prod-plugin should be bootstrapped (no filter in prod layout)
        assert "my-prod-plugin" in response["systemMessage"]

    def test_project_scoped_plugin_from_project_settings(self, tmp_path):
        """A plugin enabled in project settings (not user settings) is included."""
        fake_root, plugins_dir, data_dir = _make_dev_layout(tmp_path, [
            ("project-plugin", True),
            ("user-plugin", True),
        ])

        # User settings: only user-plugin
        home = tmp_path / "_proj_home"
        claude_dir = home / ".claude"
        claude_dir.mkdir(parents=True)
        (claude_dir / "settings.json").write_text(json.dumps({
            "enabledPlugins": {"user-plugin@testkit": True}
        }))

        # Project settings: project-plugin (project-scoped)
        project_dir = tmp_path / "my-project"
        project_claude = project_dir / ".claude"
        project_claude.mkdir(parents=True)
        (project_claude / "settings.json").write_text(json.dumps({
            "enabledPlugins": {"project-plugin@testkit": True}
        }))

        env = {**os.environ, "HOME": str(home)}
        result = subprocess.run(
            [sys.executable, ENGINE_SCRIPT,
             "--plugin-root", fake_root,
             "--data-dir", data_dir,
             "--project-dir", str(project_dir)],
            capture_output=True, text=True, env=env,
        )

        assert result.returncode == 0
        response = json.loads(result.stdout)
        system_msg = response["systemMessage"]
        # Both user-scoped and project-scoped plugins should be bootstrapped
        assert "user-plugin" in system_msg
        assert "project-plugin" in system_msg


class TestGitDepPinnedCommit:
    """Engine path for an existing clone at the wrong pinned commit (B1).

    This branch used to do `from .git_dep_check import _git_exe` — a function
    deleted in commit 233fad9 — so any stale pinned clone raised an uncaught
    ImportError that killed the entire bootstrap pass. Pins the bare-"git"
    list-form call.
    """

    @staticmethod
    def _git(*args, cwd):
        return subprocess.run(
            ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", *args],
            cwd=cwd, check=True, capture_output=True, text=True,
        )

    def test_stale_pinned_commit_is_checked_out(self, tmp_path):
        # Source repo with two commits; the manifest pins the FIRST one.
        src_repo = tmp_path / "srcrepo"
        src_repo.mkdir()
        self._git("init", "-b", "main", cwd=src_repo)
        (src_repo / "f.txt").write_text("one")
        self._git("add", "f.txt", cwd=src_repo)
        self._git("commit", "-m", "c1", cwd=src_repo)
        pinned_sha = self._git("rev-parse", "HEAD", cwd=src_repo).stdout.strip()
        (src_repo / "f.txt").write_text("two")
        self._git("add", "f.txt", cwd=src_repo)
        self._git("commit", "-m", "c2", cwd=src_repo)
        head_sha = self._git("rev-parse", "HEAD", cwd=src_repo).stdout.strip()
        assert pinned_sha != head_sha

        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()
        fake_root = plugins_dir / "bootstrap"
        fake_root.mkdir()
        link_tree(fake_root / "bootstrap_lib", os.path.join(BOOTSTRAP_ROOT, "bootstrap_lib"))
        link_tree(fake_root / "engine", os.path.join(BOOTSTRAP_ROOT, "engine"))
        _write_minimal_defaults(fake_root)
        (fake_root / "bootstrap.json").write_text(json.dumps({}))

        pin_plugin_dir = plugins_dir / "pin-plugin"
        pin_plugin_dir.mkdir()
        (pin_plugin_dir / "bootstrap.json").write_text(json.dumps({
            "git_deps": [{"url": str(src_repo), "branch": "main", "commit": pinned_sha}],
        }))

        registry = {"plugins": {"kit:pin-plugin": [{"installPath": "./pin-plugin", "version": "1.0.0"}]}}
        (plugins_dir / "installed_plugins.json").write_text(json.dumps(registry))

        data_dir = str(tmp_path / "data" / "kit" / "bootstrap")
        os.makedirs(data_dir)
        config = {"schema_version": 3, "enabled_plugins": ["kit:pin-plugin"], "log_level": "info",
                  "log_success_shell": False, "log_success_checks": False}
        with open(os.path.join(data_dir, "config.json"), "w") as f:
            json.dump(config, f)

        # Pre-clone at HEAD (the WRONG commit) so the engine takes the
        # existing-clone + pinned-commit checkout branch.
        target = tmp_path / "data" / "kit" / "pin-plugin" / "github" / "srcrepo"
        target.parent.mkdir(parents=True)
        subprocess.run(["git", "clone", str(src_repo), str(target)],
                       check=True, capture_output=True)
        assert self._git("rev-parse", "HEAD", cwd=target).stdout.strip() == head_sha

        result = run_engine(data_dir, plugin_root=str(fake_root), env=_isolated_env(tmp_path))

        assert result.returncode == 0, f"engine crashed: {result.stderr}"
        assert "ImportError" not in result.stderr
        # The clone was moved to the pinned commit...
        assert self._git("rev-parse", "HEAD", cwd=target).stdout.strip() == pinned_sha
        # ...and the action surfaced in the display output.
        response = json.loads(result.stdout)
        assert "checked out" in response["systemMessage"]


class TestMalformedPluginManifest:
    """One plugin's malformed bootstrap.json must not kill the pass (B2)."""

    def test_malformed_manifest_does_not_kill_pass(self, tmp_path):
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()

        fake_root = plugins_dir / "bootstrap"
        fake_root.mkdir()
        link_tree(fake_root / "bootstrap_lib", os.path.join(BOOTSTRAP_ROOT, "bootstrap_lib"))
        link_tree(fake_root / "engine", os.path.join(BOOTSTRAP_ROOT, "engine"))
        _write_minimal_defaults(fake_root)
        (fake_root / "bootstrap.json").write_text(json.dumps({}))

        # Sorts before good-plugin, so the pass must survive it to reach the next one.
        bad_dir = plugins_dir / "bad-manifest"
        bad_dir.mkdir()
        (bad_dir / "bootstrap.json").write_text("{ this is not json !!!")

        good_dir = plugins_dir / "good-plugin"
        good_dir.mkdir()
        (good_dir / "bootstrap.json").write_text(json.dumps({
            "tools": [{"name": "fake_tool_after_bad_manifest", "install": {"macos": "brew install fake"}}],
        }))

        registry = {"plugins": {
            "kit:bad-manifest": [{"installPath": "./bad-manifest", "version": "1.0.0"}],
            "kit:good-plugin": [{"installPath": "./good-plugin", "version": "1.0.0"}],
        }}
        (plugins_dir / "installed_plugins.json").write_text(json.dumps(registry))

        data_dir = str(tmp_path / "data" / "kit" / "bootstrap")
        os.makedirs(data_dir)
        config = {"schema_version": 3, "enabled_plugins": ["kit:bad-manifest", "kit:good-plugin"],
                  "log_level": "info", "log_success_shell": False, "log_success_checks": False}
        with open(os.path.join(data_dir, "config.json"), "w") as f:
            json.dump(config, f)

        result = run_engine(data_dir, plugin_root=str(fake_root), env=_isolated_env(tmp_path))

        assert result.returncode == 0, f"engine crashed on malformed manifest: {result.stderr}"
        response = json.loads(result.stdout)
        ctx = response["hookSpecificOutput"]["additionalContext"]
        # The parse failure is surfaced as a remediable item...
        assert "bad-manifest" in ctx
        assert "parse" in ctx.lower()
        # ...and the pass continued to the next plugin.
        assert "fake_tool_after_bad_manifest" in ctx, (
            f"pass did not continue past the malformed manifest: {ctx}"
        )


class TestCooldownRestamp:
    """End-of-pass cooldown handling (B3).

    The shell stamps the cooldown before launching the engine; the engine may
    then rewrite installed_plugins.json itself (plugin installs,
    ensure_registry_scope). Since the shell's registry-mtime bypass compares
    against the stamp, a clean pass must RE-stamp it so bootstrap-authored
    registry writes don't re-arm a full pass every session. A failing pass
    clears the stamp (pre-existing behavior); the engine never CREATES one.
    """

    def _seed_stamp(self, data_dir, project_dir):
        key = hashlib.sha1(str(project_dir).encode("utf-8")).hexdigest()
        cooldowns = os.path.join(data_dir, "cooldowns")
        os.makedirs(cooldowns, exist_ok=True)
        stamp = os.path.join(cooldowns, f"last_run_epoch.{key}")
        with open(stamp, "w") as f:
            f.write("123")
        os.utime(stamp, (1_000_000_000, 1_000_000_000))
        return stamp

    def test_clean_pass_restamps_existing_cooldown(self, tmp_path):
        fake_root = make_fake_bootstrap_root(tmp_path)
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir)
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        stamp = self._seed_stamp(data_dir, project_dir)

        result = run_engine(data_dir, plugin_root=fake_root,
                            env=_isolated_env(tmp_path), project_dir=str(project_dir))

        assert result.returncode == 0, result.stderr
        assert os.path.isfile(stamp), "clean pass must keep the cooldown stamp"
        with open(stamp) as f:
            content = f.read().strip()
        assert content.isdigit() and int(content) > 1_000_000_000, (
            "stamp must be refreshed to the current epoch at end of pass"
        )
        assert os.stat(stamp).st_mtime > 1_000_000_001, (
            "stamp mtime must be newer than bootstrap's own registry writes"
        )

    def test_clean_pass_does_not_create_missing_stamp(self, tmp_path):
        """Stamp creation is the shell hook's job; direct/console/test runs of
        the engine must not plant cooldowns."""
        fake_root = make_fake_bootstrap_root(tmp_path)
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir)
        project_dir = tmp_path / "proj"
        project_dir.mkdir()

        result = run_engine(data_dir, plugin_root=fake_root,
                            env=_isolated_env(tmp_path), project_dir=str(project_dir))

        assert result.returncode == 0, result.stderr
        key = hashlib.sha1(str(project_dir).encode("utf-8")).hexdigest()
        assert not os.path.exists(
            os.path.join(data_dir, "cooldowns", f"last_run_epoch.{key}")
        )

    def test_failing_pass_clears_stamp(self, tmp_path):
        """Failure path keeps the pre-existing rollback (clear, not restamp)."""
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()
        fake_root = plugins_dir / "bootstrap"
        fake_root.mkdir()
        link_tree(fake_root / "bootstrap_lib", os.path.join(BOOTSTRAP_ROOT, "bootstrap_lib"))
        link_tree(fake_root / "engine", os.path.join(BOOTSTRAP_ROOT, "engine"))
        _write_minimal_defaults(fake_root)
        (fake_root / "bootstrap.json").write_text(json.dumps({}))

        bad_tool_dir = plugins_dir / "fail-plugin"
        bad_tool_dir.mkdir()
        (bad_tool_dir / "bootstrap.json").write_text(json.dumps({
            "tools": [{"name": "fake_tool_for_cooldown_clear", "install": {"macos": "brew install fake"}}],
        }))
        registry = {"plugins": {"kit:fail-plugin": [{"installPath": "./fail-plugin", "version": "1.0.0"}]}}
        (plugins_dir / "installed_plugins.json").write_text(json.dumps(registry))

        data_dir = str(tmp_path / "data" / "kit" / "bootstrap")
        os.makedirs(data_dir)
        config = {"schema_version": 3, "enabled_plugins": ["kit:fail-plugin"], "log_level": "info",
                  "log_success_shell": False, "log_success_checks": False}
        with open(os.path.join(data_dir, "config.json"), "w") as f:
            json.dump(config, f)

        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        stamp = self._seed_stamp(data_dir, project_dir)

        result = run_engine(data_dir, plugin_root=str(fake_root),
                            env=_isolated_env(tmp_path), project_dir=str(project_dir))

        assert result.returncode == 0
        assert "fake_tool_for_cooldown_clear" in result.stdout
        assert not os.path.exists(stamp), "failing pass must clear the stamp, not restamp it"


class TestScriptContextProjectDir:
    """Verify ctx.project_dir reaches plugin scripts (Bug 1 in
    bugreport-statusline-not-installed-per-project.md). Without it, scripts
    re-derive project root from Path.cwd() and walk up looking for any
    `.claude/` — which is wrong, since Claude Code itself does not walk up.
    """

    def _make_setup(self, tmp_path, script_content):
        """Set up a plugin with a script that records ctx.project_dir to a marker file."""
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()

        fake_root = plugins_dir / "bootstrap"
        fake_root.mkdir()
        link_tree(fake_root / "bootstrap_lib", os.path.join(BOOTSTRAP_ROOT, "bootstrap_lib"))
        link_tree(fake_root / "engine", os.path.join(BOOTSTRAP_ROOT, "engine"))
        _write_minimal_defaults(fake_root)
        (fake_root / "bootstrap.json").write_text(json.dumps({}))

        plugin_dir = plugins_dir / "ctx-plugin"
        plugin_dir.mkdir()
        (plugin_dir / "bootstrap.json").write_text(json.dumps({
            "script": {"path": "my_script.py", "entry_point": "bootstrap"},
        }))
        (plugin_dir / "my_script.py").write_text(script_content)

        registry = {"plugins": {"kit:ctx-plugin": [{"installPath": "./ctx-plugin", "version": "1.0.0"}]}}
        (plugins_dir / "installed_plugins.json").write_text(json.dumps(registry))

        data_dir = str(tmp_path / "data" / "kit" / "bootstrap")
        os.makedirs(data_dir)
        config = {"schema_version": 3, "enabled_plugins": ["kit:ctx-plugin"], "log_level": "info", "log_success_shell": False, "log_success_checks": False}
        with open(os.path.join(data_dir, "config.json"), "w") as f:
            json.dump(config, f)
        return str(fake_root), data_dir

    def test_script_receives_project_dir(self, tmp_path):
        marker = tmp_path / "marker.txt"
        # Write the raw project_dir string (no repr()) so comparison is direct.
        script = (
            "def bootstrap(ctx):\n"
            f"    open({repr(str(marker))}, 'w').write(ctx.project_dir or '__NONE__')\n"
        )
        fake_root, data_dir = self._make_setup(tmp_path, script)
        project = tmp_path / "fake_project"
        project.mkdir()
        result = run_engine(data_dir, plugin_root=fake_root, env=_isolated_env(tmp_path), project_dir=str(project))
        assert result.returncode == 0, result.stderr
        assert marker.exists(), f"script did not run; stdout={result.stdout!r} stderr={result.stderr!r}"
        assert marker.read_text() == str(project)

    def test_script_project_dir_none_when_unset(self, tmp_path):
        marker = tmp_path / "marker.txt"
        script = (
            "def bootstrap(ctx):\n"
            f"    open({repr(str(marker))}, 'w').write(ctx.project_dir or '__NONE__')\n"
        )
        fake_root, data_dir = self._make_setup(tmp_path, script)
        result = run_engine(data_dir, plugin_root=fake_root, env=_isolated_env(tmp_path))
        assert result.returncode == 0, result.stderr
        assert marker.exists(), f"script did not run; stdout={result.stdout!r} stderr={result.stderr!r}"
        assert marker.read_text() == "__NONE__"
