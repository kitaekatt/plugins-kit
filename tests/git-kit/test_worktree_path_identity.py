"""Run: uv run --extra dev pytest tests/git-kit/test_worktree_path_identity.py -q.

Triggers: case aliases in discovery, missing twins, and duplicate parked records.
"""

from __future__ import annotations

from pathlib import Path, PureWindowsPath

import pytest

from test_worktree_lifecycle import git, lifecycle
from test_worktree_migration import legacy, migration


def case_alias(path: Path) -> Path:
    alias = path.with_name(path.name.swapcase())
    if not alias.exists() or not alias.samefile(path):
        pytest.skip("temporary directory filesystem is case-sensitive")
    return alias


def test_real_case_variant_migration_is_one_clone_worktree(migration, tmp_path: Path) -> None:
    obj = migration
    clone = tmp_path / "Afterplay"
    git(obj.repo, "clone", "--branch", "main", obj.url, str(clone))
    obj.namespace.mkdir(parents=True)
    upper = case_alias(obj.namespace)
    path = upper / "legacy-task"
    git(clone, "worktree", "add", "-b", "legacy-task", str(path), "origin/main")
    inventory = obj.migrate()
    assert len(inventory["candidates"]) == 1
    row = inventory["candidates"][0]
    assert row["path"] == str(path)
    assert row["source_clone"] == str(clone)
    result = obj.migrate(apply=True)
    assert result["phase"] == "complete" and not path.exists()
    assert len(obj.read_records()) == 1


def test_discovery_uses_injected_path_identity(migration, monkeypatch) -> None:
    import worktree_migration as module

    obj = migration
    path = legacy(obj, "case-task")
    alias = path.parent.with_name(path.parent.name.swapcase()) / path.name
    identity = module.path_identity
    monkeypatch.setattr(module, "path_identity", lambda p: ("test-alias",) if str(p).casefold() == str(path).casefold() else identity(p))
    records = module.wt.records

    def aliases(source: Path) -> list[dict]:
        return [{**row, "worktree": str(alias)} if row["worktree"] == str(path) else row for row in records(source)]

    monkeypatch.setattr(module.wt, "records", aliases)
    _, candidates, _ = module.discover(obj, [], {})
    assert len(candidates) == 1
    assert candidates[0]["path"] == str(alias)  # Clone-derived spelling wins.
    assert candidates[0]["source_clone"] == str(obj.repo)


def test_path_identity_existing_and_missing_case_variants(tmp_path: Path) -> None:
    from worktree_paths import path_identity

    parent = tmp_path / "Afterplay"
    parent.mkdir()
    alias = case_alias(parent)
    assert path_identity(parent) == path_identity(alias)
    assert path_identity(parent / "Missing") == path_identity(alias / "missing")


def test_path_identity_keeps_distinct_directories(tmp_path: Path) -> None:
    from worktree_paths import path_identity

    first, second = tmp_path / "one", tmp_path / "two"
    first.mkdir()
    second.mkdir()
    assert path_identity(first) != path_identity(second)
    assert path_identity(first / "missing") != path_identity(second / "missing")


@pytest.mark.parametrize("insensitive", [False, True])
def test_missing_suffix_folds_only_for_insensitive_parent(tmp_path: Path, monkeypatch, insensitive: bool) -> None:
    import worktree_paths as module

    monkeypatch.setattr(module, "case_insensitive", lambda parent: insensitive)
    monkeypatch.setattr(module.os.path, "normcase", lambda value: value.lower())
    assert (module.path_identity(tmp_path / "Missing") == module.path_identity(tmp_path / "missing")) is insensitive


def test_windows_path_equality_does_not_disable_case_probe() -> None:
    from worktree_paths import case_insensitive

    class WindowsDirectory(PureWindowsPath):
        def samefile(self, other: PureWindowsPath) -> bool:
            return str(self).casefold() == str(other).casefold()

    assert case_insensitive(WindowsDirectory("C:/Worktrees/Afterplay"))


def test_namespace_scan_maps_git_common_dir_to_own_clone(migration, tmp_path: Path, monkeypatch) -> None:
    import worktree_migration as module

    obj = migration
    clone = tmp_path / "other/clone"
    clone.parent.mkdir()
    git(obj.repo, "clone", "--branch", "main", obj.url, str(clone))
    legacy(obj, "unlisted", source=clone)
    records = module.wt.records
    monkeypatch.setattr(module.wt, "records", lambda source: records(source)[:1])
    _, candidates, _ = module.discover(obj, [], {})
    assert len(candidates) == 1
    assert candidates[0]["source_clone"] == str(clone)
    assert candidates[0]["common_dir"] == str(clone / ".git")
