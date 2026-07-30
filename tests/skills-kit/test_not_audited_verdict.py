"""Regression guard: a DECLINED file must never carry a passing verdict.

The defect (reproduced 2026-07-28): git-kit claims every changed `.md` away from
the generic reviewers and routed a skill reference (`*/skills/*/references/*.md`)
to the project-doc audit, whose PD-1 criterion correctly declines it -- and which
then returned `COMPLIANT` / `DIFF-CLEAN` anyway. A caller gating a publish reads
that as "audited, passed" when nothing read the file.

The fix is the `NOT-AUDITED` verdict: emitted by the decline, passed through the
review-mode relabel untouched, and counted apart from `diffClean`.

Post-fold (md-domain, 2026-07-29) the six member skills are gone: the three
per-file detect lanes live at `md-domain/workflow/{skill,claude-md,project-doc}-detect.js`
and the prose contract lives in `md-domain/SKILL.md`, `references/lanes/audit-lane.md`
and `references/standards/`. The PD-1 three-branch decline is GENERALIZED to the
skill and claude-md lanes (criteria `artifact_shape_not_skill_md` /
`artifact_shape_not_claude_md`, taxonomy "none", bucket IMPROVE); project-doc
keeps its documented `A_misclassified_skill_ref` taxonomy id.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MD_DOMAIN = REPO_ROOT / "plugins" / "skills-kit" / "skills" / "md-domain"
WORKFLOW = MD_DOMAIN / "workflow"
REFERENCES = MD_DOMAIN / "references"

# The three per-file detect lanes -- all review-capable, all declining.
DETECT_LANES = {
    "skill": WORKFLOW / "skill-detect.js",
    "claude-md": WORKFLOW / "claude-md-detect.js",
    "project-doc": WORKFLOW / "project-doc-detect.js",
}
LANE_IDS = tuple(DETECT_LANES)

PROJECT_DOC_DETECT = DETECT_LANES["project-doc"]

# The two lanes the PD-1 pattern was generalized ONTO, with the artifact token
# their three-branch routing clause switches on and their decline criterion id.
# The `kind` token each lane switches on is its dispatch-table artifact id.
GENERALIZED = {
    "skill": ("skill", "artifact_shape_not_skill_md"),
    "claude-md": ("claude-md", "artifact_shape_not_claude_md"),
}


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def detect_src(lane: str) -> str:
    return _read(DETECT_LANES[lane])


def decline_instruction(lane: str) -> str:
    """The single `const declineInstruction = ...` line of a detect lane."""
    src = detect_src(lane)
    lines = [l for l in src.splitlines() if l.lstrip().startswith("const declineInstruction")]
    assert len(lines) == 1, f"{lane}: expected exactly one declineInstruction, got {len(lines)}"
    return lines[0]


class TestReducerPreservesNotAudited:
    """The shared review-mode reducer must not relabel or miscount a decline."""

    @pytest.mark.parametrize("lane", LANE_IDS)
    def test_relabel_passes_not_audited_through(self, lane):
        src = detect_src(lane)
        assert "if (r.verdict === 'NOT-AUDITED') return { ...r, suppressed: 0 }" in src, (
            f"workflow/{lane}-detect.js: the review relabel does not pass a "
            "NOT-AUDITED verdict through -- a declined file would be renamed "
            "DIFF-CLEAN, which is the fake gate this guard exists to prevent"
        )

    @pytest.mark.parametrize("lane", LANE_IDS)
    def test_not_audited_counted_separately(self, lane):
        src = detect_src(lane)
        assert "if (r.verdict === 'NOT-AUDITED') acc.notAudited++" in src
        assert "notAudited: 0" in src, (
            f"workflow/{lane}-detect.js: totals seed lacks notAudited, so the "
            "count would be NaN and a gate could not read it"
        )

    @pytest.mark.parametrize("lane", LANE_IDS)
    def test_decline_never_folded_into_diff_clean(self, lane):
        """The two counters must stay distinct -- that separation IS the fix."""
        src = detect_src(lane)
        assert "if (r.verdict === 'DIFF-CLEAN') acc.diffClean++" in src
        assert "acc.diffClean++" not in src.replace(
            "if (r.verdict === 'DIFF-CLEAN') acc.diffClean++", "", 1
        ), f"{lane}: diffClean incremented from more than one verdict branch"


class TestProjectDocDeclineContract:
    """PD-1's decline: the original contract, preserved verbatim through the fold."""

    def test_schema_admits_not_audited(self):
        src = _read(PROJECT_DOC_DETECT)
        assert "'COMPLIANT', 'NON-COMPLIANT', 'NOT-AUDITED'" in src, (
            "the lane's verdict enum cannot express a decline, so the lane is "
            "forced to claim COMPLIANT for a file it never read"
        )

    def test_routing_clause_declines_instead_of_passing(self):
        src = _read(PROJECT_DOC_DETECT)
        assert "Verdict NOT-AUDITED." in src
        assert "Verdict COMPLIANT." not in src, (
            "the PD-1 routing clause still instructs the lane to return "
            "COMPLIANT for a declined file -- the original defect"
        )

    def test_routing_finding_is_surfaced_not_silent(self):
        """SILENT + a passing verdict is what made the decline invisible."""
        clause = decline_instruction("project-doc")
        assert 'taxonomy "A_misclassified_skill_ref"' in clause, (
            "the project-doc lane must keep its documented taxonomy id -- the "
            "fold preserves ids verbatim"
        )
        assert "bucket IMPROVE" in clause
        assert "bucket SILENT" not in clause

    def test_verdict_rule_checks_decline_first(self):
        src = _read(PROJECT_DOC_DETECT)
        assert "11. Verdict: NOT-AUDITED if the PD-1 routing decline fired" in src

    def test_declined_count_reported_in_log(self):
        src = _read(PROJECT_DOC_DETECT)
        assert "totals.notAudited ?" in src
        assert "NOT-AUDITED (declined as out of scope" in src


