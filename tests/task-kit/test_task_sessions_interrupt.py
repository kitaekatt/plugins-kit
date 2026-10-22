"""The session store survives an interruption after every phase of register, release, refresh and clear re-key.

Each operation names its phases with ``sessions._checkpoint``. For every phase of an uninterrupted run, a fresh
scenario is interrupted there (the exception releases the OS locks as a dying process would), and then:

1. the git-tracked root files are the only state, so nothing else can have been lost: no local file exists but
   information-free lock files, every task the operation was not removing still has a record of this machine
   (under the old or the new session id), and a reader still works;
2. running the same operation again completes it to the same end state: each root file is replaced in one
   atomic write and the operation rediscovers its remaining work from the files (the session id, or the
   process stamp for a re-key), so every phase is idempotent.

The order assertions pin the protocol: nothing visible changes before a root's single file write.
"""

from __future__ import annotations

import pytest
from test_task_sessions_store import HOST, World, world  # noqa: F401 -- the fixture is used by name

from task_system import procstamp, sessions


class Interrupted(Exception):
    """Stands for the process dying at a checkpoint."""


def record_checkpoints(monkeypatch, interrupt_at: int | None = None) -> list[str]:
    names: list[str] = []

    def checkpoint(name: str) -> None:
        names.append(name)
        if interrupt_at is not None and len(names) - 1 == interrupt_at:
            raise Interrupted(name)

    monkeypatch.setattr(sessions, "_checkpoint", checkpoint)
    return names


def assert_files_alone_hold_everything(scenario: "Scenario", where: str) -> None:
    world = scenario.world
    assert world.local_files() == [], f"{where}: local state other than locks appeared"
    for root, task in scenario.must_keep:
        assert [r for r in world.records(root=root) if r.task == task], f"{where}: the record of {task} was lost"
    for root in (world.root, world.tmp_root):
        sessions.read_root(root, HOST)  # a reader copes with the half-done state


# -- one scenario per operation: setup, the operation, the end state -----------------------------------


class Scenario:
    name = ""
    must_keep: tuple = ()  # (root, task) pairs that keep a record of this machine at every point

    def __init__(self, world: World) -> None:
        self.world = world

    def setup(self) -> None: ...

    def operate(self) -> None: ...

    def check_done(self) -> None: ...


class RegisterScenario(Scenario):
    """A new session id on a process that holds a record under an old id, with a closed task to drop."""

    name = "register"

    @property
    def must_keep(self):
        return ((self.world.root, "alpha"),)

    def setup(self) -> None:
        self.proc = self.world.proc()
        old = self.world.ident("sess-old", self.proc)
        self.world.register(old, "alpha")
        self.world.register(old, "beta")
        self.world.make_task("beta", status="closed")
        self.new = self.world.ident("sess-new", self.proc)

    def operate(self) -> None:
        self.world.register(self.new, "alpha")

    def check_done(self) -> None:
        assert self.world.keys() == [("alpha", "sess-new")]


class ReleaseScenario(Scenario):
    name = "release"

    @property
    def must_keep(self):
        return ((self.world.root, "beta"),)

    def setup(self) -> None:
        self.ident = self.world.ident("sess-one")
        self.world.register(self.ident, "alpha")
        self.world.register(self.ident, "beta")

    def operate(self) -> None:
        self.world.release(self.ident, "alpha")

    def check_done(self) -> None:
        assert self.world.keys() == [("beta", "sess-one")]


class RefreshScenario(Scenario):
    name = "refresh"

    @property
    def must_keep(self):
        return ((self.world.root, "alpha"), (self.world.root, "beta"))

    def setup(self) -> None:
        old = self.world.proc()
        first = self.world.ident("sess-one", old)
        self.world.register(first, "alpha")
        self.world.register(first, "beta")
        self.world.kill(old)
        self.resumed = self.world.ident("sess-one")

    def operate(self) -> None:
        self.world.refresh(self.resumed, "~/.claude/projects/-p/sess-one.jsonl")

    def check_done(self) -> None:
        assert [r.stamp for r in self.world.records()] == [self.resumed.stamp] * 2
        assert {r.transcript_path for r in self.world.records()} == {"~/.claude/projects/-p/sess-one.jsonl"}


class RekeyScenario(Scenario):
    """/clear with two tasks in the dev/tasks root and one in the tmp root."""

    name = "rekey"

    @property
    def must_keep(self):
        return ((self.world.root, "alpha"), (self.world.root, "beta"), (self.world.tmp_root, "delta"))

    def setup(self) -> None:
        self.proc = self.world.proc()
        before = self.world.ident("sess-before", self.proc)
        for ref in ("alpha", "beta", "tmp/delta"):
            self.world.register(before, ref)
        self.after = self.world.ident("sess-after", self.proc)

    def operate(self) -> None:
        self.world.rekey(self.after)

    def check_done(self) -> None:
        assert self.world.keys() == [("alpha", "sess-after"), ("beta", "sess-after")]
        assert self.world.keys(root=self.world.tmp_root) == [("delta", "sess-after")]


