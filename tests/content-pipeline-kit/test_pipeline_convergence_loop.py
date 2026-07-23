"""Tests for content_pipeline.pipeline.convergence_loop.

Translates the loc corpus-tree cycle behaviors: the fixed grade-first stage
order (the cold-start-deadlock regression -- GRADE must precede FILL so a blank
store's seed is baked gradeable before FILL produces the first reading),
early-exit on CONVERGED, halt on STALLED, the pre-loop already-converged
short-circuit (zero cycles), and max_cycles honoring.
"""

import pytest

from content_pipeline.llm.convergence import ProgressEvaluator, Verdict
from content_pipeline.pipeline.convergence_loop import run, run_cycle


# -- grade-first ordering (cold-start-deadlock regression) --------------------

def test_run_cycle_runs_grade_before_fill():
    order = []
    run_cycle(
        {},
        1,
        grade=lambda s, c: order.append("grade"),
        select=lambda s, c: order.append("select"),
        apply=lambda s, c: order.append("apply"),
        fill=lambda s, c: order.append("fill"),
        measure=lambda s: (0, 1),
    )
    # The order is structural, not caller-controllable: grade must come first
    # so a cold store is gradeable before fill tries to produce a reading.
    assert order == ["grade", "select", "apply", "fill"]
    assert order.index("grade") < order.index("fill")


def test_cold_start_grade_bakes_seed_so_fill_produces():
    # Simulate the cold-start unblock: a blank store yields no production until
    # GRADE has baked the empty seed. If fill ran first (before grade) nothing
    # would ever be produced.
    store = {"graded": False, "produced": 0, "outstanding": 1}

    def grade(s, c):
        s["graded"] = True  # bake the seed's template

    def fill(s, c):
        if s["graded"]:  # only eligible AFTER grade baked it
            s["produced"] += 1
            s["outstanding"] = 0

    result = run(
        store,
        grade=grade,
        fill=fill,
        measure=lambda s: (s["produced"], s["outstanding"]),
        max_cycles=3,
    )
    assert result.converged
    assert result.cycles_run == 1  # produced on the first cycle, not deadlocked


# -- convergence / stall verdicts ---------------------------------------------

def test_converges_when_outstanding_drains():
    state = {"outstanding": 2}

    def fill(s, c):
        s["outstanding"] -= 1

    result = run(
        state,
        fill=fill,
        measure=lambda s: (1, s["outstanding"]),
        max_cycles=5,
    )
    assert result.verdict is Verdict.CONVERGED
    assert result.cycles_run == 2  # 2 -> 1 -> 0


def test_stalls_when_no_progress_with_work_remaining():
    # Outstanding never drops and no production -> STALLED after the stall
    # window, instead of burning all max_cycles.
    result = run(
        {},
        fill=lambda s, c: None,
        measure=lambda s: (0, 5),  # produced 0, outstanding 5 every cycle
        max_cycles=10,
        gate=ProgressEvaluator(stall_window=2),
    )
    assert result.verdict is Verdict.STALLED
    assert result.cycles_run == 2  # halted at the stall window, not 10


def test_pre_loop_already_converged_runs_zero_cycles():
    ran = []
    result = run(
        {},
        grade=lambda s, c: ran.append("grade"),
        measure=lambda s: (0, 0),  # already terminal
        max_cycles=5,
    )
    assert result.verdict is Verdict.CONVERGED
    assert result.cycles_run == 0
    assert ran == []  # no stage work on an already-converged store


def test_honors_max_cycles_without_convergence():
    result = run(
        {},
        fill=lambda s, c: None,
        # produced > 0 keeps it from STALLING; outstanding stays -> CONTINUE
        measure=lambda s: (1, 3),
        max_cycles=4,
    )
    assert result.cycles_run == 4
    assert result.verdict is Verdict.CONTINUE


def test_start_cycle_offsets_indices():
    result = run(
        {},
        fill=lambda s, c: None,
        measure=lambda s: (1, 2),
        max_cycles=2,
        start_cycle=5,
    )
    assert [c.cycle for c in result.cycles] == [5, 6]


def test_negative_max_cycles_raises():
    with pytest.raises(ValueError):
        run({}, measure=lambda s: (0, 1), max_cycles=-1)
