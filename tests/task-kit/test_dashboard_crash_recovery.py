"""A runner that dies is never retried and never reported complete.

RED CHECKS (each failed against the Increment 2 scheduler):

- a killed runner left its agent and the agent's children running (the agent has its own
  session, so only the runner knew them) and the ledger open;
- a receipt.json that could not be read crashed the scheduler tick instead of blocking.

Finishing a run whose runner is gone (stopping what it left, closing its ledger, writing the block) happens OFF the scheduler's
``_tick_lock``: the barrier tests at the end of this file hold a finish open and require Cancel and a second tick to complete.

The recovery table in ``scheduler.py`` ("Recovery state table") is exercised row by row.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil
import pytest
from rig_support import Rig, alive, wait_for

from task_system.dashboard import proctree, runfiles, runtime, scheduler


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


def _make_unfinished(rig: Rig, family: str) -> runtime.Run:
    """A run whose runner is gone and which the server has not finished: ``settle`` (result.json written) or ``vanished``."""
    if family == "settle":
        run_id = rig.queue("do-it", {"timeout_s": 120})
        rig.srv.scheduler.tick()  # dispatch
        run_dir = rig.run_dir(run_id)
        assert wait_for(lambda: (run_dir / "result.json").is_file(), 60)
        return rig.run(run_id)
    run = _start_running(rig)
    _agent_and_grandchild(rig, run)  # the fake agent has created its ledger
    _kill_runner(run)
    return run


_FAMILY_FUNCTION = {"settle": "_settle", "vanished": "_runner_vanished"}


@pytest.mark.parametrize("family", ["settle", "vanished"])
def test_a_run_the_server_cannot_reconcile_ends_crashed_and_blocked_not_in_a_loop(make, monkeypatch, family):
    """An unexpected error while finishing a run is a crash-inspection block, and no later tick tries the run again.

    RED CHECKS: replace the conversion with log-and-continue and the run stays active and the spy counts 2 or more.
    """
    rig = make(behavior="done" if family == "settle" else "hang")
    run = _make_unfinished(rig, family)
    name = _FAMILY_FUNCTION[family]
    entered: list[str] = []

    def boom(self, run, *_rest):
        entered.append(run.id)
        raise RuntimeError(f"{name} exploded")

    monkeypatch.setattr(scheduler.Scheduler, name, boom)
    rig.srv.scheduler.tick()  # logs the traceback, blocks, and does not raise
    done = rig.run(run.id)
    assert (done.state, done.code) == (runtime.CRASHED, "reconcile-error") and f"{name} exploded" in done.detail
    assert rig.task_status() == "blocked"
    assert f"finishing run {run.id} failed" in rig.srv.log.getvalue()
    for _ in range(3):
        rig.srv.scheduler.tick()
    assert entered == [run.id], "a run that ended crashed was finished again"


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


# -- finishing a run whose runner is gone happens off the tick lock -----------------------------------------------
#
# Each barrier sets ENTERED when the held call starts and then blocks on GATE; GATE is always set in a ``finally``.
# The 5 s cap on the other threads bounds an operation that takes milliseconds when correct, so load cannot fail the
# green path. RED CHECKS (each seen red against the defect it names; the stale-lock variants are in the docstrings):
#
# - call ``_runner_vanished`` from ``_reconcile`` (under the lock) and B1's Cancel is still waiting at 5 s;
# - call ``_settle`` from ``_reconcile`` (under the lock) and B2's Cancel is still waiting at 5 s;
# - release the claim in a ``finally`` regardless of outcome and B5's count grows.

_JOIN_CAP_S = 5.0


class _Hold:
    """Wrap a callable so its FIRST call announces ENTERED and blocks on GATE; every call is recorded."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.gate = threading.Event()
        self.calls: list[tuple] = []

    def wrap(self, real):
        def held(*args, **kwargs):
            self.calls.append(args)
            if len(self.calls) == 1:
                self.entered.set()
                self.gate.wait(120)
            return real(*args, **kwargs)

        return held


def _in_thread(fn, *args) -> tuple[threading.Thread, dict]:
    box: dict = {}

    def target() -> None:
        try:
            box["value"] = fn(*args)
        except BaseException as exc:  # noqa: BLE001 -- handed to the test thread
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, box


def _cancel_completes_while_held(rig: Rig, hold: _Hold, queued_id: str) -> None:
    """Cancel of another run and a second tick both finish within the cap while the first finish is held open."""
    sched = rig.srv.scheduler
    canceller, cancelled = _in_thread(sched.cancel, queued_id)
    canceller.join(_JOIN_CAP_S)
    assert not canceller.is_alive(), "Cancel waited on the tick lock while a finish was running"
    assert "error" not in cancelled, cancelled
    assert rig.run(queued_id).state == runtime.CANCELLED
    second, second_box = _in_thread(sched.tick)
    second.join(_JOIN_CAP_S)
    assert not second.is_alive(), "a second tick waited on the finish of a run another tick had claimed"
    assert "error" not in second_box, second_box
    assert len(hold.calls) == 1, "the claimed run was finished twice"