SCENARIOS = (RegisterScenario, ReleaseScenario, RefreshScenario, RekeyScenario)
ORDER = {  # the protocol: (checkpoint that must come first, the one that must come after)
    "register": ("before_write", "root_written"),
    "release": ("before_write", "root_written"),
    "refresh": ("before_write", "root_written"),
    "rekey": ("before_write:0", "root_written:0"),
}


@pytest.mark.parametrize("scenario_class", SCENARIOS, ids=lambda cls: cls.name)
def test_the_protocol_changes_nothing_before_a_roots_single_file_write(tmp_path, monkeypatch, scenario_class):
    (tmp_path / "ordering").mkdir()
    scenario = scenario_class(World(tmp_path / "ordering", monkeypatch))
    try:
        scenario.setup()
        names = record_checkpoints(monkeypatch)
        scenario.operate()
        first, second = ORDER[scenario_class.name]
        assert first in names and second in names, names
        assert names.index(first) < names.index(second), names
        scenario.check_done()
        assert scenario.world.local_files() == []
    finally:
        scenario.world.close()


@pytest.mark.parametrize("scenario_class", SCENARIOS, ids=lambda cls: cls.name)
def test_an_interruption_after_any_phase_loses_nothing_and_a_rerun_completes(tmp_path, monkeypatch, scenario_class):
    probe_base = tmp_path / "probe"
    probe_base.mkdir()
    probe = scenario_class(World(probe_base, monkeypatch))
    try:
        probe.setup()
        phases = record_checkpoints(monkeypatch)
        probe.operate()
        probe.check_done()
    finally:
        probe.world.close()
    phases = list(phases)
    assert len(phases) >= 2, phases
    for number, phase in enumerate(phases):
        base = tmp_path / f"case-{number}"
        base.mkdir()
        scenario = scenario_class(World(base, monkeypatch))
        try:
            scenario.setup()
            record_checkpoints(monkeypatch, interrupt_at=number)
            with pytest.raises(Interrupted):
                scenario.operate()
            where = f"{scenario_class.name} interrupted at #{number} {phase}"
            assert_files_alone_hold_everything(scenario, where)
            record_checkpoints(monkeypatch)
            scenario.operate()  # the rerun
            try:
                scenario.check_done()
            except AssertionError as exc:
                raise AssertionError(f"{where}: the rerun did not complete: {exc}") from exc
            assert_files_alone_hold_everything(scenario, where + " (after rerun)")
        finally:
            scenario.world.close()


def test_an_interrupted_clear_before_any_record_moves_leaves_every_record_under_the_old_id(tmp_path, monkeypatch):
    base = tmp_path / "union"
    base.mkdir()
    scenario = RekeyScenario(World(base, monkeypatch))
    try:
        scenario.setup()
        record_checkpoints(monkeypatch, interrupt_at=0)  # inside the first root, before its file is written
        with pytest.raises(Interrupted, match="before_write:0"):
            scenario.operate()
        assert {sid for _t, sid in scenario.world.keys()} == {"sess-before"}  # no record has moved yet
        assert {sid for _t, sid in scenario.world.keys(root=scenario.world.tmp_root)} == {"sess-before"}
        assert scenario.world.local_files() == []
    finally:
        scenario.world.close()


def test_a_clear_interrupted_between_roots_is_found_again_by_the_process_stamp(tmp_path, monkeypatch):
    """One root is already moved, the other is not; the rerun starts from the files alone."""
    base = tmp_path / "between"
    base.mkdir()
    scenario = RekeyScenario(World(base, monkeypatch))
    try:
        scenario.setup()
        record_checkpoints(monkeypatch, interrupt_at=1)  # after the first root, before the second
        with pytest.raises(Interrupted, match="root_written:0"):
            scenario.operate()
        assert {sid for _t, sid in scenario.world.keys(root=scenario.world.tmp_root)} == {"sess-after"}  # tmp moved
        assert {sid for _t, sid in scenario.world.keys()} == {"sess-before"}  # dev/tasks not yet
        record_checkpoints(monkeypatch)
        scenario.operate()
        scenario.check_done()
        assert procstamp.same_process(scenario.world.records()[0].stamp, scenario.after.stamp)
    finally:
        scenario.world.close()
