"""Drift test: plugins/<name>/pyproject.toml version must equal the
authoritative plugins/<name>/.claude-plugin/plugin.json version (X17).

The rule itself lives in scripts/check_pyproject_sync.py and is loaded from
there rather than reimplemented, because that script is what the pre-commit
hook runs -- a second copy here could pass while the gate that actually blocks
the commit disagreed. Auto-discovers plugins and compares the two files rather
than pinning numbers, so a normal publish bump (edit both files) never touches
this test. Plugins without a pyproject.toml, or whose pyproject declares no
version, are out of scope -- pyproject versions are non-authoritative, the rule
is just "if you state one, it must not lie".

This test alone was never enough: it only fails a FULL suite run, which
CLAUDE.md tells you not to do routinely and publish.py does not do at all, so
bootstrap drifted across five releases before anyone noticed. It is the spec;
the hook is the enforcement. See the script's header.
"""

import importlib.util
import json
import subprocess
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_pyproject_sync.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_pyproject_sync", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_checker = _load_checker()


def test_discovery_finds_plugins():
    # Vacuity guard for the drift assertion below.
    assert _checker.plugins_with_both_files(), (
        "no plugins with pyproject + plugin.json found")


def test_pyproject_versions_match_plugin_json():
    drift = _checker.find_drift()
    assert not drift, (
        "pyproject.toml versions drifted from the authoritative plugin.json "
        "(set them equal; plugin.json is the source of truth):\n  "
        + "\n  ".join(drift)
    )


