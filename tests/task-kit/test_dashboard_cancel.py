"""Cancel run: process control on an immutable run id.

Cancel carries no expected_revision (the task revision moves while a worker runs). The
server validates the run's own state under the scheduler lock: queued, starting,
running and cancelling runs accept it; every other state answers 409 not-cancellable.
A run that was cancelled ends ``cancelled`` -- retryable, with a partial-work note, and
NOT a task block -- unless something survived the stop or the ledger could not be closed.

RED CHECK (failed against the Increment 2 server, where the route was a 404): cancel after a
real receipt -- the receipt wins; a cancel request never turns a verdict into a cancellation.
"""

from __future__ import annotations

import json
from pathlib import Path

import psutil
import pytest
from rig_support import Rig, alive, runners_of, wait_for

from task_system.dashboard import proctree, runtime, scheduler


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


def _running(rig: Rig, **options) -> runtime.Run:
    run_id = rig.queue("do-it", options or {"timeout_s": 120})
    run = rig.tick_until(run_id, (runtime.RUNNING,))
    assert wait_for((Path(run.run_dir) / "grandchild.pid").is_file), "the fake agent never started"
    return run


def test_cancelling_a_running_run_stops_its_whole_tree_and_closes_the_ledger(make):
    rig = make(behavior="hang")
    run = _running(rig)
    gc = int((Path(run.run_dir) / "grandchild.pid").read_text(encoding="utf-8"))
    agent = psutil.Process(gc).ppid()
    status, _h, body = rig.cancel(run.id)  # no expected_revision, by design
    assert status == 202, body
    assert body["run"]["state"] == "cancelling" and not body["run"]["cancellable"]
    assert (Path(run.run_dir) / "cancel.json").is_file()

    done = rig.tick_until(run.id, runtime.TERMINAL_STATES)

    assert (done.state, done.code) == (runtime.CANCELLED, "cancelled"), done
    assert "partial work" in done.detail.lower()
    assert wait_for(lambda: not alive(agent) and not alive(gc)), "the cancelled agent tree is still running"
    assert wait_for(lambda: not alive(run.runner_pid))
    assert rig.ledger(run.id)["jobs"][0]["state"] == "cancelled"
    (call,) = rig.cancel_calls(run.id)
    assert call["reason"] == "cancelled from the dashboard"
    result = rig.read_json(run.id, "result.json")
    assert result["outcome"] == "cancelled" and result["survivors"] == []
    assert result["cancel"]["command"]["exit_code"] == 0
    # A cancelled run is not a block: the task and the item are exactly as they were.
    assert rig.task_status() == "active" and "do-it" in rig.plan_item_ids()
    assert rig.db.runs((runtime.ACTIVE_STATES)) == []  # the lease is free


def test_a_cancelled_run_is_retryable_and_the_retry_is_a_fresh_run(make):
    rig = make(behavior="hang")
    run = _running(rig)
    rig.cancel(run.id)
    done = rig.tick_until(run.id, runtime.TERMINAL_STATES)
    assert done.state == runtime.CANCELLED
    status, _h, listing = rig.srv.json("GET", "/api/v1/dashboard")
    row = next(r for r in listing["recent_runs"] if r["id"] == run.id)
    assert row["state"] == "cancelled" and row["retryable"] and not row["cancellable"]
    status, _h, retried = rig.srv.post(f"/api/v1/runs/{run.id}/retry", {"expected_revision": rig.revision()})
    assert status == 202 and retried["run"]["retry_of"] == run.id and retried["run"]["id"] != run.id


def test_cancelling_a_queued_run_needs_no_ledger_and_no_directory(make):
    rig = make(behavior="hang")
    first = _running(rig)
    second_id = rig.queue("later-one", {"timeout_s": 120})
    assert rig.run(second_id).state == runtime.QUEUED
    status, _h, body = rig.cancel(second_id)
    assert status == 202 and body["run"]["state"] == "cancelled"
    second = rig.run(second_id)
    assert second.state == runtime.CANCELLED and second.ended_at
    assert not rig.run_dir(second_id).exists()  # nothing was ever created for it
    assert rig.run(first.id).state == runtime.RUNNING  # the running neighbour is untouched
    rig.tick_until(second_id, (runtime.CANCELLED,))  # and it never starts
    assert rig.run(second_id).state == runtime.CANCELLED


@pytest.mark.parametrize("state", [runtime.ACCEPTED, runtime.FAILED, runtime.CRASHED, runtime.CANCELLED, runtime.BLOCKED])
def test_cancel_of_a_run_that_is_not_live_is_409_with_its_state(make, state):
    rig = make(behavior="hang")
    run_id = rig.queue("do-it")
    rig.db.set_state(run_id, state, code="x", detail="y")
    status, _h, body = rig.cancel(run_id)
    assert status == 409 and body["error"]["code"] == "not-cancellable"
    assert state in body["error"]["message"]
    assert rig.run(run_id).state == state


def test_cancel_of_an_unknown_run_is_404(make):
    rig = make()
    status, _h, body = rig.cancel("nosuchrun")
    assert status == 404 and body["error"]["code"] == "unknown_run"


