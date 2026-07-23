"""Tests for content_pipeline.cli.bulk.

Pins the two-phase cache-warm bulk worker: the warm phase runs once before the
loop, per-unit error isolation keeps one bad unit from aborting the batch, and
a HaltError halts cleanly (delegating to budget.guarded_sweep) with a resume
list.
"""

import pytest

from content_pipeline.cli.bulk import run_bulk
from content_pipeline.llm.platform import HALT_RATE_LIMIT, HaltError


def test_warm_runs_once_before_worker():
    events = []
    run_bulk(
        [1, 2, 3],
        worker=lambda u: events.append(("work", u)),
        warm=lambda: events.append(("warm", None)),
    )
    assert events[0] == ("warm", None)  # warm first
    assert events.count(("warm", None)) == 1  # exactly once


def test_no_warm_still_processes():
    result = run_bulk([1, 2], worker=lambda u: u)
    assert result.warmed is False
    assert result.ok_count == 2


def test_per_unit_error_isolation():
    def worker(unit):
        if unit == "bad":
            raise ValueError("nope")
        return unit

    result = run_bulk(["a", "bad", "c"], worker)
    assert [u for u, _r in result.done] == ["a", "c"]
    assert result.errors == [("bad", "nope")]
    assert result.stopped is False


def test_halt_stops_with_remaining():
    def worker(unit):
        if unit == 2:
            raise HaltError(HALT_RATE_LIMIT, "429")
        return unit

    result = run_bulk([1, 2, 3, 4], worker)
    assert result.stopped is True
    assert result.halted.reason == HALT_RATE_LIMIT
    assert result.remaining == [3, 4]
    assert [u for u, _r in result.done] == [1]


def test_warm_failure_propagates():
    with pytest.raises(RuntimeError):
        run_bulk(
            [1],
            worker=lambda u: u,
            warm=lambda: (_ for _ in ()).throw(RuntimeError("prime failed")),
        )
