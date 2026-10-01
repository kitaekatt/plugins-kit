"""CL4: cell policy tally and the stateful measure_from."""

from content_pipeline.llm.convergence import ProgressEvaluator, Round, Verdict
from content_pipeline.pipeline.cell_policy import CellOutcome, Tally, measure_from, tally


class _Policy:
    def outcome(self, cell):
        return CellOutcome(cell)


def test_tally_counts_each_outcome():
    cells = ["open", "open", "locked", "terminal", "failed", "failed", "failed"]
    assert tally(cells, _Policy()) == Tally(total=7, open=2, locked=1, terminal=1, failed=3)


def test_measure_from_first_call_is_baseline_zero():
    store = {"cells": ["locked", "locked", "open"]}
    m = measure_from(lambda s: s["cells"], _Policy())
    assert m(store) == Round(0, 1)


def test_measure_from_reports_locked_delta_not_total():
    store = {"cells": ["locked", "open", "open", "open"]}
    m = measure_from(lambda s: s["cells"], _Policy())
    m(store)
    store["cells"] = ["locked", "locked", "locked", "open"]
    assert m(store) == Round(2, 1)
    assert m(store) == Round(0, 1)


def test_policy_failed_cells_yield_failed_verdict():
    store = {"cells": ["locked", "failed"]}
    m = measure_from(lambda s: s["cells"], _Policy())
    rnd = m(store)
    assert rnd.failed == 1 and rnd.outstanding == 0
    assert ProgressEvaluator(stall_window=None).evaluate([rnd]) == Verdict.FAILED


def test_to_round_sets_total_and_empty_store_is_distinguishable():
    assert tally(["open", "locked", "terminal"], _Policy()).to_round().total == 3
    m = measure_from(lambda s: s["cells"], _Policy())
    rnd = m({"cells": []})
    assert rnd.total == 0
    gate = ProgressEvaluator(stall_window=None, empty_is_converged=False)
    assert gate.evaluate([rnd]) == Verdict.CONTINUE
    assert ProgressEvaluator(stall_window=None).evaluate([rnd]) == Verdict.CONVERGED
