"""Snapshots of the target and pruning of the analysis inputs."""

from __future__ import annotations

from cck_support import git

from clean_code_kit import snapshot


def _edit_and_add(repo):
    (repo / "src" / "pkg" / "a.py").write_text("def edited():\n    return 2\n")
    (repo / "src" / "pkg" / "new.py").write_text("def fresh():\n    return 3\n")
    (repo / "ignored.txt").write_text("not for analysis\n")


def test_head_snapshot_holds_committed_content_only_and_counts_dirty(target_repo, tmp_path):
    committed = (target_repo / "src" / "pkg" / "a.py").read_text()
    _edit_and_add(target_repo)
    dest = tmp_path / "snap"
    dest.mkdir()
    snapshot.take(target_repo, "head", dest)
    assert (dest / "src" / "pkg" / "a.py").read_text() == committed
    assert not (dest / "src" / "pkg" / "new.py").exists()
    assert not (dest / "ignored.txt").exists()
    assert not (dest / ".git").exists()
    assert (dest / ".claude" / "clean-code-kit" / "config.yaml").is_file()
    # one modified tracked file plus one untracked file; the ignored file is not listed
    assert snapshot.dirty_paths(target_repo) == 2


def test_working_tree_snapshot_holds_edits_and_untracked_but_not_ignored(target_repo, tmp_path):
    _edit_and_add(target_repo)
    dest = tmp_path / "snap"
    dest.mkdir()
    snapshot.take(target_repo, "working-tree", dest)
    assert "edited" in (dest / "src" / "pkg" / "a.py").read_text()
    assert (dest / "src" / "pkg" / "new.py").is_file()
    assert not (dest / "ignored.txt").exists()
    assert not (dest / ".git").exists()


def test_working_tree_snapshot_skips_a_tracked_file_deleted_on_disk(target_repo, tmp_path):
    (target_repo / "src" / "pkg" / "b.py").unlink()
    dest = tmp_path / "snap"
    dest.mkdir()
    snapshot.take(target_repo, "working-tree", dest)
    assert not (dest / "src" / "pkg" / "b.py").exists()
    assert (dest / "src" / "pkg" / "a.py").is_file()


def test_prune_drops_excludes_and_unconfigured_languages_and_keeps_manifests(target_repo, tmp_path):
    dest = tmp_path / "snap"
    dest.mkdir()
    snapshot.take(target_repo, "head", dest)
    counts = snapshot.prune(dest, ["**/bootstrap_guard.py"], ["python"])
    pkg = dest / "src" / "pkg"
    assert not (pkg / "bootstrap_guard.py").exists()
    assert not (pkg / "view.ts").exists()
    assert (pkg / "package.json").is_file()
    assert (pkg / "a.py").is_file()
    assert (dest / ".gitignore").is_file()
    assert counts == {"excluded": 1, "other_language": 1}


def test_prune_keeps_a_configured_language(target_repo, tmp_path):
    dest = tmp_path / "snap"
    dest.mkdir()
    snapshot.take(target_repo, "head", dest)
    snapshot.prune(dest, [], ["python", "typescript"])
    assert (dest / "src" / "pkg" / "view.ts").is_file()
    assert (dest / "src" / "pkg" / "bootstrap_guard.py").is_file()


def test_repo_root_resolves_a_subdirectory(target_repo):
    assert snapshot.repo_root(target_repo / "src" / "pkg") == target_repo.resolve()
    git(target_repo, "status")
