"""Tests for scripts/check-staged-version-bump.sh (the X8 pre-commit gate).

Each test builds a throwaway git repo, stages a scenario, and runs the real
script (cwd inside the tmp repo; the script resolves the repo via
`git rev-parse --show-toplevel`).
"""

import json
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check-staged-version-bump.sh"


@pytest.fixture
def repo(tmp_path):
    """A git repo containing one committed plugin (foo, version 1.0.0)."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for k, v in (("user.email", "t@example.com"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(tmp_path), "config", k, v], check=True)
    plugin = tmp_path / "plugins" / "foo" / ".claude-plugin"
    plugin.mkdir(parents=True)
    (plugin / "plugin.json").write_text(
        json.dumps({"name": "foo", "version": "1.0.0"}, indent=2) + "\n")
    (tmp_path / "plugins" / "foo" / "code.py").write_text("x = 1\n")
    (tmp_path / "README.md").write_text("hi\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "init"], check=True)
    return tmp_path


def run_check(repo_path, env_extra=None):
    import os
    env = dict(os.environ)
    env.pop("PLUGINS_KIT_SKIP_BUMP_CHECK", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["sh", str(_SCRIPT)], cwd=repo_path, env=env,
        capture_output=True, text=True)


def stage(repo_path, *paths):
    subprocess.run(["git", "-C", str(repo_path), "add", "-A", *paths], check=True)


class TestBlocks:
    def test_plugin_change_without_bump_blocks(self, repo):
        (repo / "plugins" / "foo" / "code.py").write_text("x = 2\n")
        stage(repo)
        result = run_check(repo)
        assert result.returncode == 1
        assert "foo" in result.stderr
        assert "PLUGINS_KIT_SKIP_BUMP_CHECK" in result.stderr
        assert "--no-verify" in result.stderr

    def test_manifest_only_change_without_bump_blocks(self, repo):
        # bootstrap.json edits count as code (manifest_changes_need_version_bump)
        (repo / "plugins" / "foo" / "bootstrap.json").write_text("{}\n")
        stage(repo)
        result = run_check(repo)
        assert result.returncode == 1
        assert "foo" in result.stderr


class TestPasses:
    def test_plugin_change_with_staged_bump_passes(self, repo):
        (repo / "plugins" / "foo" / "code.py").write_text("x = 2\n")
        pj = repo / "plugins" / "foo" / ".claude-plugin" / "plugin.json"
        pj.write_text(json.dumps({"name": "foo", "version": "1.0.1"}, indent=2) + "\n")
        stage(repo)
        assert run_check(repo).returncode == 0

    def test_non_plugin_change_passes(self, repo):
        (repo / "README.md").write_text("changed\n")
        stage(repo)
        assert run_check(repo).returncode == 0

    def test_nothing_staged_passes(self, repo):
        (repo / "plugins" / "foo" / "code.py").write_text("x = 2\n")  # unstaged
        assert run_check(repo).returncode == 0

    def test_new_plugin_passes(self, repo):
        # A brand-new plugin stages its plugin.json as an addition -- the
        # version line appears in the staged diff, so no false block.
        plugin = repo / "plugins" / "bar" / ".claude-plugin"
        plugin.mkdir(parents=True)
        (plugin / "plugin.json").write_text(
            json.dumps({"name": "bar", "version": "0.1.0"}) + "\n")
        (repo / "plugins" / "bar" / "code.py").write_text("y = 1\n")
        stage(repo)
        assert run_check(repo).returncode == 0

    def test_deleted_plugin_passes(self, repo):
        import shutil
        shutil.rmtree(repo / "plugins" / "foo")
        stage(repo)
        assert run_check(repo).returncode == 0

    def test_escape_hatch_env_var(self, repo):
        (repo / "plugins" / "foo" / "code.py").write_text("x = 2\n")
        stage(repo)
        result = run_check(repo, env_extra={"PLUGINS_KIT_SKIP_BUMP_CHECK": "1"})
        assert result.returncode == 0
