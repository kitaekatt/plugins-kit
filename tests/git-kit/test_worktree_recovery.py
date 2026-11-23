"""Recovery contracts; run: uv run --extra dev pytest tests/git-kit/test_worktree_recovery.py -q.

Triggers: concurrent adoption, moved dirty parked work, failed path setup, scoped listings.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from test_worktree_lifecycle import deliver, git, holder, lifecycle


def park(obj, agent: str = "old", session: str = "old-session", task: str | None = "task-one") -> dict:
    obj.maintain()
    taken = obj.take(agent, session, task, process=holder())
    record = obj.get(taken["id"])
    path = Path(record["path"])
    (path / "unfinished").write_text("work to recover", encoding="utf-8")
    record.update(state="parked", last_holder=record["holder"], holder=None,
                  waiting="Review waiting for user")
    obj.save(record)
    return record


def test_adopt_has_one_winner(lifecycle) -> None:
    obj = lifecycle
    record = park(obj)
    scripts = Path(__file__).parents[2] / "plugins/git-kit/scripts"
    entry = f"import sys; sys.path.insert(0, {str(scripts)!r}); from worktree_cli import main; sys.exit(main(sys.argv[1:]))"
    children = [subprocess.Popen([sys.executable, "-c", entry, "adopt", "--repo", str(obj.repo),
                 "--id", record["id"], "--agent", f"winner-{i}", "--session", "new-session",
                 "--holder-pid", str(os.getpid())], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                 text=True) for i in range(4)]
    results = [(child.communicate(timeout=60), child.returncode) for child in children]
    winners = [json.loads(out) for ((out, _), code) in results if code == 0]
    assert len(winners) == 1, results
    current = obj.get(record["id"])
    assert current["holder"]["claim"] == winners[0]["claim"]
    assert all("holder" in err or "claim" in err or "parked" in err for ((_, err), code) in results if code)
    assert (Path(current["path"]) / "unfinished").read_text() == "work to recover"


def test_adopt_with_a_held_agent_id_names_the_holder(lifecycle) -> None:
    """Trigger: adopt reuses the agent ID of another live claim."""
    obj = lifecycle
    record = park(obj)
    obj.maintain()
    busy = obj.take("busy", "busy-session", process=holder())
    with pytest.raises(ValueError, match="already holds a claim") as refused:
        obj.adopt(record["id"], "busy", "new-session", process=holder())
    message = str(refused.value)
    assert "busy-session" in message and busy["path"] in message and busy["claim"] in message
    assert "unique" in message and "--agent" in message
    current = obj.get(record["id"])
    assert current["state"] == "parked" and current["holder"] is None


def test_adopt_preserves_relocated_work(lifecycle) -> None:
    obj = lifecycle
    record = park(obj)
    old = Path(record["path"])
    git(old, "add", "unfinished")
    git(old, "commit", "-qm", "unpublished recovery")
    (old / "unfinished").write_text("later edits", encoding="utf-8")
    (old / "local.secret").write_text("unique ignored data", encoding="utf-8")
    branch, head = git(old, "branch", "--show-current"), git(old, "rev-parse", "HEAD")
    result = obj.adopt(record["id"], "new", "new-session", process=holder())
    path = Path(result["path"])
    assert path != old and not old.exists()
    assert git(path, "rev-parse", "--show-toplevel") == str(path)
    assert git(path, "branch", "--show-current") == branch
    assert git(path, "rev-parse", "HEAD") == head
    assert (path / "unfinished").read_text() == "later edits"
    assert (path / "local.secret").read_text() == "unique ignored data"
    assert (path / "output/cwd").read_text() == str(path)
    current = obj.get(record["id"])
    assert current["setup"]["selected"] == ["paths"]
    assert current["last_holder"]["agent"] == "old" and current["task"] == "task-one"


def test_adopt_preserves_relocated_work_when_setup_fails(lifecycle) -> None:
    obj = lifecycle
    record = park(obj)
    config = obj.settings.raw
    config["pool"]["setup"][1]["argv"] = [sys.executable, "-c", "raise SystemExit(31)"]
    deliver(obj, config)
    with pytest.raises(ValueError, match="kept claim"):
        obj.adopt(record["id"], "failed", "new-session", process=holder())
    current = obj.get(record["id"])
    assert current["holder"]["agent"] == "failed" and current["error"]
    assert current["state"] == "taken"
    assert (Path(current["path"]) / "unfinished").read_text() == "work to recover"


def test_parked_listing_scope(lifecycle) -> None:
    obj = lifecycle
    own = park(obj, "own", "this-session", "own-task")
    same_task = park(obj, "same-task", "other-session", "own-task")
    taskless = park(obj, "taskless", "third-session", None)
    other = park(obj, "other", "fourth-session", "another-task")
    assert {r["id"] for r in obj.parked()["parked"]} == {own["id"], same_task["id"], taskless["id"], other["id"]}
    assert [r["id"] for r in obj.parked(session="this-session")["parked"]] == [own["id"]]
    assert {r["id"] for r in obj.parked(task="own-task")["parked"]} == {own["id"], same_task["id"]}
    obj.maintain()
    taken = obj.take("reader", "this-session", "own-task", process=holder())
    assert {r["id"] for r in taken["parked"]} == {own["id"], same_task["id"]}
    assert taken["other_parked_count"] == 2
    assert all(r["path"] and r["waiting"] and r["last_holder"] for r in taken["parked"])
    assert obj.check_task_close("own-task")["allowed"] is False
    assert len(obj.check_task_close("own-task")["parked"]) == 2
    assert obj.check_task_close("absent-task")["allowed"] is True
    with pytest.raises(ValueError, match="READY_POOL_EMPTY") as refused:
        obj.take("empty-reader", "this-session", "own-task", process=holder())
    message = str(refused.value)
    assert own["id"] in message and same_task["id"] in message
    assert taskless["id"] not in message and other["id"] not in message
    assert '"other_parked_count": 2' in message


@pytest.mark.parametrize("blocking", ["holder", "live", "uncertain"])
def test_adopt_preserves_blocked_work(lifecycle, blocking: str) -> None:
    obj = lifecycle
    record = park(obj)
    if blocking == "holder":
        record["holder"] = {"agent": "present", "session": "s", "claim": "c", **holder()}
    else:
        record["processes"] = [holder() if blocking == "live" else {"pid": 1}]
    obj.save(record)
    with pytest.raises(ValueError, match="holder|process"):
        obj.adopt(record["id"], "blocked", "session", process=holder())
    assert (Path(record["path"]) / "unfinished").read_text() == "work to recover"
    assert obj.get(record["id"])["holder"] == record["holder"]
