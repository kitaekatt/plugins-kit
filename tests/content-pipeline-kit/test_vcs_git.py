"""Tests for content_pipeline.vcs (git_vcs + null_vcs equivalents).

Translates the first-pass cl_creation behaviors into git terms (the changeset
== staged-set-finalized-as-a-commit mapping): make_changeset creates no git
object, move_into stages exactly the given paths, finalize_description commits
only the moved subset with the rebuilt message, revert restores exactly one
path, delete_if_empty is a no-op. Uses a REAL git repo in tmp_path (git is
available). The NullVcs equivalents assert the no-op backend satisfies the same
protocol shape so deliver is exercisable without a repo.
"""

import shutil
import subprocess

import pytest

from content_pipeline.deliver.inplace import deliver_changeset
from content_pipeline.vcs.git_vcs import GitChangeset, GitVcs
from content_pipeline.vcs.null_vcs import NullVcs

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not on PATH"
)


def _run(args, cwd):
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr


@pytest.fixture
def repo(tmp_path):
    _run(["init", "-q"], tmp_path)
    _run(["config", "user.email", "t@example.com"], tmp_path)
    _run(["config", "user.name", "Test"], tmp_path)
    _run(["config", "commit.gpgsign", "false"], tmp_path)
    # An initial commit so HEAD exists for revert tests.
    (tmp_path / "seed.txt").write_text("seed\n", encoding="utf-8")
    _run(["add", "seed.txt"], tmp_path)
    _run(["commit", "-q", "-m", "seed"], tmp_path)
    return tmp_path


def _head_subject(repo):
    proc = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _committed_files(repo, sha="HEAD"):
    proc = subprocess.run(
        ["git", "show", "--name-only", "--pretty=format:", sha],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    return sorted(f for f in proc.stdout.strip().splitlines() if f)


# -- git changeset mapping ----------------------------------------------------

def test_make_changeset_creates_no_git_object(repo):
    vcs = GitVcs(repo_root=repo)
    cs = vcs.make_changeset("placeholder")
    assert isinstance(cs, GitChangeset)
    assert cs.paths == []
    assert cs.committed is None
    # Nothing committed yet -- HEAD is still the seed commit.
    assert _head_subject(repo) == "seed"


def test_finalize_commits_only_moved_subset(repo):
    vcs = GitVcs(repo_root=repo)
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    # Also a stray change NOT moved into the changeset -- must not be committed.
    (repo / "stray.txt").write_text("stray\n", encoding="utf-8")

    cs = vcs.make_changeset("pending")
    vcs.move_into(cs, [repo / "a.txt", repo / "b.txt"])
    sha = vcs.finalize_description(cs, "deliver a and b")

    assert sha is not None
    assert _head_subject(repo) == "deliver a and b"
    assert _committed_files(repo, sha) == ["a.txt", "b.txt"]  # stray excluded


def test_move_into_records_exact_paths_no_wildcard(repo):
    vcs = GitVcs(repo_root=repo)
    (repo / "x.txt").write_text("x\n", encoding="utf-8")
    cs = vcs.make_changeset("p")
    vcs.move_into(cs, [repo / "x.txt"])
    vcs.move_into(cs, [repo / "x.txt"])  # duplicate move is deduped
    assert cs.paths == ["x.txt"]


def test_finalize_empty_changeset_commits_nothing(repo):
    vcs = GitVcs(repo_root=repo)
    cs = vcs.make_changeset("empty")
    sha = vcs.finalize_description(cs, "would be empty")
    assert sha is None
    assert _head_subject(repo) == "seed"  # no empty commit created


def test_revert_restores_exactly_one_path(repo):
    vcs = GitVcs(repo_root=repo)
    (repo / "seed.txt").write_text("MODIFIED\n", encoding="utf-8")
    vcs.revert(repo / "seed.txt")
    assert (repo / "seed.txt").read_text(encoding="utf-8") == "seed\n"


def test_delete_if_empty_is_noop(repo):
    vcs = GitVcs(repo_root=repo)
    cs = vcs.make_changeset("p")
    vcs.delete_if_empty(cs)  # no exception, no side effect


# -- end-to-end choreography over the git backend -----------------------------

def test_deliver_changeset_over_git_backend(repo):
    vcs = GitVcs(repo_root=repo)
    (repo / "one.txt").write_text("", encoding="utf-8")
    (repo / "two.txt").write_text("", encoding="utf-8")

    items = [
        {"id": "one", "path": str(repo / "one.txt")},
        {"id": "two", "path": str(repo / "two.txt")},
    ]

    def apply_item(it):
        # The delivery write itself: mutate the file the item names.
        with open(it["path"], "w", encoding="utf-8") as fh:
            fh.write(f"content for {it['id']}\n")

    result = deliver_changeset(
        items,
        vcs=vcs,
        item_id=lambda it: it["id"],
        path_of=lambda it: it["path"],
        apply_item=apply_item,
        describe=lambda moved: "deliver: " + ", ".join(i for i, _p in moved),
    )
    assert [i for i, _p in result.moved] == ["one", "two"]
    assert _head_subject(repo) == "deliver: one, two"
    assert _committed_files(repo) == ["one.txt", "two.txt"]


# -- null backend equivalents -------------------------------------------------

def test_null_backend_satisfies_choreography():
    vcs = NullVcs()
    cs = vcs.make_changeset("x")
    assert cs is None  # no-op backend returns no handle
    # Every method is a safe no-op.
    vcs.open_for_edit("p")
    vcs.add("p")
    vcs.move_into(cs, ["p"])
    vcs.finalize_description(cs, "d")
    vcs.revert("p")
    vcs.delete_if_empty(cs)


def test_git_runner_is_injectable_without_subprocess():
    # The runner seam lets a test script git without spawning.
    calls = []

    def fake_runner(args, cwd):
        calls.append(args)
        if args[:1] == ["rev-parse"]:
            return 0, "deadbeef\n", ""
        return 0, "", ""

    vcs = GitVcs(repo_root=".", runner=fake_runner)
    cs = vcs.make_changeset("p")
    vcs.move_into(cs, ["a.txt"])
    sha = vcs.finalize_description(cs, "msg")
    assert sha == "deadbeef"
    assert ["add", "--", "a.txt"] in calls
    assert ["commit", "-m", "msg", "--", "a.txt"] in calls
