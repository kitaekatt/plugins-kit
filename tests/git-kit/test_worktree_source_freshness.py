"""Run: uv run --extra dev pytest tests/git-kit/test_worktree_source_freshness.py -q.

Triggers: migrated records from a clone with stale upstream objects, operation-local
fetch caching, and fetch failures that must preserve work with a clear reason.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import worktrees as wt
from test_worktree_lifecycle import git, holder, lifecycle
from test_worktree_discard import approve
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


@pytest.mark.parametrize("action", ["discard", "discard-apply", "release", "adopt", "take"])
@pytest.mark.parametrize("fetch_fails", [False, True], ids=["fresh", "fetch-refused"])
def test_record_operations_fetch_own_clone(two_clones, monkeypatch, action: str, fetch_fails: bool) -> None:
    obj, clone, records, newest = two_clones
    record = next(r for r in records if r["repo"]["source_clone"] == str(clone))
    record["state"] = "ready" if action == "take" else "parked"
    if action == "take":
        for other in obj.read_records():
            if other["id"] != record["id"] and other["state"] == "ready":
                other["state"] = "parked"
                obj.save(other)
    if action == "release":
        record.update(state="taken", holder={"agent": "foreign", "session": "s", "claim": "c-foreign", **holder()})
    obj.save(record)
    approved = approve(obj.discard(record["id"])["proposal"]) if action == "discard-apply" else None
    if fetch_fails:
        git(clone, "config", "remote.origin.uploadpack", "missing-upload-pack-for-test")
    fetches = []
    real_git = wt.git

    def observe(path: Path, *args: str, **kwargs) -> str:
        if path == clone and args[0] == "fetch":
            fetches.append(args)
        return real_git(path, *args, **kwargs)

    monkeypatch.setattr(wt, "git", observe)

    def run() -> dict:
        if action == "discard-apply":
            return obj.discard(record["id"], approved=approved)
        if action == "discard":
            proposal = obj.discard(record["id"])["proposal"]
            assert git(clone, "rev-parse", "origin/main") == newest
            assert len(fetches) == 1
            return obj.discard(record["id"], approved=approve(proposal))
        if action == "release":
            return obj.release("c-foreign")
        if action == "adopt":
            return obj.adopt(record["id"], "foreign-owner", "s", process=holder())
        return obj.take("foreign-owner", "s", process=holder())

    if fetch_fails:
        with pytest.raises(ValueError, match="Source clone upstream fetch/resolve failed") as refused:
            run()
        assert str(clone) in str(refused.value)
        assert "missing-upload-pack-for-test" in str(refused.value)
        assert Path(obj.get(record["id"])["path"]).exists()
        assert not obj.get(record["id"])["safety"]["safe"]
        if action == "release":
            assert obj.get(record["id"])["holder"]["claim"] == "c-foreign"
        assert "unknown commit" not in str(refused.value)
    else:
        result = run()
        assert git(clone, "rev-parse", "origin/main") == newest
        if action in {"discard", "discard-apply", "release"}:
            assert result["state"] == ("released" if action == "release" else "removed")
        if action == "take":
            assert result["head"] == newest
    assert len(fetches) == (2 if action == "discard" and not fetch_fails else 1)


def test_recycle_refreshes_record_source(two_clones) -> None:
    obj, clone, records, newest = two_clones
    record = next(r for r in records if r["repo"]["source_clone"] == str(clone))
    obj.prepare(record, recycle=True)
    current = obj.get(record["id"])
    assert current["state"] == "ready"
    assert git(Path(current["path"]), "rev-parse", "HEAD") == newest
    assert git(clone, "rev-parse", "origin/main") == newest


@pytest.mark.parametrize("fetch_fails", [False, True], ids=["fresh", "fetch-refused"])
def test_migration_fetches_each_clone_once(migration, tmp_path: Path, monkeypatch, fetch_fails: bool) -> None:
    obj = migration
    clone = tmp_path / "clone-b"
    git(obj.repo, "clone", "--branch", "main", obj.url, str(clone))
    paths = [legacy(obj, name, source=clone) for name in ("foreign-one", "foreign-two")]
    if fetch_fails:
        git(clone, "config", "remote.origin.uploadpack", "missing-upload-pack-for-test")
    fetches = []
    real_git = wt.git

    def observe(path: Path, *args: str, **kwargs) -> str:
        if path == clone and args[0] == "fetch":
            fetches.append(args)
        return real_git(path, *args, **kwargs)

    monkeypatch.setattr(wt, "git", observe)
    result = obj.migrate(apply=True, clones=[clone])
    assert result["phase"] == "complete"
    for record in obj.read_records():
        if Path(record["path"]) not in paths:
            continue
        assert record["state"] == ("parked" if fetch_fails else "removed")
        if fetch_fails:
            assert not record["safety"]["safe"]
            assert "missing-upload-pack-for-test" in " ".join(record["safety"]["blockers"])
    assert len(fetches) == 1
