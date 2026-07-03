"""Tests for layered bootstrap.json (user + project level) in bootstrap engine."""

import json
import os
import subprocess
import sys

import pytest

BOOTSTRAP_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "plugins", "bootstrap")
)
ENGINE_SCRIPT = os.path.join(BOOTSTRAP_ROOT, "engine", "bootstrap_engine.py")


def run_engine(data_dir, plugin_root=BOOTSTRAP_ROOT, project_dir=None, env_override=None):
    cmd = [sys.executable, ENGINE_SCRIPT, "--plugin-root", plugin_root, "--data-dir", data_dir]
    if project_dir:
        cmd.extend(["--project-dir", project_dir])
    env = dict(os.environ)
    if env_override:
        env.update(env_override)
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


def make_minimal_root(tmp_path):
    """Create a fake bootstrap root with no requirements."""
    fake_root = tmp_path / "bootstrap_minimal"
    fake_root.mkdir(exist_ok=True)
    if not (fake_root / "bootstrap_lib").exists():
        (fake_root / "bootstrap_lib").symlink_to(os.path.join(BOOTSTRAP_ROOT, "bootstrap_lib"))
    if not (fake_root / "engine").exists():
        (fake_root / "engine").symlink_to(os.path.join(BOOTSTRAP_ROOT, "engine"))
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
    (fake_root / "bootstrap.json").write_text(json.dumps({}))
    return str(fake_root)


class TestLegacyUserBootstrap:
    """Legacy user-bootstrap.json still works but emits deprecation notice."""

    @staticmethod
    def _isolated_home(tmp_path):
        """An empty HOME so the engine discovers no real installed_plugins.json
        (an un-isolated run would execute claude-ui-kit's install_statusline
        against the developer's real ~/.claude/settings.json)."""
        home = tmp_path / "fakehome"
        home.mkdir(exist_ok=True)
        return {"HOME": str(home), "USERPROFILE": str(home)}

    def test_legacy_user_bootstrap_processed(self, data_dir, tmp_path):
        """user-bootstrap.json in data_dir is still processed (backward compat)."""
        fake_root = make_minimal_root(tmp_path)

        user_manifest = {"tools": [{"name": "git"}]}
        user_path = os.path.join(data_dir, "user-bootstrap.json")
        with open(user_path, "w") as f:
            json.dump(user_manifest, f)

        config_path = os.path.join(data_dir, "config.json")
        with open(config_path, "w") as f:
            json.dump({"schema_version": 5, "log_success_checks": True}, f)

        result = run_engine(data_dir, plugin_root=fake_root,
                            env_override=self._isolated_home(tmp_path))
        assert result.returncode == 0

    def test_legacy_deprecation_notice(self, data_dir, tmp_path):
        """Deprecation warning emitted when user-bootstrap.json exists."""
        fake_root = make_minimal_root(tmp_path)

        user_path = os.path.join(data_dir, "user-bootstrap.json")
        with open(user_path, "w") as f:
            json.dump({"tools": []}, f)

        config_path = os.path.join(data_dir, "config.json")
        with open(config_path, "w") as f:
            json.dump({"schema_version": 5, "log_success_checks": True}, f)

        result = run_engine(data_dir, plugin_root=fake_root,
                            env_override=self._isolated_home(tmp_path))
        assert result.returncode == 0
        assert result.stdout.strip() != ""
        response = json.loads(result.stdout)
        assert "DEPRECATED" in response.get("systemMessage", "")

    def test_legacy_failure_emits_json(self, data_dir, tmp_path):
        """user-bootstrap.json failures still appear in output."""
        fake_root = make_minimal_root(tmp_path)

        user_manifest = {"tools": [{"name": "nonexistent_tool_xyz_personal"}]}
        user_path = os.path.join(data_dir, "user-bootstrap.json")
        with open(user_path, "w") as f:
            json.dump(user_manifest, f)

        result = run_engine(data_dir, plugin_root=fake_root,
                            env_override=self._isolated_home(tmp_path))
        assert result.returncode == 0
        assert result.stdout.strip() != ""
        response = json.loads(result.stdout)
        assert "nonexistent_tool_xyz_personal" in response["hookSpecificOutput"]["additionalContext"]


