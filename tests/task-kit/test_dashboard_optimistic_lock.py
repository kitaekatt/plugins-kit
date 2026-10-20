"""Every dashboard task-folder write is guarded: expected revision, hold gate, serialization, honest partial failure.

The harness writes task folders without the dashboard's lock, so the guarantee these tests pin is the NARROWED one:
no write commits while a `/task work` hold it did not account for is live, each file is compared immediately before
its replace (a harness change that landed before that compare is never overwritten), the files are replaced one at a
time and a part-way failure is reported by file. The one residual (a hold-less harness write landing inside a single
read + compare + rename) is pinned too, by name, so no document can claim more than the code does.
"""

from __future__ import annotations

import ast
import io
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from authoring_support import rig  # noqa: F401 -- rig is a fixture
from dashboard_support import DASHBOARD_DIR, SCRIPTS_DIR, tree_digest

from task_system.dashboard import handshake, mutation, proctree, runtime, scheduler
from task_system.dashboard.mutation import LOG, PLAN, TASK_YAML, TaskMutator
from task_system.validate import validate_ref

HARNESS_LINE = "- 2026-10-03 08:00: the harness got there first\n"


def digest(rig):
    return tree_digest(rig.project / "dev")


def set_status(rig, status: str) -> None:
    TaskMutator(rig.tmp_path / "prep.lock").mutate(
        rig.alpha, lambda t: {TASK_YAML: mutation.set_task_status(t.task_yaml, status)})


def prep_none(rig) -> None:
    return None


def prep_deferred(rig) -> None:
    TaskMutator(rig.tmp_path / "prep.lock").mutate(
        rig.alpha, lambda t: {PLAN: mutation.set_item_state(t.plan, "later-one", "deferred")})


def prep_broken_item(rig) -> None:
    plan = rig.plan().replace("### later-one -- section\n\nMake the other thing.", "")
    (rig.alpha / "plan.md").write_text(plan, encoding="utf-8")


def prep_blocked(rig) -> None:
    set_status(rig, "blocked")


# name -> (prepare, call(rig, revision), status when it succeeds, the first file it replaces)
WRITES = {
    "additem": (prep_none, lambda r, rev=None: r.add(title="N", details="d", revision=rev), 201, PLAN),
    "addqueue": (prep_none, lambda r, rev=None: r.add(title="N", details="d", queue=True, revision=rev), 202, PLAN),
    "defer": (prep_none, lambda r, rev=None: r.defer("later-one", "deferred", revision=rev), 200, PLAN),
    "undefer": (prep_deferred, lambda r, rev=None: r.defer("later-one", "available", revision=rev), 200, PLAN),
    "queueall-block": (prep_broken_item, lambda r, rev=None: r.queue_all(revision=rev), 422, PLAN),
    "queue-block": (prep_broken_item, lambda r, rev=None: r.queue("later-one", revision=rev), 422, PLAN),
    "recheck": (prep_blocked, lambda r, rev=None: r.recheck(revision=rev), 200, TASK_YAML),
}
AUTHORING = ("additem", "addqueue", "defer", "undefer")


def inject_once(monkeypatch, folder: Path, filename: str, where: str = "fsync"):
    """Right after the first call of ``mutation.os.<where>`` (phase 1: the temps are written), the harness appends a line."""
    state = {"done": False}
    real = getattr(os, where)

    def wrapper(*args, **kw):
        result = real(*args, **kw)
        if not state["done"]:
            state["done"] = True
            with open(folder / filename, "a", encoding="utf-8") as handle:
                handle.write(HARNESS_LINE)
        return result

    monkeypatch.setattr(mutation.os, where, wrapper)


# -- the revision --------------------------------------------------------------------------------


@pytest.mark.parametrize("name", list(WRITES))
def test_a_write_without_an_expected_revision_is_refused_and_writes_nothing(rig, name):
    prepare, call, _ok, _first = WRITES[name]
    prepare(rig)
    before = digest(rig)
    status, _h, body = call(rig, False)
    assert status == 422 and body["error"]["code"] == "missing_expected_revision", body
    assert digest(rig) == before