def test_b1_a_vanished_runner_is_finished_off_the_tick_lock(make, monkeypatch):
    rig = make(behavior="hang")
    run = _start_running(rig)
    queued = rig.queue("later-one", {"timeout_s": 120})
    _agent_and_grandchild(rig, run)
    _kill_runner(run)
    hold = _Hold()
    monkeypatch.setattr(proctree, "stop_idents", hold.wrap(proctree.stop_idents))
    sched = rig.srv.scheduler
    sched._wake.clear()
    ticker, ticked = _in_thread(sched.tick)
    try:
        assert hold.entered.wait(60), "the vanished run was never finished"
        _cancel_completes_while_held(rig, hold, queued)
        held = rig.run(run.id)
        assert held.state == runtime.RUNNING, "the run must keep its lease and state while it is being finished"
        assert rig.task_status() == "active"
    finally:
        hold.gate.set()
        ticker.join(60)
    assert not ticker.is_alive() and "error" not in ticked, ticked
    crashed = rig.run(run.id)
    assert (crashed.state, crashed.code) == (runtime.CRASHED, "runner-crashed"), crashed
    assert rig.task_status() == "blocked" and "process vanished" in (rig.folder / "log.md").read_text(encoding="utf-8")
    assert sched._wake.is_set(), "a finished run must wake the loop so its freed checkout starts on the next tick"


def test_b2_a_settled_run_whose_ledger_is_still_open_is_closed_off_the_tick_lock(make, monkeypatch):
    """Row 10: the llm-kit command ended with its job open; the cancel verb (up to 120 s) runs off the lock."""
    import signal

    rig = make(behavior="hang")
    run = _start_running(rig)
    queued = rig.queue("later-one", {"timeout_s": 120})
    agent, _grandchild = _agent_and_grandchild(rig, run)
    os.kill(agent, signal.SIGKILL)  # llm-kit's process dies on its own; the runner lives and writes `finished`
    assert wait_for(lambda: (Path(run.run_dir) / "result.json").is_file(), 60), "the runner never reported"
    hold = _Hold()
    monkeypatch.setattr(runfiles, "run_cancel_command", hold.wrap(runfiles.run_cancel_command))
    ticker, ticked = _in_thread(rig.srv.scheduler.tick)
    try:
        assert hold.entered.wait(60), "the ledger was never closed"
        _cancel_completes_while_held(rig, hold, queued)
        assert rig.run(run.id).state == runtime.RUNNING
    finally:
        hold.gate.set()
        ticker.join(60)
    assert not ticker.is_alive() and "error" not in ticked, ticked
    done = rig.run(run.id)
    assert (done.state, done.code) == (runtime.CRASHED, "llm-kit-crashed"), done
    assert rig.ledger(run.id)["jobs"][0]["state"] == "cancelled" and len(rig.cancel_calls(run.id)) == 1
    assert rig.task_status() == "blocked"


def test_b3_a_new_server_over_a_dead_run_answers_and_cancels_while_it_adopts(make, monkeypatch):
    """The adopt path: the new server's first pass finishes the dead run off the lock, so health and Cancel answer."""
    rig = make(behavior="hang")
    run = _start_running(rig)
    queued = rig.queue("later-one", {"timeout_s": 120})
    _agent_and_grandchild(rig, run)
    _kill_runner(run)
    hold = _Hold()
    monkeypatch.setattr(proctree, "stop_idents", hold.wrap(proctree.stop_idents))
    try:
        srv = rig.restart()  # the new server's loop thread starts with ``adopt``
        assert hold.entered.wait(60), "the new server never adopted the dead run"
        status, _h, body = srv.json("GET", "/api/v1/health")
        assert status == 200, body
        canceller, cancelled = _in_thread(rig.cancel, queued)
        canceller.join(_JOIN_CAP_S)
        assert not canceller.is_alive(), "Cancel waited on the lock while the new server was adopting"
        assert cancelled["value"][0] == 202, cancelled
        assert rig.run(queued).state == runtime.CANCELLED
        assert len(hold.calls) == 1
    finally:
        hold.gate.set()
    assert wait_for(lambda: rig.run(run.id).state == runtime.CRASHED, 60), rig.run(run.id)
    assert rig.run(run.id).code == "runner-crashed" and rig.task_status() == "blocked"


