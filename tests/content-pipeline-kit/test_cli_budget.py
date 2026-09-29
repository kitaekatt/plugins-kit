"""Tests for content_pipeline.cli.budget.

Pins the budget guard: auth-expiry preflight (a halt before any unit runs),
and text-channel hard-stop detection on a response.
"""

import pytest

from content_pipeline.cli.budget import (
    BudgetStop,
    check_response,
    preflight_check,
)
from content_pipeline.llm.platform import HALT_AUTH, HALT_RATE_LIMIT, PipelineHaltError


# -- preflight ----------------------------------------------------------------


def test_preflight_reraises_halt_as_budget_stop():
    def probe():
        raise PipelineHaltError(HALT_AUTH, "logged out")

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

    with pytest.raises(PipelineHaltError) as exc:
        check_response(R())
    assert exc.value.kind == HALT_RATE_LIMIT


def test_check_response_clean_passes():
    class R:
        text = "a perfectly fine completion"

    check_response(R())  # no raise
