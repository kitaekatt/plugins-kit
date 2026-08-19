"""B1 EXIT-CRITERION scenario test (step 13).

Pins, as one continuous story driven against a scripted fake ``claude``, the
exit criterion recorded verbatim in
``docs/planning/content-pipeline-kit/session-recipients-plan.md`` for
"B1 -- Background driver and one-unit worker":

    At N=2, two units run concurrently against the fake; killing one worker
    causes reclaim after expiry; its late submission is FENCED; a hard halt
    quiesces per policy; resume completes only unfinished work; the A-min
    finalizer applies all accepted output.

Plus the ``blocked`` -> settled -> lease-expired -> reclaimable chain (see
"Why the blocked branch SETTLES" below).

**No test here ever reaches a real ``claude`` process, and none consumes live
quota.** ``_no_real_claude_subprocess`` (autouse) replaces
``claude_bg._default_runner`` with a stub that fails the test outright, and
every ``claude`` response comes from :class:`ScenarioRunner`, a ``runner=``
CALLABLE on :class:`~content_pipeline.execution.drivers.claude_bg.ClaudeCli`
-- never an executable on ``PATH``. The shared scripting fixtures are reused
from ``test_execution_driver_claude_bg`` rather than re-derived.

**Time is injected, never slept.** ``dispatch_wave`` takes ``clock_fn`` and
``sleep_fn``; this module passes a :class:`Clock` whose ``sleep_fn`` ADVANCES
it. So the wave's own poll interval is the simulation step, lease expiry
happens at an exact known instant, and nothing waits on a wall clock.

**Why every clause is a separate test over a re-run scenario.** One long test
would report only its first failure, and a scenario test is exactly the shape
that passes vacuously (a fence assertion that never fires because no late
submission was constructed; a reclaim assertion that passes because the unit
was never claimed). :func:`run_scenario` therefore replays the whole story
into a :class:`Scenario` record, and each clause asserts over its OWN fresh
replay -- so a mutation to the implementation reddens exactly the clause it
breaks. Each clause below was verified by mutating the shipped code and
watching that clause's assertion fail; the mutations are named in the
docstring of each test.

**Why the blocked branch SETTLES (``blocked-reclaim-chain-untested``).** D5
says a ``blocked`` session stops being renewed, with no grace. Stopping
renewal ALONE would leave the dispatch OPEN -- and ``reclaimable_units``
requires no open dispatch -- so the unit would be permanently unreclaimable
even after its lease expired, reintroducing the 19-day stall the rule exists
to prevent. ``supervise_tick`` therefore also settles the dispatch. The
consequence is pinned end-to-end here: a later ``dispatch_wave`` actually
re-dispatches the blocked unit once its lease expires.
"""

from __future__ import annotations

import json
import re

import pytest

from content_pipeline.execution.adapter import RunAdapter
from content_pipeline.execution.controller import (
    finalize_run,
    resume_run,
    unfinished_units,
)
from content_pipeline.execution.drivers import claude_bg
from content_pipeline.execution.drivers.claude_bg import (
    ClaudeCli,
    OpenDispatch,
    WorkerCommand,
    dispatch_wave,
    reclaimable_units,
    supervise_tick,
)
from content_pipeline.execution.model import (
    AttemptKind,
    RunHaltedError,
    StaleFenceError,
    UnitState,
)
from content_pipeline.execution.store import ExecutionStore

# Reuse (not re-derive) the scripted-fake fixtures the step 1-11 suite
# established: FakeRunner's argv-prefix scripting, the preflight-passing
# script set, and the background-record shape.
from test_execution_driver_claude_bg import FakeRunner, _bg_record, _healthy_runner

RUN_ID = "run-1"
POLL_INTERVAL = 100.0  # the simulation step; see "Time is injected" above
LEASE = 300.0  # store.DEFAULT_LEASE_SECONDS -- what lease_for(None) yields

T_START = 1000.0
T_KILL = 1100.0  # worker A's session disappears from `agents --json`
T_RATE_LIMIT = 1400.0  # u0's SECOND session fails; classified as rate_limit
T_LATE_SUBMIT_U1 = 1500.0  # u1's in-flight worker submits AFTER the halt


@pytest.fixture(autouse=True)
def _no_real_claude_subprocess(monkeypatch):
    """No test in this module may reach a real subprocess for `claude`."""

    def _raise(*args, **kwargs):
        raise AssertionError(
            "a test reached the REAL default claude runner; every test must "
            "supply its own scripted `runner=` instead"
        )

    monkeypatch.setattr(claude_bg, "_default_runner", _raise)


# ---------------------------------------------------------------------------
# Deterministic time
# ---------------------------------------------------------------------------


class Clock:
    """A callable clock advanced ONLY by the driver's own ``sleep_fn``.

    ``deadline`` converts the one failure mode this simulation can produce --
    a wave whose exit condition is never reached, which would otherwise spin
    forever with no wall-clock cost to notice -- into a loud red test."""

    def __init__(self, t: float = T_START, *, deadline: float = T_START + 100_000) -> None:
        self.t = t
        self.deadline = deadline

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds
        if self.t > self.deadline:
            raise AssertionError(
                f"simulated time ran past {self.deadline}: the dispatch loop "
                "never reached an exit condition"
            )


# ---------------------------------------------------------------------------
# The fake platform: a `claude` whose background sessions have a lifetime
# ---------------------------------------------------------------------------


