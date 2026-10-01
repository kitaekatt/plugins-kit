"""CL5: synthetic parity of the library convergence loop with a loc-shaped consumer.

Spike5 (task loc-pipeline-consumer-needs) found four loop-level gaps between
``pipeline.convergence_loop`` and loc's corpus loop: no between-stage hook
(row 9), no FAILED verdict (rows 5, 11), no error record (row 18), and no
typed cell state for the terminal rule (rows 3, 4, 6). This module builds a
consumer with loc-SHAPED semantics from library types only -- a frozen
``CellState`` subclass, a toy ``CellPolicy`` whose TERMINAL / FAILED rule is
written here, and scripted stage stubs -- and drives the real loop through
each of those rows. Nothing here imports loc; the rule below is a stand-in
for loc's own, which stays in loc.

The toy rule (mirrors the shape of loc's ``corpus_trial_status``):

- locked with ``lock_reason == "dead"``      -> FAILED
- locked otherwise                           -> LOCKED
- unlocked, ``blocked`` and no ``ok`` reading -> TERMINAL
- anything else                              -> OPEN
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from content_pipeline.llm.convergence import ProgressEvaluator, Round, Verdict
from content_pipeline.pipeline.cell_policy import CellOutcome, measure_from, tally
from content_pipeline.pipeline.convergence_loop import (
    STAGES,
    LoopEventKind,
    run,
)
from content_pipeline.provenance.snapshot import EventLog, StageSnapshotter
from content_pipeline.store.candidate import (
    Candidate,
    CandidateCell,
    CandidateStore,
    CellState,
    append_candidate,
    dump_store,
    load_store,
    set_locked,
)

NEVER_STALL = ProgressEvaluator(stall_window=None)


# -- loc-shaped consumer built from library types -----------------------------


@dataclass(frozen=True)
class LineState(CellState):
    blocked: bool = False
    decline_streak: int = 0


class ToyPolicy:
    def outcome(self, cell: CandidateCell) -> CellOutcome:
        if cell.locked:
            if cell.state.lock_reason == "dead":
                return CellOutcome.FAILED
            return CellOutcome.LOCKED
        if cell.state.blocked and not any(
            (e.riders or {}).get("ok") for e in cell.entries
        ):
            return CellOutcome.TERMINAL
        return CellOutcome.OPEN


def _store(*keys: str) -> CandidateStore:
    store = CandidateStore()
    for key in keys:
        store.add(CandidateCell(key=(key, "ja"), state=LineState()))
    return store


def _cells(store: CandidateStore):
    return store.cells.values()


def _copy(store: CandidateStore) -> CandidateStore:
    return CandidateStore(cells=dict(store.cells))


# Mutations a scripted stage applies to one cell.
def lock(reason: str = "decline"):
    return lambda cell: set_locked(cell, reason=reason)


def block():
    return lambda cell: replace(cell, state=replace(cell.state, blocked=True))


def reading(cid: str, ok: bool):
    return lambda cell: append_candidate(
        cell, Candidate(id=cid, value=cid, status="shadow", riders={"ok": ok})
    )


class Script:
    """Stage stubs answering from a per-(cycle, stage) mutation table."""

    def __init__(self, table):
        self.table = table
        self.calls = []

    def stage(self, name: str):
        def _stage(store: CandidateStore, cycle: int) -> CandidateStore:
            self.calls.append((cycle, name))
            out = _copy(store)
            for key, mutate in self.table.get((cycle, name), ()):
                out.put(mutate(out.get((key, "ja"))))
            return out

        return _stage

    def stages(self) -> dict:
        return {name: self.stage(name) for name in STAGES}


def _drive(store, script, *, gate=NEVER_STALL, max_cycles=10, start_cycle=1, observers=()):
    return run(
        store,
        **script.stages(),
        measure=measure_from(_cells, ToyPolicy()),
        max_cycles=max_cycles,
        gate=gate,
        start_cycle=start_cycle,
        observers=list(observers),
    )


# A loc-like history: cycles 1-2 lock nothing (fill/grade still productive),
# cycle 3 locks two cells, cycle 4 locks the last one.
SLOW_START = {
    (1, "fill"): [("a", reading("a1", True)), ("b", reading("b1", True)), ("c", reading("c1", False))],
    (2, "fill"): [("c", reading("c2", True))],
    (3, "select"): [("a", lock()), ("b", lock())],
    (4, "select"): [("c", lock("cap"))],
}


# -- rows 5 / 11: verdict set CONVERGED / FAILED / RUNNING, no STALLED --------


def test_running_then_converged_without_stall():
    result = _drive(_store("a", "b", "c"), Script(SLOW_START))
    assert result.verdict is Verdict.CONVERGED
    assert [c.verdict for c in result.cycles] == [Verdict.CONTINUE] * 3 + [Verdict.CONVERGED]
    assert [c.round.produced for c in result.cycles] == [0, 0, 2, 1]
    assert [c.round.outstanding for c in result.cycles] == [3, 3, 1, 0]


def test_default_gate_would_stall_the_same_history():
    # The reason loc removed its corpus stall: two lock-free cycles are normal.
    result = _drive(_store("a", "b", "c"), Script(SLOW_START), gate=ProgressEvaluator())
    assert result.verdict is Verdict.STALLED
    assert result.cycles_run == 2


def test_dead_lock_yields_failed_and_stops_the_loop():
    table = dict(SLOW_START)
    table[(4, "select")] = [("c", lock("dead"))]
    script = Script(table)
    result = _drive(_store("a", "b", "c"), script, max_cycles=8)
    assert result.verdict is Verdict.FAILED and result.failed
    assert result.cycles_run == 4
    last = result.cycles[-1].round
    assert (last.outstanding, last.failed) == (0, 1)
    assert max(c for c, _ in script.calls) == 4


def test_terminal_unlocked_cells_drain_to_converged():
    table = {
        (1, "select"): [("a", lock())],
        (1, "fill"): [("b", reading("b1", False)), ("b", block())],
    }
    result = _drive(_store("a", "b"), Script(table))
    assert result.verdict is Verdict.CONVERGED
    assert result.cycles_run == 1
    assert result.cycles[-1].round.terminal == 1


@pytest.mark.parametrize(
    "reason, verdict", [("decline", Verdict.CONVERGED), ("dead", Verdict.FAILED)]
)
def test_terminal_store_runs_zero_cycles(reason, verdict):
    store = _store("a", "b")
    for key in ("a", "b"):
        store.put(set_locked(store.get((key, "ja")), reason="decline"))
    store.put(set_locked(store.get(("b", "ja")), reason=reason))
    script = Script({})
    events = []
    result = _drive(store, script, observers=[events.append])
    assert result.verdict is verdict
    assert result.cycles_run == 0 and script.calls == []
    assert [e.kind for e in events] == [LoopEventKind.LOOP_STARTED, LoopEventKind.LOOP_FINISHED]
    assert events[-1].verdict is verdict


def test_empty_store_is_converged_by_the_library():
    # Recorded difference: loc's corpus_trial_status needs total > 0 for
    # CONVERGED and reports RUNNING on an empty store. A per-cell policy
    # cannot see "no cells"; the library drains to CONVERGED.
    result = _drive(_store(), Script({}))
    assert result.verdict is Verdict.CONVERGED and result.cycles_run == 0


# -- rows 9 / 18: between-stage hooks and the error record --------------------


def _json_write(store: CandidateStore, path: Path) -> None:
    path.write_text(
        dump_store(store, yaml_dump=lambda d: json.dumps(d, sort_keys=True)),
        encoding="ascii",
    )


def test_snapshots_every_stage_boundary_without_stage_wrappers(tmp_path):
    snap = StageSnapshotter(tmp_path, write=_json_write)
    result = _drive(_store("a", "b", "c"), Script(SLOW_START), observers=[snap])
    labels = ["before-grade"] + [f"after-{s}" for s in STAGES]
    for cycle in range(1, result.cycles_run + 1):
        names = sorted(p.name for p in (tmp_path / f"cycle-{cycle}").iterdir())
        assert names == sorted(f"store.{label}.json" for label in labels)
    # cycle-3 after-select holds the two cycle-3 locks; before-grade does not.
    doc = json.loads((tmp_path / "cycle-3" / "store.after-select.json").read_text())
    before = json.loads((tmp_path / "cycle-3" / "store.before-grade.json").read_text())
    assert sum(1 for c in doc["cells"] if c.get("locked")) == 2
    assert sum(1 for c in before["cells"] if c.get("locked")) == 0
    # The last snapshot is the final store.
    final = json.loads((tmp_path / "cycle-4" / "store.after-fill.json").read_text())
    assert final == json.loads(dump_store(result.store, yaml_dump=json.dumps))


def test_path_store_snapshots_are_byte_copies(tmp_path):
    # loc's stages are path-based: they rewrite a file and return None.
    live = tmp_path / "live.yaml"
    live.write_bytes(b"cycle 0\n")
    written = {}

    def path_stage(name):
        def _stage(path, cycle):
            data = f"cycle {cycle} after {name}\n".encode("ascii")
            path.write_bytes(data)
            written[(cycle, name)] = data

        return _stage

    counter = iter(range(100))
    snap = StageSnapshotter(
        tmp_path / "audit", write=lambda p, dst: shutil.copyfile(p, dst), suffix=".yaml"
    )
    result = run(
        live,
        **{name: path_stage(name) for name in STAGES},
        measure=lambda p: (0, 1) if next(counter) < 2 else (1, 0),
        max_cycles=5,
        gate=NEVER_STALL,
        start_cycle=3,
        observers=[snap],
    )
    assert result.verdict is Verdict.CONVERGED and result.store == live
    assert [c.cycle for c in result.cycles] == [3, 4]
    assert snap.path_for(3, "before-grade").read_bytes() == b"cycle 0\n"
    for (cycle, name), data in written.items():
        assert snap.path_for(cycle, f"after-{name}").read_bytes() == data


def test_stage_error_names_stage_and_cycle_and_reraises(tmp_path):
    boom = RuntimeError("select exploded")
    script = Script(SLOW_START)
    stages = script.stages()
    good_select = stages["select"]

    def bad_select(store, cycle):
        if cycle == 2:
            raise boom
        return good_select(store, cycle)

    stages["select"] = bad_select
    events = []
    log = EventLog(tmp_path / "events.jsonl")
    snap = StageSnapshotter(tmp_path / "audit", write=_json_write)
    with pytest.raises(RuntimeError) as info:
        run(
            _store("a", "b", "c"),
            **stages,
            measure=measure_from(_cells, ToyPolicy()),
            max_cycles=5,
            gate=NEVER_STALL,
            observers=[events.append, log, snap],
        )
    assert info.value is boom
    failed = [e for e in events if e.kind is LoopEventKind.STAGE_FAILED]
    assert [(e.cycle, e.stage) for e in failed] == [(2, "select")]
    assert failed[0].error is boom and failed[0].elapsed_s is not None
    last = json.loads((tmp_path / "events.jsonl").read_text().splitlines()[-1])
    assert (last["kind"], last["cycle"], last["stage"]) == ("stage_failed", 2, "select")
    assert last["error"] == "RuntimeError: select exploded"
    assert snap.path_for(2, "after-grade").exists()
    assert not snap.path_for(2, "after-select").exists()
    # No LOOP_FINISHED: the run-level error status stays the caller's write.
    assert events[-1].kind is LoopEventKind.STAGE_FAILED


def test_measure_error_emits_no_stage_event():
    # Recorded gap for row 18: an exception outside a stage (the measure)
    # propagates with no STAGE_FAILED; the caller's outer try/except records it.
    events = []
    calls = iter(range(100))

    def measure(store):
        if next(calls) == 1:
            raise ValueError("store unreadable")
        return (0, 1)

    with pytest.raises(ValueError):
        run(_store("a"), **Script({}).stages(), measure=measure, max_cycles=3,
            gate=NEVER_STALL, observers=[events.append])
    kinds = [e.kind for e in events]
    assert LoopEventKind.STAGE_FAILED not in kinds
    assert kinds[-1] is LoopEventKind.STAGE_FINISHED


# -- progress events (liveness) and stage timings -----------------------------


def test_event_sequence_for_one_cycle_carries_round_and_verdict():
    events = []
    _drive(_store("a"), Script({(1, "select"): [("a", lock())]}), observers=[events.append])
    kinds = [e.kind for e in events]
    stage_kinds = [LoopEventKind.STAGE_STARTED, LoopEventKind.STAGE_FINISHED] * 4
    assert kinds == (
        [LoopEventKind.LOOP_STARTED, LoopEventKind.CYCLE_STARTED]
        + stage_kinds
        + [LoopEventKind.CYCLE_FINISHED, LoopEventKind.LOOP_FINISHED]
    )
    assert [e.stage for e in events if e.kind is LoopEventKind.STAGE_STARTED] == list(STAGES)
    done = events[-2]
    assert done.round == Round(produced=1, outstanding=0)
    assert done.verdict is Verdict.CONVERGED and done.elapsed_s is not None
    assert all(e.elapsed_s is not None for e in events if e.kind is LoopEventKind.STAGE_FINISHED)


def test_cycle_result_records_stage_seconds():
    result = _drive(_store("a"), Script({(1, "select"): [("a", lock())]}))
    assert set(result.cycles[0].stage_seconds) == set(STAGES)


# -- rows 3 / 4 / 6: typed cell state drives the policy -----------------------


def test_typed_state_round_trips_and_feeds_the_policy():
    store = _store("a", "b")
    store.put(set_locked(store.get(("a", "ja")), reason="dead"))
    cell_b = store.get(("b", "ja"))
    store.put(replace(cell_b, state=replace(cell_b.state, blocked=True, decline_streak=3)))
    text = dump_store(store, yaml_dump=json.dumps)
    back = load_store(text, yaml_load=json.loads, state_type=LineState)
    a, b = back.get(("a", "ja")), back.get(("b", "ja"))
    assert isinstance(a.state, LineState) and a.state.lock_reason == "dead"
    assert isinstance(b.state, LineState) and b.state.blocked and b.state.decline_streak == 3
    counted = tally(_cells(back), ToyPolicy())
    assert (counted.failed, counted.terminal, counted.open) == (1, 1, 0)
    # set_locked keeps the subclass and clears the reason with reason=None.
    relocked = set_locked(a, reason=None)
    assert isinstance(relocked.state, LineState) and relocked.state.lock_reason is None
