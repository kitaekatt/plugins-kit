"""The working-session store: record format, ownership, states, register / release / refresh / clear re-key.

Every test passes an explicit data directory and temporary roots (the task-kit conftest does not redirect the
data directory). Real sleeper processes stand in for Claude Code processes, so process stamps are real.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest
import yaml

from task_system import harness, oslock, procstamp, sessions
from task_system.machine import Machine

HOST = Machine("testhost", "abcdef0123456789", "macos")
OTHER = Machine("otherhost", "0123456789abcdef", "linux")
SAME_NAME_OTHER_MACHINE = Machine("testhost", "ffffffffffffffff", "linux")
STUBS = ("alpha", "beta", "gamma")


class World:
    """A project with three dev/tasks tasks and one tmp task, a data directory, and sleeper processes."""

    def __init__(self, base: Path, monkeypatch) -> None:
        self.base = base
        self.home = base / "home"
        self.home.mkdir()
        monkeypatch.setenv("HOME", str(self.home))
        self.env = {"CLAUDE_CONFIG_DIR": str(self.home / ".claude")}
        self.project = base / "project"
        self.data = base / "data"
        for stub in STUBS:
            self.make_task(stub)
        self.make_task("delta", location="tmp")
        self.procs: list[subprocess.Popen] = []

    def make_task(self, stub: str, status: str = "active", location: str = "dev/tasks") -> Path:
        folder = self.project / location / stub
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "task.yaml").write_text(f"task:\n  status: {status}\n", encoding="utf-8")
        return folder

    @property
    def root(self) -> Path:
        return Path(os.path.realpath(self.project / "dev" / "tasks"))

    @property
    def tmp_root(self) -> Path:
        return Path(os.path.realpath(self.project / "tmp"))

    def file(self, host: str = "testhost", root: Path | None = None) -> Path:
        return (root or self.root) / ".sessions" / f"{host}.yaml"

    def proc(self) -> subprocess.Popen:
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        self.procs.append(proc)
        return proc

    def ident(self, session_id: str, proc: subprocess.Popen | None = None) -> harness.Identity:
        proc = proc or self.proc()
        return harness.Identity(session_id, proc.pid, procstamp.stamp_of(proc.pid))

    def kill(self, proc: subprocess.Popen) -> None:
        proc.kill()
        proc.wait()

    def register(self, ident, ref="alpha", machine=HOST, **kw):
        kw.setdefault("env", self.env)
        return sessions.register(ident, machine, self.project, ref, data_dir=self.data, **kw)

    def release(self, ident, ref="alpha", machine=HOST, **kw):
        return sessions.release(ident, machine, self.project, ref, data_dir=self.data, **kw)

    def refresh(self, ident, path=None, machine=HOST):
        return sessions.refresh(ident, machine, path, project_root=self.project, data_dir=self.data)

    def rekey(self, ident, path=None, machine=HOST):
        return sessions.rekey_after_clear(ident, machine, path, project_root=self.project, data_dir=self.data)

    def records(self, machine=HOST, root: Path | None = None):
        return sessions.read_root(root or self.root, machine).records

    def keys(self, machine=HOST, root: Path | None = None) -> list[tuple[str, str]]:
        return [(r.task, r.session_id) for r in self.records(machine, root)]

    def state(self, task: str, session_id: str, machine=HOST) -> str:
        return next(r.state for r in self.records(machine) if (r.task, r.session_id) == (task, session_id))

    def local_files(self) -> list[str]:
        """Every file under ``<data>/sessions/`` that is not an information-free lock file: there must be none."""
        directory = self.data / "sessions"
        if not directory.is_dir():
            return []
        return sorted(p.relative_to(directory).as_posix() for p in directory.rglob("*")
                      if p.is_file() and not (p.parent.name == "locks" and p.suffix == ".lock"))

    def close(self) -> None:
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
            proc.wait()


@pytest.fixture
def world(tmp_path, monkeypatch):
    built = World(tmp_path, monkeypatch)
    try:
        yield built
    finally:
        built.close()


def write_foreign_file(root: Path, machine: Machine, records: list[dict] | None = None) -> Path:
    path = root / ".sessions" / f"{machine.host}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {"_schema_version": 1, "host": machine.host, "machine": machine.fingerprint,
                "os_instance": machine.os_instance, "sessions": records or []}
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def foreign_record(task: str, session_id: str = "far-away-session", pid: int = 4242) -> dict:
    return {"session_id": session_id, "harness": "claude-code", "task": task, "checkout": "elsewhere",
            "registered_at": "2026-10-20T10:00:00Z",
            "process": {"pid": pid, "created": 1.5, "recorded_at": "2026-10-20T10:00:00Z"}}


# -- format ---------------------------------------------------------------------------------------------


def test_register_writes_the_documented_file(world):
    transcript = world.home / ".claude" / "projects" / "-proj" / "sess-one.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n", encoding="utf-8")
    ident = world.ident("sess-one")
    result = world.register(ident, "alpha")
    document = yaml.safe_load(world.file().read_text(encoding="utf-8"))
    assert list(document) == ["_schema_version", "host", "machine", "os_instance", "sessions"]
    assert (document["_schema_version"], document["host"], document["machine"], document["os_instance"]) == (
        1, "testhost", "abcdef0123456789", "macos")
    (record,) = document["sessions"]
    assert list(record) == ["session_id", "harness", "task", "checkout", "transcript_path", "registered_at", "process"]
    assert record["session_id"] == "sess-one" and record["task"] == "alpha" and record["harness"] == "claude-code"
    assert record["checkout"] == "project"
    assert record["transcript_path"] == "~/.claude/projects/-proj/sess-one.jsonl"
    assert record["process"]["pid"] == ident.pid and record["process"]["created"] == ident.stamp.created
    assert list(record["process"]) == ["pid", "created", "recorded_at"]
    assert result.action == sessions.ACTION_REGISTERED and result.changed_files == (world.file(),)
    assert result.record.state == sessions.STATE_RUNNING and result.record.owned


def test_no_absolute_path_is_written(world):
    world.register(world.ident("sess-one"), "alpha")
    assert str(world.base) not in world.file().read_text(encoding="utf-8")
    assert str(world.home) not in world.file().read_text(encoding="utf-8")


def test_a_missing_or_ambiguous_transcript_is_omitted_with_a_note(world):
    result = world.register(world.ident("sess-one"), "alpha")
    assert "transcript_path" not in yaml.safe_load(world.file().read_text(encoding="utf-8"))["sessions"][0]
    assert any("no transcript" in note for note in result.notes)


def test_the_file_is_ascii_even_for_a_non_ascii_checkout(world, tmp_path):
    project = tmp_path / "Caf\u00e9"
    (project / "dev" / "tasks" / "alpha").mkdir(parents=True)
    (project / "dev" / "tasks" / "alpha" / "task.yaml").write_text("task:\n  status: active\n", encoding="utf-8")
    sessions.register(world.ident("sess-one"), HOST, project, "alpha", data_dir=world.data, env=world.env)
    text = (project / "dev" / "tasks" / ".sessions" / "testhost.yaml").read_text(encoding="utf-8")
    text.encode("ascii")
    assert yaml.safe_load(text)["sessions"][0]["checkout"] == "Caf\u00e9"


def test_records_are_sorted_by_task_then_session(world):
    first, second = world.ident("sess-b"), world.ident("sess-a")
    world.register(first, "beta")
    world.register(second, "beta")
    world.register(first, "alpha")
    assert world.keys() == [("alpha", "sess-b"), ("beta", "sess-a"), ("beta", "sess-b")]


def test_an_unchanged_record_set_is_never_rewritten(world, monkeypatch):
    monkeypatch.setattr(sessions, "utc_now", lambda: "2026-10-21T10:00:00Z")
    ident = world.ident("sess-one")
    world.register(ident, "alpha")
    before = world.file().stat()
    again = world.register(ident, "alpha")
    after = world.file().stat()
    assert again.changed_files == () and (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns)
    assert world.release(world.ident("sess-zzz"), "alpha").changed_files == ()


def test_the_file_is_replaced_atomically_with_no_leftover(world):
    world.register(world.ident("sess-one"), "alpha")
    assert [p.name for p in (world.root / ".sessions").iterdir()] == ["testhost.yaml"]


# -- ownership, collisions, invalid files ---------------------------------------------------------------


def test_a_same_name_file_with_another_fingerprint_is_refused_for_writing_and_read_as_foreign(world):
    path = write_foreign_file(world.root, SAME_NAME_OTHER_MACHINE, [foreign_record("alpha")])
    before = path.read_bytes()
    with pytest.raises(sessions.HostCollisionError, match="sessions.host"):
        world.register(world.ident("sess-one"), "alpha")
    with pytest.raises(sessions.HostCollisionError):
        world.release(world.ident("sess-one"), "alpha")
    assert path.read_bytes() == before
    (record,) = world.records()
    assert not record.owned and record.state == sessions.STATE_UNVERIFIED


def test_an_invalid_own_file_is_not_overwritten(world):
    path = world.file()
    path.parent.mkdir(parents=True)
    path.write_text("sessions: [oops\n", encoding="utf-8")
    with pytest.raises(sessions.InvalidOwnFileError, match="not overwritten"):
        world.register(world.ident("sess-one"), "alpha")
    assert path.read_text(encoding="utf-8") == "sessions: [oops\n"


@pytest.mark.parametrize("mutate, reason", [
    (lambda d: d.update(_schema_version=2), "_schema_version"),
    (lambda d: d.update(host="elsewhere"), "does not equal the file name"),
    (lambda d: d.update(machine="not-hex"), "fingerprint"),
    (lambda d: d.update(os_instance="beos"), "os_instance"),
    (lambda d: d.update(extra=1), "unknown keys"),
    (lambda d: d["sessions"][0].update(transcript_path="/abs/path.jsonl"), "home-relative"),
    (lambda d: d["sessions"][0].pop("task"), "lacks"),
    (lambda d: d["sessions"][0]["process"].update(created="x"), "created"),
    (lambda d: d["sessions"].append(dict(d["sessions"][0])), "twice"),
])
def test_an_invalid_file_is_reported_as_a_note_and_its_records_are_ignored(world, mutate, reason):
    path = write_foreign_file(world.root, OTHER, [foreign_record("alpha")])
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(document)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    view = sessions.read_root(world.root, HOST)
    assert view.records == () and len(view.notes) == 1
    assert "otherhost.yaml" in view.notes[0] and reason in view.notes[0]


def test_unparseable_yaml_is_a_note(world):
    path = world.root / ".sessions" / "otherhost.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("a: [unclosed\n", encoding="utf-8")
    view = sessions.read_root(world.root, HOST)
    assert view.records == () and "not valid YAML" in view.notes[0]


def test_a_renamed_host_still_owns_its_old_file(world):
    old = Machine("oldname", HOST.fingerprint, "macos")
    ident = world.ident("sess-one")
    world.register(ident, "alpha", machine=old)
    old_path = world.file("oldname")
    assert old_path.exists()
    (record,) = world.records(HOST)
    assert record.owned and record.state == sessions.STATE_RUNNING and record.host == "oldname"
    # refreshed in place in the old file, not copied into the new host's file
    world.register(ident, "alpha", machine=HOST)
    assert not world.file("testhost").exists()
    assert world.keys() == [("alpha", "sess-one")]
    removed = world.release(None, "alpha", all_local=True)
    assert [r.session_id for r in removed.removed] == ["sess-one"] and world.records() == ()


def test_another_machines_records_are_unverified_reported_and_never_edited(world):
    path = write_foreign_file(world.root, OTHER, [foreign_record("alpha")])
    before = path.read_bytes()
    result = world.register(world.ident("sess-one"), "alpha")
    assert [(r.host, r.state) for r in result.others] == [("otherhost", sessions.STATE_UNVERIFIED)]
    world.release(None, "alpha", all_local=True)
    assert path.read_bytes() == before
    assert world.keys(HOST) == [("alpha", "far-away-session")]


# -- states ---------------------------------------------------------------------------------------------


def test_a_live_process_is_running_and_a_killed_one_is_not_running_and_its_record_is_kept(world):
    proc = world.proc()
    ident = world.ident("sess-one", proc)
    world.register(ident, "alpha")
    assert world.state("alpha", "sess-one") == sessions.STATE_RUNNING
    world.kill(proc)
    assert world.state("alpha", "sess-one") == sessions.STATE_NOT_RUNNING
    # another session's later write in the same root keeps it too
    world.register(world.ident("sess-two"), "beta")
    assert ("alpha", "sess-one") in world.keys()


def test_a_stamp_half_a_second_off_is_not_running(world, monkeypatch):
    proc = world.proc()
    world.register(world.ident("sess-one", proc), "alpha")
    document = yaml.safe_load(world.file().read_text(encoding="utf-8"))
    document["sessions"][0]["process"]["created"] += 0.5
    world.file().write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    assert world.state("alpha", "sess-one") == sessions.STATE_NOT_RUNNING
    monkeypatch.setattr(procstamp, "CREATED_ROUND_TRIP_S", 1.0)  # the dashboard's old window
    assert world.state("alpha", "sess-one") == sessions.STATE_RUNNING


def test_an_unreadable_process_table_is_unverified_with_a_note_not_not_running(world, monkeypatch):
    world.register(world.ident("sess-one"), "alpha")

    def refuse(_stamp, **_kw):
        raise procstamp.StampError("process table unreadable")

    monkeypatch.setattr(procstamp, "is_running", refuse)
    view = sessions.read_root(world.root, HOST)
    assert view.records[0].state == sessions.STATE_UNVERIFIED and "cannot be checked" in view.notes[0]


# -- register: takeover and coexistence -----------------------------------------------------------------


def test_the_same_session_registering_again_refreshes_in_place(world, monkeypatch):
    times = iter(["2026-10-21T10:00:00Z", "2026-10-21T11:00:00Z"])
    monkeypatch.setattr(sessions, "utc_now", lambda: next(times))
    ident = world.ident("sess-one")
    first = world.register(ident, "alpha")
    second = world.register(ident, "alpha")
    assert first.action == sessions.ACTION_REGISTERED and second.action == sessions.ACTION_REFRESHED
    (record,) = world.records()
    assert record.registered_at == "2026-10-21T10:00:00Z" and record.recorded_at == "2026-10-21T11:00:00Z"


def test_the_same_process_under_a_new_session_id_refreshes_in_place_and_rekeys(world):
    """A /clear whose hook did not run: the process is the caller's, only the session id differs."""
    proc = world.proc()
    world.register(world.ident("sess-old", proc), "alpha")
    result = world.register(world.ident("sess-new", proc), "alpha")
    assert result.action == sessions.ACTION_REFRESHED and result.others == () and result.kept_not_running == ()
    assert world.keys() == [("alpha", "sess-new")]
    assert world.local_files() == []


