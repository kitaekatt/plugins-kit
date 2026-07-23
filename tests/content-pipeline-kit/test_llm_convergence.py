"""Behavioral tests for content_pipeline.llm.convergence.

Translates loc trial.py's CONVERGED / STALLED verdict cases onto the generic
progress-based evaluator.
"""

from content_pipeline.llm.convergence import (
    ProgressEvaluator,
    Round,
    Verdict,
    evaluate,
)


def test_empty_history_is_continue():
    assert ProgressEvaluator().evaluate([]) == Verdict.CONTINUE


def test_converged_when_outstanding_drained():
    history = [Round(produced=3, outstanding=2), Round(produced=2, outstanding=0)]
    assert ProgressEvaluator().evaluate(history) == Verdict.CONVERGED


def test_stalled_after_two_zero_progress_rounds():
    history = [
        Round(produced=1, outstanding=3),
        Round(produced=0, outstanding=3),
        Round(produced=0, outstanding=3),
    ]
    assert ProgressEvaluator(stall_window=2).evaluate(history) == Verdict.STALLED


def test_not_stalled_when_recent_round_made_progress():
    history = [
        Round(produced=0, outstanding=3),
        Round(produced=1, outstanding=2),
    ]
    assert ProgressEvaluator(stall_window=2).evaluate(history) == Verdict.CONTINUE


def test_single_zero_round_is_continue_under_window_two():
    history = [Round(produced=0, outstanding=3)]
    assert ProgressEvaluator(stall_window=2).evaluate(history) == Verdict.CONTINUE


def test_converged_precedence_over_stalled():
    # Last rounds show zero progress but outstanding is also zero -> converged.
    history = [
        Round(produced=0, outstanding=0),
        Round(produced=0, outstanding=0),
    ]
    assert ProgressEvaluator(stall_window=2).evaluate(history) == Verdict.CONVERGED


def test_stall_window_parameter_respected():
    history = [
        Round(produced=0, outstanding=2),
        Round(produced=0, outstanding=2),
    ]
    # Window of 3 needs three zero rounds; only two exist -> continue.
    assert ProgressEvaluator(stall_window=3).evaluate(history) == Verdict.CONTINUE
    # Window of 2 fires.
    assert ProgressEvaluator(stall_window=2).evaluate(history) == Verdict.STALLED


def test_converge_window_requires_sustained_empty():
    # converge_window=2 needs the last TWO rounds empty.
    history = [Round(produced=1, outstanding=1), Round(produced=1, outstanding=0)]
    ev = ProgressEvaluator(converge_window=2)
    assert ev.evaluate(history) == Verdict.CONTINUE
    history.append(Round(produced=0, outstanding=0))
    assert ev.evaluate(history) == Verdict.CONVERGED


def test_module_evaluate_helper():
    assert evaluate([Round(produced=0, outstanding=0)]) == Verdict.CONVERGED
    assert evaluate(
        [Round(produced=0, outstanding=1), Round(produced=0, outstanding=1)],
        stall_window=2,
    ) == Verdict.STALLED


def test_verdict_string_values_stable():
    # Verdicts are persisted / logged; the string values are the contract.
    assert Verdict.CONVERGED.value == "converged"
    assert Verdict.STALLED.value == "stalled"
    assert Verdict.CONTINUE.value == "continue"
