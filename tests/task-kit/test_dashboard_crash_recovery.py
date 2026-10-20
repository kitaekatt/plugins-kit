"""A runner that dies is never retried and never reported complete.

RED CHECKS (each failed against the Increment 2 scheduler):

- a killed runner left its agent and the agent's children running (the agent has its own
  session, so only the runner knew them) and the ledger open;
- a receipt.json that could not be read crashed the scheduler tick instead of blocking.

The recovery table in ``scheduler.py`` ("Recovery state table") is exercised row by row.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest
from rig_support import Rig, alive, wait_for

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


def _start_running(rig: Rig, **options) -> runtime.Run:
    run_id = rig.queue("do-it", options or {"timeout_s": 120})
    return rig.tick_until(run_id, (runtime.RUNNING,))


def _agent_and_grandchild(rig: Rig, run: runtime.Run) -> list[int]:
    grandchild = Path(run.run_dir) / "grandchild.pid"
    assert wait_for(grandchild.is_file), "the fake agent never started"
    gc = int(grandchild.read_text(encoding="utf-8"))
    agent = proctree.ident_of(gc).pid and psutil.Process(gc).ppid()
    return [agent, gc]


def _kill_runner(run: runtime.Run) -> None:
    psutil.Process(run.runner_pid).kill()
    assert wait_for(lambda: not alive(run.runner_pid))


def test_a_killed_runner_leaves_no_orphan_the_ledger_ends_cancelled_and_the_task_is_blocked(make):
    rig = make(behavior="hang")
    run = _start_running(rig)
    agent, grandchild = _agent_and_grandchild(rig, run)
    assert alive(agent) and alive(grandchild)
    _kill_runner(run)
    assert alive(agent) and alive(grandchild)  # the agent lives in its own session: the runner's death orphans it

    crashed = rig.tick_until(run.id, runtime.TERMINAL_STATES)

    assert (crashed.state, crashed.code) == (runtime.CRASHED, "runner-crashed"), crashed
    assert wait_for(lambda: not alive(agent) and not alive(grandchild)), "the orphaned agent tree was left running"
    assert rig.ledger(run.id)["jobs"][0]["state"] == "cancelled"
    assert [c["run"] for c in rig.cancel_calls(run.id)] == [run.id]
    assert "runner" in rig.cancel_calls(run.id)[0]["reason"]
    assert rig.task_status() == "blocked"
    assert "do-it" in rig.plan_item_ids()  # nothing was reported complete
    assert "process vanished" in (rig.folder / "log.md").read_text(encoding="utf-8")
    assert rig.db.runs((runtime.QUEUED, runtime.STARTING, runtime.RUNNING, runtime.CANCELLING)) == []  # no retry
    assert len(rig.db.runs()) == 1


def test_a_killed_runner_whose_job_already_finished_with_a_receipt_settles_by_the_receipt(make):
    """Terminal ledger + valid receipt settles, even though the runner never wrote result.json."""
    rig = make(behavior="hang-after-done")
    run_id = rig.queue("do-it", {"timeout_s": 120})
    run = rig.tick_until(run_id, (runtime.RUNNING,))
    assert wait_for(lambda: (Path(run.run_dir) / "receipt.json").is_file())
    _kill_runner(run)
    settled = rig.tick_until(run_id, runtime.TERMINAL_STATES)
    assert settled.state == runtime.ACCEPTED, settled
    assert rig.cancel_calls(run_id) == []  # the ledger was already terminal: nothing to cancel


def test_a_terminal_ledger_without_a_receipt_blocks_for_inspection(make):
    rig = make(behavior="hang")
    run = _start_running(rig)
    _agent_and_grandchild(rig, run)
    _kill_runner(run)
    ledger = rig.ledger(run.id)
    ledger["jobs"][0]["state"] = "rejected"  # llm-kit settled the job; acceptance left no receipt
    (Path(run.run_dir) / "fake-ledger.json").write_text(json.dumps(ledger), encoding="utf-8")
    crashed = rig.tick_until(run.id, runtime.TERMINAL_STATES)
    assert crashed.state == runtime.CRASHED and crashed.code == "ledger-without-receipt", crashed
    assert rig.task_status() == "blocked" and rig.cancel_calls(run.id) == []


def test_a_failed_cancel_command_blocks_instead_of_pretending_the_ledger_is_closed(make):
    rig = make(behavior="hang")
    rig.edge.cancel_argv = lambda store, run_id, reason: [sys.executable, "-c", "import sys; sys.exit(2)"]
    run = _start_running(rig)
    _agent_and_grandchild(rig, run)
    _kill_runner(run)
    crashed = rig.tick_until(run.id, runtime.TERMINAL_STATES)
    assert crashed.state == runtime.CRASHED and crashed.code == "cancel-failed", crashed
    assert rig.ledger(run.id)["jobs"][0]["state"] == "running"  # honestly left open, and said so
    assert rig.task_status() == "blocked"


def test_survivors_after_a_stop_block_and_leave_the_ledger_untouched(make, monkeypatch):
    rig = make(behavior="hang")
    run = _start_running(rig)
    agent, grandchild = _agent_and_grandchild(rig, run)
    _kill_runner(run)
    real = proctree.stop_idents

    def refuses(idents, *, grace_s=5.0):
        real(idents, grace_s=grace_s)
        stuck = proctree.ident_of(os.getpid())
        return [stuck]  # one process that "would not stop"

    monkeypatch.setattr(proctree, "stop_idents", refuses)
    crashed = rig.tick_until(run.id, runtime.TERMINAL_STATES)
    assert crashed.state == runtime.CRASHED and crashed.code == "survivors", crashed
    assert str(os.getpid()) in crashed.detail
    assert rig.ledger(run.id)["jobs"][0]["state"] == "running" and rig.cancel_calls(run.id) == []
    assert rig.task_status() == "blocked"


def test_an_unreadable_receipt_blocks_and_never_reads_as_success(make):
    """RED CHECK: json.loads of a torn receipt used to raise out of the tick."""
    rig = make(behavior="done")
    run_id = rig.queue("do-it", {"timeout_s": 120})
    run_dir = rig.run_dir(run_id)
    rig.srv.scheduler.tick()  # dispatch
    assert wait_for(lambda: (run_dir / "result.json").is_file(), 60)
    (run_dir / "receipt.json").write_text('{"verdict": {"outcome": "acce', encoding="utf-8")
    settled = rig.tick_until(run_id, runtime.TERMINAL_STATES)
    assert settled.state == runtime.CRASHED and settled.code == "receipt-unreadable", settled
    assert rig.task_status() == "blocked"


def test_an_unreadable_result_blocks(make):
    rig = make(behavior="done")
    run_id = rig.queue("do-it", {"timeout_s": 120})
    run_dir = rig.run_dir(run_id)
    rig.srv.scheduler.tick()
    assert wait_for(lambda: (run_dir / "result.json").is_file(), 60)
    (run_dir / "result.json").write_text("{", encoding="utf-8")
    settled = rig.tick_until(run_id, runtime.TERMINAL_STATES)
    assert settled.state == runtime.CRASHED and settled.code == "result-unreadable", settled


def test_the_receipt_is_written_atomically(tmp_path, monkeypatch):
    """The receipt is the only thing that turns a run into a verdict, so it is never visible half written."""
    from task_system.dashboard import acceptance

    seen: list[str] = []
    real_replace = os.replace

    def replace(src, dst):
        seen.append(Path(dst).name)
        assert json.loads(Path(src).read_text(encoding="utf-8"))["schema"] == 1  # complete BEFORE the rename
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", replace)
    path = tmp_path / acceptance.RECEIPT_FILE
    acceptance.write_receipt(path, {"schema": 1, "verdict": {"outcome": "accepted", "code": "accepted", "detail": "x"}})
    assert seen == [acceptance.RECEIPT_FILE] and json.loads(path.read_text(encoding="utf-8"))["schema"] == 1
    assert [p.name for p in tmp_path.iterdir()] == [acceptance.RECEIPT_FILE]  # no temp file left behind


# -- death at every spawn boundary ---------------------------------------------------------------------------


def _wrapper_argv(run_dir: Path, marker: Path, parent: proctree.ProcIdent) -> list[str]:
    from task_system.dashboard import child_exec

    return [sys.executable, str(Path(child_exec.__file__)), str(run_dir), str(parent.pid), repr(parent.create_time), "--",
            sys.executable, "-c", f"open({str(marker)!r}, 'w').write('ran')"]


def _env():
    from dashboard_support import REPO_ROOT, SCRIPTS_DIR

    return dict(os.environ, PYTHONPATH=os.pathsep.join([str(REPO_ROOT / "plugins" / "skills-kit"), str(SCRIPTS_DIR)]),
                _BOOTSTRAP_GUARD_SKIP_REEXEC="1")


def test_the_wrapper_records_itself_before_the_command_and_the_command_waits_for_go(tmp_path):
    """BOUNDARY: the command cannot start, let alone spawn a child, before its identity is on disk."""
    marker = tmp_path / "ran.txt"
    owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        parent = proctree.ident_of(owner.pid)
        wrapper = subprocess.Popen(_wrapper_argv(tmp_path, marker, parent), env=_env())
        child = wait_for(lambda: (tmp_path / "child.json").is_file(), 20)
        assert child, "the wrapper never recorded itself"
        recorded = json.loads((tmp_path / "child.json").read_text(encoding="utf-8"))
        assert recorded["pid"] == wrapper.pid
        assert proctree.live(proctree.ProcIdent(recorded["pid"], recorded["create_time"])) is not None
        time.sleep(0.5)
        assert not marker.exists(), "the command ran before the runner said go"
        (tmp_path / "go").write_text("go", encoding="utf-8")
        assert wrapper.wait(timeout=20) == 0 and marker.read_text(encoding="utf-8") == "ran"
    finally:
        owner.kill()
        owner.wait()


def test_a_wrapper_whose_runner_died_before_go_never_runs_the_command(tmp_path):
    """BOUNDARY: runner dead after Popen, before the handshake -- the command must never start."""
    marker = tmp_path / "ran.txt"
    owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    parent = proctree.ident_of(owner.pid)
    wrapper = subprocess.Popen(_wrapper_argv(tmp_path, marker, parent), env=_env())
    try:
        assert wait_for(lambda: (tmp_path / "child.json").is_file(), 20)
        owner.kill()
        owner.wait()
        (tmp_path / "go").write_text("go", encoding="utf-8")  # even a late go does not matter: nobody is in charge
        assert wrapper.wait(timeout=20) != 0
        assert not marker.exists()
    finally:
        if wrapper.poll() is None:
            wrapper.kill()


_FAKE_RUNNER = """
import json, os, subprocess, sys, time
sys.path.insert(0, {scripts!r})
from task_system.dashboard import proctree
run_dir, stage = sys.argv[1], sys.argv[2]
me = proctree.ident_of(os.getpid())
with open(os.path.join(run_dir, "runner.json"), "w") as handle:
    json.dump({{"pid": me.pid, "create_time": me.create_time, "started_at": "x"}}, handle)