def test_a_second_vanished_run_is_finished_while_the_first_is_still_being_finished(tmp_path, monkeypatch):
    """The claim is per run: a run claimed by one tick is skipped, a run that vanished later is claimed by the next."""
    from test_dashboard_project_scheduler import World  # two checkouts under one dashboard

    world = World(tmp_path, monkeypatch, behavior="hang", max_parallel=2)
    try:
        first, second = world.queue("proj-a"), world.queue("proj-b")
        world.tick()
        world.tick()
        assert wait_for(lambda: all(r.state == runtime.RUNNING for r in world.sched.db.runs()), 60), world.states()
        running = {r.id: world.sched.db.get(r.id) for r in (first, second)}
        _kill_runner(running[first.id])
        hold = _Hold()
        monkeypatch.setattr(proctree, "stop_idents", hold.wrap(proctree.stop_idents))
        ticker, ticked = _in_thread(world.sched.tick)
        try:
            assert hold.entered.wait(60), "the first vanished run was never finished"
            _kill_runner(running[second.id])
            other, other_box = _in_thread(world.sched.tick)
            other.join(60)
            assert not other.is_alive() and "error" not in other_box, other_box
            assert len(hold.calls) == 2, "the second run was not finished, or the first was finished twice"
            states = world.states()
            assert states[second.id] == runtime.CRASHED, states
            assert states[first.id] == runtime.RUNNING, "the first run was settled while its finish was still held"
        finally:
            hold.gate.set()
            ticker.join(60)
        assert not ticker.is_alive() and "error" not in ticked, ticked
        assert world.states()[first.id] == runtime.CRASHED
    finally:
        world.close()


@pytest.mark.parametrize("family", ["settle", "vanished"])
def test_b5_a_run_that_cannot_even_be_marked_crashed_keeps_its_claim_and_is_not_retried(make, monkeypatch, family):
    """The conversion failed too: the traceback is logged, the run stays active, nothing retries it, the server is told to stop.

    RED CHECK: release the claim in a ``finally`` regardless of outcome and the count grows over the later ticks.
    """
    rig = make(behavior="done" if family == "settle" else "hang")
    run = _make_unfinished(rig, family)
    name = _FAMILY_FUNCTION[family]
    entered: list[str] = []
    told: list[str] = []
    sched = rig.srv.scheduler
    sched._fatal = told.append  # the real wiring (stop the server) is pinned by the next test

    def boom(self, run, *_rest):
        entered.append(run.id)
        raise RuntimeError(f"{name} exploded")

    def no_database(self, *_a, **_k):
        raise RuntimeError("the database cannot be written")

    with monkeypatch.context() as patched:
        patched.setattr(scheduler.Scheduler, name, boom)
        patched.setattr(scheduler.Scheduler, "_inspection_block", no_database)
        sched.tick()  # logs, does not raise: the server is told to stop instead
        for _ in range(3):
            sched.tick()
    assert entered == [run.id], "a run whose failure could not be recorded was retried"
    assert rig.run(run.id).state in (runtime.RUNNING, runtime.CANCELLING), rig.run(run.id)  # still active, lease held
    assert len(told) == 1 and run.id in told[0], told
    log = rig.srv.log.getvalue()
    assert f"finishing run {run.id} failed" in log and f"run {run.id} could not even be marked crashed" in log


@pytest.mark.parametrize("family", ["settle", "vanished"])
def test_b5_the_next_server_re_adopts_a_run_the_last_one_could_not_finish(make, monkeypatch, family):
    """A claim lives in memory: the failed server stops (the real ``fatal``), the next one finishes the run from its files."""
    rig = make(behavior="done" if family == "settle" else "hang")
    run = _make_unfinished(rig, family)
    name = _FAMILY_FUNCTION[family]
    server = rig.srv.server
    thread = rig.srv.thread

    def boom(self, run, *_rest):
        raise RuntimeError(f"{name} exploded")

    def no_database(self, *_a, **_k):
        raise RuntimeError("the database cannot be written")

    with monkeypatch.context() as patched:
        patched.setattr(scheduler.Scheduler, name, boom)
        patched.setattr(scheduler.Scheduler, "_inspection_block", no_database)
        rig.srv.scheduler.tick()
        assert wait_for(server._stopping.is_set, 10), "the server was not asked to stop"
        thread.join(30)
        assert not thread.is_alive(), "the server kept serving after it gave up on a run"
    rig.restart()  # the failed server is gone; the failure patches are gone with it
    expected = runtime.ACCEPTED if family == "settle" else runtime.CRASHED
    assert wait_for(lambda: rig.run(run.id).state in runtime.TERMINAL_STATES, 60), rig.run(run.id)
    done = rig.run(run.id)
    assert done.state == expected, done
    if family == "vanished":
        assert done.code == "runner-crashed" and rig.task_status() == "blocked"
        assert rig.ledger(run.id)["jobs"][0]["state"] == "cancelled"


