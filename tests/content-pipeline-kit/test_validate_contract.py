"""Tests for content_pipeline.validate.contract.

Port-equivalence baseline: these cases translate the validator behaviors
pinned by BOTH source suites into the plugin's neutral vocabulary -- the
first-pass ``validate_assignment`` (one rule set, aggregate every violation,
raise once) and the localization ``submit/validator`` (list-of-rejections,
tiered hard/soft/advisory blocking, deterministic ordering, agent-facing
feedback text). No game/loc concepts appear: a "candidate" is an opaque value
and "context" an opaque bag; validators are pure functions returning typed
rejections.
"""

import pytest

from content_pipeline.validate.contract import (
    Rejection,
    Severity,
    ValidationError,
    assert_valid,
    blocks,
    format_rejections,
    is_rejecting,
    run_rules,
)


# -- rule set shared by many call sites ---------------------------------------

def _required_keys_validator(candidate, context):
    """One rule: every expected key must be present, no extras. Mirrors the
    first-pass missing/unexpected-keys check."""
    expected = set(context["expected"])
    returned = set(candidate)
    rejections = []
    missing = expected - returned
    if missing:
        rejections.append(Rejection(
            kind="missing_keys", severity=Severity.HARD,
            detail=f"missing keys: {sorted(missing)}"))
    extra = returned - expected
    if extra:
        rejections.append(Rejection(
            kind="unexpected_keys", severity=Severity.HARD,
            detail=f"unexpected keys: {sorted(extra)}"))
    return rejections


def _advisory_validator(candidate, context):
    return [Rejection(kind="style", severity=Severity.ADVISORY, detail="nit")]


# -- run_rules ----------------------------------------------------------------

def test_run_rules_accepts_when_clean():
    rejections = run_rules(
        {"a", "b"}, {"expected": ["a", "b"]}, [_required_keys_validator])
    assert rejections == []
    assert is_rejecting(rejections) is False


def test_run_rules_aggregates_every_violation():
    # The first-pass invariant: all failures collected, not just the first.
    rejections = run_rules(
        {"a", "x"}, {"expected": ["a", "b"]}, [_required_keys_validator])
    kinds = {r.kind for r in rejections}
    assert kinds == {"missing_keys", "unexpected_keys"}


def test_run_rules_is_deterministically_sorted():
    r1 = run_rules({"x"}, {"expected": ["a", "b"]}, [_required_keys_validator])
    r2 = run_rules({"x"}, {"expected": ["a", "b"]}, [_required_keys_validator])
    assert [(r.kind, r.detail) for r in r1] == [(r.kind, r.detail) for r in r2]


def test_run_rules_shared_across_call_sites():
    # Same validator instance drives both the in-loop and the post-hoc site.
    validators = [_required_keys_validator]
    candidate, ctx = {"a"}, {"expected": ["a", "b"]}
    in_loop = run_rules(candidate, ctx, validators)
    post_hoc = run_rules(candidate, ctx, validators)
    assert in_loop == post_hoc


# -- severity tiers / blocking ------------------------------------------------

def test_hard_blocks():
    assert blocks(Rejection(kind="k", severity=Severity.HARD)) is True


def test_advisory_never_blocks():
    assert blocks(Rejection(kind="k", severity=Severity.ADVISORY)) is False


def test_soft_blocks_by_default_demotable():
    soft = Rejection(kind="k", severity=Severity.SOFT)
    assert blocks(soft) is True
    assert blocks(soft, block_soft=False) is False


def test_is_rejecting_ignores_advisory_only():
    rejections = run_rules("x", {}, [_advisory_validator])
    assert rejections  # the advisory rejection is present
    assert is_rejecting(rejections) is False  # ...but it does not block


# -- assert_valid (raise-on-violation surface) --------------------------------

def test_assert_valid_raises_on_blocking():
    rejections = [Rejection(kind="missing_keys", detail="missing keys: ['b']")]
    with pytest.raises(ValidationError) as exc:
        assert_valid(rejections)
    assert "missing keys" in str(exc.value)
    assert exc.value.rejections[0].kind == "missing_keys"


def test_assert_valid_aggregates_details():
    rejections = [
        Rejection(kind="a", detail="first"),
        Rejection(kind="b", detail="second"),
    ]
    with pytest.raises(ValidationError) as exc:
        assert_valid(rejections)
    assert str(exc.value) == "first\nsecond"


def test_assert_valid_silent_on_advisory_only():
    assert_valid([Rejection(kind="style", severity=Severity.ADVISORY, detail="nit")])


# -- format_rejections (agent-facing feedback) --------------------------------

def test_format_rejections_valid_token_when_clean():
    assert format_rejections([]) == "VALID"


def test_format_rejections_valid_when_only_advisory():
    advisory = [Rejection(kind="style", severity=Severity.ADVISORY, detail="nit")]
    assert format_rejections(advisory) == "VALID"


def test_format_rejections_lists_blocking_with_rule_id():
    rejections = [
        Rejection(kind="rule_violation", severity=Severity.SOFT,
                  detail="must contain X", rule_id="R1"),
        Rejection(kind="markup_mismatch", detail="dropped token"),
    ]
    text = format_rejections(rejections)
    assert "[rule_violation:R1] must contain X" in text
    assert "[markup_mismatch] dropped token" in text
    assert text.startswith("REJECTED")
