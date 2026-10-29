"""Tests for git-kit scripts/snapshot_review.py (freeze / land / drop / prune).

Every test runs against a real throwaway repository (the `git_repo` fixture from
conftest.py) with CLAUDE_BOOTSTRAP_DATA_ROOT and HOME redirected under tmp_path,
so nothing here touches the real ~/.claude or the real data directory.
bootstrap_guard.data_dir honours CLAUDE_BOOTSTRAP_DATA_ROOT at call time.
"""

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "plugins", "git-kit", "scripts")
)

# snapshot_review.py imports the vendored bootstrap_guard that sits next to it.
if SCRIPTS_PATH not in sys.path:
    sys.path.append(SCRIPTS_PATH)

_SPEC = importlib.util.spec_from_file_location(
    "git_kit_snapshot_review", os.path.join(SCRIPTS_PATH, "snapshot_review.py")
)
snap = importlib.util.module_from_spec(_SPEC)
sys.modules["git_kit_snapshot_review"] = snap
_SPEC.loader.exec_module(snap)

SCRIPT = os.path.join(SCRIPTS_PATH, "snapshot_review.py")


# ------------------------------------------------------------------- helpers


def lines(overrides=None, n=30, tag="line"):
    """A text of n lines; overrides maps a 1-based line number to replacement text."""
    return "".join(f"{(overrides or {}).get(i, f'{tag}{i}')}\n" for i in range(1, n + 1))


def write(repo, name, text):
    (repo.path / name).write_bytes(text.encode("utf-8"))


def commit(repo, name, text, msg=None):
    write(repo, name, text)
    repo.git("add", "--", name)
    repo.git("commit", "-qm", msg or f"edit {name}")


def git_in(repo, *args, input=None):
    proc = subprocess.run(
        ["git", *args], cwd=repo.path, input=input, capture_output=True, check=True
    )
    return proc.stdout.decode("utf-8").strip()


def commit_blob_only(repo, name, text, msg):
    """Commit `text` at `name` into HEAD via the shared index, leaving the worktree alone.

    Models a foreign commit that reached HEAD (another session, or a pull) while this
    session's edit of the same file lives only in the worktree.
    """
    sha = git_in(repo, "hash-object", "-w", "--stdin", input=text.encode("utf-8"))
    git_in(repo, "update-index", "--add", "--cacheinfo", f"100644,{sha},{name}")
    git_in(repo, "commit", "-qm", msg)


def run(capsys, *argv):
    capsys.readouterr()
    rc = snap.main(list(argv))
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def frozen(capsys, *argv):
    rc, out, err = run(capsys, "freeze", *argv)
    assert rc == 0, err
    return json.loads(out)


def msgfile(tmp_path, text="landed change\n"):
    p = tmp_path / "msg.txt"
    p.write_bytes(text.encode("utf-8"))
    return str(p)


def names(repo, *args):
    out = repo.git(*args).stdout
    return sorted(line for line in out.splitlines() if line)


def bundle_dir(sha12, repo_dir="repo-12345678"):
    d = snap.data_dir("git-kit") / "reviews" / repo_dir / f"snap-{sha12}"
    (d / "tree").mkdir(parents=True)
    (d / "tree" / "secret.env").write_text("KEY=1\n")
    (d / "bundle.json").write_text("{}\n")
    return d


def has_ref(repo, sha12):
    return repo.git("rev-parse", "--verify", "--quiet", snap.REF_PREFIX + sha12, check=False).returncode == 0