def test_a_running_session_is_kept_and_both_show(world):
    first = world.ident("sess-one")
    world.register(first, "alpha")
    result = world.register(world.ident("sess-two"), "alpha")
    assert [(r.session_id, r.state) for r in result.others] == [("sess-one", sessions.STATE_RUNNING)]
    assert result.taken_over == () and result.kept_not_running == ()
    assert world.keys() == [("alpha", "sess-one"), ("alpha", "sess-two")]


def test_a_not_running_session_is_kept_and_the_message_names_the_explicit_release(world):
    proc = world.proc()
    world.register(world.ident("sess-dead", proc), "alpha")
    world.kill(proc)
    result = world.register(world.ident("sess-two"), "alpha")
    assert result.taken_over == ()
    (kept,) = result.kept_not_running
    assert kept.record.session_id == "sess-dead" and kept.record.state == sessions.STATE_NOT_RUNNING
    assert "task sessions release alpha --all-local" in kept.message and "kept" in kept.message
    assert world.keys() == [("alpha", "sess-dead"), ("alpha", "sess-two")]


def test_the_takeover_rule_lives_in_one_named_function(world, monkeypatch):
    proc = world.proc()
    world.register(world.ident("sess-dead", proc), "alpha")
    world.kill(proc)
    seen = []

    def allow(record, ref):
        seen.append((record.session_id, ref))
        return sessions.TakeoverDecision(True, "allowed")

    monkeypatch.setattr(sessions, "not_running_takeover_policy", allow)
    result = world.register(world.ident("sess-two"), "alpha")
    assert seen == [("sess-dead", "alpha")]
    assert [r.session_id for r in result.taken_over] == ["sess-dead"] and result.kept_not_running == ()
    assert world.keys() == [("alpha", "sess-two")]