class TestTheRuleItself:
    """The checker is now a commit gate, so its verdict has to be right: a
    false positive blocks every commit in the repo, a false negative is the
    hole that let bootstrap drift across five releases.
    """

    def _plugin(self, root, name, py_version, pj_version):
        d = root / name
        (d / ".claude-plugin").mkdir(parents=True)
        body = '[project]\nname = "x"\n'
        if py_version is not None:
            body += f'version = "{py_version}"\n'
        (d / "pyproject.toml").write_text(body)
        (d / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": name, "version": pj_version}))
        return d

    def test_matching_versions_are_not_drift(self, tmp_path):
        self._plugin(tmp_path, "ok", "1.2.3", "1.2.3")
        assert _checker.find_drift(tmp_path) == []

    def test_a_stated_version_that_lies_is_drift(self, tmp_path):
        self._plugin(tmp_path, "bad", "0.43.0", "0.44.0")
        assert _checker.find_drift(tmp_path) == [
            "bad: pyproject.toml=0.43.0 plugin.json=0.44.0"]

    def test_a_versionless_pyproject_is_out_of_scope(self, tmp_path):
        """Non-authoritative by design: state nothing, lie about nothing."""
        self._plugin(tmp_path, "quiet", None, "0.44.0")
        assert _checker.find_drift(tmp_path) == []

    def test_a_plugin_without_pyproject_is_out_of_scope(self, tmp_path):
        d = tmp_path / "nopy" / ".claude-plugin"
        d.mkdir(parents=True)
        (d / "plugin.json").write_text(json.dumps({"name": "nopy", "version": "1"}))
        assert _checker.find_drift(tmp_path) == []

    def test_nothing_staged_reads_the_working_tree(self, tmp_path):
        """No staged paths -> no index snapshot to prefer; judge the worktree."""
        self._plugin(tmp_path, "ok", "1.2.3", "1.2.3")
        assert _checker.find_drift(tmp_path, staged=[]) == []

    def test_every_drifting_plugin_is_reported_not_just_the_first(self, tmp_path):
        """A publish can bump several plugins; naming one and stopping would
        turn one fix-and-retry cycle into several."""
        self._plugin(tmp_path, "a", "1.0.0", "2.0.0")
        self._plugin(tmp_path, "b", "3.0.0", "4.0.0")
        assert len(_checker.find_drift(tmp_path)) == 2


class TestJudgesTheIndex:
    """The commit gate must judge what the COMMIT will contain, not what the
    working tree happens to look like while the hook runs.

    Reading the working tree left the sanctioned fix unenforced: edit
    pyproject, never `git add` it, and the gate passed while the commit still
    carried the old version into HEAD. Each of bootstrap's five "fixed"
    releases could pass this check and ship the drift anyway.
    """

    def _repo(self, tmp_path, py_version, pj_version):
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        for k, v in (("user.email", "t@example.com"), ("user.name", "t")):
            subprocess.run(
                ["git", "-C", str(tmp_path), "config", k, v], check=True)
        d = tmp_path / "plugins" / "foo"
        (d / ".claude-plugin").mkdir(parents=True)
        self._write(tmp_path, py_version, pj_version)
        subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-qm", "init"], check=True)
        return tmp_path

    def _write(self, root, py_version, pj_version):
        d = root / "plugins" / "foo"
        (d / "pyproject.toml").write_text(
            f'[project]\nname = "foo"\nversion = "{py_version}"\n')
        (d / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "foo", "version": pj_version}))

    def _drift(self, root):
        return _checker.find_drift(root / "plugins", repo_root=root)

    def test_fix_left_unstaged_is_still_drift(self, tmp_path):
        # THE regression: stage only the plugin.json bump, then fix pyproject
        # in the working tree without staging it. The index -- and so the
        # commit -- still holds the old pyproject version.
        repo = self._repo(tmp_path, "1.0.0", "1.0.0")
        self._write(repo, "1.0.0", "1.1.0")
        subprocess.run(
            ["git", "-C", str(repo), "add",
             "plugins/foo/.claude-plugin/plugin.json"], check=True)
        (repo / "plugins" / "foo" / "pyproject.toml").write_text(
            '[project]\nname = "foo"\nversion = "1.1.0"\n')  # fixed, NOT staged
        assert self._drift(repo) == [
            "foo: pyproject.toml=1.0.0 plugin.json=1.1.0"]

    def test_fix_that_is_staged_is_clean(self, tmp_path):
        # The healthy everyday path: bump both, stage both.
        repo = self._repo(tmp_path, "1.0.0", "1.0.0")
        self._write(repo, "1.1.0", "1.1.0")
        subprocess.run(
            ["git", "-C", str(repo), "add",
             "plugins/foo/pyproject.toml",
             "plugins/foo/.claude-plugin/plugin.json"], check=True)
        assert self._drift(repo) == []

    def test_clean_repo_with_nothing_staged_passes(self, tmp_path):
        repo = self._repo(tmp_path, "1.0.0", "1.0.0")
        assert self._drift(repo) == []

    def test_untracked_plugin_falls_back_to_the_working_tree(self, tmp_path):
        # Git cannot supply an index blob for a file it does not track, so the
        # per-file fallback reads the working tree rather than crashing or
        # silently exempting the plugin.
        repo = self._repo(tmp_path, "1.0.0", "1.0.0")
        (repo / "plugins" / "foo" / "code.py").write_text("x = 1\n")
        subprocess.run(
            ["git", "-C", str(repo), "add", "plugins/foo/code.py"], check=True)
        new = repo / "plugins" / "bar"
        (new / ".claude-plugin").mkdir(parents=True)
        (new / "pyproject.toml").write_text(
            '[project]\nname = "bar"\nversion = "0.1.0"\n')
        (new / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "bar", "version": "9.9.9"}))
        assert self._drift(repo) == [
            "bar: pyproject.toml=0.1.0 plugin.json=9.9.9"]


