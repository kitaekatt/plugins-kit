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
    assert "Other non-zero exits are FAILED lanes: do NOT retry them" in body
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
    assert "never re-run the lane on a different model" in body
