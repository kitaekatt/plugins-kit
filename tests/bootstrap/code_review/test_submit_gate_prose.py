"""Contracts for submit-gate instructions rendered into both review skills."""

from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL_PATHS = (
    REPO_ROOT / "plugins/git-kit/skills/git-code-review/SKILL.md",
    REPO_ROOT / "plugins/p4-kit/skills/p4-code-review/SKILL.md",
)
AUTHORSHIP_CLAIMS = (
    "you made these edits",
    "they did not make these edits",
    "preflight is the operator's job, not the passenger's",
)
STEP_FIVE_EVIDENCE = (
    "A gate is evaluated from the diff, the repo, and commands you can run, "
    "not from anyone's memory of what was done."
)
NEEDS_USER_GOTCHA = (
    "NEEDS THE USER is for a fact you cannot derive -- an external system's "
    "state, a check that only runs on their hardware, an intent only they "
    "hold. It is not an escape hatch for a gate that is tedious to evaluate, "
    "and when you do use it, ask for that specific fact rather than asking "
    "whether they did the work."
)


def _body(path: Path) -> str:
    """Read one committed generated skill."""
    return path.read_text(encoding="utf-8")


def _step_five(body: str) -> str:
    """Isolate step 5 so a gotcha cannot satisfy its prose contract."""
    return body.split("        - n: 5\n", 1)[1].split("        - n: 6\n", 1)[0]


@pytest.mark.parametrize("claim", AUTHORSHIP_CLAIMS)
@pytest.mark.parametrize("path", SKILL_PATHS, ids=("git", "p4"))
def test_rendered_skills_do_not_assert_authorship(path: Path, claim: str) -> None:
    assert claim not in _body(path).lower()


@pytest.mark.parametrize("path", SKILL_PATHS, ids=("git", "p4"))
def test_step_five_discharge_comes_from_evidence(path: Path) -> None:
    normalized = " ".join(_step_five(_body(path)).split())
    assert STEP_FIVE_EVIDENCE in normalized


@pytest.mark.parametrize("path", SKILL_PATHS, ids=("git", "p4"))
def test_needs_user_gotcha_is_unchanged(path: Path) -> None:
    assert NEEDS_USER_GOTCHA in _body(path)
