"""Contract tests for the generated code-review effort agents."""

from pathlib import Path

import pytest
import yaml

from bootstrap_lib.code_review.review_profiles import EFFORT_LEVELS


REPO_ROOT = Path(__file__).resolve().parents[3]


def _agent_paths(kit: str) -> list[Path]:
    """Return the effort-agent files shipped by one review kit."""
    return sorted((REPO_ROOT / "plugins" / kit / "agents").glob("review-lane-*.md"))


def _frontmatter(path: Path) -> dict[str, object]:
    """Parse one agent file's YAML frontmatter."""
    _, raw, _ = path.read_text(encoding="utf-8").split("---", 2)
    value = yaml.safe_load(raw)
    if not isinstance(value, dict):
        raise TypeError(f"frontmatter is not a mapping: {path}")
    return value


@pytest.mark.parametrize("kit", ("git-kit", "p4-kit"))
def test_agent_stems_match_effort_levels(kit: str) -> None:
    levels = {
        path.stem.removeprefix("review-lane-") for path in _agent_paths(kit)
    }
    assert levels == set(EFFORT_LEVELS)


@pytest.mark.parametrize("kit", ("git-kit", "p4-kit"))
def test_agent_frontmatter_name_matches_filename(kit: str) -> None:
    for path in _agent_paths(kit):
        assert _frontmatter(path)["name"] == path.stem


@pytest.mark.parametrize("kit", ("git-kit", "p4-kit"))
def test_agent_frontmatter_effort_matches_filename(kit: str) -> None:
    for path in _agent_paths(kit):
        expected_effort = path.stem.removeprefix("review-lane-")
        assert _frontmatter(path)["effort"] == expected_effort