class World:
    """The scripted platform behind :class:`ScenarioRunner`.

    Holds the background sessions ``agents --json`` reports, mints a short
    id / session id per launch, and runs the caller's ``on_launch`` hook --
    which is where this module simulates a WORKER (claiming, submitting,
    or submitting late with a stale fence). ``schedule`` entries fire at
    most once, at the first poll at or after their time, so a session's
    state changes at an exact, known instant.
    """

    def __init__(self, store: ExecutionStore, clock: Clock) -> None:
        self.store = store
        self.clock = clock
        self.sessions: dict = {}  # short_id -> {"session_id":..., "state":...}
        self.launches: list = []  # dicts: unit_id / worker_id / short_id / session_id / at
        self.schedule: list = []  # (time, name, callable); each fires once
        self._fired: set = set()
        self.on_launch = None
        self.max_concurrent_claimed = 0
        self.max_concurrent_running = 0
        self._counter = 0

    # -- observation ------------------------------------------------------

    def _observe(self) -> None:
        claimed = [
            u.unit_id
            for u in self.store.list_units(RUN_ID)
            if u.state is UnitState.CLAIMED
        ]
        self.max_concurrent_claimed = max(self.max_concurrent_claimed, len(claimed))
        # `agents --json --all` lists settled corpses too, so only sessions
        # still WORKING count as concurrently occupied slots.
        running = [s for s in self.sessions.values() if s["state"] == "working"]
        self.max_concurrent_running = max(self.max_concurrent_running, len(running))

    # -- platform surface -------------------------------------------------

    def listing(self) -> list:
        for when, name, fn in list(self.schedule):
            if name not in self._fired and self.clock.t >= when:
                self._fired.add(name)
                fn()
        self._observe()
        return [
            _bg_record(id=short_id, session_id=s["session_id"], state=s["state"])
            for short_id, s in self.sessions.items()
        ]

    def launch(self, prompt: str):
        run_id = re.search(r"Run id: (\S+)", prompt).group(1)
        unit_id = re.search(r"Unit id: (\S+)", prompt).group(1)
        worker_id = re.search(r"Worker id: (\S+)", prompt).group(1)
        assert run_id == RUN_ID
        self._counter += 1
        # Hex: `_parse_launch_session_id`'s banner regex accepts hex only.
        short_id = f"{0xA0000000 + self._counter:08x}"
        session_id = f"sess-{self._counter}"
        self.sessions[short_id] = {"session_id": session_id, "state": "working"}
        record = {
            "unit_id": unit_id,
            "worker_id": worker_id,
            "short_id": short_id,
            "session_id": session_id,
            "at": self.clock.t,
        }
        self.launches.append(record)
        if self.on_launch is not None:
            self.on_launch(record)
        self._observe()
        return (f"backgrounded * {short_id}", "", 0)

    # -- helpers used by the timeline -------------------------------------

    def kill(self, short_id: str) -> None:
        """A killed worker: its session vanishes from `agents --json --all`."""
        del self.sessions[short_id]

    def set_state(self, short_id: str, state: str) -> None:
        self.sessions[short_id]["state"] = state


class ScenarioRunner(FakeRunner):
    """A ``runner`` callable: preflight answers come from the shared
    ``_healthy_runner`` script set; ``agents --json`` and ``--bg`` are served
    live by :class:`World`."""

    def __init__(self, world: World) -> None:
        super().__init__(scripts=dict(_healthy_runner().scripts))
        self.world = world

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        if argv[1:3] == ["agents", "--json"]:
            self.calls.append((argv, kwargs))
            return (json.dumps(self.world.listing()), "", 0)
        if argv[1:2] == ["--bg"] and argv[2:3] != ["-p"]:
            self.calls.append((argv, kwargs))
            return self.world.launch(argv[-1])
        return super().__call__(argv, **kwargs)


def _launch_count(runner: ScenarioRunner) -> int:
    return sum(
        1
        for argv, _kwargs in runner.calls
        if argv[1:2] == ["--bg"] and argv[2:3] != ["-p"]
    )


def _worker_command(tmp_path) -> WorkerCommand:
    answer_dir = tmp_path / "answers"
    answer_dir.mkdir(exist_ok=True)
    return WorkerCommand(argv=("python", "mytool.py", "run"), answer_dir=str(answer_dir))


def _seeded_store(tmp_path, *, unit_ids=("u0", "u1")) -> ExecutionStore:
    store = ExecutionStore(tmp_path / "run.db")
    store.create_run(
        RUN_ID, driver="claude_bg", backend="claude-bg", model="m", adapter_version=""
    )
    store.register_units(RUN_ID, list(unit_ids))
    return store


# ---------------------------------------------------------------------------
# The scenario
# ---------------------------------------------------------------------------


class Scenario:
    """Everything the replay observed, so each clause can assert over it."""

    def __init__(self) -> None:
        self.store = None
        self.clock = None
        self.world = None
        self.runner = None
        self.adapter = None
        self.report_wave1 = None
        self.report_wave2 = None
        self.unfinished_at_halt = ()
        self.claim_probe_error = None
        self.late_submit_error = None
        self.late_submit_text = "LATE ANSWER FROM THE KILLED WORKER"
        self.fence_before_reclaim = None
        self.fence_after_reclaim = None
        self.u0_lease_at_kill = None
        self.run_halted_when_u1_accepted = None
        self.applied = ()
        self.applied_payloads = {}
        self.accepted_texts = {}


