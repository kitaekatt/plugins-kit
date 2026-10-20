"""The server dies; the detached runner does not. A new server must adopt it, not fight it.

RED CHECKS (each failed against the Increment 2 scheduler):

- killed in the starting window -- after the runner is spawned, before its identity is
  recorded -- the old ``adopt`` put the run back in the queue, the next dispatch found
  the directory already there, deleted it under the live runner and marked the run failed;
- the same with the runner already finished by the time the server returns;
- a queued second item must start only after the first run is reconciled, with exactly one
  runner alive at any moment.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from rig_support import (
    Rig,
    die_in_await,
    die_in_spawn,
    restore_await,
    runners_of,
    wait_for,
)

from task_system.dashboard import runtime, scheduler


@pytest.fixture
def make(tmp_path, monkeypatch):
    rigs = []

    def build(**kw) -> Rig:
        rig = Rig(tmp_path, monkeypatch, **kw)
        rigs.append(rig)
        return rig

    try:
        yield build
    finally:
        for rig in rigs:
            rig.close()


def _drive_until_settled(rig: Rig, first: str, second: str, timeout_s: float = 90.0) -> None:
    """Tick until both runs are terminal. At every step: one runner at most, and the queued item waits."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        rig.srv.scheduler.tick()
        a, b = rig.run(first), rig.run(second)
        if a.state not in runtime.TERMINAL_STATES:
            assert b.state == runtime.QUEUED, f"the queued item started while {first} was {a.state}"
        live = runners_of(rig.run_dir(first)) + (runners_of(rig.run_dir(second)) if rig.run_dir(second).exists() else [])
        assert len(live) <= 1, f"{len(live)} runners alive at once"
        if a.state in runtime.TERMINAL_STATES and b.state in runtime.TERMINAL_STATES:
            return
        time.sleep(0.1)
    raise AssertionError(f"runs did not settle: {rig.run(first)} / {rig.run(second)}")


@pytest.mark.parametrize("window", ["no-identity-yet", "runner-finished", "running"])
def test_a_server_killed_around_a_healthy_runner_adopts_it_and_the_queued_item_waits(make, monkeypatch, window):
    rig = make(behavior="done" if window == "runner-finished" else "slow-done", sleep_s=3.0)
    first = rig.queue("do-it")
    second = rig.queue("later-one")
    if window == "running":
        rig.srv.scheduler.tick()
        assert rig.run(first).state == runtime.RUNNING
    else:
        die_in_await(monkeypatch, until_result=window == "runner-finished")
        with pytest.raises(BaseException) as died:
            rig.srv.scheduler.tick()
        assert type(died.value).__name__ == "ServerDied"
        assert rig.run(first).state == runtime.STARTING and rig.run(first).runner_pid is None
        restore_await(monkeypatch)
    run_dir = rig.run_dir(first)
    assert rig.run(second).state == runtime.QUEUED
    marker = run_dir / "spec.json"
    before = marker.read_bytes()

    rig.restart()  # the server is gone and back; the runner was never told
    assert run_dir.is_dir() and marker.read_bytes() == before, "the live run's directory was touched"
    _drive_until_settled(rig, first, second)

    done = rig.run(first)
    assert done.state == runtime.ACCEPTED, done
    assert run_dir.is_dir() and (run_dir / "receipt.json").is_file() and (run_dir / "result.json").is_file()
    assert rig.read_json(first, "result.json")["outcome"] == "finished"
    assert rig.run(second).state in runtime.TERMINAL_STATES
    events = [e["kind"] for e in rig.db.events(first)]
    assert events.count("queued") == 1 and "failed" not in events  # never re-queued, never failed


