"""Repo-level safety: what happens when the clone is not level with the remote.

These tests use real git against local bare repos rather than mocks, because
the whole class of bug they cover -- decisions made from a checkout that has
not fetched -- only exists in git's actual ahead/behind semantics. A mock that
returned what we expected would have passed before the fix too.
"""

import contextlib
import os
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


@contextlib.contextmanager
def _polluted_env(**values):
    """Set relocating git vars for the duration of the call under test ONLY.

    They cannot stay set across the assertions, because this module's own
    ``_git`` helper is a plain subprocess with no scrubbing -- it gets diverted
    by exactly the variable being tested. That is not a test artifact to work
    around; it is a live demonstration of what the scrub prevents, on a helper
    written the obvious way.
    """
    saved = {name: os.environ.get(name) for name in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for name, previous in saved.items():
            if previous is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous


class TestInheritedGitEnvironment:
    """A relocating git variable in the environment must not divert a verb.

    git resolves a repository from the environment BEFORE it looks at the cwd,
    so an inherited GIT_DIR outranks the explicit `cwd` every call here passes.
    On the read side that mis-answers; on the write side it can record blobs
    into a repository nobody intended, which is the failure this plugin exists
    to prevent. `_git` therefore strips the whole relocating family for every
    invocation.

    These set the real environment, so they exercise the real inheritance path
    -- `_git` copies os.environ -- rather than asserting on the scrub list.

    Verified by removing the scrub and re-running. SIX of the eight are real
    regression nets that FAIL without it: the two GIT_DIR cases, GIT_WORK_TREE,
    GIT_INDEX_FILE, the GIT_CONFIG_COUNT injection, and the env-level pair
    test -- which fails on its own when only the indexed-pair pattern loop is
    dropped, so that loop is covered specifically rather than incidentally.

    The alternates and ceiling cases pass either way -- git happens to tolerate
    them at these call sites -- so they pin the weaker property that scrubbing
    does not BREAK the verbs, and are documentation of intent rather than a
    trap that will spring. Do not read them as proof those two variables are
    dangerous here; they are scrubbed on family membership, at nil cost.
    """

    def test_commit_and_push_still_reaches_the_secrets_remote(self, fleet_git, tmp_path):
        """The worst case: a write verb recording blobs into another repository."""
        decoy = tmp_path / "decoy"
        _git(tmp_path, "init", "--quiet", str(decoy))

        (fleet_git.author / "manifest.json").write_text("{}", encoding="utf-8")
        with _polluted_env(GIT_DIR=str(decoy / ".git")):
            repo_mod.commit_and_push(fleet_git.author, "seed", ["manifest.json"])

        # It landed on the real remote, and the decoy recorded nothing.
        _git(fleet_git.other, "pull", "--quiet")
        assert (fleet_git.other / "manifest.json").is_file()
        assert _git(decoy, "log", "--oneline", "--all").strip() == ""

    def test_a_stale_work_tree_does_not_redirect_the_commit(self, fleet_git, tmp_path):
        decoy = tmp_path / "decoy"
        decoy.mkdir()

        (fleet_git.author / "manifest.json").write_text("{}", encoding="utf-8")
        with _polluted_env(GIT_WORK_TREE=str(decoy)):
            repo_mod.commit_and_push(fleet_git.author, "seed", ["manifest.json"])

        _git(fleet_git.other, "pull", "--quiet")
        assert (fleet_git.other / "manifest.json").is_file()

    def test_a_stale_index_file_does_not_delete_every_other_tracked_file(
        self, fleet_git, tmp_path
    ):
        """GIT_INDEX_FILE is DESTRUCTIVE, not merely misdirecting.

        git sets it itself for hooks and rebases, so a nested run really can
        see one. Pointed at a path that does not exist, `git add -- <path>`
        succeeds into a fresh EMPTY index and the commit records a tree holding
        only that path -- identity.age and every other blob committed as
        deleted, and pushed that way.

        So this asserts against the COMMIT's tree, not the working tree: the
        working copy still has the files, which is exactly why the damage is
        easy to miss until another machine pulls it.
        """
        (fleet_git.author / "manifest.json").write_text("{}", encoding="utf-8")
        with _polluted_env(GIT_INDEX_FILE=str(tmp_path / "foreign.index")):
            repo_mod.commit_and_push(fleet_git.author, "seed", ["manifest.json"])

        _git(fleet_git.other, "pull", "--quiet")
        tree = _git(fleet_git.other, "ls-tree", "-r", "--name-only", "HEAD").split()
        assert "manifest.json" in tree
        # The pre-existing file must survive the commit, not just the checkout.
        assert "README.md" in tree

    def test_injected_config_does_not_redirect_the_push(self, fleet_git, tmp_path):
        """The other door to the same outcome, with the repo location intact.

        Config injection does not move the repository; it rewrites what the
        repository is configured to do. An inherited GIT_CONFIG_COUNT triple
        overriding remote.origin.url publishes the encrypted blobs to whatever
        remote it names, and exits 0.
        """
        decoy = tmp_path / "decoy.git"
        _git(tmp_path, "init", "--quiet", "--bare", str(decoy))

        (fleet_git.author / "manifest.json").write_text("{}", encoding="utf-8")
        with _polluted_env(
            GIT_CONFIG_COUNT="1",
            GIT_CONFIG_KEY_0="remote.origin.url",
            GIT_CONFIG_VALUE_0=str(decoy),
        ):
            repo_mod.commit_and_push(fleet_git.author, "seed", ["manifest.json"])

        _git(fleet_git.other, "pull", "--quiet")
        assert (fleet_git.other / "manifest.json").is_file()
        assert _git(decoy, "log", "--oneline", "--all").strip() == ""

    def test_every_indexed_config_pair_is_removed_and_the_global_kept(
        self, monkeypatch
    ):
        """Asserted on the environment actually handed to git, not on an outcome.

        Deliberate, because the outcome cannot distinguish it: scrubbing
        GIT_CONFIG_COUNT alone already defeats the attack, since git ignores
        indexed pairs with no count. Removing the pairs too is defence against
        a count arriving by another route, and an outcome-level test of it
        would pass either way -- so this checks the env directly, where the
        behaviour actually lives. It fails if the pattern loop is dropped.

        It pins the retained exclusion in the same breath, so the decision to
        keep GIT_CONFIG_GLOBAL cannot be quietly reversed in either direction.
        """
        captured = {}

        def fake_run(argv, **kwargs):
            captured.update(kwargs["env"])
            raise OSError("not actually running git")

        monkeypatch.setattr(repo_mod.subprocess, "run", fake_run)

        with _polluted_env(
            GIT_CONFIG_COUNT="0",
            GIT_CONFIG_KEY_0="remote.origin.url",
            GIT_CONFIG_VALUE_0="https://example.invalid/evil.git",
            # A pair well above any plausible count, and a two-digit index.
            GIT_CONFIG_KEY_12="core.hooksPath",
            GIT_CONFIG_VALUE_12="/tmp/evil-hooks",
            GIT_CONFIG_PARAMETERS="'core.pager=cat'",
            GIT_CONFIG_GLOBAL="/tmp/isolated.gitconfig",
        ):
            # The OSError path is how _git reports "could not run git".
            assert repo_mod._git(["status"], cwd=None, timeout=5)[0] == 127

        assert "GIT_CONFIG_COUNT" not in captured
        assert "GIT_CONFIG_PARAMETERS" not in captured
        assert [n for n in captured if n.startswith("GIT_CONFIG_KEY_")] == []
        assert [n for n in captured if n.startswith("GIT_CONFIG_VALUE_")] == []
        # Deliberately retained: harnesses and CI use it to isolate config.
        assert captured["GIT_CONFIG_GLOBAL"] == "/tmp/isolated.gitconfig"
        # And the two we set survive the scrub that runs before them.
        assert captured["GIT_TERMINAL_PROMPT"] == "0"
        assert "GIT_SSH_COMMAND" in captured

    def test_sync_still_fast_forwards_the_secrets_clone(self, fleet_git, tmp_path):
        _commit(fleet_git.other, "identity.age")
        _git(fleet_git.other, "push", "--quiet")

        decoy = tmp_path / "decoy"
        _git(tmp_path, "init", "--quiet", str(decoy))

        with _polluted_env(GIT_DIR=str(decoy / ".git")):
            repo_mod.sync(fleet_git.author)

        assert (fleet_git.author / "identity.age").exists()

    def test_remote_has_is_not_answered_by_a_foreign_object_store(self, fleet_git):
        """The call that decides "is this repo already seeded".

        A false positive there is the incident this module's `sync` exists to
        prevent, so it must not be answerable from borrowed objects.
        """
        alternates = str(fleet_git.other / ".git" / "objects")
        with _polluted_env(GIT_ALTERNATE_OBJECT_DIRECTORIES=alternates):
            assert repo_mod.remote_has(fleet_git.author, "identity.age") is False

    def test_a_ceiling_directory_does_not_hide_the_clone(self, fleet_git, tmp_path):
        """A ceiling stops discovery, which would read as "no repo" -- permissive."""
        with _polluted_env(GIT_CEILING_DIRECTORIES=str(tmp_path)):
            assert repo_mod.head_sha(fleet_git.author) is not None


class TestRollback:
    def test_discards_the_commit_and_its_files(self, fleet_git):
        before = repo_mod.head_sha(fleet_git.author)
        _commit(fleet_git.author, "identity.age")
        assert (fleet_git.author / "identity.age").exists()

        repo_mod.rollback_to(fleet_git.author, before)

        assert not (fleet_git.author / "identity.age").exists()
        assert repo_mod.head_sha(fleet_git.author) == before