class TestGeneralizedDeclineContract:
    """The PD-1 pattern generalized onto the skill and claude-md detect lanes.

    Router enforcement leg 3 of the phase-3 design: every detect lane
    self-applies its artifact shape test when `kind` is absent and declines a
    non-matching file with NOT-AUDITED plus an IMPROVE routing finding.
    """

    @pytest.mark.parametrize("lane", sorted(GENERALIZED))
    def test_schema_admits_not_audited_as_non_pass(self, lane):
        src = detect_src(lane)
        assert "'COMPLIANT', 'NON-COMPLIANT', 'NOT-AUDITED'" in src, (
            f"{lane}: the verdict enum cannot express a decline, so the lane is "
            "forced to claim COMPLIANT for a file it never read"
        )
        assert "It is NOT a passing verdict and must never be reported as one." in src, (
            f"{lane}: the enum admits NOT-AUDITED but does not tell the model it "
            "is a non-pass -- the description is the only thing stopping a "
            "declined file being summarized as clean"
        )

    @pytest.mark.parametrize("lane", sorted(GENERALIZED))
    def test_routing_clause_has_all_three_branches(self, lane):
        """Absence of a classification is not a classification (branch 3)."""
        src = detect_src(lane)
        token, _criterion = GENERALIZED[lane]
        assert f"const routingClause = f.kind && f.kind !== '{token}'" in src, (
            f"{lane}: branch 1 missing -- an explicit foreign `kind` must decline"
        )
        assert f": f.kind === '{token}'" in src, (
            f"{lane}: branch 2 missing -- an explicit own `kind` must apply the criteria"
        )
        # backtick is escaped inside the JS template literal
        assert r"No \`kind\` signal was provided" in src, (
            f"{lane}: branch 3 missing -- a kind-less (review-mode subject-lens) "
            "call must self-apply the artifact shape test, not assume a match"
        )
        assert "Run the artifact-shape test YOURSELF FIRST" in src

    @pytest.mark.parametrize("lane", sorted(GENERALIZED))
    def test_routing_finding_is_improve_taxonomy_none_never_suppressed(self, lane):
        clause = decline_instruction(lane)
        _token, criterion = GENERALIZED[lane]
        assert f'criterion "{criterion}"' in clause
        assert 'taxonomy "none"' in clause, (
            f"{lane}: the generalized decline must emit the unlettered taxonomy "
            "so the folded taxonomy tables stay byte-identical to their sources"
        )
        assert "bucket IMPROVE" in clause
        assert "bucket SILENT" not in clause
        assert "It is never suppressed in either mode" in clause, (
            f"{lane}: the routing finding must survive the review-mode "
            "attributability filter -- it is the record that nothing was read"
        )
        assert "THIS FILE WAS NOT AUDITED" in clause
        assert "Verdict NOT-AUDITED." in clause

    @pytest.mark.parametrize("lane", sorted(GENERALIZED))
    def test_verdict_rule_checks_decline_first(self, lane):
        src = detect_src(lane)
        assert "Verdict: NOT-AUDITED if the artifact-shape decline fired" in src
        assert "checked FIRST and overriding everything else, in BOTH modes" in src, (
            f"{lane}: the decline must override every other verdict rule in both "
            "modes; anything else lets a declined file be relabelled DIFF-CLEAN"
        )
        assert "never COMPLIANT or DIFF-CLEAN" in src

    @pytest.mark.parametrize("lane", sorted(GENERALIZED))
    def test_declined_count_reported_in_log(self, lane):
        src = detect_src(lane)
        assert "totals.notAudited ?" in src
        assert "NOT-AUDITED (declined as out of scope" in src


