"""Extension points of content_pipeline.pipeline.convergence_loop: observers,
stage events, Round coercion, FAILED handling, stage timings."""

import pytest

from content_pipeline.llm.convergence import ProgressEvaluator, Round, Verdict
from content_pipeline.pipeline.convergence_loop import (
    STAGES,
    CycleResult,
    LoopEventKind,
    run,
    run_cycle,
)

K = LoopEventKind


def _noop(s, c):
    return None


def _stages(**over):
    base = dict(grade=_noop, select=_noop, apply=_noop, fill=_noop)
    base.update(over)
    return base


def test_observer_sees_fixed_stage_order():
    events = []
    run_cycle({}, 1, measure=lambda s: (0, 1), observers=[events.append], **_stages())
    kinds = [(e.kind, e.stage) for e in events]
    expected = [(K.CYCLE_STARTED, None)]
    for name in STAGES:
        expected += [(K.STAGE_STARTED, name), (K.STAGE_FINISHED, name)]
    expected.append((K.CYCLE_FINISHED, None))
    assert kinds == expected
    assert STAGES == ("grade", "select", "apply", "fill")


def test_none_stage_emits_no_stage_events():
    events = []
    run(
        {},
        grade=_noop,
        measure=lambda s: (0, 1),
        max_cycles=1,
        observers=[events.append],
    )
    stages = {e.stage for e in events if e.stage}
    assert stages == {"grade"}
    assert events[0].kind is K.LOOP_STARTED
    assert events[-1].kind is K.LOOP_FINISHED


def test_stage_exception_emits_stage_failed_and_reraises():
    events = []

    def boom(s, c):
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        run_cycle(
            {}, 3, measure=lambda s: (0, 1), observers=[events.append],
            **_stages(select=boom),
        )
    failed = [e for e in events if e.kind is K.STAGE_FAILED]
    assert len(failed) == 1
    assert failed[0].stage == "select" and failed[0].cycle == 3
    assert isinstance(failed[0].error, ValueError)
    assert failed[0].elapsed_s is not None


def test_observer_exception_stops_loop():
    calls = []

    def stage(s, c):
        calls.append(c)

    def observer(e):
        if e.kind is K.CYCLE_FINISHED:
            raise RuntimeError("snapshot failed")

    with pytest.raises(RuntimeError, match="snapshot failed"):
        run(
            {}, grade=stage, measure=lambda s: (0, 1), max_cycles=5,
            gate=ProgressEvaluator(stall_window=None), observers=[observer],
        )
    assert calls == [1]


def test_observer_raise_on_stage_failed_keeps_stage_error():
    stage_error = ValueError("stage broke")

    def boom(s, c):
        raise stage_error

    def observer(e):
        if e.kind is K.STAGE_FAILED:
            raise RuntimeError("observer broke")

    with pytest.raises(ValueError) as info:
        run_cycle(
            {}, 1, measure=lambda s: (0, 1), observers=[observer],
            **_stages(grade=boom),
        )
    assert info.value is stage_error
    assert isinstance(info.value.__cause__, RuntimeError)


def test_measure_may_return_round():
    seen = []
    result = run(
        {},
        fill=_noop,
        measure=lambda s: Round(produced=1, outstanding=0, failed=0, terminal=2),
        max_cycles=2,
    )
    assert result.history[-1].terminal == 2
    # pre-loop probe is measured outstanding==0 -> converged with zero cycles
    assert result.converged and result.cycles_run == 0
    r = run_cycle({}, 1, measure=lambda s: Round(2, 1, terminal=4), **_stages())
    assert r.round == Round(2, 1, terminal=4)
    assert seen == []


def test_failed_verdict_stops_loop():
    state = {"n": 0}

    def measure(s):
        state["n"] += 1
        # pre-loop probe is open; cycle 1 drains with a failure
        return Round(1, 0, failed=1) if state["n"] > 1 else Round(0, 1)

    result = run({}, fill=_noop, measure=measure, max_cycles=5)
    assert result.verdict is Verdict.FAILED and result.failed
    assert result.cycles_run == 1
    assert not result.converged


def test_pre_loop_failed_runs_zero_cycles():
    ran = []
    result = run(
        {},
        grade=lambda s, c: ran.append(c),
        measure=lambda s: Round(produced=5, outstanding=0, failed=2),
        max_cycles=3,
    )
    assert result.verdict is Verdict.FAILED
    assert result.cycles_run == 0 and ran == []
    assert result.history == [Round(0, 0, failed=2)]


def test_cycle_result_records_stage_seconds():
    r = run_cycle({}, 1, measure=lambda s: (0, 1), **_stages(select=None))
    assert set(r.stage_seconds) == {"grade", "apply", "fill"}
    assert all(v >= 0 for v in r.stage_seconds.values())


def test_cycle_result_still_hashable():
    a = CycleResult(1, 0, Round(0, 1), Verdict.CONTINUE)
    b = CycleResult(1, 0, Round(0, 1), Verdict.CONTINUE, stage_seconds={"grade": 1.0})
    assert hash(a) == hash(b)
    assert hash(Round(0, 1, detail={"x": 1})) == hash(Round(0, 1))


def test_no_observers_matches_pre_change_results():
    def build():
        return {"n": 0}

    def fill(s, c):
        s["n"] += 1

    def measure(s):
        return (1, 0 if s["n"] >= 2 else 1)

    plain = run(build(), fill=fill, measure=measure, max_cycles=5)
    events = []
    observed = run(
        build(), fill=fill, measure=measure, max_cycles=5, observers=[events.append]
    )
    assert plain.verdict is observed.verdict is Verdict.CONVERGED
    assert plain.history == observed.history
    assert [c.round for c in plain.cycles] == [c.round for c in observed.cycles]
    assert plain.cycles_run == observed.cycles_run == 2
    assert events