def test_a_second_cancel_is_accepted_and_changes_nothing(make):
    rig = make(behavior="hang")
    run = _running(rig)
    assert rig.cancel(run.id)[0] == 202
    marker = (Path(run.run_dir) / "cancel.json").read_bytes()
    status, _h, body = rig.cancel(run.id)
    assert status == 202 and body["run"]["state"] == "cancelling"
    assert (Path(run.run_dir) / "cancel.json").read_bytes() == marker
    assert rig.tick_until(run.id, runtime.TERMINAL_STATES).state == runtime.CANCELLED
    assert len(rig.cancel_calls(run.id)) == 1


def test_cancel_needs_a_post(make):
    rig = make()
    run_id = rig.queue("do-it")
    status, _h, body = rig.srv.json("GET", f"/api/v1/runs/{run_id}/cancel")
    assert status == 405


def test_the_receipt_wins_when_a_cancel_arrives_after_the_job_already_has_a_verdict(make):
    """RED CHECK: cancel after a real receipt. The accepted verdict stands; nothing is cancelled."""
    rig = make(behavior="done")
    run_id = rig.queue("do-it", {"timeout_s": 120})
    rig.srv.scheduler.tick()  # dispatch only; the DB still says running
    run = rig.run(run_id)
    run_dir = Path(run.run_dir)
    assert run.state == runtime.RUNNING
    assert wait_for(lambda: (run_dir / "result.json").is_file(), 60)
    assert (run_dir / "receipt.json").is_file()
    status, _h, body = rig.cancel(run_id)
    assert status == 202  # the run was still `running` as far as the server knew
    done = rig.tick_until(run_id, runtime.TERMINAL_STATES)
    assert done.state == runtime.ACCEPTED, done
    assert rig.cancel_calls(run_id) == []
    assert "do-it" not in rig.plan_item_ids()  # the work the agent did is the work that was accepted


def test_the_receipt_wins_over_a_cancelled_outcome_too(make):
    """The runner stopped the tree for a cancel, but acceptance had already written its (accepting) receipt."""
    rig = make(behavior="hang-after-done")
    run_id = rig.queue("do-it", {"timeout_s": 120})
    run = rig.tick_until(run_id, (runtime.RUNNING,))
    assert wait_for(lambda: (Path(run.run_dir) / "receipt.json").is_file())
    rig.cancel(run_id)
    done = rig.tick_until(run_id, runtime.TERMINAL_STATES)
    assert done.state == runtime.ACCEPTED, done  # accepted before the cancel stopped the lingering agent
    assert rig.read_json(run_id, "result.json")["outcome"] == "cancelled"


def test_a_timeout_also_closes_the_ledger_as_cancelled_with_the_run_wide_reason(make):
    rig = make(behavior="hang", slack_s=0.0)
    run_id = rig.queue("do-it", {"timeout_s": 1})
    done = rig.tick_until(run_id, runtime.TERMINAL_STATES)
    assert (done.state, done.code) == (runtime.TIMEOUT, "timeout"), done
    assert rig.ledger(run_id)["jobs"][0]["state"] == "cancelled"
    assert [c["reason"] for c in rig.cancel_calls(run_id)] == ["run-wide timeout"]
    assert rig.task_status() == "active"


def test_a_cancel_whose_command_fails_blocks_and_says_so(make):
    rig = make(behavior="hang")
    rig.edge.cancel_argv = lambda store, run_id, reason: [__import__("sys").executable, "-c", "raise SystemExit(2)"]
    run = _running(rig)
    rig.cancel(run.id)
    done = rig.tick_until(run.id, runtime.TERMINAL_STATES)
    assert (done.state, done.code) == (runtime.CRASHED, "cancel-failed"), done
    assert rig.task_status() == "blocked"


def test_cancel_before_the_runner_dispatches_leaves_no_command_and_no_ledger(make, monkeypatch):
    """cancel.json already there when the runner starts: it never starts the command."""
    rig = make(behavior="hang")
    real = scheduler.Scheduler._default_spawn

    def spawn_after_cancel(argv, run_dir):
        (Path(run_dir) / "cancel.json").write_text(json.dumps({"reason": "cancelled from the dashboard"}), encoding="utf-8")
        real(argv, run_dir)

    rig.srv.scheduler._spawn = spawn_after_cancel
    run_id = rig.queue("do-it", {"timeout_s": 120})
    done = rig.tick_until(run_id, runtime.TERMINAL_STATES)
    assert done.state == runtime.CANCELLED, done
    assert not (rig.run_dir(run_id) / "fake-ledger.json").exists() and not (rig.run_dir(run_id) / "child.json").exists()
    assert rig.read_json(run_id, "result.json")["cancel"]["command"] == {"skipped": "no-dispatch"}


def test_cancel_is_offered_whether_or_not_llm_kit_is_available(make):
    rig = make(behavior="hang")
    run = _running(rig)
    rig.edge.state = "absent"  # llm-kit went away while the run was live
    status, _h, body = rig.cancel(run.id)
    assert status == 202
    caps = rig.srv.json("GET", "/api/v1/dashboard")[2]["capabilities"]
    assert "cancel" in caps["mutations"]["actions"]