def test_a_second_task_leaves_the_first_registered_and_is_reported(world):
    ident = world.ident("sess-one")
    first = world.register(ident, "alpha")
    second = world.register(ident, "beta")
    assert first.also_holds == () and second.also_holds == ("alpha",)
    assert world.keys() == [("alpha", "sess-one"), ("beta", "sess-one")]
    assert world.local_files() == []


def test_a_tmp_task_registers_under_the_tmp_root(world):
    world.register(world.ident("sess-one"), "tmp/delta")
    assert world.keys(root=world.tmp_root) == [("delta", "sess-one")]
    assert world.keys() == []


def test_two_checkouts_of_one_physical_task_root_agree(tmp_path, monkeypatch):
    if os.name == "nt":
        pytest.skip("real symlinks need a privilege on Windows")
    world = World(tmp_path, monkeypatch)
    try:
        shared = tmp_path / "tasks-repo"
        (shared / "alpha").mkdir(parents=True)
        (shared / "alpha" / "task.yaml").write_text("task:\n  status: active\n", encoding="utf-8")
        projects = []
        for name in ("checkout-a", "checkout-b"):
            project = tmp_path / name
            (project / "dev").mkdir(parents=True)
            os.symlink(shared, project / "dev" / "tasks", target_is_directory=True)
            projects.append(project)
        proc = world.proc()
        ident = harness.Identity("sess-one", proc.pid, procstamp.stamp_of(proc.pid))
        sessions.register(ident, HOST, projects[0], "alpha", data_dir=world.data, env=world.env)
        (view_a,), (view_b,) = (sessions.read_project(p, HOST) for p in projects)
        assert view_a.root == view_b.root and view_a.records == view_b.records
        assert [r.checkout for r in view_b.records] == ["checkout-a"]
    finally:
        world.close()


