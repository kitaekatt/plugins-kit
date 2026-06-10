"""Unit tests for the {{ }} expression mini-language."""

import pytest

from workflow_kit_lib.errors import WorkflowError
from workflow_kit_lib.expr import Scope, compile_expr, compile_single, compile_template


def test_inputs_reference():
    assert compile_expr("inputs.diff", Scope()) == "inputs.diff"
    assert compile_expr("inputs.a.b", Scope()) == "inputs.a.b"


def test_inputs_head_checked_against_declared():
    scope = Scope(inputs={"diff"})
    assert compile_expr("inputs.diff", scope) == "inputs.diff"
    assert compile_expr("inputs.diff.lines", scope) == "inputs.diff.lines"
    with pytest.raises(WorkflowError, match="unknown input 'dif'"):
        compile_expr("inputs.dif", scope)


def test_inputs_unchecked_when_scope_declares_none():
    # inputs=None (the default) skips the check -- back-compat for direct Scope use.
    assert compile_expr("inputs.anything", Scope()) == "inputs.anything"


def test_step_reference():
    scope = Scope(step_vars={"gather": "step_gather"})
    assert compile_expr("steps.gather", scope) == "step_gather"
    assert compile_expr("steps.gather.files", scope) == "step_gather.files"


def test_step_flatten_with_star():
    scope = Scope(step_vars={"review": "step_review"})
    assert (
        compile_expr("steps.review[*].findings", scope)
        == "step_review.flatMap((r) => r.findings)"
    )


def test_star_requires_field():
    scope = Scope(step_vars={"review": "step_review"})
    with pytest.raises(WorkflowError, match=r"must be followed"):
        compile_expr("steps.review[*]", scope)


def test_local_item_reference():
    scope = Scope(locals={"dim": "dim"})
    assert compile_expr("dim", scope) == "dim"
    assert compile_expr("finding.title", Scope(locals={"finding": "finding"})) == "finding.title"


def test_prev_stage_reference():
    scope = Scope(prev_stage=("review", "prev"))
    assert compile_expr("review.findings", scope) == "prev.findings"


def test_unknown_step_raises():
    with pytest.raises(WorkflowError, match="unknown step reference"):
        compile_expr("steps.nope", Scope())


def test_unknown_reference_raises():
    with pytest.raises(WorkflowError, match="unknown reference"):
        compile_expr("mystery", Scope())


def test_template_interpolation_and_escaping():
    scope = Scope(step_vars={}, locals={"item": "item"})
    out = compile_template("Scan {{ item }} now", scope)
    assert out == "`Scan ${item} now`"


def test_template_escapes_backtick_and_dollar_brace():
    out = compile_template("a `b` ${c}", Scope())
    assert "\\`b\\`" in out
    assert "\\${c}" in out


def test_compile_single_requires_single_expression():
    assert compile_single("{{ inputs.x }}", Scope()) == "inputs.x"
    with pytest.raises(WorkflowError, match="single"):
        compile_single("prefix {{ inputs.x }}", Scope())