@pytest.mark.parametrize("name", list(WRITES))
def test_a_stale_revision_is_a_409_with_the_current_one_and_writes_nothing(rig, name):
    """RED CHECK: pass expected_revision=None to the mutator and the stale write lands."""
    prepare, call, _ok, _first = WRITES[name]
    prepare(rig)
    stale = rig.rev()
    with open(rig.alpha / "log.md", "a", encoding="utf-8") as handle:
        handle.write(HARNESS_LINE)
    before = digest(rig)
    status, _h, body = call(rig, stale)
    assert status == 409 and body["error"]["code"] == "stale-revision", body
    assert body["error"]["current_revision"] == rig.rev()
    assert digest(rig) == before


@pytest.mark.parametrize("name", list(WRITES))
def test_a_harness_write_before_the_final_compare_is_never_overwritten(rig, monkeypatch, name):
    """RED CHECK: remove the pre-replace compare in TaskMutator._replace and the harness change is replaced."""
    prepare, call, _ok, first = WRITES[name]
    prepare(rig)
    inject_once(monkeypatch, rig.alpha, first)
    status, _h, body = call(rig)
    assert status == 409 and body["error"]["code"] == "stale-revision", body  # the first file: nothing was written
    assert HARNESS_LINE in (rig.alpha / first).read_text(encoding="utf-8")
    assert not list(rig.alpha.glob("*.dash.tmp"))
    assert "partial" not in json.dumps(body)


def test_residual_race_a_harness_write_landing_inside_the_replace_is_lost_and_is_documented_as_such(rig, monkeypatch):
    """The residual the architecture document states: one read + compare + rename wide, undetected.

    A harness writer without a hold that lands AFTER the final compare and before the rename is replaced. This test
    is the proof that the window exists; it fails if someone adds a post-replace "detection" that retries, because
    the replace is called exactly once per file and no retry may be added silently.
    """
    calls = []
    real = os.replace

    def wrapper(src, dst):
        calls.append(Path(dst).name)
        if Path(dst).name == PLAN:
            with open(dst, "a", encoding="utf-8") as handle:
                handle.write("\nthe harness wrote here, inside the replace\n")
        return real(src, dst)

    rig.freeze_scheduler()
    monkeypatch.setattr(mutation.os, "replace", wrapper)
    status, _h, body = rig.add(title="Lands anyway", details="d")
    assert status == 201, body
    assert "the harness wrote here" not in rig.plan()  # lost, and not detected
    assert sorted(calls) == [LOG, PLAN]  # once per file: no retry


# -- holds and runs --------------------------------------------------------------------------------


@pytest.mark.parametrize("name", AUTHORING)
def test_every_authoring_write_is_refused_while_a_session_holds_the_task(rig, name):
    """RED CHECK: drop the hold check in the authoring precommit and the write lands under the session."""
    prepare, call, _ok, _first = WRITES[name]
    prepare(rig)
    rig.hold(rig.alpha)
    before = digest(rig)
    status, _h, body = call(rig)
    assert status == 409 and body["error"]["code"] == "held", body
    assert digest(rig) == before and not list(rig.alpha.glob("*.dash.tmp"))


@pytest.mark.parametrize("name", ["queue-block", "queueall-block"])
def test_a_translation_block_is_not_written_under_a_session_and_says_so(rig, name):
    prepare, call, _ok, _first = WRITES[name]
    prepare(rig)
    rig.hold(rig.alpha)
    before = digest(rig)
    status, _h, body = call(rig)
    assert status == 422 and body["error"]["code"] == "translation-failed" and "blocked" not in body["error"], body
    assert digest(rig) == before


def test_authoring_is_refused_while_a_run_works_the_task_and_while_the_item_has_a_queued_run(rig):
    rig.freeze_scheduler()
    status, _h, queued = rig.queue("do-it", options={"timeout_s": 60})
    before = digest(rig)
    status, _h, body = rig.defer("do-it", "deferred")
    assert status == 409 and body["error"]["code"] == "item-has-live-run", body
    assert digest(rig) == before
    assert rig.add(title="Other thing")[0] == 201  # a queued (not active) run does not stop adding
    rig.edge.behavior = "hang"
    rig.srv.scheduler.tick()
    running = rig.srv.settle(queued["run"]["id"], states=(runtime.RUNNING,))
    before = digest(rig)
    for status, _h, body in (rig.add(title="Third"), rig.defer("later-one", "deferred")):
        assert status == 409 and body["error"]["code"] == "run-active", body
    assert digest(rig) == before
    row = next(t for t in rig.dashboard()["tasks"] if t["id"] == "dev/tasks/alpha")
    assert row["run_active"] is True and row["held"] is False
    assert rig.srv.scheduler.db.get(running.id).state == runtime.RUNNING


