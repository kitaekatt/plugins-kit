"""Tests for scripts/_gitindex.py, the one implementation of "read the index"
shared by this repo's pre-commit checks.

It replaced four near-copies that had diverged in ways that were invisible
until they mattered: missing timeouts, a --diff-filter that hid staged
deletions, quoted (mangled) paths for anything non-ASCII, and a non-repo
directory reporting "nothing staged" instead of "cannot answer". Each of those
divergences gets a test here, because the copies proved that a comment is not
enough to keep them from coming back.
"""

import importlib.util
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "_gitindex.py"


def _load():
    spec = importlib.util.spec_from_file_location("_gitindex_under_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gi = _load()


def _git(repo, *args, check=True):
    return subprocess.run(["git", "-C", str(repo)] + list(args), check=check,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "a.txt").write_text("a\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")
    return tmp_path


class TestStagedPaths:
    def test_empty_when_nothing_staged(self, repo):
        assert gi.staged_paths(repo) == []

    def test_none_outside_a_repo(self, tmp_path):
        """"Cannot answer" must not look like "nothing staged".

        One of the replaced copies returned [] here. Under a SCOPED check that
        turns an unanswerable question into a silent pass; None makes the
        caller fall back to the working tree instead.
        """
        assert gi.staged_paths(tmp_path / "not-a-repo") is None

    def test_staged_deletions_are_included(self, repo):
        """The --diff-filter=AM copy dropped these -- the exact state an
        invariant check most needs to see."""
        _git(repo, "rm", "-q", "--cached", "b.txt")
        assert "b.txt" in gi.staged_paths(repo)

    def test_an_explicit_diff_filter_is_honoured(self, repo):
        _git(repo, "rm", "-q", "--cached", "b.txt")
        assert gi.staged_paths(repo, diff_filter="AM") == []

    def test_paths_with_spaces_and_non_ascii_are_not_quoted(self, repo):
        """Plain --name-only quotes and escapes these; -z does not."""
        odd = repo / "a dir" / "éé.txt"
        odd.parent.mkdir()
        odd.write_text("x\n", encoding="utf-8")
        _git(repo, "add", "-A")
        assert "a dir/éé.txt" in gi.staged_paths(repo)


class TestIndexReads:
    def test_index_text_prefers_the_index_over_the_worktree(self, repo):
        (repo / "a.txt").write_text("staged\n", encoding="utf-8")
        _git(repo, "add", "a.txt")
        (repo / "a.txt").write_text("worktree\n", encoding="utf-8")
        assert gi.index_text(repo, "a.txt") == "staged\n"

    def test_index_blob_returns_bytes(self, repo):
        assert gi.index_blob(repo, "a.txt") == b"a\n"

    def test_missing_path_is_none_not_a_worktree_fallback(self, repo):
        (repo / "untracked.txt").write_text("x\n", encoding="utf-8")
        assert gi.index_text(repo, "untracked.txt") is None

    def test_index_files_lists_the_index_not_the_worktree(self, repo):
        _git(repo, "rm", "-q", "--cached", "b.txt")
        listed = gi.index_files(repo, "*.txt")
        assert listed == ["a.txt"]
        assert (repo / "b.txt").is_file()   # still on disk, absent from index

    def test_index_files_is_none_outside_a_repo(self, tmp_path):
        assert gi.index_files(tmp_path / "nope", "*") is None


class TestClassifyScope:
    def _is_input(self, path):
        return path.endswith(".txt")

    def test_skip_when_no_input_is_staged(self, repo):
        (repo / "c.md").write_text("x\n", encoding="utf-8")
        _git(repo, "add", "c.md")
        verdict, _ = gi.classify_scope(repo, self._is_input)
        assert verdict == gi.SCOPE_SKIP

    def test_index_when_an_input_is_staged(self, repo):
        (repo / "a.txt").write_text("x\n", encoding="utf-8")
        _git(repo, "add", "a.txt")
        verdict, staged = gi.classify_scope(repo, self._is_input)
        assert verdict == gi.SCOPE_INDEX
        assert staged == ["a.txt"]

    def test_worktree_when_git_cannot_answer(self, tmp_path):
        verdict, staged = gi.classify_scope(tmp_path / "nope", self._is_input)
        assert verdict == gi.SCOPE_WORKTREE
        assert staged == []

    def test_injected_staged_list_bypasses_git(self, tmp_path):
        verdict, staged = gi.classify_scope(
            tmp_path / "nope", self._is_input, staged=["x.txt"])
        assert (verdict, staged) == (gi.SCOPE_INDEX, ["x.txt"])


def test_every_git_call_is_bounded(repo, monkeypatch):
    """Two of the replaced copies had no timeout, so a wedged git hung the
    commit with no output at all."""
    seen = {}
    real = subprocess.run

    def spy(*args, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        return real(*args, **kwargs)

    monkeypatch.setattr(gi.subprocess, "run", spy)
    gi.staged_paths(repo)
    assert seen["timeout"] == gi.GIT_TIMEOUT