def run_scenario(tmp_path, monkeypatch) -> Scenario:
    """Replay the whole exit-criterion story once. Pure fakes; no wall clock."""
    sc = Scenario()
    store = sc.store = _seeded_store(tmp_path)
    clock = sc.clock = Clock()
    world = sc.world = World(store, clock)
    runner = sc.runner = ScenarioRunner(world)
    cli = ClaudeCli(executable="claude", runner=runner)
    wc = _worker_command(tmp_path)

    sc.applied_payloads = {}

    def _apply(unit_id, payload):
        sc.applied_payloads[unit_id] = payload

    adapter = sc.adapter = RunAdapter(
        parse_fn=lambda text: {"answer": text},
        apply=_apply,
    )

    # Halt classification is the ONLY thing this scenario stubs on the
    # library side: the real one reads ~/.claude, which a test must not.
    # Keyed by session id so exactly one settled session halts the run.
    halt_by_session = {}
    monkeypatch.setattr(
        claude_bg,
        "classify_settled_failure",
        lambda session_id, **kwargs: halt_by_session.get(session_id),
    )

    state = {"u0_first": None, "u0_fence_first": None}

    def on_launch(rec):
        """Stand in for the launched WORKER's own protocol calls."""
        unit_id = rec["unit_id"]
        claim = store.claim_unit(RUN_ID, unit_id, rec["worker_id"], at=clock.t)
        rec["fencing_token"] = claim.fencing_token

        if unit_id == "u0" and state["u0_first"] is None:
            # First worker for u0. Remember it so it can be killed, and so
            # its stale fence can be replayed as a late submission later.
            state["u0_first"] = rec
            state["u0_fence_first"] = claim.fencing_token
            # Timeline: this worker is killed mid-flight.
            world.schedule.append(
                (T_KILL, "kill-u0-worker", lambda: _kill_u0_worker(rec))
            )
        elif unit_id == "u0":
            # The RECLAIM dispatch. The fence has just moved; the killed
            # worker's answer arrives now, too late.
            sc.fence_before_reclaim = state["u0_fence_first"]
            sc.fence_after_reclaim = claim.fencing_token
            try:
                store.accept_unit(
                    RUN_ID,
                    "u0",
                    state["u0_fence_first"],
                    text=sc.late_submit_text,
                    at=clock.t,
                )
            except StaleFenceError as exc:
                sc.late_submit_error = exc
            # This second session hits a rate limit.
            halt_by_session[rec["session_id"]] = "rate_limit"
            world.schedule.append(
                (
                    T_RATE_LIMIT,
                    "rate-limit-u0",
                    lambda: world.set_state(rec["short_id"], "failed"),
                )
            )
        elif unit_id == "u1":
            # u1's worker is still in flight when the halt lands, and
            # submits afterwards with a VALID fence (D4).
            world.schedule.append(
                (T_LATE_SUBMIT_U1, "u1-post-halt-submit", lambda: _u1_submits(rec))
            )

    def _kill_u0_worker(rec):
        sc.u0_lease_at_kill = store.get_unit(RUN_ID, "u0").lease_expires_at
        world.kill(rec["short_id"])

    def _u1_submits(rec):
        run = store.get_run(RUN_ID)
        sc.run_halted_when_u1_accepted = run.halted_kind
        store.accept_unit(
            RUN_ID, "u1", rec["fencing_token"], text="u1 answer", at=clock.t
        )
        world.set_state(rec["short_id"], "done")

    world.on_launch = on_launch

    # -- wave 1: N=2, kill, reclaim, fenced late submit, halt -------------
    sc.report_wave1 = dispatch_wave(
        store,
        RUN_ID,
        store.list_units(RUN_ID),
        adapter,
        cli=cli,
        worker_command=wc,
        max_agents=2,
        poll_interval_s=POLL_INTERVAL,
        env={},
        sleep_fn=clock.advance,
        clock_fn=clock,
    )

    sc.unfinished_at_halt = tuple(u.unit_id for u in unfinished_units(store, RUN_ID))

    # Halt blocks new claims. Probed against a TERMINAL unit on purpose: the
    # halt check precedes the state check, so a halted run raises
    # RunHaltedError and an un-halted one raises TerminalStateError -- the
    # probe distinguishes them without ever mutating a unit.
    try:
        store.claim_unit(RUN_ID, "u1", "probe-worker", at=clock.t)
    except Exception as exc:  # noqa: BLE001 -- the exception TYPE is the observation
        sc.claim_probe_error = exc

    # -- resume ------------------------------------------------------------
    resume_run(store, RUN_ID)
    clock.advance(POLL_INTERVAL)

    def on_launch_wave2(rec):
        claim = store.claim_unit(RUN_ID, rec["unit_id"], rec["worker_id"], at=clock.t)
        rec["fencing_token"] = claim.fencing_token
        store.accept_unit(
            RUN_ID, rec["unit_id"], claim.fencing_token, text="u0 answer", at=clock.t
        )
        world.set_state(rec["short_id"], "done")

    world.on_launch = on_launch_wave2

    # The FULL unit list is passed deliberately, not just the unfinished
    # one: "resume completes only unfinished work" must hold because the
    # driver selects candidates by state, not because the caller pre-filtered.
    sc.report_wave2 = dispatch_wave(
        store,
        RUN_ID,
        store.list_units(RUN_ID),
        adapter,
        cli=cli,
        worker_command=wc,
        max_agents=2,
        poll_interval_s=POLL_INTERVAL,
        env={},
        sleep_fn=clock.advance,
        clock_fn=clock,
    )

    sc.accepted_texts = {
        u.unit_id: u.accepted_text for u in store.list_units(RUN_ID)
    }

    # -- finalize ----------------------------------------------------------
    sc.applied = tuple(finalize_run(store, RUN_ID, adapter, at=clock.t))
    return sc