def test_the_task_row_says_when_a_session_holds_it(rig):
    row = lambda: next(t for t in rig.dashboard()["tasks"] if t["id"] == "dev/tasks/alpha")  # noqa: E731
    assert row()["held"] is False
    session = rig.hold(rig.alpha)
    assert row()["held"] is True and next(t for t in rig.dashboard()["tasks"] if t["id"] == "dev/tasks/beta")["held"] is False
    rig.end_session(session)
    assert row()["held"] is False


# -- the start race and the hold race -----------------------------------------------------------------


def test_a_run_cannot_start_between_an_authoring_check_and_its_write(rig, monkeypatch):
    """RED CHECK: remove BOTH guards (authoring's _tick_lock and the start's hold gate) and the tick starts the run mid-write.

    Either guard alone keeps this green: the start takes the gate for its lease, and the write holds the gate from its
    precommit checks to its last replace, so each subsumes the other here. The lock order stays as designed.
    """
    rig.freeze_scheduler()
    status, _h, queued = rig.queue("do-it", options={"timeout_s": 60})
    rig.edge.behavior = "hang"
    parked, release = threading.Event(), threading.Event()
    real = rig.srv.scheduler._authoring_precommit

    def parked_precommit(*args, **kw):
        check = real(*args, **kw)

        def wait_then_check():
            parked.set()
            assert release.wait(30)
            check()
        return wait_then_check

    monkeypatch.setattr(rig.srv.scheduler, "_authoring_precommit", parked_precommit)
    answer = {}
    writer = threading.Thread(target=lambda: answer.update(r=rig.add(title="Mid write", details="d")))
    writer.start()
    assert parked.wait(10)
    ticker = threading.Thread(target=rig.srv.scheduler.tick)
    ticker.start()
    time.sleep(0.6)
    assert ticker.is_alive(), "the tick ran while an authoring write sat between its check and its write"
    assert rig.srv.scheduler.db.get(queued["run"]["id"]).state == runtime.QUEUED
    release.set()
    writer.join(15)
    ticker.join(15)
    assert answer["r"][0] == 201, answer
    assert rig.srv.scheduler.db.get(queued["run"]["id"]).state in (runtime.STARTING, runtime.RUNNING)  # started after the write


def test_a_write_is_refused_when_a_run_started_first(rig):
    rig.freeze_scheduler()
    rig.edge.behavior = "hang"
    status, _h, queued = rig.queue("do-it", options={"timeout_s": 60})
    rig.srv.scheduler.tick()
    rig.srv.settle(queued["run"]["id"], states=(runtime.RUNNING,))
    before = digest(rig)
    status, _h, body = rig.add(title="Too late")
    assert status == 409 and body["error"]["code"] == "run-active" and digest(rig) == before


def test_a_hold_registered_while_a_write_is_being_prepared_refuses_it(rig, monkeypatch):
    rig.freeze_scheduler()
    proc = rig.session()
    state = {"done": False}
    real = os.fsync

    def register_during_phase_1(fd):
        if not state["done"]:
            state["done"] = True
            rig.srv.scheduler.register_hold(str(rig.alpha), "late", proctree.ident_of(proc.pid))
        return real(fd)

    monkeypatch.setattr(mutation.os, "fsync", register_during_phase_1)
    before = digest(rig)
    status, _h, body = rig.add(title="Raced", details="d")
    assert status == 409 and body["error"]["code"] == "held", body
    assert digest(rig) == before and not list(rig.alpha.glob("*.dash.tmp"))


def test_a_hold_registered_during_the_commit_waits_for_that_commit_only(rig, monkeypatch):
    """RED CHECK: take the gate out of register_hold and the registration does not wait for the commit."""
    rig.freeze_scheduler()
    proc = rig.session()
    in_commit, release = threading.Event(), threading.Event()
    real = os.replace

    def slow_replace(src, dst):
        if Path(dst).name == PLAN:
            in_commit.set()
            assert release.wait(30)
        return real(src, dst)

    monkeypatch.setattr(mutation.os, "replace", slow_replace)
    answer = {}
    writer = threading.Thread(target=lambda: answer.update(r=rig.add(title="Committing", details="d")))
    writer.start()
    assert in_commit.wait(10)
    registered = threading.Event()
    registrar = threading.Thread(target=lambda: (rig.srv.scheduler.register_hold(str(rig.alpha), "s", proctree.ident_of(proc.pid)),
                                                 registered.set()))
    registrar.start()
    time.sleep(0.5)
    assert not registered.is_set(), "a hold registered in the middle of a commit without waiting for it"
    release.set()
    writer.join(15)
    registrar.join(15)
    assert answer["r"][0] == 201 and registered.is_set()
    monkeypatch.setattr(mutation.os, "replace", real)
    assert rig.add(title="Next")[0] == 409  # the next write sees the hold


