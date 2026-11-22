"""Run: uv run --extra dev pytest tests/git-kit/test_worktree_duplicate_repair.py -q.

Trigger: completed migration journals contain case aliases or missing twins.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from test_worktree_lifecycle import git, holder, lifecycle, write_json
from test_worktree_migration import legacy, migration
from test_worktree_path_identity import case_alias


@pytest.mark.parametrize("operation", ["maintenance", "migrate"])
@pytest.mark.parametrize("kind", ["existing", "missing", "holder", "holder-clean", "worker-clean", "distinct"])
def test_duplicate_repair_is_record_only(migration, operation: str, kind: str) -> None:
    obj = migration
    path = legacy(obj, "unfinished")
    (path / "unfinished").write_text("preserve this work")
    obj.migrate(apply=True)
    original = obj.read_records()[0]
    alias = case_alias(path.parent) / path.name
    duplicate = copy.deepcopy(original)
    duplicate.update(id="w-" + "d" * 32, path=str(alias))
    duplicate["migration"]["origins"] = ["namespace-filesystem"]
    if kind == "missing":
        git(obj.repo, "worktree", "remove", "--force", str(path))
        original["state"] = "removed"
        obj.save(original)
    elif kind in {"holder", "holder-clean"}:
        duplicate["holder"] = holder()
        if kind == "holder-clean":
            (path / "unfinished").unlink()
    elif kind == "worker-clean":
        duplicate["worker"] = holder()
        (path / "unfinished").unlink()
    elif kind == "distinct":
        other = legacy(obj, "different")
        (other / "unfinished").write_text("another worktree")
        duplicate["path"] = str(other)
    obj.save(duplicate)
    journal_path = obj.state / "migration.json"
    journal = json.loads(journal_path.read_text())
    row = copy.deepcopy(journal["candidates"][0])
    row.update(id=duplicate["id"], path=duplicate["path"], phase="parked")
    journal["candidates"].append(row)
    if kind == "missing":
        journal["candidates"][0]["phase"] = "removed"
    write_json(journal_path, journal)
    held_before = (obj.state / "records" / f"{duplicate['id']}.json").read_bytes()
    result = obj.maintain() if operation == "maintenance" else obj.migrate(apply=True)
    repaired = obj.get(duplicate["id"])
    if kind in {"existing", "missing"}:
        assert repaired["state"] == "retired-duplicate"
        assert result["duplicates_retired"] == 1
        assert repaired["duplicate_of"] == original["id"]
        journal = json.loads(journal_path.read_text())
        assert next(r for r in journal["candidates"] if r["id"] == duplicate["id"])["phase"] == "retired-duplicate"
        assert next(r for r in obj.snapshot()["records"] if r["id"] == duplicate["id"])["state"] == "retired-duplicate"
        again = obj.maintain() if operation == "maintenance" else obj.migrate(apply=True)
        assert again["duplicates_retired"] == 0
    elif kind in {"holder", "holder-clean", "worker-clean"}:
        assert (obj.state / "records" / f"{duplicate['id']}.json").read_bytes() == held_before
    else:
        assert repaired["state"] == "parked" and result["duplicates_retired"] == 0
    if kind in {"holder-clean", "worker-clean"}:
        assert path.is_dir()
    elif kind != "missing":
        assert (path / "unfinished").read_text() == "preserve this work"
    else:
        assert not path.exists()


def test_repair_uses_injected_identity_and_preserves_correct_clone(migration, tmp_path: Path, monkeypatch) -> None:
    import worktree_retirement as module

    obj = migration
    clone = tmp_path / "other"
    git(obj.repo, "clone", "--branch", "main", obj.url, str(clone))
    path = legacy(obj, "clone-work", source=clone)
    (path / "unfinished").write_text("keep")
    obj.migrate(apply=True)
    original = obj.read_records()[0]
    duplicate = copy.deepcopy(original)
    duplicate.update(id="w-" + "0" * 32, path=str(path.with_name("synthetic-alias")))
    duplicate["repo"] = {"remote": obj.url, "source_clone": str(obj.repo), "common_dir": str(obj.repo / ".git")}
    duplicate["migration"]["origins"] = ["namespace-filesystem"]
    obj.save(duplicate)
    identity = module.path_identity
    monkeypatch.setattr(module, "path_identity", lambda p: identity(path) if p == Path(duplicate["path"]) else identity(p))
    result = obj.maintain()
    assert result["duplicates_retired"] == 1
    assert obj.get(duplicate["id"])["state"] == "retired-duplicate"
    assert obj.get(original["id"])["repo"]["source_clone"] == str(clone)
    assert (path / "unfinished").read_text() == "keep"


@pytest.mark.parametrize("operation", ["maintenance", "migrate"])
def test_removed_migrated_record_does_not_retire_reused_live_path(migration, operation: str) -> None:
    obj = migration
    path = legacy(obj, "reused-agent")
    obj.migrate(apply=True)
    old = obj.read_records()[0]
    assert old["state"] == "removed"
    git(obj.repo, "worktree", "add", "-b", "current-task", str(path), "origin/main")
    (path / "unfinished").write_text("current work")
    current = copy.deepcopy(old)
    current.update(id="w-" + "e" * 32, state="released")
    current.pop("migration")
    obj.save(current)
    result = obj.maintain() if operation == "maintenance" else obj.migrate(apply=True)
    assert result["duplicates_retired"] == 1
    assert obj.get(current["id"])["state"] != "retired-duplicate"
    assert obj.get(old["id"])["state"] == "retired-duplicate"
    assert (path / "unfinished").read_text() == "current work"
