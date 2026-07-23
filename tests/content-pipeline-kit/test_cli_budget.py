"""Tests for content_pipeline.cli.budget.

Pins the budget guard: auth-expiry preflight (a halt before any unit runs),
text-channel hard-stop detection on a response, and a guarded sweep that halts
cleanly with partial progress + a resume list on the first HaltError while
isolating non-halt per-unit errors.
"""

import pytest

from content_pipeline.cli.budget import (
    BudgetStop,
    check_response,
    guarded_sweep,
    preflight_check,
)
from content_pipeline.llm.platform import HALT_AUTH, HALT_RATE_LIMIT, HaltError


# -- preflight ----------------------------------------------------------------

def test_preflight_reraises_halt_as_budget_stop():
    def probe():
        raise HaltError(HALT_AUTH, "logged out")

    with pytest.raises(BudgetStop) as exc:
        preflight_check(probe)
    assert exc.value.reason == HALT_AUTH
    assert exc.value.done == []  # nothing ran


def test_preflight_passes_when_probe_clean():
    preflight_check(lambda: None)  # no raise


def test_preflight_non_halt_error_propagates_unchanged():
    with pytest.raises(ValueError):
        preflight_check(lambda: (_ for _ in ()).throw(ValueError("other")))


# -- check_response -----------------------------------------------------------

def test_check_response_raises_on_rate_limit_marker():
    class R:
        text = 'error: "api_error_status":429 hit your limit'

    with pytest.raises(HaltError) as exc:
        check_response(R())
    assert exc.value.kind == HALT_RATE_LIMIT


def test_check_response_clean_passes():
    class R:
        text = "a perfectly fine completion"

    check_response(R())  # no raise


# -- guarded_sweep ------------------------------------------------------------

def test_sweep_completes_when_no_halt():
    result = guarded_sweep([1, 2, 3], worker=lambda u: u * 10)
    assert result.stopped is False
    assert [r for _u, r in result.done] == [10, 20, 30]


def test_sweep_halts_cleanly_with_partial_progress():
    def worker(unit):
        if unit == "c":
            raise HaltError(HALT_RATE_LIMIT, "429")
        return f"ok-{unit}"

    result = guarded_sweep(["a", "b", "c", "d", "e"], worker)
    assert result.stopped is True
    assert result.halted.reason == HALT_RATE_LIMIT
    assert [u for u, _r in result.done] == ["a", "b"]  # progress before halt
    assert result.remaining == ["d", "e"]  # c tripped; d,e never attempted
    assert result.halted.remaining == ["d", "e"]


def test_sweep_isolates_non_halt_errors():
    def worker(unit):
        if unit == 2:
            raise RuntimeError("bad unit")
        return unit

    result = guarded_sweep([1, 2, 3], worker)
    assert result.stopped is False  # non-halt error does not stop the sweep
    assert [u for u, _r in result.done] == [1, 3]
    assert result.errors == [(2, "bad unit")]


def test_sweep_can_propagate_non_halt_errors():
    with pytest.raises(RuntimeError):
        guarded_sweep(
            [1], worker=lambda u: (_ for _ in ()).throw(RuntimeError("x")),
            isolate_errors=False,
        )