def test_a_hold_that_arrives_between_translation_and_the_lease_stops_the_start(rig, monkeypatch):
    """RED CHECK: take the hold re-check out of the start's gate and the run starts under the session."""
    rig.freeze_scheduler()
    status, _h, queued = rig.queue("do-it", options={"timeout_s": 60})
    proc = rig.session()
    real = scheduler.translate

    def translate_then_hold(**kw):
        result = real(**kw)
        rig.srv.scheduler.register_hold(str(rig.alpha), "between", proctree.ident_of(proc.pid))
        return result

    monkeypatch.setattr(scheduler, "translate", translate_then_hold)
    rig.srv.scheduler.tick()
    run = rig.srv.scheduler.db.get(queued["run"]["id"])
    assert run.state == runtime.QUEUED and not (rig.srv.server.root / "projects").exists()


def test_a_hold_registered_after_the_lease_sees_the_active_run(rig):
    rig.freeze_scheduler()
    rig.edge.behavior = "hang"
    status, _h, queued = rig.queue("do-it", options={"timeout_s": 60})
    rig.srv.scheduler.tick()
    live = rig.srv.settle(queued["run"]["id"], states=(runtime.RUNNING,))
    proc = rig.session()
    active = rig.srv.scheduler.register_hold(str(rig.alpha), "after", proctree.ident_of(proc.pid))
    assert active is not None and active.id == live.id


# -- gate contention: a hold registration fits the handshake's 2 second budget --------------------------


def hold_the_process_lock(rig, seconds: float) -> subprocess.Popen:
    lock = rig.srv.server.root / "mutation.lock"
    code = ("import sys, time; sys.path.insert(0, sys.argv[1]);"
            "from task_system.dashboard.mutation import ProcessLock;"
            "l = ProcessLock(sys.argv[2], 5); l.__enter__(); print('held', flush=True); time.sleep(float(sys.argv[3]))")
    holder = subprocess.Popen([sys.executable, "-c", code, str(SCRIPTS_DIR), str(lock), str(seconds)], stdout=subprocess.PIPE, text=True,
                              env=dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path), _BOOTSTRAP_GUARD_SKIP_REEXEC="1"))
    assert holder.stdout.readline().strip() == "held"
    return holder


def register_fast(rig, name: str = "quick") -> float:
    """Register a hold through the real handshake; the seconds it took. Nothing may be said on stderr."""
    proc = rig.session()
    err = io.StringIO()
    started = time.monotonic()
    assert handshake.run("dev/tasks/alpha", rig.project, session=name, data_dir=rig.srv.data_dir, session_pid=proc.pid, err=err) == 0
    elapsed = time.monotonic() - started
    assert err.getvalue() == "", err.getvalue()
    assert name in [h["session"] for h in rig.srv.scheduler.db.live_holds(str(rig.alpha))]
    return elapsed


def test_a_hold_registers_within_the_handshake_budget_while_a_write_waits_for_the_process_lock(rig):
    """RED CHECK: enter the gate before the process lock and the registration waits for the other process (~5 s)."""
    rig.freeze_scheduler()
    holder = hold_the_process_lock(rig, 5)
    try:
        answer = {}
        writer = threading.Thread(target=lambda: answer.update(r=rig.add(title="Waits", details="d")))
        before = digest(rig)
        writer.start()
        time.sleep(0.5)  # the write is now waiting on the lock
        assert writer.is_alive()
        elapsed = register_fast(rig)
        assert elapsed < 0.5, elapsed
        writer.join(30)
    finally:
        holder.kill()
        holder.wait()
    status, _h, body = answer["r"]
    assert status == 409 and body["error"]["code"] == "held", body
    assert digest(rig) == before