def test_every_claimed_run_is_attempted_before_the_server_is_told_to_stop(tmp_path, monkeypatch):
    """One run whose failure cannot be recorded never strands the runs claimed after it in the same pass.

    RED CHECK: re-raise from ``_finish_all`` at the first unrecordable run and the second run is never finished.
    """
    from test_dashboard_project_scheduler import World

    world = World(tmp_path, monkeypatch, behavior="hang", max_parallel=2)
    try:
        first, second = world.queue("proj-a"), world.queue("proj-b")
        world.tick()
        world.tick()
        assert wait_for(lambda: all(r.state == runtime.RUNNING for r in world.sched.db.runs()), 60), world.states()
        for r in (first, second):
            _kill_runner(world.sched.db.get(r.id))
        told: list[str] = []
        world.sched._fatal = told.append
        order: list[str] = []
        real_vanished = scheduler.Scheduler._runner_vanished
        real_block = scheduler.Scheduler._inspection_block

        def vanished(self, run, run_dir):
            order.append(run.id)
            if run.id == first.id:
                raise RuntimeError("the first run cannot be finished")
            return real_vanished(self, run, run_dir)

        def block(self, run, code, detail):
            if run.id == first.id:
                raise RuntimeError("the database cannot be written")
            return real_block(self, run, code, detail)

        monkeypatch.setattr(scheduler.Scheduler, "_runner_vanished", vanished)
        monkeypatch.setattr(scheduler.Scheduler, "_inspection_block", block)
        world.sched.tick()
        assert order == [first.id, second.id], order
        states = world.states()
        assert states[second.id] == runtime.CRASHED and states[first.id] == runtime.RUNNING, states
        assert len(told) == 1 and first.id in told[0] and second.id not in told[0], told
    finally:
        world.close()


# -- a pass that fails after it claimed runs still finishes them ---------------------------------------------------


def test_a_start_that_raises_does_not_strand_the_runs_the_same_tick_claimed(make, monkeypatch):
    """The claim and the finish are one unit: ``_start_queued`` failing must not leave a claimed run unfinished.

    RED CHECK: finish only after the lock block with no ``finally`` and the vanished run stays active, and every later
    pass skips it because only ``_finish`` clears the claim.
    """
    rig = make(behavior="hang")
    run = _make_unfinished(rig, "vanished")
    sched = rig.srv.scheduler

    def start_exploded(self):
        raise RuntimeError("start exploded")

    with monkeypatch.context() as patched:
        patched.setattr(scheduler.Scheduler, "_start_queued", start_exploded)
        with pytest.raises(RuntimeError, match="start exploded"):  # the failure stays loud: the loop logs it
            sched.tick()
    done = rig.run(run.id)
    assert (done.state, done.code) == (runtime.CRASHED, "runner-crashed"), done
    assert rig.task_status() == "blocked"
    assert run.id not in sched._recovering, "a finished run must not stay claimed"


def test_a_reconcile_pass_that_raises_midway_still_finishes_the_runs_it_already_claimed(tmp_path, monkeypatch):
    """Two runs, the first claimed, then the pass itself fails on the second (its own failure handler fails too)."""
    from test_dashboard_project_scheduler import World

    world = World(tmp_path, monkeypatch, behavior="hang", max_parallel=2)
    try:
        first, second = world.queue("proj-a"), world.queue("proj-b")
        world.tick()
        world.tick()
        assert wait_for(lambda: all(r.state == runtime.RUNNING for r in world.sched.db.runs()), 60), world.states()
        for r in (first, second):
            _agent_and_grandchild_of(world, r)
            _kill_runner(world.sched.db.get(r.id))
        sched = world.sched
        real_reconcile = scheduler.Scheduler._reconcile
        real_log = sched._log

        def reconcile(self, run):
            if run.id == second.id:
                raise RuntimeError("cannot classify the second run")
            return real_reconcile(self, run)

        def log(message):
            if message.startswith(f"reconciling run {second.id} failed"):
                raise RuntimeError("the log cannot be written")
            real_log(message)

        monkeypatch.setattr(scheduler.Scheduler, "_reconcile", reconcile)
        sched._log = log
        with pytest.raises(RuntimeError, match="the log cannot be written"):
            sched.tick()
        sched._log = real_log
        assert world.states()[first.id] == runtime.CRASHED, "the run claimed before the failure was stranded"
        assert first.id not in sched._recovering
    finally:
        world.close()


def _agent_and_grandchild_of(world, run) -> None:
    """Wait until the fake agent of a World run has created its ledger (its grandchild.pid exists)."""
    current = world.sched.db.get(run.id)
    assert wait_for((Path(current.run_dir) / "grandchild.pid").is_file), "the fake agent never started"
