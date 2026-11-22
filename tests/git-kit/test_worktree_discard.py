"""User discard contracts. Run: uv run --extra dev pytest tests/git-kit/test_worktree_discard.py -q.

Triggers: explicit loss approval, stale proposals, holders, processes, ambiguous targets.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from test_worktree_lifecycle import git, holder, lifecycle
from test_worktree_recovery import park


def approve(proposal: dict) -> dict:
    return {**proposal, "user_direction": {"actor": "user", "id": proposal["id"],
            "text": "Discard the identified unfinished work in this worktree"}}


def test_discard_requires_targeted_user_direction(lifecycle) -> None:
    obj = lifecycle
    record = park(obj)
    path = Path(record["path"])
    (path / "local.secret").write_text("unique ignored loss", encoding="utf-8")
    proposal = obj.discard(record["id"])["proposal"]
    assert path.exists() and obj.get(record["id"])["state"] == "parked"
    assert "unfinished" in proposal["lost_work"]["untracked"]
    assert "local.secret" in proposal["lost_work"]["ignored"]
    with pytest.raises(ValueError, match="user direction"):
        obj.discard(record["id"], approved=proposal, force=True)
    for direction in ({"actor": "agent", "id": record["id"], "text": "discard"},
                      {"actor": "user", "id": "different", "text": "discard"},
                      {"actor": "user", "id": record["id"], "text": ""}):
        with pytest.raises(ValueError, match="user direction"):
            obj.discard(record["id"], approved={**proposal, "user_direction": direction})
        assert (path / "unfinished").exists()
    result = obj.discard(record["id"], approved=approve(proposal))
    assert result["state"] == "removed" and not path.exists()
    saved = obj.get(record["id"])["discard"]
    assert saved["outcome"] == "removed" and saved["user_direction"]["actor"] == "user"
    assert saved["proposal"]["lost_work"] == proposal["lost_work"]


@pytest.mark.parametrize("blocked", ["holder", "live", "uncertain", "ambiguous", "changed", "symlink"])
def test_discard_preserves_blocked_targets(lifecycle, blocked: str) -> None:
    obj = lifecycle
    record = park(obj)
    path = Path(record["path"])
    proposal = obj.discard(record["id"])["proposal"]
    if blocked == "holder":
        record["holder"] = {"agent": "owner", "session": "s", "claim": "c", **holder()}
        obj.save(record)
    elif blocked in {"live", "uncertain"}:
        record["processes"] = [holder() if blocked == "live" else {"pid": 1}]
        obj.save(record)
    elif blocked == "ambiguous":
        duplicate = copy.deepcopy(record)
        duplicate["id"] = "w-" + "a" * 32
        obj.save(duplicate)
    elif blocked == "changed":
        # Same filename and size: content, not just status, binds the proposal.
        (path / "unfinished").write_text("WORK to recover", encoding="utf-8")
    else:
        moved = path.with_name("outside")
        path.rename(moved)
        path.symlink_to(moved, target_is_directory=True)
    with pytest.raises(ValueError, match="holder|process|[Aa]mbiguous|changed|symlink"):
        obj.discard(record["id"], approved=approve(proposal), force=True)
    assert (path / "unfinished").exists()
    assert obj.get(record["id"])["state"] == "parked"


def test_discard_preserves_changed_index(lifecycle) -> None:
    obj = lifecycle
    record = park(obj)
    path = Path(record["path"])
    original = (path / "lock.txt").read_text()
    (path / "lock.txt").write_text("first staged version", encoding="utf-8")
    git(path, "add", "lock.txt")
    (path / "lock.txt").write_text(original, encoding="utf-8")
    proposal = obj.discard(record["id"])["proposal"]
    (path / "lock.txt").write_text("second staged version", encoding="utf-8")
    git(path, "add", "lock.txt")
    (path / "lock.txt").write_text(original, encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        obj.discard(record["id"], approved=approve(proposal))
    assert git(path, "show", ":lock.txt") == "second staged version"


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_discard_refuses_hidden_tracked_work(lifecycle, flag: str) -> None:
    obj = lifecycle
    record = park(obj)
    path = Path(record["path"])
    git(path, "update-index", flag, "lock.txt")
    (path / "lock.txt").write_text("hidden unique work", encoding="utf-8")
    with pytest.raises(ValueError, match="[Aa]mbiguous"):
        obj.discard(record["id"])
    assert (path / "lock.txt").read_text() == "hidden unique work"


def test_discard_records_outcome_after_remove_crash(lifecycle) -> None:
    obj = lifecycle
    record = park(obj)
    proposal = obj.discard(record["id"])["proposal"]
    record = obj.get(record["id"])
    direction = approve(proposal)["user_direction"]
    record["discard"].update(user_direction=direction, outcome="applying")
    obj.transition(record, "discard", "remove")
    git(obj.repo, "worktree", "remove", "--force", record["path"])
    obj.maintain()
    removed = obj.get(record["id"])
    assert removed["state"] == "removed"
    assert removed["discard"]["outcome"] == "removed"
    assert removed["discard"]["user_direction"] == direction
