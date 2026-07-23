"""Tests for content_pipeline.validate.floor_guard.

Port-equivalence baseline: these cases translate the floor-guard validation
mode pinned by first-pass ``transition_diagnostic`` (and its siblings) into
the plugin's neutral vocabulary -- run a candidate guard over a known-good
corpus, measure its flag rate, and REJECT the guard when it disagrees with
known-good work at/above the threshold (default 0.10), gating each signal
independently. The advisory application never blocks. No game concepts
appear: a "guard" is any ``item -> bool`` and "known-good items" are opaque.
"""

from content_pipeline.validate.floor_guard import (
    DEFAULT_THRESHOLD,
    corpus_flag_rate,
    evaluate_guard,
    evaluate_guards,
    flag,
)


# A guard flags an item when it is >= a cutoff (a stand-in for any signal).
def _over(cutoff):
    return lambda item: item >= cutoff


# -- flag rate ----------------------------------------------------------------

def test_corpus_flag_rate():
    known_good = [1, 2, 3, 4, 5]  # guard flags >= 4 -> 2/5 = 0.4
    assert corpus_flag_rate(_over(4), known_good) == 0.4


def test_empty_corpus_is_zero_rate():
    assert corpus_flag_rate(_over(0), []) == 0.0


# -- acceptance gate ----------------------------------------------------------

def test_guard_accepted_when_under_threshold():
    # 20 known-good items, guard flags 1 -> 5% < 10% -> accepted.
    known_good = list(range(20))
    report = evaluate_guard(_over(19), known_good, name="sig")
    assert report.flagged == 1
    assert report.population == 20
    assert report.flag_rate == 0.05
    assert report.accepted is True


def test_guard_rejected_when_over_threshold():
    # Guard disagrees with known-good work on 40% of cases -> a bad signal.
    known_good = list(range(10))
    report = evaluate_guard(_over(6), known_good, name="noisy")
    assert report.flag_rate == 0.4
    assert report.accepted is False


def test_threshold_is_strict_less_than():
    # Exactly at the threshold is NOT accepted (must be comfortably under).
    known_good = list(range(10))  # flag 1 -> 0.10 exactly
    report = evaluate_guard(_over(9), known_good, threshold=0.10)
    assert report.flag_rate == 0.10
    assert report.accepted is False


def test_empty_corpus_accepts_guard():
    report = evaluate_guard(_over(0), [], name="sig")
    assert report.flag_rate == 0.0
    assert report.accepted is True


def test_default_threshold_value():
    assert DEFAULT_THRESHOLD == 0.10


# -- per-signal gate ----------------------------------------------------------

def test_evaluate_guards_gates_each_signal_independently():
    known_good = list(range(10))
    guards = {
        "clean": _over(100),   # flags nothing -> accepted
        "noisy": _over(5),     # flags 5/10 -> rejected
    }
    reports = evaluate_guards(guards, known_good)
    assert reports["clean"].accepted is True
    assert reports["noisy"].accepted is False


# -- advisory application -----------------------------------------------------

def test_flag_returns_flagged_items_only():
    assert flag(_over(3), [1, 2, 3, 4, 5]) == [3, 4, 5]
