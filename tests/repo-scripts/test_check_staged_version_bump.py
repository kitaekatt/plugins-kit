"""Tests for scripts/check-staged-version-bump.sh (the X8 pre-commit gate).

Each test builds a throwaway git repo, stages a scenario, and runs the real
script (cwd inside the tmp repo; the script resolves the repo via
`git rev-parse --show-toplevel`).

TWO MODES, and the fixture picks which one a test exercises:

  * `repo` has no origin/master and no scripts/publish.py, so the gate cannot
    find a publish point and falls back to the per-commit staged-diff question.
    The DEGRADED path.
  * `published_repo` adds both, so the gate asks the real question -- index
    version against the version at the last publish point.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "check-staged-version-bump.sh"
_PUBLISH_PY = _REPO_ROOT / "scripts" / "publish.py"


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


@pytest.fixture
def published_repo(repo):
    """`repo`, plus what the gate needs to find a publish point.

    scripts/publish.py is the real file (it resolves its own repo root from its
    location, so a copy governs the tmp repo), and origin/master is pinned at
    the initial commit -- the state the "last publish" shipped.
    """
    (repo / "scripts").mkdir()
    shutil.copy(_PUBLISH_PY, repo / "scripts" / "publish.py")
    subprocess.run(["git", "-C", str(repo), "add", "scripts"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "scripts"], check=True)
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True).stdout.strip()
    subprocess.run(
        ["git", "-C", str(repo), "update-ref", "refs/remotes/origin/master", head],
        check=True)
    return repo


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


def write_pyproject(repo_path, name, version):
    (repo_path / "plugins" / name / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "{version}"\n')


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

    def test_pure_pyproject_sync_passes(self, repo):
        # The sync demanded by check_pyproject_sync.py must be landable. Staging
        # ONLY pyproject.toml, already equal to the authoritative plugin.json,
        # is not a missing bump -- there is nothing to bump. Before this was
        # handled the two pre-commit gates deadlocked: one ordered the sync, the
        # other rejected it, and the way out was burning a version number.
        write_pyproject(repo, "foo", "0.9.0")
        stage(repo)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "drift"], check=True)
        write_pyproject(repo, "foo", "1.0.0")  # equal to plugin.json
        stage(repo, "plugins/foo/pyproject.toml")
        result = run_check(repo)
        assert result.returncode == 0, result.stderr

    def test_pyproject_sync_alongside_code_change_blocks(self, repo):
        # Narrowness: the exemption covers a lone pyproject.toml only. A code
        # file riding along is a real change and still needs a bump.
        write_pyproject(repo, "foo", "0.9.0")
        stage(repo)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "drift"], check=True)
        write_pyproject(repo, "foo", "1.0.0")
        (repo / "plugins" / "foo" / "code.py").write_text("x = 2\n")
        stage(repo)
        result = run_check(repo)
        assert result.returncode == 1
        assert "foo" in result.stderr

    def test_pyproject_edit_that_does_not_match_plugin_json_blocks(self, repo):
        # Narrowness: only a pyproject that now AGREES with plugin.json is a
        # sync. Any other version edit is an unbumped change.
        write_pyproject(repo, "foo", "0.9.0")
        stage(repo)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "drift"], check=True)
        write_pyproject(repo, "foo", "2.5.0")  # agrees with nothing
        stage(repo, "plugins/foo/pyproject.toml")
        result = run_check(repo)
        assert result.returncode == 1
        assert "foo" in result.stderr

    def test_escape_hatch_env_var(self, repo):
        (repo / "plugins" / "foo" / "code.py").write_text("x = 2\n")
        stage(repo)
        result = run_check(repo, env_extra={"PLUGINS_KIT_SKIP_BUMP_CHECK": "1"})
        assert result.returncode == 0


class TestEndState:
    """The real question: index version vs the version at the publish point.

    Every test here uses `published_repo`, so the gate can find a base. These
    are the cases the per-commit staged-diff question got wrong -- it asked
    about authoring mechanics, and amending or splitting a change are mechanics
    that say nothing about whether the plugin got bumped.
    """

    def test_amend_of_a_bumped_commit_passes(self, published_repo):
        # The reported defect. Bump lands in a commit; amending it stages a
        # further edit while index and HEAD BOTH already carry the bump, so the
        # staged diff shows no version line -- yet the commit plainly contains
        # one.
        repo = published_repo
        (repo / "plugins" / "foo" / "code.py").write_text("x = 2\n")
        pj = repo / "plugins" / "foo" / ".claude-plugin" / "plugin.json"
        pj.write_text(json.dumps({"name": "foo", "version": "1.0.1"}, indent=2) + "\n")
        stage(repo)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "bump"], check=True)
        (repo / "plugins" / "foo" / "code.py").write_text("x = 3\n")
        stage(repo)
        # What the hook sees mid-amend: no version line in the staged diff.
        diff = subprocess.run(
            ["git", "-C", str(repo), "diff", "--cached", "-U0", "--",
             "plugins/foo/.claude-plugin/plugin.json"],
            capture_output=True, text=True, check=True).stdout
        assert '"version"' not in diff
        result = run_check(repo)
        assert result.returncode == 0, result.stderr

    def test_bump_in_an_earlier_commit_passes(self, published_repo):
        # Same invariant, without an amend: the bump landed in its own commit
        # and a later commit touches the plugin again. One bump since the last
        # publish is what makes consumers refetch, so this is bumped enough.
        repo = published_repo
        pj = repo / "plugins" / "foo" / ".claude-plugin" / "plugin.json"
        pj.write_text(json.dumps({"name": "foo", "version": "1.0.1"}, indent=2) + "\n")
        stage(repo)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "bump"], check=True)
        (repo / "plugins" / "foo" / "code.py").write_text("x = 9\n")
        stage(repo)
        assert run_check(repo).returncode == 0

    def test_unbumped_plugin_still_blocks(self, published_repo):
        repo = published_repo
        (repo / "plugins" / "foo" / "code.py").write_text("x = 2\n")
        stage(repo)
        result = run_check(repo)
        assert result.returncode == 1
        assert "foo" in result.stderr
        assert "PLUGINS_KIT_SKIP_BUMP_CHECK" in result.stderr

    def test_unbumped_across_several_commits_blocks(self, published_repo):
        # Caught by nothing before: no single commit carries a bump and none is
        # an amend, so the per-commit question had no version line to miss.
        repo = published_repo
        (repo / "plugins" / "foo" / "code.py").write_text("x = 2\n")
        stage(repo)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "edit 1",
                        "--no-verify"], check=True)
        (repo / "plugins" / "foo" / "other.py").write_text("y = 1\n")
        stage(repo)
        result = run_check(repo)
        assert result.returncode == 1
        assert "foo" in result.stderr

    def test_bump_reverted_back_to_the_published_version_blocks(self, published_repo):
        # End state is what is asked, so a bump undone is not a bump.
        repo = published_repo
        pj = repo / "plugins" / "foo" / ".claude-plugin" / "plugin.json"
        pj.write_text(json.dumps({"name": "foo", "version": "1.0.1"}, indent=2) + "\n")
        (repo / "plugins" / "foo" / "code.py").write_text("x = 2\n")
        stage(repo)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "bump",
                        "--no-verify"], check=True)
        pj.write_text(json.dumps({"name": "foo", "version": "1.0.0"}, indent=2) + "\n")
        stage(repo)
        result = run_check(repo)
        assert result.returncode == 1
        assert "foo" in result.stderr

    def test_pure_pyproject_sync_still_passes(self, published_repo):
        # The deadlock carve-out survives the rewrite: an unbumped plugin whose
        # only staged path is a pyproject.toml now agreeing with plugin.json.
        repo = published_repo
        write_pyproject(repo, "foo", "0.9.0")
        stage(repo)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "drift",
                        "--no-verify"], check=True)
        write_pyproject(repo, "foo", "1.0.0")
        stage(repo, "plugins/foo/pyproject.toml")
        result = run_check(repo)
        assert result.returncode == 0, result.stderr

    def test_pyproject_sync_alongside_code_still_blocks(self, published_repo):
        repo = published_repo
        write_pyproject(repo, "foo", "0.9.0")
        stage(repo)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "drift",
                        "--no-verify"], check=True)
        write_pyproject(repo, "foo", "1.0.0")
        (repo / "plugins" / "foo" / "code.py").write_text("x = 2\n")
        stage(repo)
        assert run_check(repo).returncode == 1

    def test_new_plugin_passes(self, published_repo):
        # Absent at the publish point, so any version it states differs.
        repo = published_repo
        plugin = repo / "plugins" / "bar" / ".claude-plugin"
        plugin.mkdir(parents=True)
        (plugin / "plugin.json").write_text(
            json.dumps({"name": "bar", "version": "0.1.0"}) + "\n")
        (repo / "plugins" / "bar" / "code.py").write_text("y = 1\n")
        stage(repo)
        assert run_check(repo).returncode == 0

    def test_deleted_plugin_passes(self, published_repo):
        repo = published_repo
        shutil.rmtree(repo / "plugins" / "foo")
        stage(repo)
        assert run_check(repo).returncode == 0

    def test_unparseable_index_version_blocks(self, published_repo):
        # Conservative failure direction: a version that will not parse is not
        # evidence of a bump.
        repo = published_repo
        pj = repo / "plugins" / "foo" / ".claude-plugin" / "plugin.json"
        pj.write_text("{ this is not json and states no version }\n")
        stage(repo)
        assert run_check(repo).returncode == 1


class TestDegraded:
    """No publish point discoverable. Must warn and fall back, never crash."""

    def test_no_origin_master_does_not_crash(self, repo):
        # `repo` has neither origin/master nor scripts/publish.py, which is an
        # unprovisioned or freshly-initialised clone.
        (repo / "plugins" / "foo" / "code.py").write_text("x = 2\n")
        stage(repo)
        result = run_check(repo)
        assert result.returncode == 1
        assert "falling back" in result.stderr
        assert "Traceback" not in result.stderr

    def test_publish_py_present_but_no_origin_master_degrades(self, repo):
        # range_base() falls back to the literal ref name origin/master, which
        # this clone cannot resolve. That is a degrade, not a crash -- and the
        # staged bump still passes through the fallback question.
        (repo / "scripts").mkdir()
        shutil.copy(_PUBLISH_PY, repo / "scripts" / "publish.py")
        (repo / "plugins" / "foo" / "code.py").write_text("x = 2\n")
        pj = repo / "plugins" / "foo" / ".claude-plugin" / "plugin.json"
        pj.write_text(json.dumps({"name": "foo", "version": "1.0.1"}, indent=2) + "\n")
        stage(repo)
        result = run_check(repo)
        assert result.returncode == 0, result.stderr
        assert "does not resolve" in result.stderr

    def test_escape_hatch_still_works_in_end_state_mode(self, published_repo):
        repo = published_repo
        (repo / "plugins" / "foo" / "code.py").write_text("x = 2\n")
        stage(repo)
        result = run_check(repo, env_extra={"PLUGINS_KIT_SKIP_BUMP_CHECK": "1"})
        assert result.returncode == 0
