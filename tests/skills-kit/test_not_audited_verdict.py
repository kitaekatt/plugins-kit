"""Regression guard: a DECLINED file must never carry a passing verdict.

The defect (reproduced 2026-07-28): git-kit claims every changed `.md` away from
the generic reviewers and routes a skill reference (`*/skills/*/references/*.md`)
to project-doc-audit, whose PD-1 criterion correctly declines it -- and which
then returned `COMPLIANT` / `DIFF-CLEAN` anyway. A caller gating a publish reads
that as "audited, passed" when nothing read the file.

The fix is the `NOT-AUDITED` verdict: emitted by the PD-1 decline, passed through
the review-mode relabel untouched, and counted apart from `diffClean`.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS = REPO_ROOT / "plugins" / "skills-kit" / "skills"

REVIEW_MEMBERS = ("claude-md-audit", "skill-audit", "project-doc-audit")

PROJECT_DOC_DETECT = SKILLS / "project-doc-audit" / "workflow" / "detect.js"


def detect_src(member: str) -> str:
    return (SKILLS / member / "workflow" / "detect.js").read_text(encoding="utf-8")


class TestReducerPreservesNotAudited:
    """The shared review-mode reducer must not relabel or miscount a decline."""

    @pytest.mark.parametrize("member", REVIEW_MEMBERS)
    def test_relabel_passes_not_audited_through(self, member):
        src = detect_src(member)
        assert "if (r.verdict === 'NOT-AUDITED') return { ...r, suppressed: 0 }" in src, (
            f"{member}/workflow/detect.js: the review relabel does not pass a "
            "NOT-AUDITED verdict through -- a declined file would be renamed "
            "DIFF-CLEAN, which is the fake gate this guard exists to prevent"
        )

    @pytest.mark.parametrize("member", REVIEW_MEMBERS)
    def test_not_audited_counted_separately(self, member):
        src = detect_src(member)
        assert "if (r.verdict === 'NOT-AUDITED') acc.notAudited++" in src
        assert "notAudited: 0" in src, (
            f"{member}/workflow/detect.js: totals seed lacks notAudited, so the "
            "count would be NaN and a gate could not read it"
        )

    @pytest.mark.parametrize("member", REVIEW_MEMBERS)
    def test_decline_never_folded_into_diff_clean(self, member):
        """The two counters must stay distinct -- that separation IS the fix."""
        src = detect_src(member)
        assert "if (r.verdict === 'DIFF-CLEAN') acc.diffClean++" in src
        assert "acc.diffClean++" not in src.replace(
            "if (r.verdict === 'DIFF-CLEAN') acc.diffClean++", "", 1
        ), f"{member}: diffClean incremented from more than one verdict branch"


class TestProjectDocDeclineContract:
    """PD-1's decline is the only path that produces NOT-AUDITED today."""

    def test_schema_admits_not_audited(self):
        src = PROJECT_DOC_DETECT.read_text(encoding="utf-8")
        assert "'COMPLIANT', 'NON-COMPLIANT', 'NOT-AUDITED'" in src, (
            "the lane's verdict enum cannot express a decline, so the lane is "
            "forced to claim COMPLIANT for a file it never read"
        )

    def test_routing_clause_declines_instead_of_passing(self):
        src = PROJECT_DOC_DETECT.read_text(encoding="utf-8")
        assert "Verdict NOT-AUDITED." in src
        assert "Verdict COMPLIANT." not in src, (
            "the PD-1 routing clause still instructs the lane to return "
            "COMPLIANT for a declined file -- the original defect"
        )

    def test_routing_finding_is_surfaced_not_silent(self):
        """SILENT + a passing verdict is what made the decline invisible."""
        src = PROJECT_DOC_DETECT.read_text(encoding="utf-8")
        clause = src.split("A_misclassified_skill_ref", 1)[1].split("`\n", 1)[0]
        assert "bucket IMPROVE" in clause
        assert "bucket SILENT" not in clause

    def test_verdict_rule_checks_decline_first(self):
        src = PROJECT_DOC_DETECT.read_text(encoding="utf-8")
        assert "11. Verdict: NOT-AUDITED if the PD-1 routing decline fired" in src

    def test_declined_count_reported_in_log(self):
        src = PROJECT_DOC_DETECT.read_text(encoding="utf-8")
        assert "totals.notAudited ?" in src
        assert "NOT-AUDITED (declined as out of scope" in src


class TestDocumentedContract:
    """The prose contract must match the code, or the next author re-breaks it."""

    def test_skill_md_decision_rules_state_the_override(self):
        skill_md = (SKILLS / "project-doc-audit" / "SKILL.md").read_text(encoding="utf-8")
        assert "-> file is `NOT-AUDITED`, in BOTH modes" in skill_md
        assert "Verdict: COMPLIANT | NON-COMPLIANT | NOT-AUDITED" in skill_md

    def test_taxonomy_a_bucket_is_improve(self):
        skill_md = (SKILLS / "project-doc-audit" / "SKILL.md").read_text(encoding="utf-8")
        record = skill_md.split('- id: "A_misclassified_skill_ref"', 1)[1].split("- id:", 1)[0]
        assert 'bucket: "IMPROVE"' in record
        assert 'bucket: "SILENT"' not in record

    def test_criteria_doc_states_the_verdict(self):
        criteria = (
            SKILLS / "project-doc-audit" / "references" / "audit-criteria.md"
        ).read_text(encoding="utf-8")
        assert "**Verdict:** `NOT-AUDITED` -- never `COMPLIANT`." in criteria
