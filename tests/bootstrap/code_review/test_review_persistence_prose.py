"""Contracts for review-output and on-disk-artifact prose."""

from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
SKILLS = (
    (
        REPO_ROOT / "plugins/git-kit/skills/git-code-review/SKILL.md",
        "does not post a PR comment",
        "publishing the rendered review to a PR comment",
    ),
    (
        REPO_ROOT / "plugins/p4-kit/skills/p4-code-review/SKILL.md",
        "does not post a Swarm or PR comment",
        "publishing the rendered review to Swarm or a PR comment",
    ),
)


def _body(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _gotchas(body: str) -> str:
    return body.split("      gotchas:\n", 1)[1].split("  narration:\n", 1)[0]


def _scope(body: str) -> str:
    return body.split("  scope:\n", 1)[1].split("  techniques:\n", 1)[0]


@pytest.mark.parametrize("path,comment_claim,scope_claim", SKILLS, ids=("git", "p4"))
def test_gotcha_does_not_claim_disk_free_operation(
    path: Path, comment_claim: str, scope_claim: str
) -> None:
    assert "disk write step" not in _gotchas(_body(path))


@pytest.mark.parametrize("path,comment_claim,scope_claim", SKILLS, ids=("git", "p4"))
def test_gotcha_retains_the_no_comment_boundary(
    path: Path, comment_claim: str, scope_claim: str
) -> None:
    assert comment_claim in _gotchas(_body(path))


@pytest.mark.parametrize("path,comment_claim,scope_claim", SKILLS, ids=("git", "p4"))
def test_gotcha_names_transient_and_durable_disk_artifacts(
    path: Path, comment_claim: str, scope_claim: str
) -> None:
    gotchas = _gotchas(_body(path))
    required = ("diff chunks", "bundle.json", "pre-images", "durable ledger.json")
    assert all(term in gotchas for term in required)


@pytest.mark.parametrize("path,comment_claim,scope_claim", SKILLS, ids=("git", "p4"))
def test_scope_does_not_exclude_disk_persistence(
    path: Path, comment_claim: str, scope_claim: str
) -> None:
    assert "persisting review output to disk" not in _scope(_body(path))


@pytest.mark.parametrize("path,comment_claim,scope_claim", SKILLS, ids=("git", "p4"))
def test_scope_retains_the_no_comment_boundary(
    path: Path, comment_claim: str, scope_claim: str
) -> None:
    assert scope_claim in _scope(_body(path))