def test_registering_a_task_that_does_not_exist_or_an_unsafe_session_id_is_an_error(world):
    with pytest.raises(sessions.SessionsError, match="matches no task folder"):
        world.register(world.ident("sess-one"), "nope")
    with pytest.raises(sessions.SessionsError, match="cannot be recorded"):
        world.register(world.ident("../escape"), "alpha")
    assert not (world.root / ".sessions").exists()


# -- records on closed, archived or absent tasks --------------------------------------------------------


def test_records_on_closed_archived_or_absent_tasks_are_shown_then_dropped_at_the_owners_next_write(world):
    ident = world.ident("sess-one")
    for stub in STUBS:
        world.register(ident, stub)
    other = world.ident("sess-two")
    world.make_task("alpha", status="closed")
    world.make_task("beta", status="archived")
    import shutil

    shutil.rmtree(world.root / "gamma")
    world.make_task("epsilon")
    states = {r.task: r.task_state for r in world.records()}
    assert states == {"alpha": "closed", "beta": "archived", "gamma": "absent"}
    assert all(r.dropped_on_next_write for r in world.records())
    result = world.register(other, "epsilon")
    assert sorted(r.task for r in result.dropped) == ["alpha", "beta", "gamma"]
    assert world.keys() == [("epsilon", "sess-two")]
    assert world.local_files() == []