@pytest.fixture
def scenario(tmp_path, monkeypatch) -> Scenario:
    return run_scenario(tmp_path, monkeypatch)


# ===========================================================================
# Clause 1 -- "At N=2, two units run concurrently against the fake"
# ===========================================================================


def test_clause1_two_units_run_concurrently(scenario):
    """MUTATION: cap the loop's ``free_slots`` at 1 -- the fake then never
    observes two claimed units or two live sessions at once -> red."""
    assert scenario.world.max_concurrent_claimed == 2
    assert scenario.world.max_concurrent_running == 2

    first_two = scenario.world.launches[:2]
    assert [rec["unit_id"] for rec in first_two] == ["u0", "u1"]
    # Concurrent means at the SAME instant, with distinct worker identities
    # and distinct sessions -- not two launches that merely both happened.
    assert first_two[0]["at"] == first_two[1]["at"] == T_START
    assert first_two[0]["worker_id"] != first_two[1]["worker_id"]
    assert first_two[0]["session_id"] != first_two[1]["session_id"]


def test_clause1_the_n_equals_2_bound_is_never_exceeded(tmp_path, monkeypatch):
    """N=2 is a CEILING, not just a count. This needs a THIRD unit to be a
    real assertion: with only the scenario's two units in the run, "never
    more than two at once" holds no matter what the driver does.

    MUTATION: make ``free_slots`` ignore ``max_agents`` -- all three units
    are launched in the first pass and three sessions are live at once -> red."""
    monkeypatch.setattr(claude_bg, "classify_settled_failure", lambda *a, **k: None)
    store = _seeded_store(tmp_path, unit_ids=("u0", "u1", "u2"))
    clock = Clock()
    world = World(store, clock)
    runner = ScenarioRunner(world)
    cli = ClaudeCli(executable="claude", runner=runner)

    def on_launch(rec):
        claim = store.claim_unit(RUN_ID, rec["unit_id"], rec["worker_id"], at=clock.t)
        unit_id, short_id = rec["unit_id"], rec["short_id"]

        def _finish():
            store.accept_unit(
                RUN_ID, unit_id, claim.fencing_token, text="answer", at=clock.t
            )
            world.set_state(short_id, "done")

        # One full simulation step of overlap before this worker finishes, so
        # a slot is genuinely occupied while the next unit wants one.
        # (Submission and session exit are deliberately simultaneous here --
        # see test_defect_supervise_tick_renews_an_already_accepted_unit.)
        world.schedule.append((clock.t + POLL_INTERVAL, f"done-{short_id}", _finish))

    world.on_launch = on_launch

    report = dispatch_wave(
        store, RUN_ID, store.list_units(RUN_ID), RunAdapter(), cli=cli,
        worker_command=_worker_command(tmp_path), max_agents=2,
        poll_interval_s=POLL_INTERVAL, env={},
        sleep_fn=clock.advance, clock_fn=clock,
    )
    assert set(report.dispatched) == {"u0", "u1", "u2"}  # all three did run
    assert world.max_concurrent_running <= 2  # but never more than N at once
    assert world.max_concurrent_claimed <= 2
    # And the ceiling actually bound: the third launch waited for a free slot.
    assert world.launches[2]["at"] > world.launches[1]["at"]


# ===========================================================================
# Clause 2 -- "killing one worker causes reclaim after expiry"
# ===========================================================================


def test_clause2_killed_worker_is_reclaimed(scenario):
    """MUTATION: make ``reclaimable_units`` return ``[]`` -- u0 is never
    re-dispatched -> red."""
    u0_launches = [
        r
        for r in scenario.world.launches
        if r["unit_id"] == "u0" and r["at"] <= T_LATE_SUBMIT_U1  # wave 1 only
    ]
    assert len(u0_launches) == 2, "the killed worker's unit was never reclaimed"
    assert u0_launches[0]["worker_id"] != u0_launches[1]["worker_id"]
    assert scenario.report_wave1.dispatched.count("u0") == 2

    expires = [
        a
        for a in scenario.store.list_attempts(RUN_ID, "u0")
        if a.kind is AttemptKind.EXPIRE
    ]
    assert len(expires) == 1
    assert expires[0].worker_id == u0_launches[0]["worker_id"]


def test_clause2_the_reclaim_happens_only_AFTER_lease_expiry(scenario):
    """MUTATION: drop the ``lease_expires_at <= now`` term from
    ``reclaimable_units`` -- the reclaim then lands at the kill instant,
    before the lease expired -> red."""
    kill_lease = scenario.u0_lease_at_kill
    assert kill_lease is not None, "the kill hook never fired"
    reclaim_at = [r for r in scenario.world.launches if r["unit_id"] == "u0"][1]["at"]
    assert T_KILL < kill_lease, "the fixture must kill the worker while its lease is LIVE"
    # Not merely "after the kill" -- at the exact instant the lease expired.
    # Several simulation steps separate the two, and the driver used none of
    # them: dropping the expiry term reclaims at the first step after the kill.
    assert kill_lease - T_KILL >= 2 * POLL_INTERVAL
    assert reclaim_at == kill_lease


# ===========================================================================
# Clause 3 -- "its late submission is FENCED"
# ===========================================================================