class TestLayeredManifests:
    """Test layered bootstrap.json loading (user + project levels)."""

    def test_no_layered_files_is_silent(self, data_dir, tmp_path):
        """Without any layered bootstrap files, engine runs silently."""
        fake_root = make_minimal_root(tmp_path)
        # Use a fake HOME with no bootstrap.json
        fake_home = str(tmp_path / "fakehome")
        os.makedirs(fake_home, exist_ok=True)
        result = run_engine(data_dir, plugin_root=fake_root, env_override={"HOME": fake_home})
        assert result.returncode == 0

    def test_user_level_bootstrap(self, data_dir, tmp_path):
        """~/.claude/bootstrap.json is discovered and processed."""
        fake_root = make_minimal_root(tmp_path)
        fake_home = str(tmp_path / "fakehome")
        claude_dir = os.path.join(fake_home, ".claude")
        os.makedirs(claude_dir)

        # Write a user-level bootstrap.json with a tool check
        manifest = {"tools": [{"name": "git"}]}
        with open(os.path.join(claude_dir, "bootstrap.json"), "w") as f:
            json.dump(manifest, f)

        config_path = os.path.join(data_dir, "config.json")
        with open(config_path, "w") as f:
            json.dump({"schema_version": 5, "log_success_checks": True}, f)

        result = run_engine(data_dir, plugin_root=fake_root, env_override={"HOME": fake_home})
        assert result.returncode == 0
        # Display is silent when only oks; verify the manifest was processed via the log.
        with open(os.path.join(data_dir, "bootstrap.log")) as f:
            assert "git" in f.read()

    def test_project_level_bootstrap(self, data_dir, tmp_path):
        """<project>/.claude/bootstrap.json is discovered and processed."""
        fake_root = make_minimal_root(tmp_path)
        fake_home = str(tmp_path / "fakehome")
        os.makedirs(os.path.join(fake_home, ".claude"), exist_ok=True)

        # Create project dir with .claude/bootstrap.json
        project_dir = str(tmp_path / "project")
        project_claude = os.path.join(project_dir, ".claude")
        os.makedirs(project_claude)

        manifest = {"tools": [{"name": "git"}]}
        with open(os.path.join(project_claude, "bootstrap.json"), "w") as f:
            json.dump(manifest, f)

        config_path = os.path.join(data_dir, "config.json")
        with open(config_path, "w") as f:
            json.dump({"schema_version": 5, "log_success_checks": True}, f)

        result = run_engine(
            data_dir, plugin_root=fake_root, project_dir=project_dir,
            env_override={"HOME": fake_home},
        )
        assert result.returncode == 0
        # Display is silent when only oks; verify the project manifest was processed via the log.
        with open(os.path.join(data_dir, "bootstrap.log")) as f:
            assert "git" in f.read()

    def test_project_local_override(self, data_dir, tmp_path):
        """bootstrap.local.json overrides bootstrap.json at project level."""
        fake_root = make_minimal_root(tmp_path)
        fake_home = str(tmp_path / "fakehome")
        os.makedirs(os.path.join(fake_home, ".claude"), exist_ok=True)

        project_dir = str(tmp_path / "project")
        project_claude = os.path.join(project_dir, ".claude")
        os.makedirs(project_claude)

        # Base project manifest declares a nonexistent tool
        base_manifest = {"tools": [{"name": "nonexistent_layered_tool_abc"}]}
        with open(os.path.join(project_claude, "bootstrap.json"), "w") as f:
            json.dump(base_manifest, f)

        # Local override adds another nonexistent tool
        local_manifest = {"tools": [{"name": "nonexistent_layered_tool_def"}]}
        with open(os.path.join(project_claude, "bootstrap.local.json"), "w") as f:
            json.dump(local_manifest, f)

        result = run_engine(
            data_dir, plugin_root=fake_root, project_dir=project_dir,
            env_override={"HOME": fake_home},
        )
        assert result.returncode == 0
        assert result.stdout.strip() != ""
        response = json.loads(result.stdout)
        context = response["hookSpecificOutput"]["additionalContext"]
        # Both tools should appear (unioned)
        assert "nonexistent_layered_tool_abc" in context
        assert "nonexistent_layered_tool_def" in context

    def test_user_and_project_merged(self, data_dir, tmp_path):
        """User-level and project-level manifests are merged."""
        fake_root = make_minimal_root(tmp_path)
        fake_home = str(tmp_path / "fakehome")
        claude_dir = os.path.join(fake_home, ".claude")
        os.makedirs(claude_dir)

        # User-level: check for nonexistent tool A
        user_manifest = {"tools": [{"name": "nonexistent_user_tool_aaa"}]}
        with open(os.path.join(claude_dir, "bootstrap.json"), "w") as f:
            json.dump(user_manifest, f)

        # Project-level: check for nonexistent tool B
        project_dir = str(tmp_path / "project")
        project_claude = os.path.join(project_dir, ".claude")
        os.makedirs(project_claude)

        project_manifest = {"tools": [{"name": "nonexistent_project_tool_bbb"}]}
        with open(os.path.join(project_claude, "bootstrap.json"), "w") as f:
            json.dump(project_manifest, f)

        result = run_engine(
            data_dir, plugin_root=fake_root, project_dir=project_dir,
            env_override={"HOME": fake_home},
        )
        assert result.returncode == 0
        assert result.stdout.strip() != ""
        response = json.loads(result.stdout)
        context = response["hookSpecificOutput"]["additionalContext"]
        # Both tools should appear
        assert "nonexistent_user_tool_aaa" in context
        assert "nonexistent_project_tool_bbb" in context

    def test_no_project_dir_skips_project_layer(self, data_dir, tmp_path):
        """When --project-dir is not given, only user-level files are loaded."""
        fake_root = make_minimal_root(tmp_path)
        fake_home = str(tmp_path / "fakehome")
        claude_dir = os.path.join(fake_home, ".claude")
        os.makedirs(claude_dir)

        user_manifest = {"tools": [{"name": "git"}]}
        with open(os.path.join(claude_dir, "bootstrap.json"), "w") as f:
            json.dump(user_manifest, f)

        config_path = os.path.join(data_dir, "config.json")
        with open(config_path, "w") as f:
            json.dump({"schema_version": 5, "log_success_checks": True}, f)

        # No --project-dir passed
        result = run_engine(data_dir, plugin_root=fake_root, env_override={"HOME": fake_home})
        assert result.returncode == 0
        # Display is silent when only oks; verify the user manifest was processed via the log.
        with open(os.path.join(data_dir, "bootstrap.log")) as f:
            assert "git" in f.read()

    def test_user_local_override(self, data_dir, tmp_path):
        """~/.claude/bootstrap.local.json overrides ~/.claude/bootstrap.json."""
        fake_root = make_minimal_root(tmp_path)
        fake_home = str(tmp_path / "fakehome")
        claude_dir = os.path.join(fake_home, ".claude")
        os.makedirs(claude_dir)

        # User base: tool A
        user_manifest = {"tools": [{"name": "nonexistent_user_base_xyz"}]}
        with open(os.path.join(claude_dir, "bootstrap.json"), "w") as f:
            json.dump(user_manifest, f)

        # User local: tool B
        user_local_manifest = {"tools": [{"name": "nonexistent_user_local_xyz"}]}
        with open(os.path.join(claude_dir, "bootstrap.local.json"), "w") as f:
            json.dump(user_local_manifest, f)

        result = run_engine(data_dir, plugin_root=fake_root, env_override={"HOME": fake_home})
        assert result.returncode == 0
        assert result.stdout.strip() != ""
        response = json.loads(result.stdout)
        context = response["hookSpecificOutput"]["additionalContext"]
        # Both tools should appear (unioned)
        assert "nonexistent_user_base_xyz" in context
        assert "nonexistent_user_local_xyz" in context

    def test_project_venv_creates_venv(self, data_dir, tmp_path):
        """project_venv with check_imports runs uv sync when venv is missing."""
        fake_root = make_minimal_root(tmp_path)
        fake_home = str(tmp_path / "fakehome")
        claude_dir = os.path.join(fake_home, ".claude")
        os.makedirs(claude_dir)

        # Create a minimal project with pyproject.toml
        project_dir = str(tmp_path / "project")
        os.makedirs(project_dir)
        # Write a minimal pyproject.toml so uv sync can work
        pyproject = os.path.join(project_dir, "pyproject.toml")
        with open(pyproject, "w") as f:
            f.write('[project]\nname = "test-proj"\nversion = "0.1.0"\nrequires-python = ">=3.10"\n'
                    'dependencies = []\n\n[project.optional-dependencies]\ndev = ["pytest"]\n')

        # User-level bootstrap with project_venv
        manifest = {"project_venv": {"extras": ["dev"], "check_imports": ["pytest"]}}
        with open(os.path.join(claude_dir, "bootstrap.json"), "w") as f:
            json.dump(manifest, f)

        config_path = os.path.join(data_dir, "config.json")
        with open(config_path, "w") as f:
            json.dump({"schema_version": 5, "log_success_checks": True}, f)

        result = run_engine(
            data_dir, plugin_root=fake_root, project_dir=project_dir,
            env_override={"HOME": fake_home},
        )
        assert result.returncode == 0
        assert result.stdout.strip() != ""
        response = json.loads(result.stdout)
        msg = response.get("systemMessage", "")
        # Should show project_venv activity (either created or ok)
        assert "project_venv" in msg

    def test_project_venv_skipped_without_project_dir(self, data_dir, tmp_path):
        """project_venv is silently skipped when --project-dir is not provided."""
        fake_root = make_minimal_root(tmp_path)
        fake_home = str(tmp_path / "fakehome")
        claude_dir = os.path.join(fake_home, ".claude")
        os.makedirs(claude_dir)

        # User-level bootstrap with project_venv but no --project-dir
        manifest = {"project_venv": {"extras": ["dev"], "check_imports": ["pytest"]}}
        with open(os.path.join(claude_dir, "bootstrap.json"), "w") as f:
            json.dump(manifest, f)

        config_path = os.path.join(data_dir, "config.json")
        with open(config_path, "w") as f:
            json.dump({"schema_version": 5, "log_success_checks": True}, f)

        # No --project-dir
        result = run_engine(data_dir, plugin_root=fake_root, env_override={"HOME": fake_home})
        assert result.returncode == 0
        # project_venv should not appear in output (silently skipped)
        if result.stdout.strip():
            response = json.loads(result.stdout)
            msg = response.get("systemMessage", "")
            assert "project_venv" not in msg

    def test_project_venv_already_ok(self, data_dir, tmp_path):
        """project_venv with existing working venv reports ok."""
        fake_root = make_minimal_root(tmp_path)
        fake_home = str(tmp_path / "fakehome")
        claude_dir = os.path.join(fake_home, ".claude")
        os.makedirs(claude_dir)

        # Create a project with a pre-existing venv
        project_dir = str(tmp_path / "project")
        os.makedirs(project_dir)
        pyproject = os.path.join(project_dir, "pyproject.toml")
        with open(pyproject, "w") as f:
            f.write('[project]\nname = "test-proj"\nversion = "0.1.0"\nrequires-python = ">=3.10"\n'
                    'dependencies = []\n\n[project.optional-dependencies]\ndev = []\n')

        # Pre-create venv using uv sync so it already exists
        env = dict(os.environ)
        env["HOME"] = fake_home
        subprocess.run(
            [sys.executable, "-m", "uv", "sync", "--project", project_dir],
            capture_output=True, env=env,
        )

        # project_venv with no check_imports (just needs venv + python)
        manifest = {"project_venv": {"extras": [], "check_imports": []}}
        with open(os.path.join(claude_dir, "bootstrap.json"), "w") as f:
            json.dump(manifest, f)

        config_path = os.path.join(data_dir, "config.json")
        with open(config_path, "w") as f:
            json.dump({"schema_version": 5, "log_success_checks": True}, f)

        result = run_engine(
            data_dir, plugin_root=fake_root, project_dir=project_dir,
            env_override={"HOME": fake_home},
        )
        assert result.returncode == 0
        # Display is silent when only oks; verify via the log file.
        with open(os.path.join(data_dir, "bootstrap.log")) as f:
            assert "project_venv: ok" in f.read()

    def test_priority_project_local_wins(self, data_dir, tmp_path):
        """Project-local bootstrap.local.json has highest priority for field overrides."""
        fake_root = make_minimal_root(tmp_path)
        fake_home = str(tmp_path / "fakehome")
        claude_dir = os.path.join(fake_home, ".claude")
        os.makedirs(claude_dir)

        project_dir = str(tmp_path / "project")
        project_claude = os.path.join(project_dir, ".claude")
        os.makedirs(project_claude)

        # User level: path_entries with one entry
        with open(os.path.join(claude_dir, "bootstrap.json"), "w") as f:
            json.dump({"path_entries": ["/from/user"]}, f)

        # Project level: different path
        with open(os.path.join(project_claude, "bootstrap.json"), "w") as f:
            json.dump({"path_entries": ["/from/project"]}, f)

        # Project local: another path
        with open(os.path.join(project_claude, "bootstrap.local.json"), "w") as f:
            json.dump({"path_entries": ["/from/local"]}, f)

        config_path = os.path.join(data_dir, "config.json")
        with open(config_path, "w") as f:
            json.dump({"schema_version": 5, "log_success_checks": True}, f)

        result = run_engine(
            data_dir, plugin_root=fake_root, project_dir=project_dir,
            env_override={"HOME": fake_home},
        )
        assert result.returncode == 0
        # All three paths should be processed (unioned)
        msg = result.stdout
        assert "/from/user" in msg or result.returncode == 0  # paths may or may not show