def test_other_machines_records_on_closed_tasks_are_not_dropped(world):
    write_foreign_file(world.root, OTHER, [foreign_record("alpha")])
    world.make_task("alpha", status="closed")
    world.register(world.ident("sess-one"), "beta")
    assert world.keys(HOST) == [("alpha", "far-away-session"), ("beta", "sess-one")]


def test_a_task_that_cannot_be_classified_is_kept(world):
    world.register(world.ident("sess-one"), "alpha")
    (world.root / "alpha" / "task.yaml").write_text("not: [valid\n", encoding="utf-8")
    result = world.register(world.ident("sess-two"), "beta")
    assert result.dropped == () and ("alpha", "sess-one") in world.keys()


def test_a_deferred_or_blocked_task_keeps_its_registration(world):
    world.register(world.ident("sess-one"), "alpha")
    world.make_task("alpha", status="deferred")
    world.register(world.ident("sess-two"), "beta")
    assert ("alpha", "sess-one") in world.keys()


# -- release --------------------------------------------------------------------------------------------


def test_release_by_session_id_removes_only_that_session_on_that_task(world):
    one, two = world.ident("sess-one"), world.ident("sess-two")
    world.register(one, "alpha")
    world.register(one, "beta")
    world.register(two, "alpha")
    result = world.release(one, "alpha")
    assert [r.session_id for r in result.removed] == ["sess-one"]
    assert world.keys() == [("alpha", "sess-two"), ("beta", "sess-one")]
    assert world.local_files() == []


def test_release_by_process_stamp_removes_the_record_of_a_session_id_that_changed(world):
    """A /clear whose hook did not run, then hand-off: the record still carries the old id."""
    proc = world.proc()
    world.register(world.ident("sess-old", proc), "alpha")
    result = world.release(world.ident("sess-new", proc), "alpha")
    assert [r.session_id for r in result.removed] == ["sess-old"] and world.records() == ()


def test_release_leaves_a_process_that_is_not_the_callers_alone(world):
    world.register(world.ident("sess-one"), "alpha")
    assert world.release(world.ident("sess-two"), "alpha").removed == ()
    assert world.keys() == [("alpha", "sess-one")]


def test_release_all_local_removes_every_local_record_of_the_task_and_needs_no_identity(world):
    proc = world.proc()
    world.register(world.ident("sess-dead", proc), "alpha")
    world.kill(proc)
    world.register(world.ident("sess-live"), "alpha")
    world.register(world.ident("sess-other-task"), "beta")
    result = world.release(None, "alpha", all_local=True)
    assert sorted(r.session_id for r in result.removed) == ["sess-dead", "sess-live"]
    assert world.keys() == [("beta", "sess-other-task")]
    assert world.local_files() == []


def test_releasing_the_callers_registration_needs_an_identity(world):
    with pytest.raises(sessions.SessionsError, match="needs the caller's session identity"):
        world.release(None, "alpha")


def test_release_of_a_task_whose_folder_is_gone_resolves_a_bare_stub_through_the_records(world):
    import shutil

    world.register(world.ident("sess-one"), "alpha")
    shutil.rmtree(world.root / "alpha")
    result = world.release(None, "alpha", all_local=True)
    assert [r.task for r in result.removed] == ["alpha"] and world.records() == ()


def test_the_emptied_file_stays_with_an_empty_set(world):
    ident = world.ident("sess-one")
    world.register(ident, "alpha")
    world.release(ident, "alpha")
    assert yaml.safe_load(world.file().read_text(encoding="utf-8"))["sessions"] == []


def test_release_of_nothing_writes_nothing(world):
    result = world.release(world.ident("sess-one"), "alpha")
    assert result.removed == () and result.changed_files == () and not (world.root / ".sessions").exists()


# -- refresh (resume) -----------------------------------------------------------------------------------