def test_clause3_late_submission_is_fenced(scenario):
    """MUTATION: neuter ``accept_unit``'s fencing comparison -- the late
    answer lands as the accepted text and no StaleFenceError is raised -> red."""
    # The late submission was actually constructed (guards against a vacuous pass).
    assert scenario.fence_before_reclaim is not None
    assert scenario.fence_after_reclaim > scenario.fence_before_reclaim

    assert isinstance(scenario.late_submit_error, StaleFenceError)

    superseded = [
        a
        for a in scenario.store.list_attempts(RUN_ID, "u0")
        if a.kind is AttemptKind.SUPERSEDED
    ]
    assert len(superseded) == 1
    assert superseded[0].fencing_token == scenario.fence_before_reclaim

    # Recorded as superseded, never applied (invariant 4).
    assert scenario.accepted_texts["u0"] != scenario.late_submit_text
    assert scenario.late_submit_text not in json.dumps(scenario.applied_payloads)


# ===========================================================================
# Clause 4 -- "a hard halt quiesces per policy"
# ===========================================================================


def test_clause4_halt_is_recorded_and_the_trigger_returns_to_pending(scenario):
    """MUTATION: make ``_classify_and_maybe_halt`` return ``None`` -- no halt
    is ever recorded -> red."""
    assert scenario.report_wave1.halted == "rate_limit"
    # The run was halted at the moment wave 1 ended (before resume cleared it).
    assert scenario.run_halted_when_u1_accepted == "rate_limit"
    # D4: the triggering unit is returned to PENDING, not terminally failed.
    assert scenario.unfinished_at_halt == ("u0",)


def test_clause4_halt_blocks_new_claims(scenario):
    """MUTATION: delete ``claim_unit``'s ``halted_kind`` check -- the probe
    then raises TerminalStateError instead of RunHaltedError -> red."""
    assert isinstance(scenario.claim_probe_error, RunHaltedError)


def test_clause4_no_further_dispatch_after_the_halt(scenario):
    """MUTATION: drop the ``halted is None`` guard from the dispatch
    condition -- u0 is launched a third time inside wave 1 -> red."""
    wave1_launches = [
        r for r in scenario.world.launches if r["at"] <= T_LATE_SUBMIT_U1
    ]
    assert len(wave1_launches) == 3  # u0, u1, u0-reclaim -- and nothing after the halt
    assert all(r["at"] <= T_RATE_LIMIT for r in wave1_launches)


def test_clause4_a_post_halt_valid_fence_submission_is_still_accepted(scenario):
    """D4 -- the other half of "quiesces per policy": in-flight, paid-for work
    is not thrown away.

    MUTATION: add a ``halted_kind`` refusal to ``accept_unit`` -- u1's
    post-halt submission raises instead of landing -> red."""
    assert scenario.run_halted_when_u1_accepted == "rate_limit", (
        "the fixture must submit u1 while the run is actually halted"
    )
    assert scenario.report_wave1.accepted == ("u1",)
    assert scenario.accepted_texts["u1"] == "u1 answer"


# ===========================================================================
# Clause 5 -- "resume completes only unfinished work"
# ===========================================================================


def test_clause5_resume_dispatches_only_the_unfinished_unit(scenario):
    """MUTATION: widen ``_select_dispatch_candidates``'s PENDING filter to any
    state -- the already-accepted u1 is re-dispatched -> red."""
    assert scenario.report_wave2.dispatcher_acquired is True
    assert scenario.report_wave2.dispatched == ("u0",)
    wave2_launches = [r for r in scenario.world.launches if r["at"] > T_LATE_SUBMIT_U1]
    assert [r["unit_id"] for r in wave2_launches] == ["u0"]


def test_clause5_resume_does_not_touch_the_already_accepted_unit(scenario):
    """The completed unit is left strictly alone: no new attempt of any kind
    is recorded against u1 during wave 2."""
    kinds = [a.kind for a in scenario.store.list_attempts(RUN_ID, "u1")]
    # Exactly one claim and one acceptance for its whole life, and no reclaim,
    # renewal-after-acceptance, or superseded row added by the resume wave.
    # (The APPLY_* rows come from the finalizer, which is clause 6's subject.)
    assert kinds.count(AttemptKind.CLAIM) == 1
    assert kinds.count(AttemptKind.ACCEPT) == 1
    assert AttemptKind.EXPIRE not in kinds
    assert AttemptKind.SUPERSEDED not in kinds
    assert AttemptKind.FAIL not in kinds
    assert scenario.store.get_unit(RUN_ID, "u1").state is UnitState.ACCEPTED


# ===========================================================================
# Clause 6 -- "the A-min finalizer applies all accepted output"
# ===========================================================================


def test_clause6_finalizer_applies_all_accepted_output(scenario):
    """MUTATION: make ``finalize_run`` stop after the first applied unit --
    only u0 is applied -> red."""
    assert scenario.applied == ("u0", "u1")  # ordinal order, serial
    assert scenario.applied_payloads == {
        "u0": {"answer": "u0 answer"},
        "u1": {"answer": "u1 answer"},
    }
    for unit_id in ("u0", "u1"):
        kinds = [a.kind for a in scenario.store.list_attempts(RUN_ID, unit_id)]
        assert AttemptKind.APPLY_STARTED in kinds
        assert AttemptKind.APPLY_SUCCEEDED in kinds
    # Every unit of the run reached a terminal accepted state.
    assert unfinished_units(scenario.store, RUN_ID) == []


def test_clause6_finalizer_is_idempotent_and_never_reapplies(scenario):
    """A second finalize applies nothing (invariant 3)."""
    scenario.applied_payloads.clear()
    again = finalize_run(scenario.store, RUN_ID, scenario.adapter, at=scenario.clock.t)
    assert again == []
    assert scenario.applied_payloads == {}