@pytest.fixture
def repo(git_repo, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_BOOTSTRAP_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    commit(git_repo, "f.txt", lines())
    commit(git_repo, "other.txt", "o\n")
    return git_repo


# ----------------------------------------------------------- freeze (cases 1-5)


def test_post_freeze_edit_leaves_snapshot_tree_unchanged(repo, capsys):
    """Case 1."""
    edited = lines({2: "EDITED"})
    write(repo, "f.txt", edited)
    result = frozen(capsys, "--paths", "f.txt")
    tree_before = repo.git("rev-parse", f"{result['snapshot']}^{{tree}}").stdout.strip()
    assert tree_before == result["tree"]

    write(repo, "f.txt", lines({2: "EDITED", 3: "LATER"}))

    assert repo.git("rev-parse", f"{result['snapshot']}^{{tree}}").stdout.strip() == tree_before
    assert repo.git("show", f"{result['snapshot']}:f.txt").stdout == edited
    assert result["ref"] == snap.REF_PREFIX + result["snapshot"][:12]
    assert result["range"] == f"{result['base']}..{result['snapshot']}"
    assert result["base"] == repo.git("rev-parse", "HEAD").stdout.strip()
    assert result["paths"] == ["f.txt"]
    assert has_ref(repo, result["snapshot"][:12])


def test_untracked_file_is_included(repo, capsys):
    """Case 2."""
    write(repo, "brand_new.txt", "new\n")
    result = frozen(capsys, "--paths", "brand_new.txt")
    assert repo.git("show", f"{result['snapshot']}:brand_new.txt").stdout == "new\n"
    assert result["paths"] == ["brand_new.txt"]


def test_deletion_is_included(repo, capsys):
    (repo.path / "other.txt").unlink()
    result = frozen(capsys, "--paths", "other.txt")
    assert result["paths"] == ["other.txt"]
    assert repo.git("cat-file", "-e", f"{result['snapshot']}:other.txt", check=False).returncode != 0


def test_another_sessions_change_in_another_file_is_excluded(repo, capsys):
    """Case 3."""
    write(repo, "f.txt", lines({2: "MINE"}))
    write(repo, "other.txt", "FOREIGN\n")  # unstaged foreign edit
    commit_other = repo.path / "staged_foreign.txt"
    commit_other.write_text("staged by someone else\n")
    repo.git("add", "--", "staged_foreign.txt")

    result = frozen(capsys, "--paths", "f.txt")

    assert result["paths"] == ["f.txt"]
    assert repo.git("show", f"{result['snapshot']}:other.txt").stdout == "o\n"
    assert repo.git("cat-file", "-e", f"{result['snapshot']}:staged_foreign.txt", check=False).returncode != 0


def test_patch_excludes_foreign_hunks_in_a_mixed_file(repo, capsys, tmp_path):
    """Case 4."""
    ours = lines({2: "OURS"})
    both = lines({2: "OURS", 28: "FOREIGN"})
    write(repo, "f.txt", ours)
    patch = repo.git("diff", "--", "f.txt").stdout
    assert "OURS" in patch and "FOREIGN" not in patch
    patch_file = tmp_path / "ours.patch"
    patch_file.write_bytes(patch.encode("utf-8"))
    write(repo, "f.txt", both)

    result = frozen(capsys, "--patch", str(patch_file))

    assert repo.git("show", f"{result['snapshot']}:f.txt").stdout == ours
    assert (repo.path / "f.txt").read_bytes().decode() == both  # worktree untouched
    assert result["paths"] == ["f.txt"]


def test_freeze_leaves_shared_index_bytes_unchanged(repo, capsys, tmp_path):
    """Case 5, for all three selectors."""
    write(repo, "f.txt", lines({2: "OURS"}))
    patch_file = tmp_path / "p.patch"
    patch_file.write_bytes(repo.git("diff", "--", "f.txt").stdout.encode("utf-8"))
    write(repo, "other.txt", "staged foreign\n")
    repo.git("add", "--", "other.txt")
    write(repo, "untracked.txt", "u\n")
    index = repo.path / ".git" / "index"

    for argv in (
        ("--paths", "f.txt", "untracked.txt"),
        ("--patch", str(patch_file)),
        ("--staged",),
    ):
        before = index.read_bytes()
        frozen(capsys, *argv)
        assert index.read_bytes() == before, argv


def test_staged_freeze_snapshots_the_shared_index(repo, capsys):
    write(repo, "f.txt", lines({2: "STAGED"}))
    repo.git("add", "--", "f.txt")
    write(repo, "f.txt", lines({2: "STAGED", 3: "UNSTAGED"}))
    result = frozen(capsys, "--staged")
    assert repo.git("show", f"{result['snapshot']}:f.txt").stdout == lines({2: "STAGED"})


def test_bare_freeze_is_a_usage_error(repo, capsys):
    with pytest.raises(SystemExit) as exc:
        snap.main(["freeze"])
    assert exc.value.code == 2


def test_cli_runs_as_a_script(repo, tmp_path):
    bare = subprocess.run([sys.executable, SCRIPT, "freeze"], cwd=repo.path, capture_output=True, text=True)
    assert bare.returncode == 2
    write(repo, "f.txt", lines({2: "CLI"}))
    proc = subprocess.run(
        [sys.executable, SCRIPT, "freeze", "--paths", "f.txt"], cwd=repo.path, capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["paths"] == ["f.txt"]


def test_freeze_of_an_unchanged_selection_exits_1(repo, capsys):
    rc, out, err = run(capsys, "freeze", "--paths", "other.txt")
    assert rc == 1
    assert "nothing to review" in err
    assert out == ""
    assert names(repo, "for-each-ref", "--format=%(refname)", snap.REF_PREFIX) == []


# ------------------------------------------------------------------ land (6-9)


def test_land_unmoved_head_gives_tree_equality_and_keeps_later_edit_unstaged(repo, capsys, tmp_path):
    """Case 6."""
    reviewed = lines({2: "REVIEWED"})
    write(repo, "f.txt", reviewed)
    write(repo, "other.txt", "foreign staged\n")
    repo.git("add", "--", "other.txt")  # another session's staged work
    result = frozen(capsys, "--paths", "f.txt")
    sha12 = result["snapshot"][:12]
    bdir = bundle_dir(sha12)
    later = lines({2: "REVIEWED", 5: "POST-SNAPSHOT"})
    write(repo, "f.txt", later)

    rc, out, err = run(capsys, "land", result["snapshot"], "-F", msgfile(tmp_path))

    assert rc == 0, err
    landed = json.loads(out)
    assert landed["tree_equal"] is True and landed["patch_id_equal"] is True
    assert landed["snapshot"] == result["snapshot"]
    head = repo.git("rev-parse", "HEAD").stdout.strip()
    assert landed["landed"] == head
    assert repo.git("rev-parse", "HEAD^").stdout.strip() == result["base"]
    assert repo.git("rev-parse", "HEAD^{tree}").stdout.strip() == result["tree"]
    assert repo.git("show", "HEAD:f.txt").stdout == reviewed
    assert repo.git("log", "-1", "--format=%s").stdout.strip() == "landed change"
    # the post-snapshot edit survives as an unstaged change; the foreign staging is untouched
    assert (repo.path / "f.txt").read_bytes().decode() == later
    assert names(repo, "diff", "--name-only") == ["f.txt"]
    assert names(repo, "diff", "--cached", "--name-only") == ["other.txt"]
    # ref and bundle directory are gone
    assert not has_ref(repo, sha12)
    assert not bdir.exists()


def test_land_after_a_foreign_commit_matches_patch_id_and_keeps_the_foreign_commit(repo, capsys, tmp_path):
    """Case 7."""
    write(repo, "f.txt", lines({2: "REVIEWED"}))
    result = frozen(capsys, "--paths", "f.txt")
    commit(repo, "third.txt", "foreign\n", "foreign commit")
    foreign = repo.git("rev-parse", "HEAD").stdout.strip()

    rc, out, err = run(capsys, "land", result["snapshot"], "-F", msgfile(tmp_path))

    assert rc == 0, err
    landed = json.loads(out)
    assert landed["tree_equal"] is False
    assert landed["patch_id_equal"] is True
    assert repo.git("rev-parse", "HEAD^").stdout.strip() == foreign
    assert repo.git("show", "HEAD:f.txt").stdout == lines({2: "REVIEWED"})
    assert repo.git("show", "HEAD:third.txt").stdout == "foreign\n"
    assert names(repo, "show", "--name-only", "--format=", "HEAD") == ["f.txt"]
    assert names(repo, "diff", "--cached", "--name-only") == []


def test_land_after_a_foreign_commit_in_the_same_file_replays_the_patch(repo, capsys, tmp_path):
    write(repo, "f.txt", lines({28: "OURS"}))
    result = frozen(capsys, "--paths", "f.txt")
    commit_blob_only(repo, "f.txt", lines({2: "THEIRS"}), "foreign hunk")

    rc, out, err = run(capsys, "land", result["snapshot"], "-F", msgfile(tmp_path))

    assert rc == 0, err
    assert json.loads(out)["patch_id_equal"] is True
    assert repo.git("show", "HEAD:f.txt").stdout == lines({2: "THEIRS", 28: "OURS"})


def test_conflicting_replay_is_refused_with_head_unchanged(repo, capsys, tmp_path):
    """Case 8."""
    write(repo, "f.txt", lines({2: "OURS"}))
    result = frozen(capsys, "--paths", "f.txt")
    commit_blob_only(repo, "f.txt", lines({2: "THEIRS"}), "conflicting foreign commit")
    head = repo.git("rev-parse", "HEAD").stdout.strip()
    index_before = repo.git("ls-files", "-s").stdout

    rc, out, err = run(capsys, "land", result["snapshot"], "-F", msgfile(tmp_path))

    assert rc == 1
    assert out == ""
    assert "f.txt" in err
    assert repo.git("rev-parse", "HEAD").stdout.strip() == head
    assert repo.git("ls-files", "-s").stdout == index_before
    assert has_ref(repo, result["snapshot"][:12])  # kept for a retry or drop


def test_foreign_staged_entry_on_an_own_path_is_refused(repo, capsys, tmp_path):
    """Case 9."""
    write(repo, "f.txt", lines({2: "OURS"}))
    result = frozen(capsys, "--paths", "f.txt")
    sha = git_in(repo, "hash-object", "-w", "--stdin", input=lines({2: "STAGED BY ANOTHER SESSION"}).encode())
    git_in(repo, "update-index", "--cacheinfo", f"100644,{sha},f.txt")
    head = repo.git("rev-parse", "HEAD").stdout.strip()
    index_before = repo.git("ls-files", "-s").stdout

    rc, out, err = run(capsys, "land", result["snapshot"], "-F", msgfile(tmp_path))

    assert rc == 1
    assert "f.txt" in err and "staged" in err
    assert repo.git("rev-parse", "HEAD").stdout.strip() == head
    assert repo.git("ls-files", "-s").stdout == index_before


def test_land_allows_own_staged_entries_from_a_staged_freeze(repo, capsys, tmp_path):
    """Correction 3: an entry equal to the snapshot is this session's own staging."""
    reviewed = lines({2: "STAGED"})
    write(repo, "f.txt", reviewed)
    repo.git("add", "--", "f.txt")
    result = frozen(capsys, "--staged")
    write(repo, "f.txt", lines({2: "STAGED", 4: "AFTER"}))

    rc, out, err = run(capsys, "land", result["snapshot"], "-F", msgfile(tmp_path))

    assert rc == 0, err
    assert repo.git("show", "HEAD:f.txt").stdout == reviewed
    assert names(repo, "diff", "--cached", "--name-only") == []
    assert names(repo, "diff", "--name-only") == ["f.txt"]


def test_land_handles_a_deleted_and_an_added_path(repo, capsys, tmp_path):
    (repo.path / "other.txt").unlink()
    write(repo, "added.txt", "a\n")
    result = frozen(capsys, "--paths", "other.txt", "added.txt")
    rc, out, err = run(capsys, "land", result["snapshot"], "-F", msgfile(tmp_path))
    assert rc == 0, err
    assert repo.git("rev-parse", "HEAD^{tree}").stdout.strip() == result["tree"]
    assert names(repo, "diff", "--cached", "--name-only") == []


# -------------------------------------------------- land: hooks and corrections


def _install_hook(repo, body):
    hook = repo.path / ".git" / "hooks" / "pre-commit"
    hook.write_bytes(("#!/bin/sh\n" + body).encode("utf-8"))
    hook.chmod(0o755)


def test_pre_commit_hook_sees_the_landing_index(repo, capsys, tmp_path):
    """Case 10."""
    out_file = tmp_path / "hook.out"
    _install_hook(
        repo,
        f"printf '%s\\n' \"$GIT_INDEX_FILE\" > '{out_file.as_posix()}'\n"
        f"git diff --cached --name-only >> '{out_file.as_posix()}'\n",
    )
    write(repo, "f.txt", lines({2: "HOOKED"}))
    result = frozen(capsys, "--paths", "f.txt")
    assert names(repo, "diff", "--cached", "--name-only") == []  # the shared index is clean

    rc, out, err = run(capsys, "land", result["snapshot"], "-F", msgfile(tmp_path))

    assert rc == 0, err
    seen = out_file.read_text().splitlines()
    assert seen[0] and Path(seen[0]).name == "landing.index"
    assert "f.txt" in seen[1:]


def test_reset_runs_even_when_verification_fails(repo, capsys, tmp_path):
    """Correction 1: a hook that adds a file to the landing index breaks tree equality."""
    _install_hook(repo, "echo extra > extra.txt\ngit add extra.txt\n")
    write(repo, "f.txt", lines({2: "REVIEWED"}))
    result = frozen(capsys, "--paths", "f.txt")

    rc, out, err = run(capsys, "land", result["snapshot"], "-F", msgfile(tmp_path))

    assert rc == 1
    assert "reviewed tree" in err
    assert repo.git("show", "HEAD:f.txt").stdout == lines({2: "REVIEWED"})  # the commit did land
    # the shared index no longer holds the pre-land f.txt (which would read as a revert)
    assert "f.txt" not in names(repo, "diff", "--cached", "--name-only")
    assert has_ref(repo, result["snapshot"][:12])


def test_head_race_between_precheck_and_commit_is_undone(repo, capsys, tmp_path, monkeypatch):
    """Correction 2: a foreign commit lands after land's pre-check, before git commit starts."""
    write(repo, "f.txt", lines({2: "REVIEWED"}))
    result = frozen(capsys, "--paths", "f.txt")
    foreign = {}
    original = snap._run_commit

    def racing_commit(root, landing_index, msg):
        commit(repo, "third.txt", "foreign\n", "racing foreign commit")
        foreign["sha"] = repo.git("rev-parse", "HEAD").stdout.strip()
        original(root, landing_index, msg)

    monkeypatch.setattr(snap, "_run_commit", racing_commit)

    rc, out, err = run(capsys, "land", result["snapshot"], "-F", msgfile(tmp_path))

    assert rc == 1
    assert foreign["sha"] in err
    assert repo.git("rev-parse", "HEAD").stdout.strip() == foreign["sha"]
    assert repo.git("show", "HEAD:third.txt").stdout == "foreign\n"
    assert repo.git("show", "HEAD:f.txt").stdout == lines()  # our commit is not in HEAD
    assert has_ref(repo, result["snapshot"][:12])


def test_head_moved_before_landing_index_is_built_is_replayed_not_raced(repo, capsys, tmp_path):
    write(repo, "f.txt", lines({2: "REVIEWED"}))
    result = frozen(capsys, "--paths", "f.txt")
    commit(repo, "third.txt", "foreign\n", "foreign commit")
    rc, out, err = run(capsys, "land", result["snapshot"], "-F", msgfile(tmp_path))
    assert rc == 0, err
    assert repo.git("show", "HEAD:third.txt").stdout == "foreign\n"


def test_every_git_index_file_is_absolute(repo, capsys, tmp_path, monkeypatch):
    seen = []
    real_run = subprocess.run

    def recording_run(*a, **kw):
        env = kw.get("env") or {}
        if "GIT_INDEX_FILE" in env:
            seen.append(env["GIT_INDEX_FILE"])
        return real_run(*a, **kw)

    write(repo, "f.txt", lines({2: "ABS"}))
    monkeypatch.setattr(snap.subprocess, "run", recording_run)
    result = frozen(capsys, "--paths", "f.txt")
    rc, out, err = run(capsys, "land", result["snapshot"], "-F", msgfile(tmp_path))
    assert rc == 0, err
    assert len(seen) >= 4
    assert all(os.path.isabs(p) for p in seen), seen


def test_land_refuses_unknown_and_unparented_snapshots(repo, capsys, tmp_path):
    rc, out, err = run(capsys, "land", "0" * 12, "-F", msgfile(tmp_path))
    assert rc == 1 and "no snapshot ref" in err
    rc, out, err = run(capsys, "land", "zz", "-F", msgfile(tmp_path))
    assert rc == 1 and "not a snapshot sha" in err


# ------------------------------------------------------------------ drop/prune


def test_drop_tree_only_removes_just_the_tree(repo, capsys):
    write(repo, "f.txt", lines({2: "D"}))
    result = frozen(capsys, "--paths", "f.txt")
    sha12 = result["snapshot"][:12]
    bdir = bundle_dir(sha12)

    rc, out, err = run(capsys, "drop", result["snapshot"], "--tree-only")

    assert rc == 0, err
    assert not (bdir / "tree").exists()
    assert (bdir / "bundle.json").exists()
    assert has_ref(repo, sha12)


def test_drop_removes_ref_and_directory_and_only_that_snapshot(repo, capsys):
    write(repo, "f.txt", lines({2: "D"}))
    first = frozen(capsys, "--paths", "f.txt")
    write(repo, "f.txt", lines({2: "D2"}))
    second = frozen(capsys, "--paths", "f.txt")
    d1 = bundle_dir(first["snapshot"][:12])
    d2 = bundle_dir(second["snapshot"][:12])
    unrelated = snap.data_dir("git-kit") / "reviews" / "repo-12345678" / "origin-main..HEAD"
    unrelated.mkdir(parents=True)
    repo.git("branch", "keepme")

    rc, out, err = run(capsys, "drop", first["snapshot"])

    assert rc == 0, err
    assert not d1.exists() and d2.exists() and unrelated.exists()
    assert not has_ref(repo, first["snapshot"][:12]) and has_ref(repo, second["snapshot"][:12])
    assert repo.git("rev-parse", "--verify", "refs/heads/keepme", check=False).returncode == 0
    rc, out, err = run(capsys, "drop", first["snapshot"])
    assert rc == 1 and "no snapshot" in err


def _old_snapshot_ref(repo, tag):
    """A snapshot ref whose commit is dated 2020, as if left by a crashed session."""
    tree = repo.git("rev-parse", "HEAD^{tree}").stdout.strip()
    env = dict(os.environ)
    env.update(
        GIT_AUTHOR_NAME="x", GIT_AUTHOR_EMAIL="x@x", GIT_COMMITTER_NAME="x", GIT_COMMITTER_EMAIL="x@x",
        GIT_COMMITTER_DATE="2020-01-01T00:00:00Z", GIT_AUTHOR_DATE="2020-01-01T00:00:00Z",
    )
    sha = subprocess.run(
        ["git", "commit-tree", tree, "-p", "HEAD", "-m", tag],
        cwd=repo.path, env=env, capture_output=True, text=True, check=True,
    ).stdout.strip()
    return sha


def test_prune_touches_only_its_own_namespace(repo, capsys):
    """Case 11."""
    old = _old_snapshot_ref(repo, "old snapshot")
    old12 = old[:12]
    repo.git("update-ref", snap.REF_PREFIX + old12, old)
    old_dir = bundle_dir(old12)
    # things prune must never delete, however old
    other_old = _old_snapshot_ref(repo, "old but elsewhere")
    repo.git("update-ref", "refs/other/old", other_old)
    repo.git("update-ref", "refs/heads/old-branch", other_old)
    repo.git("update-ref", snap.REF_PREFIX + "not-a-snapshot-id", other_old)
    stray_dir = bundle_dir("abcdef012345")  # snap-dir whose ref is not in this repo
    other_dir = snap.data_dir("git-kit") / "reviews" / "repo-12345678" / "origin-main..HEAD"
    other_dir.mkdir(parents=True)
    write(repo, "f.txt", lines({2: "FRESH"}))
    fresh = frozen(capsys, "--paths", "f.txt")  # freeze prunes opportunistically
    fresh_dir = bundle_dir(fresh["snapshot"][:12])

    rc, out, err = run(capsys, "prune")

    assert rc == 0, err
    assert not has_ref(repo, old12) and not old_dir.exists()
    assert has_ref(repo, fresh["snapshot"][:12]) and fresh_dir.exists()
    for ref in ("refs/other/old", "refs/heads/old-branch", snap.REF_PREFIX + "not-a-snapshot-id"):
        assert repo.git("rev-parse", "--verify", "--quiet", ref, check=False).returncode == 0, ref
    assert stray_dir.exists() and other_dir.exists()


def test_freeze_prunes_opportunistically(repo, capsys):
    old = _old_snapshot_ref(repo, "crashed session")
    repo.git("update-ref", snap.REF_PREFIX + old[:12], old)
    write(repo, "f.txt", lines({2: "FRESH"}))
    frozen(capsys, "--paths", "f.txt")
    assert not has_ref(repo, old[:12])


def test_prune_rejects_a_negative_age(repo, capsys):
    rc, out, err = run(capsys, "prune", "--older-than-days", "-1")
    assert rc == 1 and "negative" in err


# ------------------------------------------------------------------- static


def test_script_never_uses_git_stash_or_commit_with_paths():
    """Case 12."""
    source = Path(SCRIPT).read_text(encoding="utf-8")
    assert "stash" not in source.lower()
    assert not re.search(r"""["']commit["'][^\n]*["']--["']""", source)
    source.encode("ascii")  # ASCII only in tracked files


def test_drop_accepts_the_full_ref_form(repo, capsys):
    write(repo, "f.txt", lines({2: "D"}))
    result = frozen(capsys, "--paths", "f.txt")
    sha12 = result["snapshot"][:12]

    rc, out, err = run(capsys, "drop", snap.REF_PREFIX + result["snapshot"])

    assert rc == 0, err
    assert not has_ref(repo, sha12)


def test_land_accepts_the_full_ref_form_with_a_long_sha(repo, capsys, tmp_path):
    write(repo, "f.txt", lines({2: "D"}))
    result = frozen(capsys, "--paths", "f.txt")
    ref = snap.REF_PREFIX + result["snapshot"]

    rc, out, err = run(capsys, "land", ref, "-F", msgfile(tmp_path))

    assert "does not match" not in err
    assert rc == 0, err
