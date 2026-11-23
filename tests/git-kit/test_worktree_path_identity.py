"""Run: uv run --extra dev pytest tests/git-kit/test_worktree_path_identity.py -q.

Triggers: case aliases in discovery and registration checks, missing twins,
duplicate parked records, and locked registrations.
"""

from __future__ import annotations

from pathlib import Path, PureWindowsPath
import shutil

import pytest

from test_worktree_lifecycle import git, holder, lifecycle
from test_worktree_migration import legacy, migration


def case_alias(path: Path) -> Path:
    alias = path.with_name(path.name.swapcase())
    if not alias.exists() or not alias.samefile(path):
        pytest.skip("temporary directory filesystem is case-sensitive")
    return alias


@pytest.mark.parametrize("action", ["release", "discard", "maintenance", "adopt"])
@pytest.mark.parametrize("locked", [False, True], ids=["unlocked", "locked"])
def test_case_variant_registration_lifecycle(lifecycle, action: str, locked: bool) -> None:
    import worktrees as wt
    from test_worktree_discard import approve

    obj = lifecycle
    case_alias(obj.repo.parent)
    # Match a legacy on-disk namespace spelling while lifecycle uses lower case.
    actual = obj.namespace.with_name(obj.namespace.name.swapcase())
    actual.mkdir(parents=True)
    assert case_alias(actual).samefile(obj.namespace)
    obj.maintain()
    taken = obj.take("case-task", "session", process=holder())
    record = obj.get(taken["id"])
    path = Path(record["path"])
    registration = next(r for r in wt.records(obj.repo) if Path(r["worktree"]).samefile(path))
    assert registration["worktree"] != path.as_posix()
    if action != "release":
        record.update(state="parked", holder=None)
        obj.save(record)
    if action in {"discard", "adopt"}:
        (path / "unfinished").write_text("preserve unless explicitly discarded", encoding="utf-8")
    if locked:
        git(obj.repo, "worktree", "lock", "--reason", "real registration lock", str(path))

    def run() -> dict:
        if action == "release":
            return obj.release(taken["claim"])
        if action == "discard":
            proposal = obj.discard(record["id"])["proposal"]
            return obj.discard(record["id"], approved=approve(proposal))
        if action == "adopt":
            return obj.adopt(record["id"], "new-agent", "new-session", process=holder())
        return obj.maintain()

    if locked:
        if action == "maintenance":
            result = run()
            assert result["parked_recheck"]["removed"] == 0
            assert "missing or locked Git registration" in obj.get(record["id"])["safety"]["blockers"]
        else:
            with pytest.raises(ValueError, match="locked"):
                run()
        assert path.exists()
        assert obj.get(record["id"])["state"] != "removed"
        assert any("locked" in r and Path(r["worktree"]).samefile(path) for r in wt.records(obj.repo))
    else:
        result = run()
        saved = obj.get(record["id"])
        if action == "release":
            assert result["state"] == saved["state"] == "released"
            assert saved["holder"] is None and path.exists()
        elif action == "adopt":
            assert saved["state"] == "taken" and saved["holder"]["claim"] == result["claim"]
            assert (Path(result["path"]) / "unfinished").read_text() == "preserve unless explicitly discarded"
        else:
            assert saved["state"] == "removed" and not path.exists()
            assert all(r["worktree"] != registration["worktree"] for r in wt.records(obj.repo))


@pytest.mark.parametrize("action", ["reconcile", "migration-agreement", "reserved-removal"])
def test_missing_case_variant_registration_is_preserved(lifecycle, monkeypatch, action: str) -> None:
    import worktree_migration as migration_module
    import worktree_retirement as retirement
    import worktrees as wt

    obj = lifecycle
    case_alias(obj.repo.parent)
    obj.namespace.with_name(obj.namespace.name.swapcase()).mkdir(parents=True)
    obj.maintain()
    record = obj.read_records()[0]
    path = Path(record["path"])
    registration = next(r for r in wt.records(obj.repo) if Path(r["worktree"]).samefile(path))
    assert registration["worktree"] != path.as_posix()
    shutil.rmtree(path)
    if action == "reconcile":
        obj.reconcile()
        assert obj.get(record["id"])["state"] == "ready"
    elif action == "migration-agreement":
        record["state"] = "parked"
        obj.save(record)
        row = {"id": record["id"], "path": record["path"], "phase": "parked",
               "source_clone": record["repo"]["source_clone"], "common_dir": record["repo"]["common_dir"]}
        migration_module.verify_agreement(obj, {"candidates": [row]})
    else:
        record["operation"] = {"name": "parked-recheck", "phase": "reserved"}
        # Isolate the recovery shortcut: a remaining registration still needs safety evidence.
        monkeypatch.setattr(obj, "safety", lambda *args, **kwargs: {"safe": False, "blockers": ["directory missing"]})
        assert retirement.evidence(obj, record, recover_removed=True)["safe"] is False
    assert any(r["worktree"] == registration["worktree"] for r in wt.records(obj.repo))


def test_case_variant_main_registration_cannot_be_retired(lifecycle) -> None:
    import worktree_safety as safety

    obj = lifecycle
    path = case_alias(obj.repo)
    record = {"path": str(path), "holder": None, "processes": []}
    evidence = safety.inspect(record, git(obj.repo, "rev-parse", "HEAD"), [])
    assert not evidence["safe"]
    assert "main checkout cannot be retired" in evidence["blockers"]


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