# ===========================================================================
# `blocked-reclaim-chain-untested` -- blocked -> SETTLED -> expired -> reclaimed
# ===========================================================================


def _blocked_setup(tmp_path):
    store = _seeded_store(tmp_path, unit_ids=("u0",))
    claim = store.claim_unit(RUN_ID, "u0", "worker-a", lease_seconds=50.0, at=T_START)
    store.record_dispatch(RUN_ID, "u0", "worker-a", session_id="sess-1", at=T_START)
    od = OpenDispatch(
        unit_id="u0",
        worker_id="worker-a",
        session_id="sess-1",
        id="short1",
        fencing_token=claim.fencing_token,
        claimed_by="worker-a",
    )
    runner = FakeRunner()
    runner.script(
        ("claude", "agents", "--json"),
        (json.dumps([_bg_record(id="short1", session_id="sess-1", state="blocked")]), "", 0),
    )
    return store, od, ClaudeCli(executable="claude", runner=runner)


def test_defect_supervise_tick_renews_an_already_accepted_unit(tmp_path):
    store = _seeded_store(tmp_path, unit_ids=("u0",))
    claim = store.claim_unit(RUN_ID, "u0", "worker-a", lease_seconds=50.0, at=T_START)
    store.record_dispatch(RUN_ID, "u0", "worker-a", session_id="sess-1", at=T_START)
    od = OpenDispatch(
        unit_id="u0", worker_id="worker-a", session_id="sess-1", id="short1",
        fencing_token=claim.fencing_token, claimed_by="worker-a",
    )
    # The worker submitted; its background session has not exited yet.
    store.accept_unit(RUN_ID, "u0", claim.fencing_token, text="answer", at=T_START + 1)

    runner = FakeRunner()
    runner.script(
        ("claude", "agents", "--json"),
        (json.dumps([_bg_record(id="short1", session_id="sess-1", state="working")]), "", 0),
    )
    cli = ClaudeCli(executable="claude", runner=runner)

    result = supervise_tick(store, RUN_ID, cli, RunAdapter(), {"u0": od}, at=T_START + 2)
    assert result.renewed == ()


def test_blocked_settles_the_dispatch_so_the_unit_can_ever_be_reclaimed(tmp_path):
    """The half that `blocked-reclaim-chain-untested` flagged as uncovered:
    stopping renewal is not enough -- the dispatch must also be SETTLED, or
    ``reclaimable_units``' "no open dispatch" guard makes the unit
    permanently unreclaimable.

    MUTATION: delete the ``store.settle_dispatch`` call from
    ``supervise_tick``'s ``blocked`` branch -- the dispatch stays open and
    the unit is never reclaimable at any time -> red."""
    store, od, cli = _blocked_setup(tmp_path)
    lease_expiry = store.get_unit(RUN_ID, "u0").lease_expires_at

    result = supervise_tick(store, RUN_ID, cli, RunAdapter(), {"u0": od}, at=T_START + 10)

    # 1. blocked -> not renewed, and settled.
    assert result.renewed == ()
    assert result.settled == {"u0": "blocked"}
    # 2. the dispatch is CLOSED (this is the assertion the deviation earned).
    assert store.open_dispatches(RUN_ID) == []
    # 3. still not reclaimable while the lease is live -- no early duplicate.
    assert reclaimable_units(store, RUN_ID, at=lease_expiry - 1) == []
    # 4. reclaimable once the lease expires.
    assert [u.unit_id for u in reclaimable_units(store, RUN_ID, at=lease_expiry)] == ["u0"]


def test_a_blocked_unit_is_actually_re_dispatched_by_a_later_wave(tmp_path, monkeypatch):
    """The CONSEQUENCE the deviation exists for, end to end: a unit whose
    worker went ``blocked`` is picked up again by a later ``dispatch_wave``
    once its lease expires -- the 19-day stall does not recur.

    MUTATION: delete the ``store.settle_dispatch`` call from
    ``supervise_tick``'s ``blocked`` branch -- wave 2 dispatches nothing and
    the unit stalls forever -> red."""
    monkeypatch.setattr(claude_bg, "_default_runner", lambda *a, **k: None)
    store = _seeded_store(tmp_path, unit_ids=("u0",))
    clock = Clock()
    world = World(store, clock)
    runner = ScenarioRunner(world)
    cli = ClaudeCli(executable="claude", runner=runner)
    wc = _worker_command(tmp_path)
    monkeypatch.setattr(claude_bg, "classify_settled_failure", lambda *a, **k: None)

    def on_launch(rec):
        claim = store.claim_unit(RUN_ID, rec["unit_id"], rec["worker_id"], at=clock.t)
        rec["fencing_token"] = claim.fencing_token
        if len(world.launches) == 1:
            # The first worker stalls on a question and never returns.
            world.schedule.append(
                (
                    T_START + POLL_INTERVAL,
                    "block",
                    lambda: world.set_state(rec["short_id"], "blocked"),
                )
            )
        else:
            # The replacement worker completes normally.
            store.accept_unit(
                RUN_ID, rec["unit_id"], claim.fencing_token, text="answer", at=clock.t
            )
            world.set_state(rec["short_id"], "done")

    world.on_launch = on_launch

    report1 = dispatch_wave(
        store, RUN_ID, store.list_units(RUN_ID), RunAdapter(), cli=cli,
        worker_command=wc, max_agents=1, poll_interval_s=POLL_INTERVAL, env={},
        sleep_fn=clock.advance, clock_fn=clock,
    )
    assert report1.dispatched == ("u0",)
    assert report1.settled == {"u0": "blocked"}
    assert store.open_dispatches(RUN_ID) == []
    assert store.get_unit(RUN_ID, "u0").state is UnitState.CLAIMED

    # Time passes past the (never-renewed) lease, and a later wave runs.
    clock.t = T_START + LEASE + 1
    report2 = dispatch_wave(
        store, RUN_ID, store.list_units(RUN_ID), RunAdapter(), cli=cli,
        worker_command=wc, max_agents=1, poll_interval_s=POLL_INTERVAL, env={},
        sleep_fn=clock.advance, clock_fn=clock,
    )
    assert report2.dispatched == ("u0",), "a blocked unit was never reclaimed"
    assert len(world.launches) == 2
    assert world.launches[0]["worker_id"] != world.launches[1]["worker_id"]
    expires = [
        a for a in store.list_attempts(RUN_ID, "u0") if a.kind is AttemptKind.EXPIRE
    ]
    assert len(expires) == 1


