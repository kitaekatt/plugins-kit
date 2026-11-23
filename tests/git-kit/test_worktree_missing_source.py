"""A deleted source clone. Run: uv run --extra dev pytest tests/git-kit/test_worktree_missing_source.py -q.

Triggers: a recorded source clone and its worktree path are deleted from disk;
records, status, take, maintenance and discard must keep working without it.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from test_worktree_discard import approve
from test_worktree_lifecycle import git, holder, lifecycle  # noqa: F401
from test_worktree_migration import legacy, migration  # noqa: F401
from test_worktree_recovery import park


@pytest.fixture
def gone_clone(migration, tmp_path: Path) -> tuple:
    """A migrated, parked record whose source clone and worktree no longer exist."""
    obj = migration
    clone = tmp_path / "Clone5"
    git(obj.repo, "clone", "--branch", "main", obj.url, str(clone))
    path = legacy(obj, "gone-work", source=clone)
    (path / "unfinished").write_text("kept during migration", encoding="utf-8")
    obj.migrate(apply=True, clones=[clone])
    record = next(r for r in obj.read_records() if r["path"] == str(path))
    assert record["state"] == "parked"
    shutil.rmtree(path)
    shutil.rmtree(clone)
    return obj, clone, record


def sources(obj) -> list[str]:
    return [s["path"] for s in json.loads((obj.state / "namespace.json").read_text())["sources"]]


def test_discard_retires_record_whose_clone_and_path_are_gone(gone_clone) -> None:
    obj, clone, record = gone_clone
    proposal = obj.discard(record["id"])["proposal"]
    lost = proposal["lost_work"]
    assert lost["absent"] is True and lost["files"] == [] and lost["commits"] == []
    assert "Nothing remains on disk to lose" in lost["statement"]
    with pytest.raises(ValueError, match="user direction"):
        obj.discard(record["id"], approved=proposal)
    result = obj.discard(record["id"], approved=approve(proposal))
    assert result["state"] == "removed"
    saved = obj.get(record["id"])
    assert saved["discard"]["outcome"] == "removed" and saved["discard"]["user_direction"]["actor"] == "user"
    assert not clone.exists() and not Path(record["path"]).exists()


def test_status_take_and_maintenance_survive_missing_clone(gone_clone) -> None:
    obj, clone, record = gone_clone
    # A lifecycle record (not a migration mapping) that names the deleted clone.
    other = park(obj, agent="moved", session="moved-session")
    other["repo"] = {**other["repo"], "source_clone": str(clone), "common_dir": str(clone / ".git")}
    obj.save(other)
    # A ready spare that names the deleted clone; it sorts first, so take meets it first.
    ready = {**obj.get(other["id"]), "id": "w-" + "0" * 32, "state": "ready", "holder": None,
             "path": str(obj.namespace / "ready" / ("w-" + "0" * 32))}
    obj.save(ready)

    rows = {r["id"]: r for r in obj.status()["records"]}
    for identifier in (record["id"], other["id"], ready["id"]):
        assert "Source clone" in rows[identifier]["observed"]["git_blockers"][0]

    result = obj.maintain()
    lines = [line for line in result["failures"] if line.get("source_clone") == str(clone)]
    assert len(lines) == 1
    assert set(lines[0]["records"]) == {record["id"], other["id"], ready["id"]}
    assert str(clone) in sources(obj)  # Live records still name it.
    assert obj.get(ready["id"])["state"] == "parked"
    assert (Path(other["path"]) / "unfinished").read_text() == "work to recover"

    taken = obj.take("after-gone", "s", process=holder())
    assert taken["id"] not in {record["id"], other["id"], ready["id"]}

    proposal = obj.discard(record["id"])["proposal"]
    obj.discard(record["id"], approved=approve(proposal))
    proposal = obj.discard(ready["id"])["proposal"]
    obj.discard(ready["id"], approved=approve(proposal))
    other = obj.get(other["id"])
    other["repo"] = {**other["repo"], "source_clone": str(obj.repo),
                     "common_dir": git(obj.repo, "rev-parse", "--path-format=absolute", "--git-common-dir")}
    obj.save(other)
    result = obj.maintain()
    lines = [line for line in result.get("failures", []) if line.get("source_clone")]
    assert len(lines) == 1 and lines[0]["records"] == [] and "dropped" in lines[0]["reason"]
    assert str(clone) not in sources(obj) and str(obj.repo) in sources(obj)
    assert not [line for line in obj.maintain().get("failures", []) if line.get("source_clone")]