class TestDocumentedContract:
    """The prose contract must match the code, or the next author re-breaks it."""

    def test_skill_md_states_not_audited_is_not_a_pass(self):
        skill_md = _read(MD_DOMAIN / "SKILL.md")
        assert "**`NOT-AUDITED` is not a pass.**" in skill_md
        assert "APART from `diffClean`" in skill_md, (
            "md-domain/SKILL.md's review-mode routing rules no longer say a "
            "decline is counted apart from the clean count"
        )

    def test_audit_lane_carries_the_decline_contract(self):
        lane = _read(REFERENCES / "lanes" / "audit-lane.md")
        assert "### Step 2a -- The decline contract (MANDATORY, every detect lane)" in lane
        # the three branches, stated once for all lanes
        assert "-> DECLINE. Verdict `NOT-AUDITED`. Emit the routing finding. No criteria run." in lane
        assert "the caller's classification is authoritative." in lane.lower()
        assert "**`kind` is ABSENT**" in lane
        assert "Absence of a classification is not a classification." in lane

    def test_audit_lane_verdict_rules_check_decline_first(self):
        lane = _read(REFERENCES / "lanes" / "audit-lane.md")
        assert "`NOT-AUDITED`, in BOTH modes. Checked FIRST; overrides everything below." in lane
        assert "is **not a passing verdict**" in lane
        assert "Verdict: COMPLIANT | NON-COMPLIANT | DIFF-CLEAN | NOT-AUDITED" in lane

    def test_audit_lane_pins_the_routing_finding_shape(self):
        lane = _read(REFERENCES / "lanes" / "audit-lane.md")
        assert "Bucket **IMPROVE**, severity INFO." in lane
        assert "It is **never suppressed**, in either mode." in lane
        # per-lane taxonomy ids, preserved verbatim through the fold
        assert "`A_misclassified_skill_ref`" in lane
        assert "`artifact_shape_not_skill_md`" in lane
        assert "artifact_shape_not_claude_md" in lane
        assert 'taxonomy: "none"' in lane

    def test_audit_lane_reducer_invariants_are_documented(self):
        lane = _read(REFERENCES / "lanes" / "audit-lane.md")
        assert (
            "The review reducer passes `NOT-AUDITED` through relabel UNTOUCHED and counts it"
            in lane
        )
        assert "relabel untouched and is counted apart from `diffClean`" in lane

    def test_project_doc_standards_states_the_verdict(self):
        standards = _read(REFERENCES / "standards" / "project-doc-standards.md")
        assert "**Verdict:** `NOT-AUDITED` -- never `COMPLIANT`." in standards

    def test_project_doc_standards_taxonomy_a_bucket_is_improve(self):
        standards = _read(REFERENCES / "standards" / "project-doc-standards.md")
        assert "| `A_misclassified_skill_ref` | IMPROVE |" in standards, (
            "PD-1's routing finding must stay bucket IMPROVE -- SILENT next to a "
            "verdict is what made the decline invisible"
        )
