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


# --------------------------------------------------------------------------- #
# typed node contracts: the `artifacts.NAME` head (only when Scope.artifacts is set)
# --------------------------------------------------------------------------- #
def test_artifact_head_compiles_to_the_mapped_path():
    scope = Scope(artifacts={"doc": "step_p.path"}, where="step 'c'")
    assert compile_expr("artifacts.doc", scope) == "step_p.path"
    assert compile_template("read {{ artifacts.doc }}", scope) == "`read ${step_p.path}`"


def test_artifact_head_refuses_an_undeclared_name():
    scope = Scope(artifacts={"doc": "step_p.path"}, where="step 'c'")
    with pytest.raises(WorkflowError, match=r"step 'c': .* uses artifact 'other', which this "
                                            r"step does not declare in `requires`"):
        compile_expr("artifacts.other", scope)


def test_artifact_head_refuses_a_member_tail():
    scope = Scope(artifacts={"doc": "step_p.path"}, where="step 'c'")
    with pytest.raises(WorkflowError, match="has a member tail"):
        compile_expr("artifacts.doc.x", scope)
    with pytest.raises(WorkflowError, match=r"expected `artifacts\.NAME`"):
        compile_expr("artifacts", scope)


def test_artifact_head_is_not_recognized_without_an_artifact_map():
    # Scope.artifacts None: `artifacts` is an ordinary head (a local, or unknown).
    assert compile_expr("artifacts.x", Scope(locals={"artifacts": "artifacts"})) == "artifacts.x"
    with pytest.raises(WorkflowError, match="unknown reference 'artifacts'"):
        compile_expr("artifacts.x", Scope())