# ===========================================================================
# Liveness -- the dispatch loop must be BOUNDED
# ===========================================================================
#
# `dispatch_wave`'s only exit conditions are an abort and "no open
# dispatches and no candidates". Both of the tests below construct an open
# dispatch that nothing in the loop can ever close, and each one HANGS
# against an unbounded loop -- caught here as a red test, never as a real
# hang, by `Clock.deadline`.


def _lifecycle_scripted(runner):
    """Let `claude stop|rm <id>` succeed so the calls are observable rather
    than swallowed by the drivers' best-effort ``except``."""
    runner.script(("claude", "stop"), ("", "", 0))
    runner.script(("claude", "rm"), ("", "", 0))
    return runner


def test_a_terminal_unit_whose_session_never_exits_does_not_hang_the_wave(
    tmp_path, monkeypatch
):
    """PRE-FIX DEMONSTRATION. The worker submits (unit ACCEPTED) and its
    background session never exits. The unit is no longer CLAIMED, so it is
    not renewed; it is terminal, so it is never a candidate; and
    ``accept_unit`` leaves ``claimed_by``/the fence intact, so the drift
    guard never drops it. The dispatch therefore stays open forever and the
    loop spins on ``sleep_fn`` with all work accepted.

    MUTATION: remove the grace expiry from ``supervise_tick``'s
    terminal-unit branch (make it an unconditional ``continue`` again) ->
    the Clock deadline fires -> red."""
    monkeypatch.setattr(claude_bg, "classify_settled_failure", lambda *a, **k: None)
    store = _seeded_store(tmp_path, unit_ids=("u0",))
    clock = Clock(deadline=T_START + 20_000)
    world = World(store, clock)
    runner = _lifecycle_scripted(ScenarioRunner(world))
    cli = ClaudeCli(executable="claude", runner=runner)

    def on_launch(rec):
        claim = store.claim_unit(RUN_ID, rec["unit_id"], rec["worker_id"], at=clock.t)
        # Submitted through the protocol; the session stays "working".
        store.accept_unit(
            RUN_ID, rec["unit_id"], claim.fencing_token, text="answer", at=clock.t
        )

    world.on_launch = on_launch

    report = dispatch_wave(
        store, RUN_ID, store.list_units(RUN_ID), RunAdapter(), cli=cli,
        worker_command=_worker_command(tmp_path), max_agents=1,
        poll_interval_s=POLL_INTERVAL, env={},
        terminal_exit_grace_seconds=300.0,
        sleep_fn=clock.advance, clock_fn=clock,
    )

    assert report.settled == {"u0": "session_lingering"}
    assert report.aborted_reason is None
    # The lingering session was ENDED, not leaked: settling removed it from
    # the wave's own `finally` cleanup, so the tick had to stop/rm it.
    short_id = world.launches[0]["short_id"]
    argvs = [argv for argv, _kw in runner.calls]
    assert ["claude", "stop", short_id] in argvs
    assert ["claude", "rm", short_id] in argvs
    # No dispatch left open -> no unit stranded out of a later reclaim.
    assert store.open_dispatches(RUN_ID) == []
    assert store.get_unit(RUN_ID, "u0").state is UnitState.ACCEPTED
    # The grace was a grace, not an immediate settle: it took time to fire.
    assert clock.t >= T_START + 300.0