def test_refresh_gives_every_record_of_the_session_the_resumed_process_and_transcript(world):
    old = world.proc()
    one = world.ident("sess-one", old)
    world.register(one, "alpha")
    world.register(one, "beta")
    world.register(world.ident("sess-two"), "alpha")
    world.kill(old)
    resumed = world.ident("sess-one")  # same session id, a new process
    result = world.refresh(resumed, "~/.claude/projects/-p/sess-one.jsonl")
    assert sorted(r.task for r in result.refreshed) == ["alpha", "beta"]
    assert len(result.changed_files) == 1
    for record in world.records():
        if record.session_id == "sess-one":
            assert record.stamp == resumed.stamp and record.state == sessions.STATE_RUNNING
            assert record.transcript_path == "~/.claude/projects/-p/sess-one.jsonl"
        else:
            assert record.session_id == "sess-two"
    assert world.local_files() == []


def test_refresh_writes_nothing_when_the_session_holds_no_record(world):
    world.register(world.ident("sess-one"), "alpha")
    before = world.file().read_bytes()
    result = world.refresh(world.ident("sess-stranger"))
    assert result.refreshed == () and result.changed_files == ()
    assert world.file().read_bytes() == before
    assert world.local_files() == []


def test_refresh_with_nothing_different_writes_nothing(world):
    ident = world.ident("sess-one")
    world.register(ident, "alpha", transcript_path="~/.claude/projects/-p/sess-one.jsonl")
    result = world.refresh(ident, "~/.claude/projects/-p/sess-one.jsonl")
    assert result.changed_files == () and result.refreshed == ()


def test_refresh_never_edits_another_machines_records(world):
    path = write_foreign_file(world.root, OTHER, [foreign_record("alpha", session_id="sess-one")])
    before = path.read_bytes()
    world.register(world.ident("sess-one"), "beta")
    world.refresh(world.ident("sess-one"))
    assert path.read_bytes() == before


# -- clear re-key and the lifecycle ---------------------------------------------------------------------


def test_two_registered_tasks_across_clear_exit_and_resume(world):
    """The user's lifecycle: register two tasks, /clear, exit, resume, hand off one."""
    first_process = world.proc()
    before_clear = world.ident("sess-before", first_process)
    world.register(before_clear, "alpha")
    world.register(before_clear, "beta")

    # /clear: a new session id, the same process
    after_clear = world.ident("sess-after", first_process)
    result = world.rekey(after_clear, "~/.claude/projects/-p/sess-after.jsonl")
    assert result.previous_ids == ("sess-before",)
    assert world.keys() == [("alpha", "sess-after"), ("beta", "sess-after")]
    assert all(r.state == sessions.STATE_RUNNING for r in world.records())

    # exit: the harness process ends; both registrations stay
    world.kill(first_process)
    assert [r.state for r in world.records()] == [sessions.STATE_NOT_RUNNING] * 2

    # resume of the post-clear conversation: same id, a new process
    resumed = world.ident("sess-after")
    assert world.refresh(resumed).refreshed and world.refresh(resumed).refreshed == ()
    assert [r.state for r in world.records()] == [sessions.STATE_RUNNING] * 2
    assert {r.stamp for r in world.records()} == {resumed.stamp}

    # hand-off of one task releases only that one
    world.release(resumed, "alpha")
    assert world.keys() == [("beta", "sess-after")]
    assert world.local_files() == []


def test_rekey_moves_every_registration_in_every_root(world):
    proc = world.proc()
    before = world.ident("sess-before", proc)
    world.register(before, "alpha")
    world.register(before, "tmp/delta")
    result = world.rekey(world.ident("sess-after", proc))
    assert len(result.rekeyed) == 2
    assert world.keys() == [("alpha", "sess-after")] and world.keys(root=world.tmp_root) == [("delta", "sess-after")]


def test_rekey_leaves_other_processes_registrations_alone(world):
    mine = world.proc()
    world.register(world.ident("sess-mine", mine), "alpha")
    world.register(world.ident("sess-bystander"), "beta")
    world.rekey(world.ident("sess-after", mine))
    assert world.keys() == [("alpha", "sess-after"), ("beta", "sess-bystander")]


def test_rekey_with_no_registration_of_the_process_writes_nothing(world):
    world.register(world.ident("sess-one"), "alpha")
    before = world.file().read_bytes()
    result = world.rekey(world.ident("sess-after"))
    assert result.previous_ids == () and result.changed_files == ()
    assert world.file().read_bytes() == before and world.local_files() == []


def test_rekey_merges_a_record_the_new_id_already_registered(world):
    """/task work ran under the new id before the hook did: one record per (task, session)."""
    proc = world.proc()
    world.register(world.ident("sess-before", proc), "alpha")
    # the same-process register already re-keys (refresh in place); recreate the split state by hand
    document = yaml.safe_load(world.file().read_text(encoding="utf-8"))
    duplicate = dict(document["sessions"][0], session_id="sess-after")
    document["sessions"].append(duplicate)
    world.file().write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    world.rekey(world.ident("sess-after", proc))
    assert world.keys() == [("alpha", "sess-after")]


