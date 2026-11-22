"""Run: uv run --extra dev pytest tests/git-kit/test_worktree_source_freshness.py -q.

Triggers: maintenance of migrated records from a clone with stale upstream objects,
and a source-clone fetch failure while other records can still be retired.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import worktrees as wt
from test_worktree_lifecycle import git, lifecycle
from test_worktree_migration import legacy, migration


@pytest.fixture
def two_clones(migration, tmp_path: Path):
    obj = migration
    clone = tmp_path / "clone-b"
    git(obj.repo, "clone", "--branch", "main", obj.url, str(clone))
    paths = [legacy(obj, "foreign-one", source=clone),
             legacy(obj, "foreign-two", source=clone), legacy(obj, "configured")]
    for path in paths:
        (path / "unfinished").write_text("keep during migration")
    obj.migrate(apply=True, clones=[clone])
    obj.maintain()  # Fill the ready pool before making retirement candidates safe.
    records = [r for r in obj.read_records() if Path(r["path"]) in paths]
    for record in records:
        (Path(record["path"]) / "unfinished").unlink()
        record["state"] = "released"
        obj.save(record)
    (obj.repo / "new-upstream").write_text("published after clone B was created")
    git(obj.repo, "add", "new-upstream")
    git(obj.repo, "commit", "-qm", "advance upstream")
    git(obj.repo, "push", "origin", "main")
    newest = git(obj.repo, "rev-parse", "HEAD")
    with pytest.raises(subprocess.CalledProcessError):
        git(clone, "cat-file", "-e", newest)
    return obj, clone, records, newest


def test_maintenance_fetches_record_source_once_per_pass(two_clones, monkeypatch) -> None:
    obj, clone, records, newest = two_clones
    real_git = wt.git
    fetches = []

    def observe(path: Path, *args: str, **kwargs) -> str:
        if path == clone and args[0] == "fetch":
            fetches.append(args)
        return real_git(path, *args, **kwargs)

    monkeypatch.setattr(wt, "git", observe)
    result = obj.maintain()
    assert result["outcome"] == "complete"
    assert {r["id"] for r in records} <= set(result["removed"])
    assert all(not Path(r["path"]).exists() for r in records)
    assert git(clone, "rev-parse", "origin/main") == newest
    assert len(fetches) == 1


def test_maintenance_parks_only_records_with_failed_source_fetch(two_clones, monkeypatch) -> None:
    obj, clone, records, _ = two_clones
    # Keep the remote identity valid while making its real local transport fail.
    git(clone, "config", "remote.origin.uploadpack", "missing-upload-pack-for-test")
    real_git = wt.git
    fetches = []

    def observe(path: Path, *args: str, **kwargs) -> str:
        if path == clone and args[0] == "fetch":
            fetches.append(args)
        return real_git(path, *args, **kwargs)

    monkeypatch.setattr(wt, "git", observe)
    result = obj.maintain()
    assert result["outcome"] == "complete"
    for prior in records:
        record = obj.get(prior["id"])
        if record["repo"]["source_clone"] == str(clone):
            assert record["state"] == "parked" and Path(record["path"]).exists()
            assert not record["safety"]["safe"]
            blockers = " ".join(record["safety"]["blockers"])
            assert str(clone) in blockers and "fetch" in blockers
            assert "missing-upload-pack-for-test" in blockers
        else:
            assert record["state"] == "removed" and record["id"] in result["removed"]
    assert len(fetches) == 1
    # A failed result is cached only for this pass; the next pass can recover.
    git(clone, "config", "--unset", "remote.origin.uploadpack")
    again = obj.maintain()
    assert all(obj.get(r["id"])["state"] == "removed" for r in records)
    assert again["parked_recheck"]["removed"] == 2
    assert len(fetches) == 2
