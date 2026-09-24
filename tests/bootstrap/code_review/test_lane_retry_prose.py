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
    # Scoped, not dropped: a lane reaching this section has no usable entry
    # left, so "a different model" means one its declaration never named.
    assert "never re-run the lane on a model its own declaration did not name" in body
    assert "only when its declaration has no usable entry left" in body


# --------------------------------------------------------------------------
# Migration step 5: a failed lane re-selects through `describe`, following
# the rule describe prints. The skill does not restate that rule, and the
# old "walk model_fallbacks in order" chain is gone.
# --------------------------------------------------------------------------


def test_failed_lane_reselects_through_describe_with_exclusions(skill_dir: Path) -> None:
    body = _normalized(skill_dir / "SKILL.md")
    assert "follow the `Re-select:` line describe printed" in body
    assert "one `--exclude <entry>` per entry this lane has already failed on" in body
    assert "Each entry is tried at most once per lane." in body


def test_the_old_fallback_chain_walk_is_gone(skill_dir: Path) -> None:
    body = _normalized(skill_dir / "SKILL.md")
    ref = _normalized(skill_dir / "references/configuration.md")
    for text in (body, ref):
        assert "FAILOVER-ELIGIBLE" not in text
        assert "Walk the chain in order" not in text
        assert "re-dispatch the SAME lane on the next entry in `model_fallbacks`" not in text
        assert "## Lane failovers" not in text


def test_the_skill_prints_the_rule_instead_of_restating_it(skill_dir: Path) -> None:
    """The trigger set is describe's text (llm-scripting-kit RULE_TRIGGER_SESSION)."""
    body = _normalized(skill_dir / "SKILL.md")
    assert "print its stdout verbatim" in body
    assert "a launch that produced no output -- moves to another usable entry" not in body
    assert "is a task failure, not a trigger" not in body


def test_a_lane_with_no_usable_entry_left_is_a_failed_lane(skill_dir: Path) -> None:
    body = _normalized(skill_dir / "SKILL.md")
    assert (
        "the lane has no usable entry left: report it in step 9 under `## Lane failures`, "
        "naming every entry tried and why it failed, and mark coverage missing -- never treat "
        'absent output as "no issues found"'
    ) in body
    ref = _normalized(skill_dir / "references/configuration.md")
    assert "A lane reaches `## Lane failures` only when no usable entry of its declaration is left." in ref


def test_lane_routes_disclosure_section_is_required(skill_dir: Path) -> None:
    body = _normalized(skill_dir / "SKILL.md")
    assert "prepend a `## Lane routes` section carrying every `route:` line announced in step 6, verbatim" in body
    assert "This is a disclosure, not a warning" in body
    assert "`## Lane routes` section" in body
    ref = _normalized(skill_dir / "references/configuration.md")
    assert "the rendered review carries a `## Lane routes` section" in ref


# --------------------------------------------------------------------------
# A reviewer lane that can run a transport entry keeps it in the describe
# menu (carried fix from the step 5 review). The runner binds only the lanes
# outside LANES_REQUIRING_AGENT_LOOP to a transport, so only those describes
# pass `--dispatchable transport`; the others keep the default session menu.
# --------------------------------------------------------------------------


def test_describe_keeps_transports_for_lanes_that_can_run_them(skill_dir: Path) -> None:
    from bootstrap_lib.code_review.lane_prompts import LANES_REQUIRING_AGENT_LOOP

    body = _normalized(skill_dir / "SKILL.md")
    assert "plus `--dispatchable transport` when the reviewer is not" in body
    for lane in sorted(LANES_REQUIRING_AGENT_LOOP):
        assert f"`{lane}`" in body
    ref = _normalized(skill_dir / "references/configuration.md")
    assert "[--dispatchable transport]" in ref
    assert "a transport endpoint is left out of a multi-entry declaration's menu" not in ref