def test_a_wave_that_makes_no_progress_at_all_aborts_instead_of_spinning(
    tmp_path, monkeypatch
):
    """PRE-FIX DEMONSTRATION, second wedge -- a cause the terminal-unit grace
    does NOT cover. ``agents --json`` starts failing after the launch is
    confirmed, so every ``supervise_tick`` returns an empty result: nothing
    renewed, settled, or dropped, and the open dispatch keeps the only slot.

    MUTATION: remove the ``stall_timeout_seconds`` check from
    ``dispatch_wave`` -> the Clock deadline fires -> red."""
    monkeypatch.setattr(claude_bg, "classify_settled_failure", lambda *a, **k: None)
    store = _seeded_store(tmp_path, unit_ids=("u0",))
    clock = Clock(deadline=T_START + 20_000)
    world = World(store, clock)

    class BreakingRunner(ScenarioRunner):
        """`agents --json` works for preflight + launch confirmation, then
        fails for good."""

        def __init__(self, world):
            super().__init__(world)
            self.agents_calls = 0

        def __call__(self, argv, **kwargs):
            argv = list(argv)
            if argv[1:3] == ["agents", "--json"]:
                self.agents_calls += 1
                if self.agents_calls > 2:
                    self.calls.append((argv, kwargs))
                    return ("", "the daemon is not answering", 1)
            return super().__call__(argv, **kwargs)

    runner = _lifecycle_scripted(BreakingRunner(world))
    cli = ClaudeCli(executable="claude", runner=runner)

    def on_launch(rec):
        store.claim_unit(RUN_ID, rec["unit_id"], rec["worker_id"], at=clock.t)

    world.on_launch = on_launch

    report = dispatch_wave(
        store, RUN_ID, store.list_units(RUN_ID), RunAdapter(), cli=cli,
        worker_command=_worker_command(tmp_path), max_agents=1,
        poll_interval_s=POLL_INTERVAL, env={},
        stall_timeout_seconds=900.0,
        sleep_fn=clock.advance, clock_fn=clock,
    )

    assert report.aborted_reason == "wave_stalled"
    # The bound did not fire early: a full stall window had to elapse.
    assert clock.t >= T_START + 900.0
    # Cleanup on the abort path settles what it stops, so the unit is not
    # left permanently unreclaimable by an open dispatch.
    assert store.open_dispatches(RUN_ID) == []
    assert report.settled.get("u0") == "wave_exit"
    short_id = world.launches[0]["short_id"]
    assert ["claude", "stop", short_id] in [argv for argv, _kw in runner.calls]


# -- refusal direction: what the bounds must NOT cut off ---------------------


def test_a_long_running_unit_is_never_cut_off_by_the_stall_bound(tmp_path, monkeypatch):
    """REFUSAL DIRECTION. A unit that legitimately runs far longer than
    ``stall_timeout_seconds`` must complete. Renewal is progress, so a live
    CLAIMED unit re-arms the stall bound on every tick.

    MUTATION: count only settlements (not renewals) as progress -> this wave
    aborts at 900s with the unit still working -> red."""
    monkeypatch.setattr(claude_bg, "classify_settled_failure", lambda *a, **k: None)
    store = _seeded_store(tmp_path, unit_ids=("u0",))
    clock = Clock(deadline=T_START + 20_000)
    world = World(store, clock)
    runner = _lifecycle_scripted(ScenarioRunner(world))
    cli = ClaudeCli(executable="claude", runner=runner)
    finish_at = T_START + 2000.0  # >> both bounds

    def on_launch(rec):
        claim = store.claim_unit(RUN_ID, rec["unit_id"], rec["worker_id"], at=clock.t)
        unit_id, short_id = rec["unit_id"], rec["short_id"]

        def _finish():
            store.accept_unit(
                RUN_ID, unit_id, claim.fencing_token, text="answer", at=clock.t
            )
            world.set_state(short_id, "done")

        world.schedule.append((finish_at, "finish", _finish))

    world.on_launch = on_launch

    report = dispatch_wave(
        store, RUN_ID, store.list_units(RUN_ID), RunAdapter(), cli=cli,
        worker_command=_worker_command(tmp_path), max_agents=1,
        poll_interval_s=POLL_INTERVAL, env={},
        terminal_exit_grace_seconds=300.0, stall_timeout_seconds=900.0,
        sleep_fn=clock.advance, clock_fn=clock,
    )

    assert report.aborted_reason is None
    assert report.settled == {"u0": "accepted"}
    assert report.accepted == ("u0",)
    assert clock.t >= finish_at


def test_a_normal_submit_then_exit_still_settles_via_the_done_branch(
    tmp_path, monkeypatch
):
    """REFUSAL DIRECTION. The ordinary submit-then-exit window -- the unit is
    terminal while its session is still 'working' for a tick or two -- must
    settle as ``accepted`` through the ``done`` branch, NOT be force-stopped
    by the grace.

    MUTATION: settle the terminal-unit branch immediately (a grace of 0) ->
    the outcome becomes ``session_lingering`` and stop/rm fires -> red."""
    monkeypatch.setattr(claude_bg, "classify_settled_failure", lambda *a, **k: None)
    store = _seeded_store(tmp_path, unit_ids=("u0",))
    clock = Clock(deadline=T_START + 20_000)
    world = World(store, clock)
    runner = _lifecycle_scripted(ScenarioRunner(world))
    cli = ClaudeCli(executable="claude", runner=runner)

    def on_launch(rec):
        claim = store.claim_unit(RUN_ID, rec["unit_id"], rec["worker_id"], at=clock.t)
        store.accept_unit(
            RUN_ID, rec["unit_id"], claim.fencing_token, text="answer", at=clock.t
        )
        short_id = rec["short_id"]
        # Two full poll intervals of lingering -- well inside the grace.
        world.schedule.append(
            (clock.t + 2 * POLL_INTERVAL, "exit", lambda: world.set_state(short_id, "done"))
        )

    world.on_launch = on_launch

    report = dispatch_wave(
        store, RUN_ID, store.list_units(RUN_ID), RunAdapter(), cli=cli,
        worker_command=_worker_command(tmp_path), max_agents=1,
        poll_interval_s=POLL_INTERVAL, env={},
        terminal_exit_grace_seconds=300.0,
        sleep_fn=clock.advance, clock_fn=clock,
    )

    assert report.settled == {"u0": "accepted"}
    assert report.accepted == ("u0",)
    short_id = world.launches[0]["short_id"]
    assert ["claude", "stop", short_id] not in [argv for argv, _kw in runner.calls]
