"""The untracked loop helpers each emit exactly one DeprecationWarning per call.

``run_single_pass``, ``guarded_sweep`` and ``run_bulk`` are deprecated in favor
of the tracked execution path. ``run_bulk`` calls the sweep internally; it must
call the private ``_guarded_sweep`` so a caller sees ONE warning, not two.
The other names in the same modules are NOT deprecated and must stay silent.
"""

from __future__ import annotations

import warnings

import pytest

from content_pipeline.cli import budget, bulk
from content_pipeline.llm.platform import HALT_AUTH, PipelineHaltError
from content_pipeline.pipeline import gate as gate_mod
from content_pipeline.pipeline.single_pass import run_single_pass
from content_pipeline.pipeline.workunit import WorkUnit

THIS_FILE = __file__


def _deprecations(record):
    return [w for w in record if issubclass(w.category, DeprecationWarning)]


def _unit(uid: str = "u1") -> WorkUnit:
    return WorkUnit(id=uid)


def test_guarded_sweep_warns_once_at_caller() -> None:
    with warnings.catch_warnings(record=True) as rec:
        warnings.simplefilter("always")
        result = budget.guarded_sweep(["a", "b"], lambda u: u.upper())
    dep = _deprecations(rec)
    assert len(dep) == 1
    assert dep[0].filename == THIS_FILE
    assert "prepare_run" in str(dep[0].message)
    assert [o for _, o in result.done] == ["A", "B"]


def test_run_bulk_warns_exactly_once_and_at_caller() -> None:
    with warnings.catch_warnings(record=True) as rec:
        warnings.simplefilter("always")
        result = bulk.run_bulk(["a", "b"], lambda u: u)
    dep = _deprecations(rec)
    assert len(dep) == 1, [str(w.message)[:30] for w in dep]
    assert dep[0].filename == THIS_FILE
    assert "run_bulk" in str(dep[0].message)
    assert result.ok_count == 2


def test_run_bulk_unguarded_warns_once() -> None:
    with warnings.catch_warnings(record=True) as rec:
        warnings.simplefilter("always")
        bulk.run_bulk(["a"], lambda u: u, guard_halts=False)
    assert len(_deprecations(rec)) == 1


def test_run_single_pass_warns_once_at_caller() -> None:
    with warnings.catch_warnings(record=True) as rec:
        warnings.simplefilter("always")
        run_single_pass(
            [],
            freshness_of=lambda u: None,
            generate=lambda u: None,
        )
    dep = _deprecations(rec)
    assert len(dep) == 1
    assert dep[0].filename == THIS_FILE
    assert "run_single_pass" in str(dep[0].message)


def test_non_helper_names_emit_no_deprecation() -> None:
    def probe() -> None:
        raise PipelineHaltError(HALT_AUTH, "x")

    with warnings.catch_warnings(record=True) as rec:
        warnings.simplefilter("always")
        with pytest.raises(budget.BudgetStop):
            budget.preflight_check(probe)
        budget.check_response("fine")
        budget.BudgetStop("auth")
        budget.SweepResult()
        bulk.BulkResult()
        g = gate_mod.Gate("g", lambda u: None)
        assert gate_mod.run_gates([g], _unit()) is None
    assert _deprecations(rec) == []