def test_a_server_killed_before_it_spawned_anything_does_not_queue_or_delete_blindly(make, monkeypatch):
    """The directory exists, no runner ever started: not a retry, not a deletion, a loud failure."""
    monkeypatch.setattr(scheduler, "ADOPT_GRACE_S", 0.5, raising=False)
    rig = make(behavior="done")
    run_id = rig.queue("do-it")
    die_in_spawn(rig)
    with pytest.raises(BaseException) as died:
        rig.srv.scheduler.tick()
    assert type(died.value).__name__ == "ServerDied"
    run_dir = rig.run_dir(run_id)
    assert run_dir.is_dir() and not (run_dir / "runner.json").exists()

    rig.restart()
    run = rig.srv.settle(run_id, timeout_s=30)
    assert run.state == runtime.FAILED and run.code == "dispatch-interrupted", run
    assert run_dir.is_dir(), "a directory the new server did not create was deleted"
    assert (run_dir / "cancel.json").is_file()  # a runner that wakes up late refuses to dispatch
    assert rig.db.runs((runtime.QUEUED, runtime.STARTING, runtime.RUNNING)) == []  # no automatic retry
    assert rig.plan_item_ids().count("do-it") == 1  # nothing reported as complete
    assert rig.task_status() == "active"  # nothing ran, so the task is not blocked


def test_a_starting_run_whose_directory_never_existed_goes_back_to_the_queue_and_runs(make):
    rig = make(behavior="done")
    run_id = rig.queue("do-it")
    # The server died between taking the lease and creating the directory.
    assert rig.db.try_lease(run_id, rig.pid, max_projects=2)
    assert not rig.run_dir(run_id).exists()
    rig.restart()
    run = rig.srv.settle(run_id, timeout_s=60)
    assert run.state == runtime.ACCEPTED
    assert [e["kind"] for e in rig.db.events(run_id)].count("accepted") == 1


def test_a_late_runner_that_finds_a_cancel_marker_refuses_to_dispatch(tmp_path):
    """The other half of the grace logic: cancel.json written first means the runner never starts the command."""
    import json
    import sys

    from task_system.dashboard import runner

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    marker = tmp_path / "ran.txt"
    spec = {"project_root": str(tmp_path), "runner": {
        "argv": [sys.executable, "-c", f"open({str(marker)!r}, 'w').write('x')"], "deadline_s": 30}}
    (run_dir / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
    (run_dir / "cancel.json").write_text(json.dumps({"reason": "dispatch-interrupted"}), encoding="utf-8")
    runner.run(run_dir)
    result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    assert result["outcome"] == "cancelled" and result["survivors"] == []
    assert not marker.exists()
    assert not (run_dir / "child.json").exists() and not (run_dir / "llm.out").exists()  # no command process was ever made


def test_a_cancel_asked_in_the_starting_window_is_honoured_by_the_adopted_runner(make, monkeypatch):
    """Cancel before the server has even recorded the runner: the marker waits on disk for the adopting server."""
    rig = make(behavior="slow-done", sleep_s=30)
    run_id = rig.queue("do-it", {"timeout_s": 120})
    die_in_await(monkeypatch)
    with pytest.raises(BaseException):
        rig.srv.scheduler.tick()
    restore_await(monkeypatch)
    assert rig.run(run_id).state == runtime.STARTING
    assert wait_for((rig.run_dir(run_id) / "go").is_file, 30)  # the runner dispatched; only the server missed it
    assert wait_for((rig.run_dir(run_id) / "fake-ledger.json").is_file, 30)  # and the job opened its ledger
    rig.srv.scheduler.cancel(run_id)  # STARTING: the marker is written, the state waits for the runner to be adopted
    assert (rig.run_dir(run_id) / "cancel.json").is_file() and rig.run(run_id).state == runtime.STARTING
    rig.restart()
    done = rig.srv.settle(run_id, timeout_s=60)
    assert done.state == runtime.CANCELLED, done
    assert [e["kind"] for e in rig.db.events(run_id)].count("cancelled") == 1
    assert rig.ledger(run_id)["jobs"][0]["state"] == "cancelled"
    assert wait_for(lambda: not runners_of(rig.run_dir(run_id)))


def test_a_run_directory_this_attempt_did_not_create_is_never_deleted(make):
    """RED CHECK: the old failure path rmtree'd whatever it found at the run's path."""
    rig = make(behavior="done")
    run_id = rig.queue("do-it")
    run_dir = rig.run_dir(run_id)
    run_dir.mkdir(parents=True)
    (run_dir / "evidence.txt").write_text("somebody else's", encoding="utf-8")
    rig.srv.scheduler.tick()
    run = rig.run(run_id)
    assert (run.state, run.code) == (runtime.FAILED, "dispatch-failed"), run
    assert (run_dir / "evidence.txt").read_text(encoding="utf-8") == "somebody else's"
    assert not (run_dir / "runner.json").exists()  # and no runner was started behind it
