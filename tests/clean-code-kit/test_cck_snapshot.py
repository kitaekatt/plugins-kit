"""Snapshots of the target and selection of the analysis inputs."""

from __future__ import annotations

from pathlib import Path

import pytest
from cck_support import git

from clean_code_kit import snapshot
from clean_code_kit.errors import Refusal


def _take(repo, mode, dest, source_roots=("src",), test_roots=("tests",), exclude=(), languages=("python",)):
    paths = snapshot.select_paths(snapshot.tracked_files(repo, mode), list(source_roots), list(test_roots),
                                  list(exclude), list(languages))
    snapshot.take(repo, mode, dest, paths, [*source_roots, *test_roots])
    return paths


def _edit_and_add(repo):
    (repo / "src" / "pkg" / "a.py").write_text("def edited():\n    return 2\n")
    (repo / "src" / "pkg" / "new.py").write_text("def fresh():\n    return 3\n")
    (repo / "ignored.txt").write_text("not for analysis\n")


def test_head_snapshot_holds_committed_content_only_and_counts_dirty(target_repo, tmp_path):
    committed = (target_repo / "src" / "pkg" / "a.py").read_text()
    _edit_and_add(target_repo)
    dest = tmp_path / "snap"
    dest.mkdir()
    _take(target_repo, "head", dest)
    assert (dest / "src" / "pkg" / "a.py").read_text() == committed
    assert not (dest / "src" / "pkg" / "new.py").exists()
    assert not (dest / "ignored.txt").exists()
    assert not (dest / ".git").exists()
    assert not (dest / ".claude").exists()  # outside every root
    # one modified tracked file plus one untracked file; the ignored file is not listed
    assert len(snapshot.dirty_paths(target_repo)) == 2


def test_working_tree_snapshot_holds_edits_and_untracked_but_not_ignored(target_repo, tmp_path):
    _edit_and_add(target_repo)
    dest = tmp_path / "snap"
    dest.mkdir()
    _take(target_repo, "working-tree", dest)
    assert "edited" in (dest / "src" / "pkg" / "a.py").read_text()
    assert (dest / "src" / "pkg" / "new.py").is_file()
    assert not (dest / "ignored.txt").exists()
    assert not (dest / ".git").exists()


def test_working_tree_snapshot_skips_a_tracked_file_deleted_on_disk(target_repo, tmp_path):
    (target_repo / "src" / "pkg" / "b.py").unlink()
    dest = tmp_path / "snap"
    dest.mkdir()
    _take(target_repo, "working-tree", dest)
    assert not (dest / "src" / "pkg" / "b.py").exists()
    assert (dest / "src" / "pkg" / "a.py").is_file()


@pytest.mark.parametrize("mode", ["head", "working-tree"])
def test_take_writes_only_selected_paths_and_keeps_in_root_manifests(target_repo, tmp_path, mode):
    dest = tmp_path / "snap"
    dest.mkdir()
    paths = _take(target_repo, mode, dest, exclude=["**/bootstrap_guard.py"])
    pkg = dest / "src" / "pkg"
    assert not (pkg / "bootstrap_guard.py").exists()
    assert not (pkg / "view.ts").exists()
    assert (pkg / "package.json").is_file()
    assert (pkg / "a.py").is_file()
    assert (dest / "tests" / "test_a.py").is_file()
    assert not (dest / ".gitignore").exists()
    written = sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file())
    assert written == sorted(paths)


def test_select_paths_keeps_a_configured_language_and_drops_the_rest(target_repo):
    tracked = snapshot.tracked_files(target_repo, "head")
    both = snapshot.select_paths(tracked, ["src"], ["tests"], [], ["python", "typescript"])
    assert "src/pkg/view.ts" in both and "src/pkg/bootstrap_guard.py" in both
    only_python = snapshot.select_paths(tracked, ["src"], ["tests"], [], ["python"])
    assert "src/pkg/view.ts" not in only_python and "src/pkg/package.json" in only_python


def test_select_paths_matches_whole_path_components_of_a_root():
    tracked = ["src/a.py", "srcx/b.py", "src.py", "tests/t.py", "other/c.py"]
    assert snapshot.select_paths(tracked, ["src"], ["tests"], [], ["python"]) == ["src/a.py", "tests/t.py"]
    assert snapshot.select_paths(tracked, ["src.py"], [], [], ["python"]) == ["src.py"]


def test_head_snapshot_refuses_a_listed_path_the_archive_lacks(target_repo, tmp_path):
    dest = tmp_path / "snap"
    dest.mkdir()
    with pytest.raises(Refusal, match="lacks 1 listed paths.*nowhere.py"):
        snapshot.take(target_repo, "head", dest, ["src/pkg/a.py", "src/pkg/nowhere.py"], ["src"])


def test_require_roots_names_a_root_with_no_tracked_path():
    tracked = ["src/a.py", "tests/t.py"]
    snapshot.require_roots(tracked, ["src", "tests"], "source_roots", Path("config.yaml"), "head")
    with pytest.raises(Refusal, match=r"test_roots entry 'spec' does not exist in the head snapshot"):
        snapshot.require_roots(tracked, ["spec"], "test_roots", Path("config.yaml"), "head")


def test_repo_root_resolves_a_subdirectory(target_repo):
    assert snapshot.repo_root(target_repo / "src" / "pkg") == target_repo.resolve()
    git(target_repo, "status")
