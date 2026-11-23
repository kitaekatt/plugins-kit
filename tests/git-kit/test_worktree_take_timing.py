"""Run: uv run --extra dev pytest tests/git-kit/test_worktree_take_timing.py -q.

Trigger: taking a ready spare alongside many parked records. Count foreground
work through real functions; elapsed time depends on the machine.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

import worktree_safety as safety
from worktree_lifecycle import Lifecycle
from test_worktree_lifecycle import git, holder, lifecycle
from test_worktree_extensions import configured


@pytest.mark.parametrize("empty,changed", [(False, False), (False, True), (True, False)], ids=["ready", "rerun", "empty"])
def test_take_does_no_pool_maintenance_inline(lifecycle: Lifecycle, monkeypatch: pytest.MonkeyPatch,
                                            empty: bool, changed: bool) -> None:
    obj = lifecycle
    if changed:
        # Both selectors own paths; deps is selected only by the changed lock.
        config = obj.settings.raw
        config["pool"]["rerun_files"]["lock.txt"].append("paths")
        obj.configure(config)
        git(obj.repo, "commit", "-qam", "map path-bound step to lock")
        git(obj.repo, "push", "origin", "main")
    obj.maintain()
    spare = obj.read_records()[0]
    if changed:
        (obj.repo / "lock.txt").write_text("two", encoding="utf-8")
        git(obj.repo, "commit", "-qam", "lock changes")
        git(obj.repo, "push", "origin", "main")
    if empty:
        spare["state"] = "parked"
        obj.save(spare)
    for index in range(50):
        record = obj.new_record()
        record.update(state="parked", worker=None,
                      last_holder={"session": "previous"}, waiting="preserve work")
        # Include an extant parked worktree so snapshot's process scan is visible.
        # Other records model paths unavailable since they were parked.
        if index == 0:
            git(obj.repo, "worktree", "add", "--detach", record["path"], "HEAD")
            (Path(record["path"]) / "unfinished").write_text("preserve", encoding="utf-8")
        obj.save(record)

    calls = {}
    for name in ("parked_rows", "snapshot", "setup", "read_records", "record_path"):
        calls[name] = Mock(wraps=getattr(obj, name))
        monkeypatch.setattr(obj, name, calls[name])
    for name in ("path_processes", "remove_outputs"):
        calls[name] = Mock(wraps=getattr(safety, name))
        monkeypatch.setattr(safety, name, calls[name])

    if empty:
        with pytest.raises(ValueError, match="READY_POOL_EMPTY"):
            obj.take("fast", "session", process=holder())
        target_paths = set()
        expected_steps = []
        expected_removals = []
    else:
        result = obj.take("fast", "session", process=holder())
        assert result["id"] == spare["id"] and Path(result["path"]).is_dir()
        target_paths = {Path(spare["path"]), Path(result["path"])}
        expected_steps = ["paths", "deps"] if changed else ["paths"]
        expected_removals = [(Path(result["path"]), ["output"])]
        if changed:
            expected_removals.append((Path(result["path"]), ["deps-output"]))

    assert {
        "parked_scans": calls["parked_rows"].call_count,
        "snapshots": calls["snapshot"].call_count,
        "other_worktree_path_checks": sum(call.args[0]["id"] != spare["id"] or empty
                                          for call in calls["record_path"].call_args_list),
        "other_worktree_process_scans": sum(call.args[0] not in target_paths
                                            for call in calls["path_processes"].call_args_list),
    } == dict.fromkeys(("parked_scans", "snapshots", "other_worktree_path_checks",
                        "other_worktree_process_scans"), 0)
    assert [step.id for call in calls["setup"].call_args_list for step in call.args[1]] == expected_steps
    assert [call.args for call in calls["remove_outputs"].call_args_list] == expected_removals
    # One metadata inventory preserves holder checks and historical parked hints.
    assert calls["read_records"].call_count == 1


def test_take_reports_project_commands_and_maintenance_time_separately(lifecycle: Lifecycle, monkeypatch: pytest.MonkeyPatch) -> None:
    import worktree_lifecycle as lc

    obj = configured(lifecycle, "after_take", "from pathlib import Path; Path('output/ran').write_text(str(Path.cwd()))")
    obj.maintain()
    clock = [0.0]
    monkeypatch.setattr(lc.time, "perf_counter", lambda: clock[0])
    real_extension = lc.extensions.run
    real_request = obj.request_maintenance
    real_setup = obj.setup

    def setup(record: dict[str, Any], selected: list[Any]) -> None:
        real_setup(record, selected)
        clock[0] += 2.0 if selected[0].path_bound else 3.0

    def project_commands(*args: Any, **kwargs: Any) -> None:
        real_extension(*args, **kwargs)
        clock[0] += 5.0

    def maintenance() -> dict[str, Any]:
        outcome = real_request()
        clock[0] += 7.0
        return outcome

    monkeypatch.setattr(lc.extensions, "run", project_commands)
    monkeypatch.setattr(obj, "request_maintenance", maintenance)
    monkeypatch.setattr(obj, "setup", setup)
    (obj.repo / "lock.txt").write_text("two", encoding="utf-8")
    git(obj.repo, "commit", "-qam", "lock changes")
    git(obj.repo, "push", "origin", "main")
    result = obj.take("timed", "session", process=holder())
    assert (Path(result["path"]) / "output/ran").read_text() == result["path"]
    assert result["timings"] == {
        "initialize": 0.0, "holder": 0.0, "claim": 0.0, "freshness": 0.0,
        "branch": 0.0, "path_bound": 2.0, "rerun": 3.0, "after_take": 5.0, "finalize": 0.0,
        "maintenance": 7.0, "total": 17.0,
    }
