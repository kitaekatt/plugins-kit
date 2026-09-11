"""Reject time-relative wording in code-review contracts."""

from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
GENERATOR = REPO_ROOT / "scripts/gen_code_review_skills.py"
GIT_PREPARE = REPO_ROOT / "plugins/git-kit/scripts/prepare_review.py"
GIT_SKILL = REPO_ROOT / "plugins/git-kit/skills/git-code-review/SKILL.md"
P4_SKILL = REPO_ROOT / "plugins/p4-kit/skills/p4-code-review/SKILL.md"
GIT_MD_DOMAIN = (
    REPO_ROOT
    / "plugins/git-kit/skills/git-code-review/references/md-domain-review.md"
)
P4_MD_DOMAIN = (
    REPO_ROOT
    / "plugins/p4-kit/skills/p4-code-review/references/md-domain-review.md"
)
RELATIVE_DAY = "to" + "day"
RELATIVE_OWNERSHIP = "now " + "owns"


@pytest.mark.parametrize(
    "path,phrase",
    (
        (GENERATOR, RELATIVE_DAY),
        (GENERATOR, RELATIVE_OWNERSHIP),
        (GIT_PREPARE, RELATIVE_DAY),
        (GIT_SKILL, RELATIVE_DAY),
        (P4_SKILL, RELATIVE_DAY),
        (GIT_MD_DOMAIN, RELATIVE_OWNERSHIP),
        (P4_MD_DOMAIN, RELATIVE_OWNERSHIP),
    ),
    ids=(
        "generator-relative-day",
        "generator-relative-ownership",
        "git-prepare-relative-day",
        "git-skill-relative-day",
        "p4-skill-relative-day",
        "git-md-domain-relative-ownership",
        "p4-md-domain-relative-ownership",
    ),
)
def test_contract_prose_has_no_temporal_deixis(path: Path, phrase: str) -> None:
    assert phrase not in path.read_text(encoding="utf-8").lower()


def test_git_no_claim_path_names_the_pre_claim_bundle_contract() -> None:
    body = " ".join(GIT_PREPARE.read_text(encoding="utf-8").split())
    assert body.count("byte-identical to the pre-claim bundle contract") == 2
