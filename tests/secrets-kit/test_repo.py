"""Repo-level safety: what happens when the clone is not level with the remote.

These tests use real git against local bare repos rather than mocks, because
the whole class of bug they cover -- decisions made from a checkout that has
not fetched -- only exists in git's actual ahead/behind semantics. A mock that
returned what we expected would have passed before the fix too.
"""

import subprocess
from pathlib import Path

import pytest

from secrets_kit import SecretsError
from secrets_kit import repo as repo_mod


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git"] + list(args),
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=True,
    )
    return proc.stdout.decode()


def _commit(clone: Path, name: str, text: str = "x") -> None:
    (clone / name).write_text(text, encoding="utf-8")
    _git(clone, "add", "--", name)
    _git(clone, "commit", "--quiet", "-m", f"add {name}")


@pytest.fixture
def fleet_git(tmp_path):
    """A bare 'remote' with one commit, plus two clones of it.

    Two clones is the point: `author` stands in for the machine doing the
    writing, `other` for a machine that seeded the repo earlier and whose work
    `author` has not fetched.
    """
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--quiet", "--bare", "--initial-branch=main", str(remote))

    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "--quiet", str(remote), str(seed))
    _git(seed, "config", "user.email", "t@example.com")
    _git(seed, "config", "user.name", "t")
    _commit(seed, "README.md")
    _git(seed, "push", "--quiet", "origin", "main")

    clones = {}
    for name in ("author", "other"):
        path = tmp_path / name
        _git(tmp_path, "clone", "--quiet", str(remote), str(path))
        _git(path, "config", "user.email", "t@example.com")
        _git(path, "config", "user.name", "t")
        clones[name] = path

    class Fleet:
        pass

    f = Fleet()
    f.remote = remote
    f.author = clones["author"]
    f.other = clones["other"]
    return f


class TestSync:
    def test_fast_forwards_a_behind_clone(self, fleet_git):
        _commit(fleet_git.other, "identity.age")
        _git(fleet_git.other, "push", "--quiet")

        assert not (fleet_git.author / "identity.age").exists()
        repo_mod.sync(fleet_git.author)
        assert (fleet_git.author / "identity.age").exists()

    def test_is_a_noop_when_level(self, fleet_git):
        repo_mod.sync(fleet_git.author)
        repo_mod.sync(fleet_git.author)

    def test_raises_on_divergence_rather_than_merging(self, fleet_git):
        _commit(fleet_git.other, "identity.age", "theirs")
        _git(fleet_git.other, "push", "--quiet")
        _commit(fleet_git.author, "identity.age", "ours")

        with pytest.raises(SecretsError) as excinfo:
            repo_mod.sync(fleet_git.author)
        assert "diverged" in str(excinfo.value)
        assert "reset --hard" in str(excinfo.value)

    def test_raises_when_the_remote_is_unreachable(self, fleet_git, tmp_path):
        _git(fleet_git.author, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
        with pytest.raises(SecretsError):
            repo_mod.sync(fleet_git.author)


class TestRemoteHas:
    def test_sees_a_file_the_checkout_does_not_have_yet(self, fleet_git):
        _commit(fleet_git.other, "identity.age")
        _git(fleet_git.other, "push", "--quiet")

        # The stale checkout is the whole point: nothing local says "seeded".
        assert not (fleet_git.author / "identity.age").exists()
        assert repo_mod.remote_has(fleet_git.author, "identity.age") is False

        _git(fleet_git.author, "fetch", "--quiet")
        assert repo_mod.remote_has(fleet_git.author, "identity.age") is True

    def test_false_for_an_absent_path(self, fleet_git):
        assert repo_mod.remote_has(fleet_git.author, "identity.age") is False


class TestCommitAndPush:
    def test_publishes_a_change(self, fleet_git):
        (fleet_git.author / "manifest.json").write_text("{}", encoding="utf-8")
        repo_mod.commit_and_push(fleet_git.author, "seed", ["manifest.json"])

        _git(fleet_git.other, "pull", "--quiet")
        assert (fleet_git.other / "manifest.json").is_file()

    def test_rebases_over_an_unrelated_remote_commit(self, fleet_git):
        """Two machines adding different secrets must not need manual repair."""
        _commit(fleet_git.other, "blobs-theirs.age")
        _git(fleet_git.other, "push", "--quiet")

        (fleet_git.author / "blobs-ours.age").write_text("ours", encoding="utf-8")
        repo_mod.commit_and_push(fleet_git.author, "add: ours", ["blobs-ours.age"])

        _git(fleet_git.other, "pull", "--quiet")
        assert (fleet_git.other / "blobs-ours.age").is_file()
        assert (fleet_git.other / "blobs-theirs.age").is_file()

    def test_raises_and_leaves_no_rebase_in_progress_on_conflict(self, fleet_git):
        _commit(fleet_git.other, "manifest.json", "theirs")
        _git(fleet_git.other, "push", "--quiet")

        (fleet_git.author / "manifest.json").write_text("ours", encoding="utf-8")
        with pytest.raises(SecretsError):
            repo_mod.commit_and_push(fleet_git.author, "seed", ["manifest.json"])

        assert not (fleet_git.author / ".git" / "rebase-merge").exists()
        assert not (fleet_git.author / ".git" / "rebase-apply").exists()


class TestRollback:
    def test_discards_the_commit_and_its_files(self, fleet_git):
        before = repo_mod.head_sha(fleet_git.author)
        _commit(fleet_git.author, "identity.age")
        assert (fleet_git.author / "identity.age").exists()

        repo_mod.rollback_to(fleet_git.author, before)

        assert not (fleet_git.author / "identity.age").exists()
        assert repo_mod.head_sha(fleet_git.author) == before