def test_no_session_state_is_kept_in_the_data_directory_only_lock_files(world):
    """Guard (distributed truth, principle 1): register, release, refresh, re-key and dismiss leave nothing under
    ``<data>/sessions/`` but information-free lock files, however many sessions and roots are involved."""
    proc = world.proc()
    before = world.ident("sess-before", proc)
    for ref in ("alpha", "beta", "tmp/delta"):
        world.register(before, ref)
    assert world.local_files() == []
    world.rekey(world.ident("sess-after", proc), "~/.claude/projects/-p/sess-after.jsonl")
    assert world.local_files() == []
    world.kill(proc)
    world.refresh(world.ident("sess-after"), "~/.claude/projects/-p/sess-after-2.jsonl")
    assert world.local_files() == []
    world.release(world.ident("sess-after"), "alpha")
    world.release(None, "beta", all_local=True)
    assert world.local_files() == []
    locks = sorted(p.name for p in (world.data / "sessions").rglob("*") if p.is_file())
    assert locks and all(name.endswith(".lock") for name in locks), locks


def test_refresh_and_rekey_find_the_registrations_through_the_files_alone(world):
    """No data directory exists at all: the files in the project are enough to find, and to correct, a session."""
    proc = world.proc()
    ident = world.ident("sess-one", proc)
    world.register(ident, "alpha")
    world.register(ident, "tmp/delta")
    import shutil

    shutil.rmtree(world.data)  # whatever the machine kept locally is gone
    world.kill(proc)
    resumed = world.ident("sess-one")
    result = world.refresh(resumed)
    assert sorted(r.task for r in result.refreshed) == ["alpha", "delta"], "both roots are refreshed"
    assert {r.stamp for r in world.records()} == {resumed.stamp}
    shutil.rmtree(world.data)
    result = world.rekey(harness.Identity("sess-three", resumed.pid, resumed.stamp))  # /clear of the resumed process
    assert result.previous_ids == ("sess-one",) and len(result.rekeyed) == 2
    assert world.keys() == [("alpha", "sess-three")] and world.keys(root=world.tmp_root) == [("delta", "sess-three")]


def test_refresh_does_not_see_a_registration_in_another_project(world, tmp_path):
    """The case the files-only design does not cover: the session registered in a project other than the one scanned."""
    old = world.proc()
    world.register(world.ident("sess-one", old), "alpha")
    world.kill(old)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    result = sessions.refresh(world.ident("sess-one"), HOST, project_root=elsewhere, data_dir=world.data)
    assert result.refreshed == () and result.changed_files == ()
    assert world.records()[0].state == sessions.STATE_NOT_RUNNING


# -- read verbs, locks ----------------------------------------------------------------------------------


def test_reading_never_writes(world):
    write_foreign_file(world.root, OTHER, [foreign_record("alpha")])
    proc = world.proc()
    world.register(world.ident("sess-dead", proc), "beta")
    world.kill(proc)

    def snapshot() -> dict:
        return {str(p.relative_to(world.base)): (p.stat().st_mtime_ns, p.read_bytes())
                for p in world.base.rglob("*")
                if p.is_file() and (".sessions" in p.parts or "sessions" in p.parts)}

    before = snapshot()
    assert before
    sessions.read_root(world.root, HOST)
    sessions.read_project(world.project, HOST)
    assert snapshot() == before


def test_a_held_session_lock_makes_the_writer_fail_loudly(world, monkeypatch):
    monkeypatch.setattr(sessions, "LOCK_TIMEOUT_S", 0.2)
    ident = world.ident("sess-one")
    with oslock.FileLock(world.data / "sessions" / "locks" / "session-sess-one.lock", 5):
        with pytest.raises(sessions.SessionsError, match="holds a session lock"):
            world.register(ident, "alpha")
    assert not (world.root / ".sessions").exists()


def test_concurrent_registrations_into_one_root_all_survive(world):
    idents = [world.ident(f"sess-{n}") for n in range(8)]
    barrier = threading.Barrier(len(idents))
    errors = []

    def go(ident):
        try:
            barrier.wait()
            world.register(ident, "alpha")
        except Exception as exc:  # noqa: BLE001 -- reported through the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=go, args=(ident,)) for ident in idents]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert sorted(s for _t, s in world.keys()) == sorted(i.session_id for i in idents)


def test_to_home_relative(world):
    inside = world.home / ".claude" / "projects" / "p" / "s.jsonl"
    assert sessions.to_home_relative(inside) == "~/.claude/projects/p/s.jsonl"
    assert sessions.to_home_relative(world.base / "elsewhere" / "s.jsonl") is None


# -- dismissals: a human hides another machine's registration, recorded in git ------------------------------


def _other_registration(world, ref="alpha", session="far-session"):
    """A live registration of OTHER on ``ref``; returns (record, bytes of OTHER's file)."""
    world.register(world.ident(session), ref=ref, machine=OTHER)
    record = next(r for r in world.records(OTHER) if r.session_id == session and r.task == ref)
    return record, world.file("otherhost").read_bytes()