def test_a_hold_registers_within_the_budget_while_a_write_is_slow_to_prepare(rig, monkeypatch):
    """RED CHECK: write the temp files inside the gate and the registration waits for the slow disk."""
    rig.freeze_scheduler()
    real = os.fsync
    slow = threading.Event()

    def slow_fsync(fd):
        slow.set()
        time.sleep(3)
        return real(fd)

    monkeypatch.setattr(mutation.os, "fsync", slow_fsync)
    answer = {}
    writer = threading.Thread(target=lambda: answer.update(r=rig.add(title="Slow", details="d")))
    writer.start()
    assert slow.wait(10)
    assert register_fast(rig) < 0.5
    writer.join(30)
    assert answer["r"][0] == 409


def test_a_hold_registers_within_the_budget_while_a_tick_is_slow(rig):
    """RED CHECK: take _tick_lock in register_hold and a 20 second tick loses the hold."""
    rig.freeze_scheduler()
    taken, release = threading.Event(), threading.Event()

    def long_tick():
        with rig.srv.scheduler._tick_lock:
            taken.set()
            release.wait(30)

    ticker = threading.Thread(target=long_tick)
    ticker.start()
    assert taken.wait(10)
    try:
        assert register_fast(rig) < 0.5
        status, _h, body = rig.srv.post("/api/v1/holds", {"folder": str(rig.alpha), "session": "direct",
                                                          "pid": rig.session().pid,
                                                          "create_time": proctree.ident_of(rig.sessions[-1].pid).create_time})
        assert status == 200
    finally:
        release.set()
        ticker.join(15)


# -- stale blocks (a block carries the revision its translation read) ---------------------------------------


def heal_after_translating(monkeypatch, rig):
    """After the translation read the folder, the harness adds the missing section (so a block would be on stale text)."""
    real = scheduler.translate
    state = {"healed": False}

    def wrapper(**kw):
        result = real(**kw)
        if kw["item_id"] == "later-one" and not state["healed"]:
            state["healed"] = True
            (rig.alpha / "plan.md").write_text(
                (rig.alpha / "plan.md").read_text() + "\n### later-one -- section\n\nMake the other thing.\n", encoding="utf-8")
        return result

    monkeypatch.setattr(scheduler, "translate", wrapper)


@pytest.mark.parametrize("name", ["queue-block", "queueall-block"])
def test_a_block_on_stale_text_is_a_409_and_the_task_is_not_blocked(rig, monkeypatch, name):
    """RED CHECK: revert _block_for to expected_revision=None and the task is blocked on text the harness just fixed."""
    prepare, call, _ok, _first = WRITES[name]
    prepare(rig)
    heal_after_translating(monkeypatch, rig)
    status, _h, body = call(rig)
    assert status == 409 and body["error"]["code"] == "stale-revision", body
    assert mutation.stored_status((rig.alpha / "task.yaml").read_text()) == "active"
    assert rig.srv.scheduler.db.runs() == []
    assert "blocked-user" not in rig.plan()


def test_a_start_that_finds_the_task_changed_under_it_stays_queued_and_runs_next_tick(rig, monkeypatch):
    rig.freeze_scheduler()
    status, _h, queued = rig.queue("do-it", options={"timeout_s": 60})
    plan = rig.plan().replace("### do-it -- section\n\nMake the thing.\n\nDone when it exists.", "")
    (rig.alpha / "plan.md").write_text(plan, encoding="utf-8")
    restore = rig.alpha.joinpath("plan.md").read_text() + "\n### do-it -- section\n\nMake the thing.\n\nDone when it exists.\n"
    real = scheduler.translate
    state = {"healed": False}

    def wrapper(**kw):
        result = real(**kw)
        if not state["healed"]:
            state["healed"] = True
            (rig.alpha / "plan.md").write_text(restore, encoding="utf-8")
        return result

    monkeypatch.setattr(scheduler, "translate", wrapper)
    rig.srv.scheduler.tick()
    run = rig.srv.scheduler.db.get(queued["run"]["id"])
    assert run.state == runtime.QUEUED and mutation.stored_status((rig.alpha / "task.yaml").read_text()) == "active"
    assert "task-source-changed" in [e["kind"] for e in rig.srv.scheduler.db.events(run.id)]
    assert rig.srv.settle(run.id).state == runtime.ACCEPTED


def test_queue_on_a_held_task_with_a_broken_item_writes_nothing(rig):
    prep_broken_item(rig)
    rig.hold(rig.alpha)
    before = digest(rig)
    status, _h, body = rig.queue("later-one")
    assert status == 422 and body["error"]["code"] == "translation-failed" and "blocked" not in body["error"]
    assert digest(rig) == before


