"""Contracts for md-domain dimension classification from review skills."""

from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
REFERENCES = (
    REPO_ROOT / "plugins/git-kit/skills/git-code-review/references/md-domain-review.md",
    REPO_ROOT / "plugins/p4-kit/skills/p4-code-review/references/md-domain-review.md",
)


@pytest.mark.parametrize("path", REFERENCES, ids=("git", "p4"))
def test_dimension_calls_the_shipped_classifier(path: Path) -> None:
    body = " ".join(path.read_text(encoding="utf-8").split())
    required = (
        "from discover_claude_md import classify_dimension",
        "print(classify_dimension(Path(sys.argv[2])))",
    )
    assert all(fragment in body for fragment in required)


@pytest.mark.parametrize("path", REFERENCES, ids=("git", "p4"))
def test_dimension_does_not_restate_an_incomplete_heuristic(path: Path) -> None:
    body = " ".join(path.read_text(encoding="utf-8").split())
    assert "only if the file has code/yaml/csv siblings" not in body


@pytest.mark.parametrize("path", REFERENCES, ids=("git", "p4"))
def test_unavailable_classifier_takes_the_broad_skew_fallback(path: Path) -> None:
    body = " ".join(path.read_text(encoding="utf-8").split())
    assert "discover_claude_md.classify_dimension is unavailable" in body
