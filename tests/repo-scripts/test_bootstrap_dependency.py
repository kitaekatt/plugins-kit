"""Invariant test: EVERY plugin except bootstrap itself must declare the
bootstrap plugin in its plugin.json dependencies (bare string, per CLAUDE.md
"Plugin dependencies on bootstrap"). Shipping a bootstrap.json is irrelevant
to the rule -- the edge is universal, so anything built on "bootstrap is
present wherever a plugin is" holds without a per-plugin check.

The rule itself lives in scripts/check_bootstrap_dependency.py and is loaded
from there rather than reimplemented, because that script is what the
pre-commit hook runs -- a second copy here could pass while the gate that
actually blocks the commit disagreed. This test is the spec; the hook is the
enforcement (see the script's header for why suite-only invariants lose).
"""

import importlib.util
import json
import subprocess
from pathlib import Path

_SCRIPT = (Path(__file__).resolve().parents[2] / "scripts"
           / "check_bootstrap_dependency.py")


def _load_checker():
    spec = importlib.util.spec_from_file_location(
        "check_bootstrap_dependency", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_checker = _load_checker()


def _plugin(root, name, *, bootstrap_json=True, deps=None, plugin_json=True):
    d = root / name
    (d / ".claude-plugin").mkdir(parents=True)
    if bootstrap_json:
        (d / "bootstrap.json").write_text("{}")
    if plugin_json:
        manifest = {"name": name, "version": "0.1.0"}
        if deps is not None:
            manifest["dependencies"] = deps
        (d / ".claude-plugin" / "plugin.json").write_text(json.dumps(manifest))
    return d


def test_real_tree_has_no_outliers():
    assert _checker.find_outliers() == []


def test_real_tree_discovery_is_not_vacuous():
    # The clean result above must come from actually checking plugins.
    plugin_dirs = [
        d for d in _checker.PLUGINS_DIR.iterdir()
        if (d / ".claude-plugin" / "plugin.json").is_file()
    ]
    assert len(plugin_dirs) >= 5


def test_missing_dependency_is_an_outlier(tmp_path):
    _plugin(tmp_path, "some-kit", deps=[])
    (out,) = _checker.find_outliers(tmp_path)
    assert "some-kit" in out and "does not declare" in out


def test_absent_dependencies_field_is_an_outlier(tmp_path):
    _plugin(tmp_path, "some-kit")
    assert len(_checker.find_outliers(tmp_path)) == 1


def test_bare_string_dependency_passes(tmp_path):
    _plugin(tmp_path, "some-kit", deps=["bootstrap"])
    assert _checker.find_outliers(tmp_path) == []


def test_bootstrap_itself_is_exempt(tmp_path):
    _plugin(tmp_path, "bootstrap")
    assert _checker.find_outliers(tmp_path) == []


def test_plugin_without_bootstrap_json_still_must_declare(tmp_path):
    # Retired carve-out: shipping no bootstrap.json used to exempt a plugin.
    # agent-glue was its only occupant and now declares the edge like the rest.
    _plugin(tmp_path, "agent-glue-like", bootstrap_json=False)
    (out,) = _checker.find_outliers(tmp_path)
    assert "does not declare" in out


def test_plugin_without_bootstrap_json_passes_when_it_declares(tmp_path):
    _plugin(tmp_path, "agent-glue-like", bootstrap_json=False,
            deps=["bootstrap"])
    assert _checker.find_outliers(tmp_path) == []


def test_missing_plugin_json_is_an_outlier(tmp_path):
    _plugin(tmp_path, "some-kit", plugin_json=False)
    (out,) = _checker.find_outliers(tmp_path)
    assert "no .claude-plugin/plugin.json" in out


class TestStagedScoping:
    """`--staged` judges the COMMIT: index-aware and scoped to staged plugins.

    Worktree-wide and unscoped, this check cross-contaminated the shared tree
    in the worst way available to it -- one session scaffolding plugins/<new>/
    (no plugin.json yet, or one without the dependencies field) made EVERY
    commit by EVERY session fail, on a plugin none of them were touching.
    """

    def _repo(self, tmp_path):
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        for k, v in (("user.email", "t@example.com"), ("user.name", "t")):
            subprocess.run(
                ["git", "-C", str(tmp_path), "config", k, v], check=True)
        (tmp_path / "plugins").mkdir()
        (tmp_path / "README.md").write_text("hi\n")
        return tmp_path

    def _commit_all(self, root):
        subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(root), "commit", "-qm", "init"], check=True)

    def _add(self, root, *paths):
        subprocess.run(["git", "-C", str(root), "add", *paths], check=True)

    def _main(self, root, monkeypatch, argv):
        monkeypatch.setattr(_checker, "PLUGINS_DIR", root / "plugins")
        return _checker.main(argv)

    def test_unrelated_staged_file_does_not_block(self, tmp_path, monkeypatch):
        repo = self._repo(tmp_path)
        _plugin(repo / "plugins", "good-kit", deps=["bootstrap"])
        self._commit_all(repo)
        # Another session scaffolds a plugin with no dependencies declared;
        # dirty but NOT staged.
        _plugin(repo / "plugins", "wip-kit", deps=[])
        # My commit touches only README.md.
        (repo / "README.md").write_text("changed\n")
        self._add(repo, "README.md")

        assert self._main(repo, monkeypatch, ["--staged"]) == 0
        # The unscoped sweep publish.py runs still sees it.
        assert self._main(repo, monkeypatch, []) == 1

    def test_untracked_scaffold_is_invisible_to_the_commit(
            self, tmp_path, monkeypatch):
        """Not staged at all -> not part of any commit -> judged by none."""
        repo = self._repo(tmp_path)
        _plugin(repo / "plugins", "good-kit", deps=["bootstrap"])
        self._commit_all(repo)
        (repo / "plugins" / "wip-kit" / ".claude-plugin").mkdir(parents=True)
        (repo / "README.md").write_text("changed\n")
        self._add(repo, "README.md")
        assert self._main(repo, monkeypatch, ["--staged"]) == 0

    def test_nothing_staged_does_not_block(self, tmp_path, monkeypatch):
        repo = self._repo(tmp_path)
        _plugin(repo / "plugins", "bad-kit", deps=[])   # pre-existing outlier
        self._commit_all(repo)
        assert self._main(repo, monkeypatch, ["--staged"]) == 0

    def test_staged_plugin_without_the_edge_still_blocks(
            self, tmp_path, monkeypatch):
        repo = self._repo(tmp_path)
        self._commit_all(repo)
        _plugin(repo / "plugins", "new-kit", deps=[])
        self._add(repo, "plugins/new-kit")
        assert self._main(repo, monkeypatch, ["--staged"]) == 1

    def test_staged_plugin_with_the_edge_passes(self, tmp_path, monkeypatch):
        repo = self._repo(tmp_path)
        self._commit_all(repo)
        _plugin(repo / "plugins", "new-kit", deps=["bootstrap"])
        self._add(repo, "plugins/new-kit")
        assert self._main(repo, monkeypatch, ["--staged"]) == 0

    def test_judges_the_index_not_the_worktree(self, tmp_path, monkeypatch):
        """A fix left unstaged does not reach the commit, so it does not count."""
        repo = self._repo(tmp_path)
        self._commit_all(repo)
        _plugin(repo / "plugins", "new-kit", deps=[])
        self._add(repo, "plugins/new-kit")
        # Fix it in the worktree only.
        (repo / "plugins" / "new-kit" / ".claude-plugin"
         / "plugin.json").write_text(json.dumps(
             {"name": "new-kit", "version": "0.1.0",
              "dependencies": ["bootstrap"]}))
        assert self._main(repo, monkeypatch, ["--staged"]) == 1
        self._add(repo, "plugins/new-kit")
        assert self._main(repo, monkeypatch, ["--staged"]) == 0

    def test_failure_message_names_the_inputs_it_judged(
            self, tmp_path, monkeypatch, capsys):
        repo = self._repo(tmp_path)
        self._commit_all(repo)
        _plugin(repo / "plugins", "new-kit", deps=[])
        self._add(repo, "plugins/new-kit")
        assert self._main(repo, monkeypatch, ["--staged"]) == 1
        assert "(staged inputs)" in capsys.readouterr().err

    def test_falls_back_to_worktree_when_git_cannot_answer(
            self, tmp_path, monkeypatch, capsys):
        """A check whose input is unavailable must not silently pass."""
        repo = self._repo(tmp_path)
        self._commit_all(repo)
        _plugin(repo / "plugins", "new-kit", deps=[])  # worktree drift
        monkeypatch.setattr(_checker._gitindex, "staged_paths", lambda root: None)
        assert self._main(repo, monkeypatch, ["--staged"]) == 1
        assert "could not read the index" in capsys.readouterr().err