# -- partial application ---------------------------------------------------------------------------------------


def fail_replace_of(monkeypatch, name: str) -> list[str]:
    calls: list[str] = []
    real = os.replace

    def wrapper(src, dst):
        if Path(dst).name in (PLAN, TASK_YAML, LOG):
            calls.append(Path(dst).name)
        if Path(dst).name == name:
            raise PermissionError("locked by another process")
        return real(src, dst)

    monkeypatch.setattr(mutation.os, "replace", wrapper)
    return calls


def test_a_failure_part_way_is_reported_by_file_and_the_folder_still_validates(rig, monkeypatch):
    """RED CHECK: drop the `written` bookkeeping and the answer says nothing of what changed."""
    rig.freeze_scheduler()
    calls = fail_replace_of(monkeypatch, LOG)
    status, _h, body = rig.add(title="Half", details="d")
    error = body["error"]
    assert status == 409 and error["code"] == "partial-write", body
    assert (error["cause"], error["file"], error["written"], error["not_written"]) == ("replace-failed", LOG, [PLAN], [LOG])
    assert error["current_revision"] == rig.rev()
    assert "half" in {i["id"] for i in mutation.raw_items(rig.plan())}
    assert validate_ref("dev/tasks/alpha", rig.project).errors == []
    assert calls == [PLAN, LOG]  # one attempt per file: no retry


def test_a_block_whose_task_yaml_cannot_be_written_says_the_task_was_not_marked_blocked(rig, monkeypatch):
    rig.freeze_scheduler()
    prep_broken_item(rig)
    calls = fail_replace_of(monkeypatch, TASK_YAML)
    status, _h, body = rig.queue_all()
    error = body["error"]
    assert status == 409 and error["code"] == "partial-write" and error["written"] == [PLAN] and TASK_YAML in error["not_written"], body
    assert mutation.stored_status((rig.alpha / "task.yaml").read_text()) == "active"
    assert calls == [PLAN, TASK_YAML]


def test_a_later_file_that_went_stale_after_an_earlier_one_was_replaced_is_a_partial_write(rig, monkeypatch):
    """RED CHECK: map the error by its code before looking at `written` and the answer is a bare stale-revision."""
    rig.freeze_scheduler()
    real = os.replace

    def wrapper(src, dst):
        result = real(src, dst)
        if Path(dst).name == PLAN:
            with open(Path(dst).with_name(LOG), "a", encoding="utf-8") as handle:
                handle.write(HARNESS_LINE)
        return result

    monkeypatch.setattr(mutation.os, "replace", wrapper)
    status, _h, body = rig.add(title="Both", details="d")
    error = body["error"]
    assert status == 409 and error["code"] == "partial-write", body
    assert (error["cause"], error["file"], error["written"]) == ("stale-revision", LOG, [PLAN])
    assert HARNESS_LINE in rig.log() and error["current_revision"] == rig.rev()


# -- Re-check through the gate ------------------------------------------------------------------------------------


def test_recheck_ends_the_holds_it_predates_and_reactivates_the_task(rig):
    prep_blocked(rig)
    rig.hold(rig.alpha, name="A")
    status, _h, body = rig.recheck()
    assert status == 200 and body["changed"] is True and body["holds_released"] == 1, body
    assert rig.srv.scheduler.db.live_holds(str(rig.alpha)) == []
    assert mutation.stored_status((rig.alpha / "task.yaml").read_text()) == "active"


def test_recheck_never_deletes_a_hold_registered_while_it_ran(rig, monkeypatch):
    """RED CHECK: restore the clear-first, ungated Re-check and the new session's hold is deleted under it."""
    rig.freeze_scheduler()
    prep_blocked(rig)
    session_a = rig.hold(rig.alpha, name="A")
    session_b = rig.session()
    parked, release = threading.Event(), threading.Event()
    real = os.fsync

    def park(fd):
        parked.set()
        assert release.wait(30)
        return real(fd)

    monkeypatch.setattr(mutation.os, "fsync", park)
    answer = {}
    checker = threading.Thread(target=lambda: answer.update(r=rig.recheck()))
    log_before = rig.log()
    checker.start()
    assert parked.wait(10)
    err = io.StringIO()
    assert handshake.run("dev/tasks/alpha", rig.project, session="B", data_dir=rig.srv.data_dir, session_pid=session_b.pid, err=err) == 0
    assert err.getvalue() == ""
    release.set()
    checker.join(30)
    status, _h, body = answer["r"]
    assert status == 409 and body["error"]["code"] == "held", body
    assert mutation.stored_status((rig.alpha / "task.yaml").read_text()) == "blocked"
    assert rig.log() == log_before
    assert sorted(h["session"] for h in rig.srv.scheduler.db.live_holds(str(rig.alpha))) == ["A", "B"]
    assert session_a.poll() is None


