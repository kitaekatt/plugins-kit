"""Run: uv run --extra dev pytest tests/git-kit/test_worktree_parked_recheck.py -q.

Triggers: current rebuildable settings, delivery elsewhere, claims, and changes
between the durable retirement reservation and the final safety observation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from test_worktree_lifecycle import deliver, git, holder, lifecycle, write_json
from test_worktree_migration import legacy, migration


def parked(obj, name: str = "parked") -> dict:
    path = legacy(obj, name)
    (path / "unfinished").write_text("work")
    obj.migrate(apply=True)
    return next(r for r in obj.read_records() if r["path"] == str(path))


@pytest.mark.parametrize("operation", ["maintenance", "migrate"])
def test_parked_recheck_uses_current_rebuildable_settings(migration, operation: str) -> None:
    import worktree_lifecycle as lc

    obj = migration
    ignore = obj.repo / ".gitignore"
    ignore.write_text(ignore.read_text() + "devtools/dist/\n*.tsbuildinfo\n")
    git(obj.repo, "add", ".gitignore")
    git(obj.repo, "commit", "-qm", "ignore build output")
    git(obj.repo, "push", "origin", "main")
    path = legacy(obj, "old-outputs")
    (path / "devtools/dist").mkdir(parents=True)
    (path / "devtools/dist/index.js").write_text("compiled")
    (path / "build.tsbuildinfo").write_text("cache")
    obj.migrate(apply=True)
    record = obj.read_records()[0]
    assert record["state"] == "parked" and not record["safety"]["safe"]
    config = obj.settings.raw
    config["pool"]["rebuildable"] = ["devtools/dist/**", "**/*.tsbuildinfo"]
    write_json(obj.repo / ".claude/bootstrap.json", {"git_kit": {"worktrees": config}})
    deliver(obj)
    obj = lc.Lifecycle(obj.repo)
    result = obj.maintain() if operation == "maintenance" else obj.migrate(apply=True)
    assert not path.exists(), result
    assert obj.get(record["id"])["state"] == "removed"
    assert result["parked_recheck"] == {"rechecked": 1, "removed": 1, "still_parked": 0, "by_reason": {}}
    journal = json.loads((obj.state / "migration.json").read_text())
    assert journal["candidates"][0]["phase"] == "removed"
    again = obj.maintain() if operation == "maintenance" else obj.migrate(apply=True)
    assert again["parked_recheck"]["rechecked"] == 0


def test_parked_work_delivered_elsewhere_is_removed(migration) -> None:
    obj = migration
    record = parked(obj)
    path = Path(record["path"])
    git(path, "add", "unfinished")
    git(path, "commit", "-qm", "work")
    blocked = obj.maintain()
    assert path.exists()
    assert blocked["parked_recheck"]["by_reason"]["commits outside fresh upstream"] == 1
    git(path, "push", "origin", "HEAD:main")
    result = obj.maintain()
    assert not path.exists() and result["parked_recheck"]["removed"] == 1


def test_ordinary_parked_record_is_removed_without_recycling(migration) -> None:
    obj = migration
    obj.maintain()
    taken = obj.take("agent", "session", process=holder())
    record = obj.get(taken["id"])
    record.update(state="parked", holder=None)
    obj.save(record)
    path = Path(record["path"])
    result = obj.maintain()
    assert not path.exists()
    assert obj.get(record["id"])["state"] == "removed"
    assert result["removed"] == [record["id"]]
    assert result["parked_recheck"]["removed"] == 1


@pytest.mark.parametrize("blocker", ["holder", "worker", "dirty", "ignored", "process", "uncertain"])
def test_parked_recheck_preserves_unsafe_and_claimed_records(migration, monkeypatch, blocker: str) -> None:
    import worktree_migration as module

    obj = migration
    record = parked(obj)
    path = Path(record["path"])
    if blocker != "dirty":
        (path / "unfinished").unlink()
    if blocker in {"holder", "worker"}:
        record[blocker] = holder()
        obj.save(record)
    elif blocker == "ignored":
        (path / "local.secret").write_text("keep")
    elif blocker == "process":
        record["processes"] = [holder()]
        obj.save(record)
    elif blocker == "uncertain":
        monkeypatch.setattr(module, "evidence", lambda *args: (_ for _ in ()).throw(ValueError("uncertain evidence")))
    before = (obj.state / "records" / f"{record['id']}.json").read_bytes()
    result = obj.maintain()
    assert path.exists()
    summary = result["parked_recheck"]
    assert summary["removed"] == 0 and summary["still_parked"] == 1 and summary["by_reason"]
    if blocker in {"holder", "worker"}:
        assert summary["rechecked"] == 0
        assert (obj.state / "records" / f"{record['id']}.json").read_bytes() == before


@pytest.mark.parametrize("change", ["dirty", "claude", "uncertain-claude"])
def test_parked_removal_rechecks_after_reservation(migration, monkeypatch, change: str) -> None:
    import worktree_migration as module

    obj = migration
    record = parked(obj)
    path = Path(record["path"])
    (path / "unfinished").unlink()
    save = obj.save
    reservations = []

    def reserve(value: dict) -> None:
        save(value)
        if value["operation"].get("name") == "parked-recheck" and value["operation"]["phase"] == "reserved":
            reservations.append(value["id"])
            if change == "dirty":
                (path / "new-work").write_text("arrived during reservation")
            else:
                message = "Claude candidate live process/runtime" if change == "claude" else "Claude uncertain process evidence"
                monkeypatch.setattr(module, "process_check", lambda rows: (_ for _ in ()).throw(ValueError(message)))

    monkeypatch.setattr(obj, "save", reserve)
    result = obj.maintain()
    assert reservations == [record["id"]]
    assert path.exists() and obj.get(record["id"])["state"] == "parked"
    assert result["parked_recheck"]["removed"] == 0
    assert result["parked_recheck"]["by_reason"]


def test_parked_recheck_recovers_after_removal_before_record_save(migration, monkeypatch) -> None:
    obj = migration
    record = parked(obj)
    path = Path(record["path"])
    (path / "unfinished").unlink()
    save = obj.save

    def crash(value: dict) -> None:
        if value["id"] == record["id"] and value["state"] == "removed":
            raise OSError("interrupted after Git removal")
        save(value)

    monkeypatch.setattr(obj, "save", crash)
    with pytest.raises(OSError, match="interrupted after Git removal"):
        obj.maintain()
    assert not path.exists()
    monkeypatch.setattr(obj, "save", save)
    reserved = obj.get(record["id"])
    assert reserved["operation"]["phase"] == "reserved"
    reserved["worker"] = {"pid": 99999999, "start": "exited worker"}
    obj.save(reserved)
    result = obj.maintain()
    assert obj.get(record["id"])["state"] == "removed"
    assert result["parked_recheck"]["removed"] == 1
    journal = json.loads((obj.state / "migration.json").read_text())
    assert journal["candidates"][0]["phase"] == "removed"


def test_maintenance_gathers_unsafe_parked_evidence_without_claim_lock(migration, monkeypatch) -> None:
    """Trigger: many parked trees. take, release and status must not wait for every recheck."""
    import worktree_retirement as retirement
    from worktree_pool import _file_lock

    obj = migration
    record = parked(obj)
    real, free = retirement.evidence, []

    def observe(owner, row, **options):
        with _file_lock(owner.state / "claim.lock", blocking=False) as acquired:
            free.append(acquired)
        return real(owner, row, **options)

    monkeypatch.setattr(retirement, "evidence", observe)
    result = obj.maintain()
    assert free and free[0] is True, free
    assert obj.get(record["id"])["state"] == "parked"
    assert result["parked_recheck"]["still_parked"] == 1