if stage == "after-child-json":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"], start_new_session=True)
    ident = proctree.ident_of(child.pid)
    with open(os.path.join(run_dir, "child.json"), "w") as handle:
        json.dump({{"pid": ident.pid, "create_time": ident.create_time}}, handle)
    open(os.path.join(run_dir, "child.pid"), "w").write(str(child.pid))
time.sleep(600)
"""


@pytest.mark.parametrize("stage", ["after-runner-json", "after-child-json"])
def test_recovery_from_a_runner_that_died_before_the_go_signal(make, stage):
    """BOUNDARY: dead after runner.json, and dead after the child recorded itself but before go.

    The first leaves no command at all; the second leaves a recorded child that recovery must stop
    from child.json alone (no tree.json, no result.json, no go).
    """
    from dashboard_support import SCRIPTS_DIR

    rig = make(behavior="hang")

    def spawn(argv, run_dir):
        subprocess.Popen([sys.executable, "-c", _FAKE_RUNNER.format(scripts=str(SCRIPTS_DIR)), str(run_dir), stage],
                         env=_env(), start_new_session=True, stdin=subprocess.DEVNULL)

    rig.srv.scheduler._spawn = spawn
    run = _start_running(rig)
    run_dir = Path(run.run_dir)
    child_pid = None
    if stage == "after-child-json":
        assert wait_for(lambda: (run_dir / "child.pid").is_file())
        child_pid = int((run_dir / "child.pid").read_text(encoding="utf-8"))
        assert alive(child_pid)
    _kill_runner(run)
    crashed = rig.tick_until(run.id, runtime.TERMINAL_STATES)
    assert (crashed.state, crashed.code) == (runtime.CRASHED, "runner-crashed"), crashed
    assert rig.task_status() == "blocked" and "do-it" in rig.plan_item_ids()
    if child_pid is not None:
        assert wait_for(lambda: not alive(child_pid)), "the recorded child outlived recovery"
    assert rig.cancel_calls(run.id) == []  # no ledger was ever created: there is nothing to cancel


def test_a_run_the_server_cannot_reconcile_ends_crashed_and_blocked_not_in_a_loop(make, monkeypatch):
    """An unexpected error while reading a run's directory is a crash-inspection block, and the tick moves on."""
    rig = make(behavior="done")
    first = rig.queue("do-it", {"timeout_s": 120})
    rig.srv.scheduler.tick()  # dispatch
    run_dir = rig.run_dir(first)
    assert wait_for(lambda: (run_dir / "result.json").is_file(), 60)

    def boom(self, run, run_dir, result):
        raise RuntimeError("settle exploded")

    monkeypatch.setattr(scheduler.Scheduler, "_settle", boom)
    rig.srv.scheduler.tick()  # logs the traceback, blocks, and does not raise
    run = rig.run(first)
    assert (run.state, run.code) == (runtime.CRASHED, "reconcile-error") and "settle exploded" in run.detail
    assert rig.task_status() == "blocked"


def test_an_llm_kit_command_that_dies_with_the_ledger_open_closes_it_and_blocks(make):
    """The command was killed (the runner lived and wrote `finished`): the open ledger is closed, not left armed."""
    import signal

    rig = make(behavior="hang")
    run = _start_running(rig)
    agent, grandchild = _agent_and_grandchild(rig, run)
    os.kill(agent, signal.SIGKILL)  # llm-kit's process dies on its own; the runner survives and reports `finished`
    done = rig.tick_until(run.id, runtime.TERMINAL_STATES)
    assert (done.state, done.code) == (runtime.CRASHED, "llm-kit-crashed"), done
    assert rig.ledger(run.id)["jobs"][0]["state"] == "cancelled" and len(rig.cancel_calls(run.id)) == 1
    assert rig.task_status() == "blocked" and "do-it" in rig.plan_item_ids()
    assert wait_for(lambda: not alive(grandchild))
