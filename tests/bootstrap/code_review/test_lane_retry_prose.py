"""Protect launch correction and actual-lane failure policy independently of drift."""

from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(params=("git", "p4"))
def skill_dir(request: pytest.FixtureRequest) -> Path:
    vcs = request.param
    return REPO_ROOT / f"plugins/{vcs}-kit/skills/{vcs}-code-review"


def _normalized(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def test_dispatch_rule_reaches_launch_correction_policy(skill_dir: Path) -> None:
    body = _normalized(skill_dir / "SKILL.md")
    assert "apply the pre-dispatch launch-correction rule in references/configuration.md" in body
    assert "A NON-ZERO exit is a FAILED lane, never an empty result: do NOT retry it" not in body


def test_correction_requires_positive_pre_dispatch_evidence(skill_dir: Path) -> None:
    ref = _normalized(skill_dir / "references/configuration.md")
    assert "positive evidence that no reviewer process or Agent started and no provider request was sent" in ref
    assert "CLI argument/JSON quoting errors" in ref
    assert "an Agent alias sent to the endpoint runner" in ref
    assert "A non-zero exit alone is not that evidence" in ref
    assert "uncertain dispatch state" in ref


def test_correction_preserves_lane_and_security_boundaries(skill_dir: Path) -> None:
    ref = _normalized(skill_dir / "references/configuration.md")
    assert "same resolved model, effort, chunk, files, and review criteria" in ref
    assert "Provider/auth/quota/network failures, timeouts, and invalid reviewer output" in ref
    assert "Permission or sharing denials require the normal approval flow" in ref
    assert "not eligible launch corrections" in ref
    assert "Unsupported lane/model configurations remain errors to report" in ref


def test_corrected_attempts_are_disclosed_not_counted_as_missing(skill_dir: Path) -> None:
    body = _normalized(skill_dir / "SKILL.md")
    assert "Report each corrected launch's original stderr, no-dispatch evidence, correction, and final outcome" in body
    assert "Only a completed, schema-valid reviewer result restores that lane's coverage" in body
    assert "including an unsuccessful launch correction" in body
    assert "non-zero parser exit is a FAILED lane" in body
    # Scoped, not dropped: a lane reaching this section has no fallback left, so
    # "a different model" means one its own configuration never named.
    assert "never re-run the lane on a model its own configuration did not name" in body
    assert "only with an EMPTY or EXHAUSTED `model_fallbacks`" in body


def test_failed_lane_fails_over_along_its_configured_chain(skill_dir: Path) -> None:
    body = _normalized(skill_dir / "SKILL.md")
    assert "FAILOVER-ELIGIBLE" in body
    assert "when the resolved reviewer record's `model_fallbacks` is non-empty" in body
    assert "re-dispatch the SAME lane on the next entry in `model_fallbacks`" in body
    assert (
        "Walk the chain in order, trying each entry AT MOST ONCE, until one produces a "
        "schema-valid result or the chain is exhausted"
    ) in body
    assert "do not retry an entry already tried and do not skip ahead" in body


def test_lane_with_no_fallbacks_still_fails_exactly_as_before(skill_dir: Path) -> None:
    body = _normalized(skill_dir / "SKILL.md")
    assert (
        "A lane with an EMPTY `model_fallbacks` -- every validator lane, and any reviewer "
        "configured with no fallback -- behaves exactly as before: do NOT retry it, silently "
        'substitute an Agent, or treat absent output as "no issues found"'
    ) in body


def test_exhausted_chain_is_still_a_failed_lane_with_missing_coverage(skill_dir: Path) -> None:
    body = _normalized(skill_dir / "SKILL.md")
    assert (
        "A lane whose `model_fallbacks` chain is EXHAUSTED (every entry tried and failed) is a "
        "FAILED lane the same way: report it in step 9, name every model tried, and mark "
        'coverage missing -- never treat absent output as "no issues found"'
    ) in body
    ref = _normalized(skill_dir / "references/configuration.md")
    assert (
        "This happens when a lane has no `model_fallbacks` to try, or when every entry in its "
        "chain has been tried and failed."
    ) in ref
    assert "no fallback beyond the configured chain to an unlisted Agent" in ref


def test_lane_failovers_disclosure_section_is_required(skill_dir: Path) -> None:
    body = _normalized(skill_dir / "SKILL.md")
    assert "prepend a `## Lane failovers` section naming, per failed-over lane" in body
    assert (
        "the lane, the model that failed and the runner's stderr reason, and the model that "
        "actually produced the review"
    ) in body
    assert (
        "State once that these files were reviewed by a different model than the "
        "configuration's first choice."
    ) in body
    assert "This is a disclosure, not a warning" in body
    checklist = _normalized(skill_dir / "SKILL.md")
    assert "`## Lane failovers` section (lane, failed model + stderr reason, model that actually reviewed)" in checklist
    ref = _normalized(skill_dir / "references/configuration.md")
    assert "the rendered review carries a `## Lane failovers` section naming the model that failed" in ref