def _dismiss(world, record, machine=HOST):
    return sessions.dismiss(machine, world.project, record.host, record.session_id, record.registered_at,
                            data_dir=world.data)


def test_a_dismissal_goes_into_the_dismissers_own_file_and_never_into_the_targets(world):
    record, before = _other_registration(world)
    result = _dismiss(world, record)
    assert world.file("otherhost").read_bytes() == before  # one writer per file: the owner's file is untouched
    document = yaml.safe_load(world.file("testhost").read_text(encoding="utf-8"))
    assert document["dismissed"] == [{"host": "otherhost", "session_id": "far-session",
                                      "registered_at": record.registered_at, "dismissed_at": result.dismissal.dismissed_at}]
    assert result.changed_files == (world.file("testhost"),)
    assert result.dismissal.by == "testhost" and result.target.host == "otherhost"


def test_readers_hide_a_dismissed_record_for_every_machine(world):
    keep, _ = _other_registration(world, "beta", "stays")
    gone, _ = _other_registration(world, "alpha", "gone")
    assert {r.session_id for r in world.records(HOST)} == {"stays", "gone"}
    _dismiss(world, gone)
    assert {r.session_id for r in world.records(HOST)} == {"stays"}
    assert {r.session_id for r in world.records(OTHER)} == {"stays"}  # the owner reads it hidden too
    assert [d.session_id for d in sessions.read_root(world.root, HOST).dismissals] == ["gone"]
    assert keep.session_id == "stays"


def test_the_owners_next_write_drops_a_dismissed_record_even_though_its_process_is_alive_then_the_tombstone_goes(world):
    gone, _ = _other_registration(world, "alpha", "gone")
    assert world.state("alpha", "gone", OTHER) == "running"  # alive: the dismissal still wins
    _dismiss(world, gone)
    world.register(world.ident("other-later"), ref="beta", machine=OTHER)  # any write of the owner's file
    owner_file = yaml.safe_load(world.file("otherhost").read_text(encoding="utf-8"))
    assert [r["session_id"] for r in owner_file["sessions"]] == ["other-later"]
    assert "dismissed" in yaml.safe_load(world.file("testhost").read_text(encoding="utf-8"))  # not yet: it is HOST's file
    result = sessions.reconcile(HOST, world.root, data_dir=world.data)  # the dismisser's next write
    assert [d.session_id for d in result.tombstones] == ["gone"]
    assert "dismissed" not in yaml.safe_load(world.file("testhost").read_text(encoding="utf-8"))


def test_a_tombstone_is_kept_while_its_target_still_exists_and_while_a_file_is_unreadable(world):
    gone, _ = _other_registration(world, "alpha", "gone")
    _dismiss(world, gone)
    assert sessions.reconcile(HOST, world.root, data_dir=world.data).tombstones == ()  # the target is still there
    world.file("otherhost").write_text("not: [valid", encoding="utf-8")
    assert sessions.reconcile(HOST, world.root, data_dir=world.data).tombstones == ()  # and may live in the broken file
    assert "dismissed" in yaml.safe_load(world.file("testhost").read_text(encoding="utf-8"))


def test_a_new_registration_of_the_dismissed_session_shows_again(world, monkeypatch):
    gone, _ = _other_registration(world, "alpha", "gone")
    _dismiss(world, gone)
    monkeypatch.setattr(sessions, "utc_now", lambda: "2030-01-01T00:00:00Z")  # a later registered_at
    world.register(world.ident("gone"), ref="alpha", machine=OTHER)  # the same session works the task again
    shown = [r for r in world.records(HOST) if r.session_id == "gone"]
    assert len(shown) == 1 and shown[0].registered_at == "2030-01-01T00:00:00Z"
    assert shown[0].registered_at != gone.registered_at


def test_dismissing_is_idempotent_and_refuses_the_dismissers_own_record_and_an_unknown_one(world):
    gone, _ = _other_registration(world)
    first = _dismiss(world, gone)
    again = _dismiss(world, gone)
    assert again.changed_files == () and again.dismissal == first.dismissal
    world.register(world.ident("mine"), ref="beta")
    mine = next(r for r in world.records(HOST) if r.session_id == "mine")
    with pytest.raises(sessions.SessionsError, match="own registration"):
        _dismiss(world, mine)
    with pytest.raises(sessions.SessionsError, match="no registration"):
        sessions.dismiss(HOST, world.project, "otherhost", "nobody", "2026-10-20T10:00:00Z", data_dir=world.data)


def test_a_file_without_dismissals_is_serialized_exactly_as_before(world):
    world.register(world.ident("plain"), ref="alpha")
    text = world.file("testhost").read_text(encoding="utf-8")
    assert "dismissed" not in text
    document = yaml.safe_load(text)
    assert list(document) == ["_schema_version", "host", "machine", "os_instance", "sessions"]
