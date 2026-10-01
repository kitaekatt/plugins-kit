"""CL1: FAILED verdict, Round extension fields, stall_window=None."""

from content_pipeline.llm.convergence import ProgressEvaluator, Round, Verdict, evaluate


def test_failed_when_drained_with_failures():
    assert ProgressEvaluator().evaluate([Round(1, 0, failed=2)]) == Verdict.FAILED


def test_converged_unchanged_when_failed_is_zero():
    assert ProgressEvaluator().evaluate([Round(1, 0, terminal=3)]) == Verdict.CONVERGED


def test_failed_needs_drained_outstanding():
    assert ProgressEvaluator(stall_window=None).evaluate([Round(1, 2, failed=1)]) == Verdict.CONTINUE


def test_stall_window_none_never_stalls():
    history = [Round(0, 3)] * 10
    assert ProgressEvaluator(stall_window=None).evaluate(history) == Verdict.CONTINUE
    assert evaluate(history, stall_window=None) == Verdict.CONTINUE
    assert ProgressEvaluator(stall_window=2).evaluate(history) == Verdict.STALLED


def test_round_positional_construction():
    r = Round(3, 4)
    assert (r.produced, r.outstanding, r.failed, r.terminal, dict(r.detail)) == (3, 4, 0, 0, {})
    assert Round(3, 4) == Round(3, 4, 0, 0)


def test_round_still_hashable():
    assert isinstance(hash(Round(1, 2, detail={"k": [1]})), int)
    assert hash(Round(1, 2, detail={"a": 1})) == hash(Round(1, 2))


def test_verdict_failed_value_stable():
    assert Verdict.FAILED.value == "failed"
    assert Verdict("failed") is Verdict.FAILED


def test_empty_store_continues_when_empty_is_not_converged():
    gate = ProgressEvaluator(stall_window=None, empty_is_converged=False)
    assert gate.evaluate([Round(0, 0, total=0)]) == Verdict.CONTINUE
    assert gate.evaluate([Round(0, 0, total=0)] * 3) == Verdict.CONTINUE
    # Default keeps the existing verdict for an empty store.
    assert ProgressEvaluator(stall_window=None).evaluate([Round(0, 0, total=0)]) == Verdict.CONVERGED
    # A drained non-empty or unknown-size population still converges.
    assert gate.evaluate([Round(1, 0, total=5)]) == Verdict.CONVERGED
    assert gate.evaluate([Round(1, 0)]) == Verdict.CONVERGED
    assert gate.evaluate([Round(1, 0, failed=1, total=5)]) == Verdict.FAILED


def test_round_total_defaults_none_and_stays_hashable():
    r = Round(3, 4)
    assert r.total is None
    assert Round(3, 4, total=9).total == 9
    assert hash(Round(3, 4)) == hash(Round(3, 4, total=9))
    assert len({Round(3, 4), Round(3, 4, total=9)}) == 1