class TestStagedScoping:
    """`--staged` must judge only the plugins the COMMIT touches.

    Index-awareness alone was not enough: this check judged EVERY plugin
    whenever ANYTHING was staged, so a concurrent session's in-flight drift in
    an unrelated plugin blocked every other session's commit. That is a false
    positive on a commit that cannot possibly have caused it.
    """

    def _repo(self, tmp_path):
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        for k, v in (("user.email", "t@example.com"), ("user.name", "t")):
            subprocess.run(
                ["git", "-C", str(tmp_path), "config", k, v], check=True)
        (tmp_path / "plugins").mkdir()
        (tmp_path / "README.md").write_text("hi\n")
        return tmp_path

    def _plugin(self, root, name, py_version, pj_version):
        d = root / "plugins" / name
        (d / ".claude-plugin").mkdir(parents=True, exist_ok=True)
        (d / "pyproject.toml").write_text(
            f'[project]\nname = "{name}"\nversion = "{py_version}"\n')
        (d / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": name, "version": pj_version}))

    def _commit_all(self, root):
        subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(root), "commit", "-qm", "init"], check=True)

    def _add(self, root, *paths):
        subprocess.run(
            ["git", "-C", str(root), "add", *paths], check=True)

    def _main(self, root, monkeypatch, argv):
        monkeypatch.setattr(_checker, "PLUGINS_DIR", root / "plugins")
        return _checker.main(argv)

    def test_unrelated_staged_file_does_not_block(self, tmp_path, monkeypatch):
        repo = self._repo(tmp_path)
        self._plugin(repo, "alpha", "1.0.0", "1.0.0")
        self._commit_all(repo)
        # Another session drifts beta in the shared tree; dirty but NOT staged.
        self._plugin(repo, "beta", "1.0.0", "2.0.0")
        # My commit touches only README.md.
        (repo / "README.md").write_text("changed\n")
        self._add(repo, "README.md")

        assert self._main(repo, monkeypatch, ["--staged"]) == 0
        # The unscoped sweep still sees it -- that is publish.py's job.
        assert self._main(repo, monkeypatch, []) == 1

    def test_nothing_staged_does_not_block(self, tmp_path, monkeypatch):
        repo = self._repo(tmp_path)
        self._plugin(repo, "alpha", "1.0.0", "2.0.0")   # pre-existing drift
        self._commit_all(repo)
        assert self._main(repo, monkeypatch, ["--staged"]) == 0

    def test_real_drift_in_a_staged_plugin_still_blocks(
            self, tmp_path, monkeypatch):
        repo = self._repo(tmp_path)
        self._plugin(repo, "alpha", "1.0.0", "1.0.0")
        self._commit_all(repo)
        self._plugin(repo, "alpha", "1.0.0", "1.1.0")   # bumped only plugin.json
        self._add(repo, "plugins/alpha/.claude-plugin/plugin.json")
        assert self._main(repo, monkeypatch, ["--staged"]) == 1

    def test_staged_fix_passes(self, tmp_path, monkeypatch):
        repo = self._repo(tmp_path)
        self._plugin(repo, "alpha", "1.0.0", "1.0.0")
        self._commit_all(repo)
        self._plugin(repo, "alpha", "1.1.0", "1.1.0")
        self._add(repo, "plugins/alpha")
        assert self._main(repo, monkeypatch, ["--staged"]) == 0

    def test_failure_message_names_the_inputs_it_judged(
            self, tmp_path, monkeypatch, capsys):
        repo = self._repo(tmp_path)
        self._plugin(repo, "alpha", "1.0.0", "1.0.0")
        self._commit_all(repo)
        self._plugin(repo, "alpha", "1.0.0", "1.1.0")
        self._add(repo, "plugins/alpha/.claude-plugin/plugin.json")
        assert self._main(repo, monkeypatch, ["--staged"]) == 1
        assert "(staged inputs)" in capsys.readouterr().err

    def test_falls_back_to_worktree_when_git_cannot_answer(
            self, tmp_path, monkeypatch, capsys):
        """A check whose input is unavailable must not silently pass."""
        repo = self._repo(tmp_path)
        self._plugin(repo, "alpha", "1.0.0", "1.0.0")
        self._commit_all(repo)
        self._plugin(repo, "alpha", "1.0.0", "1.1.0")   # worktree drift
        monkeypatch.setattr(_checker._gitindex, "staged_paths", lambda root: None)
        assert self._main(repo, monkeypatch, ["--staged"]) == 1
        assert "could not read the index" in capsys.readouterr().err