def test_a_refused_recheck_releases_the_old_holds_unless_a_new_one_arrived(rig, monkeypatch):
    prep_blocked(rig)
    (rig.alpha / "plan.md").write_text(rig.plan().replace("state: available", "state: blocked-user", 1), encoding="utf-8")
    rig.hold(rig.alpha, name="A")
    status, _h, body = rig.recheck()
    assert status == 422 and body["error"]["code"] == "recheck-failed"
    assert rig.srv.scheduler.db.live_holds(str(rig.alpha)) == []  # A is released, as it always was
    rig.hold(rig.alpha, name="A")
    proc = rig.session()
    real = scheduler.validate_ref

    def register_then_validate(*args, **kw):
        rig.srv.scheduler.register_hold(str(rig.alpha), "B", proctree.ident_of(proc.pid))
        return real(*args, **kw)

    monkeypatch.setattr(scheduler, "validate_ref", register_then_validate)
    status, _h, body = rig.recheck()
    assert status == 422 and body["error"]["code"] == "recheck-failed"
    assert sorted(h["session"] for h in rig.srv.scheduler.db.live_holds(str(rig.alpha))) == ["A", "B"]  # nothing released


# -- every writer is gated ----------------------------------------------------------------------------------------------


WRITER_CALLS = {"mutate", "record_block", "record_blocks"}
UNGATED_BY_DESIGN = {"acceptance.py"}  # runs in the runner process, which cannot share the server's in-process gate


def ungated_writer_calls(files: dict[str, str]) -> list[str]:
    """``file:function`` for every call of a task-folder writer that does not pass ``gate=``."""
    found = []
    for name, source in files.items():
        if name in UNGATED_BY_DESIGN:
            continue
        tree = ast.parse(source)
        stack: list[str] = []

        def visit(node):
            pushed = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            if pushed:
                stack.append(node.name)
            if isinstance(node, ast.Call):
                func = node.func
                called = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else None
                if called in WRITER_CALLS and "gate" not in {k.arg for k in node.keywords}:
                    found.append(f"{name}:{stack[-1] if stack else '<module>'}")
            for child in ast.iter_child_nodes(node):
                visit(child)
            if pushed:
                stack.pop()

        visit(tree)
    return found


def test_every_server_process_writer_passes_the_gate():
    """RED CHECK: add an ungated `.mutate(` call in a handler and this lists it."""
    files = {p.name: p.read_text(encoding="utf-8") for p in DASHBOARD_DIR.glob("*.py")}
    assert ungated_writer_calls(files) == []
    planted = dict(files, **{"server.py": files["server.py"] + "\n\ndef _new_handler(self):\n    self.mutator.mutate(self.folder, edit)\n"})
    assert ungated_writer_calls(planted) == ["server.py:_new_handler"]  # the scanner sees what it must


# -- concurrency and runtime records ---------------------------------------------------------------------------------------


def test_two_writes_on_one_revision_commit_once(rig):
    revision = rig.rev()
    barrier = threading.Barrier(2)
    answers = []

    def go(title):
        barrier.wait()
        answers.append(rig.add(title=title, revision=revision)[:3:2])

    threads = [threading.Thread(target=go, args=(t,)) for t in ("First", "Second")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    statuses = sorted(status for status, _body in answers)
    assert statuses == [201, 409]
    assert next(b for s, b in answers if s == 409)["error"]["code"] == "stale-revision"
    assert len(mutation.raw_items(rig.plan())) == 3


def test_actions_on_a_queue_entry_carry_no_task_revision(rig):
    rig.freeze_scheduler()
    status, _h, queued = rig.queue("do-it", options={"timeout_s": 60})
    assert rig.run_options("do-it", {"effort": "low"})[0] == 200  # no expected_revision in the request
    assert rig.remove(queued["run"]["id"])[0] == 200
    assert rig.run_options("do-it", {"effort": "low"})[0] == 409  # refused by state, not by a revision
