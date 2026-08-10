"""Tests for scripts/regen_marketplace.py --check --staged (the pre-commit gate).

The gate answers "is the COMMIT I am about to make self-consistent", not "is the
working tree self-consistent". Those differ constantly in this repo: the tree is
shared with concurrent agent sessions, so a worktree-wide check fails on edits
the commit does not contain, and -- the direction that actually matters for
correctness -- it passes on an inconsistent pair that IS staged, because history
is built from the index.

Each test builds a throwaway git repo and repoints the module's path globals at
it. Unlike its sibling tests/repo-scripts/test_check_staged_version_bump.py,
this cannot just run the script with cwd inside a tmp repo: regen_marketplace
resolves REPO_ROOT from its own __file__, so cwd would not redirect it.
"""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "regen_marketplace.py"


def _load_module():
    """Fresh module instance, so per-test global patching cannot leak."""
    spec = importlib.util.spec_from_file_location("regen_marketplace_under_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo)] + list(args), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _plugin_json(repo, name, version):
    d = repo / "plugins" / name / ".claude-plugin"
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.json").write_text(
        json.dumps({"name": name, "version": version,
                    "description": f"{name} plugin",
                    "author": {"name": "t"}}, indent=2) + "\n",
        encoding="utf-8")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A git repo whose marketplace.json agrees with two plugin.json files."""
    _git_init = ["git", "init", "-q", str(tmp_path)]
    subprocess.run(_git_init, check=True)
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")

    _plugin_json(tmp_path, "alpha", "1.0.0")
    _plugin_json(tmp_path, "beta", "2.0.0")
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps({"name": "test-mkt", "plugins": []}, indent=2) + "\n",
        encoding="utf-8")
    (tmp_path / "README.md").write_text("hi\n", encoding="utf-8")

    mod = _load_module()
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(mod, "PLUGINS_DIR", tmp_path / "plugins")
    monkeypatch.setattr(mod, "MARKETPLACE_JSON",
                        tmp_path / ".claude-plugin" / "marketplace.json")

    # Write the consistent marketplace.json through the generator itself, so the
    # committed baseline is exactly what the checker expects to see.
    assert mod.main([]) == 0
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")
    assert mod.main(["--check"]) == 0, "fixture must start consistent"
    return tmp_path, mod


class TestStagedScoping:
    """A commit that cannot change the derivation has nothing to answer for."""

    def test_nothing_staged_passes(self, repo):
        _, mod = repo
        assert mod.main(["--check", "--staged"]) == 0

    def test_unrelated_staged_file_passes(self, repo):
        path, mod = repo
        (path / "README.md").write_text("changed\n", encoding="utf-8")
        _git(path, "add", "README.md")
        assert mod.main(["--check", "--staged"]) == 0

    def test_unstaged_plugin_bump_does_not_block_an_unrelated_commit(self, repo):
        """THE regression this mode exists for.

        Another session bumps a plugin.json in the shared worktree and has not
        regenerated marketplace.json yet. That is their in-flight state; it must
        not block a commit that touches neither file. The old worktree-wide
        check failed here, which is what made the hook fire constantly.
        """
        path, mod = repo
        _plugin_json(path, "alpha", "9.9.9")          # dirty worktree, unstaged
        (path / "README.md").write_text("changed\n", encoding="utf-8")
        _git(path, "add", "README.md")                 # commit touches only this

        assert mod.main(["--check", "--staged"]) == 0
        # The same tree state still fails the worktree-wide mode, which is what
        # publish.py and a bare --check invocation continue to use.
        assert mod.main(["--check"]) == 1


class TestStagedStillCatchesDrift:
    """Relaxing the scope must not relax the invariant."""

    def test_staged_bump_without_regen_blocks(self, repo):
        path, mod = repo
        _plugin_json(path, "alpha", "9.9.9")
        _git(path, "add", "plugins/alpha/.claude-plugin/plugin.json")
        assert mod.main(["--check", "--staged"]) == 1

    def test_staged_bump_with_regen_passes(self, repo):
        path, mod = repo
        _plugin_json(path, "alpha", "9.9.9")
        assert mod.main([]) == 0                       # regenerate
        _git(path, "add", "-A")
        assert mod.main(["--check", "--staged"]) == 0

    def test_judges_the_index_not_the_worktree(self, repo):
        """The staged pair is consistent; the worktree is not. Must pass.

        This is the half a worktree check gets WRONG rather than merely noisy:
        what lands in history is the index, so that is what must be judged.
        """
        path, mod = repo
        _plugin_json(path, "alpha", "9.9.9")
        assert mod.main([]) == 0
        _git(path, "add", "-A")
        # Now dirty the worktree AFTER staging a consistent pair.
        _plugin_json(path, "alpha", "7.7.7")

        assert mod.main(["--check", "--staged"]) == 0
        assert mod.main(["--check"]) == 1              # worktree mode disagrees

    def test_hand_edited_marketplace_json_blocks(self, repo):
        """marketplace.json is derived; staging a hand edit must not slip through."""
        path, mod = repo
        mk = path / ".claude-plugin" / "marketplace.json"
        data = json.loads(mk.read_text(encoding="utf-8"))
        data["plugins"][0]["version"] = "6.6.6"
        mk.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        _git(path, "add", ".claude-plugin/marketplace.json")
        assert mod.main(["--check", "--staged"]) == 1


class TestStagedDeletionIsHonoured:
    """The plugin SET must come from the index too, not just each file's text.

    Enumerating plugins from the worktree while reading their text from the
    index produced a snapshot that was neither: a plugin.json whose deletion is
    staged still exists on disk, so the plugin was judged present in a commit
    that removes it.
    """

    def test_staged_removal_without_regen_blocks(self, repo):
        path, mod = repo
        _git(path, "rm", "-q", "-r", "plugins/beta")
        # marketplace.json still lists beta, so the commit is inconsistent.
        assert mod.main(["--check", "--staged"]) == 1

    def test_staged_removal_with_regen_passes(self, repo):
        path, mod = repo
        _git(path, "rm", "-q", "-r", "plugins/beta")
        assert mod.main([]) == 0                       # regenerate (beta gone)
        _git(path, "add", "-A")
        assert mod.main(["--check", "--staged"]) == 0

    def test_cached_removal_leaving_the_worktree_file_blocks(self, repo):
        """`git rm --cached`: absent from the commit, still present on disk.

        The worktree-enumerating version passed this, because it found beta on
        disk and its index blob was gone only for the deleted path -- exactly
        the mixed snapshot that both false-passes and false-blocks.
        """
        path, mod = repo
        _git(path, "rm", "-q", "--cached",
             "plugins/beta/.claude-plugin/plugin.json")
        assert (path / "plugins" / "beta" / ".claude-plugin"
                / "plugin.json").is_file()
        assert mod.main(["--check", "--staged"]) == 1


class TestFallback:
    def test_falls_back_to_worktree_when_git_cannot_answer(self, repo, monkeypatch, capsys):
        """A check whose input is unavailable must not silently pass."""
        path, mod = repo
        monkeypatch.setattr(mod, "staged_paths", lambda: None)
        _plugin_json(path, "alpha", "9.9.9")           # worktree drift

        assert mod.main(["--check", "--staged"]) == 1
        assert "could not read the index" in capsys.readouterr().err
