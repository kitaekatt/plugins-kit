"""Regression pins for exception-aware built-in conventions (skills-kit 0.31.0).

Companion to the H-11 ancestor-convention feature (0.30.0). The md-audit
classifiers carry HARDCODED universal-convention checks (a non-ASCII look-alike
or a hardcoded absolute path is a convention-violation FIX). This feature makes
those checks EXCEPTION-AWARE: when an ancestor CLAUDE.md explicitly declares a
scoped exception covering the specific instance, the built-in FIX is suppressed
(demoted to PASS/INFO citing the verbatim exception + source path), and the same
declared rule+exception governs BOTH the ancestor-convention finding and the
built-in check so they can never contradict.

0.32.0 brought project-doc-audit to parity with claude-md-audit and skill-audit
(the PD-11 ancestor-convention finding, taxonomy S_ancestor_convention_violation,
plus --review mode), so all three review-capable members are pinned here.

These are content pins in the spirit of the H-11 regression pin: the wording
lives hand-maintained in detect.js and the SKILL.md inline paths (the generator
only fully renders remediate.js), so a pin keeps the three members and the two
detection paths (workflow lane + inline single-file) from silently diverging.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS = REPO_ROOT / "plugins" / "skills-kit" / "skills"

CLAUDE_MD_DETECT = SKILLS / "claude-md-audit" / "workflow" / "detect.js"
SKILL_DETECT = SKILLS / "skill-audit" / "workflow" / "detect.js"
PROJECT_DOC_DETECT = SKILLS / "project-doc-audit" / "workflow" / "detect.js"
CLAUDE_MD_SKILL = SKILLS / "claude-md-audit" / "SKILL.md"
SKILL_SKILL = SKILLS / "skill-audit" / "SKILL.md"
PROJECT_DOC_SKILL = SKILLS / "project-doc-audit" / "SKILL.md"
AUDIT_CRITERIA = SKILLS / "claude-md-audit" / "references" / "audit-criteria.md"
PROJECT_DOC_CRITERIA = SKILLS / "project-doc-audit" / "references" / "audit-criteria.md"

# All three review-capable members' detect.js share the exception carve-out.
ALL_DETECT = (CLAUDE_MD_DETECT, SKILL_DETECT, PROJECT_DOC_DETECT)

# The gate the exception carve-out is built on: it is present ONLY when ancestor
# paths were supplied, so absent ancestors == today's behavior.
GATE = "ancestorPaths.length > 0"
EXCEPTION_CONST = "builtinConventionExceptionClause"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


class TestBuiltinExceptionClauseInDetectJs:
    """Both members' detect.js gate the built-in carve-out on ancestor paths."""

    def test_claude_md_detect_declares_exception_clause(self):
        text = _read(CLAUDE_MD_DETECT)
        assert f"const {EXCEPTION_CONST} = {GATE}" in text
        # empty string when no ancestor paths -> behavior exactly as today
        assert f"const {EXCEPTION_CONST} = {GATE}\n    ? " in text
        # injected into the step-8 convention-violation FIX bullet
        assert f"${{{EXCEPTION_CONST}}}" in text

    def test_skill_detect_declares_exception_clause(self):
        text = _read(SKILL_DETECT)
        assert f"const {EXCEPTION_CONST} = {GATE}" in text
        assert f"${{{EXCEPTION_CONST}}}" in text

    def test_project_doc_detect_declares_exception_clause(self):
        text = _read(PROJECT_DOC_DETECT)
        assert f"const {EXCEPTION_CONST} = {GATE}" in text
        assert f"const {EXCEPTION_CONST} = {GATE}\n    ? " in text
        assert f"${{{EXCEPTION_CONST}}}" in text

    def test_carve_out_names_scope_and_content_kind(self):
        # No inferred/stretched exceptions: it must cover the exact instance.
        for p in ALL_DETECT:
            text = _read(p)
            assert "COVERS this exact instance" in text
            assert "right file scope AND the right content kind" in text
            # motivating example is pinned so the wording stays concrete
            assert "developer names in the contributors section" in text

    def test_carve_out_states_precedence(self):
        # The declared rule+exception must yield ONE outcome across both checks.
        for p in ALL_DETECT:
            text = _read(p)
            assert "PRECEDENCE:" in text
            assert "must never contradict" in text
            assert "and vice versa" in text

    def test_carve_out_keeps_verbatim_quote_posture(self):
        for p in ALL_DETECT:
            text = _read(p)
            assert "when in doubt the built-in check STILL fires" in text
            # the demotion carries the exception provenance
            assert "verbatim quoted exception rule plus the ancestor source path" in text


class TestAncestorClauseIsExceptionAware:
    """The ancestor-convention finding honors a declared exception (the other half of precedence)."""

    def test_ancestor_clause_mentions_exception_awareness(self):
        for p, tax in ((CLAUDE_MD_DETECT, "R_ancestor_convention_violation"),
                       (SKILL_DETECT, "M_ancestor_convention_violation"),
                       (PROJECT_DOC_DETECT, "S_ancestor_convention_violation")):
            text = _read(p)
            assert "EXCEPTION AWARENESS:" in text
            # the ancestor-convention check does not fire on an instance a scoped exception covers
            assert f"do NOT emit the {tax} finding for it" in text


class TestInlineSingleFilePathsMirrorTheLane:
    """The SKILL.md inline (non-workflow) detect paths carry the same carve-out."""

    def test_claude_md_inline_path_has_exception_awareness(self):
        text = _read(CLAUDE_MD_SKILL)
        assert "exception awareness (applies to H-11 AND to the built-in" in text
        assert "do NOT flag that instance under either check" in text
        assert "when in doubt the check STILL fires" in text

    def test_skill_inline_path_has_exception_awareness(self):
        text = _read(SKILL_SKILL)
        assert "exception awareness (applies to H-11 AND to the built-in" in text
        assert "do NOT flag that instance under either check" in text

    def test_project_doc_inline_path_has_exception_awareness(self):
        text = _read(PROJECT_DOC_SKILL)
        assert "exception awareness (applies to PD-11 AND to the built-in" in text
        assert "do NOT flag that instance under either check" in text
        assert "when in doubt the check STILL fires" in text

    def test_skill_cohesion_recap_notes_exception(self):
        # The recap's H-11 line must carry the suppression note too.
        text = _read(SKILL_SKILL)
        assert "UNLESS the ancestor also declares an explicit scoped exception" in text


class TestAuditCriteriaDocumentsSuppression:
    def test_claude_md_h11_section_documents_exception_suppression(self):
        text = _read(AUDIT_CRITERIA)
        assert "Ancestor-declared exceptions suppress the built-in universal conventions." in text
        assert "demotes to PASS/INFO" in text
        # precedence is spelled out so the doc matches the prompt
        assert "an exception that silences H-11 silences the built-in FIX too" in text

    def test_project_doc_pd11_section_documents_exception_suppression(self):
        text = _read(PROJECT_DOC_CRITERIA)
        assert "Ancestor-declared exceptions suppress the built-in universal conventions." in text
        assert "demotes to PASS/INFO" in text
        assert "an exception that silences PD-11 silences the built-in FIX too" in text


class TestReviewModeParityAcrossMembers:
    """All three review-capable members carry the same --review contract in detect.js.

    The DIFF-CLEAN reducer body is enforced byte-identical by
    test_workflow_js_drift.py (DETECT_REVIEW_TOTALS_CHUNK). These pins cover the
    lane-side pieces that live outside the shared chunk: the attributable schema
    field, the review flag, the attributability clause, and the DIFF-CLEAN relabel.
    """

    def test_detect_declares_review_flag_and_attributable(self):
        for p in ALL_DETECT:
            text = _read(p)
            assert "const review = input.review === true" in text
            # schema carries the per-finding attributability marker
            assert "attributable: { type: 'boolean'" in text
            assert "'remediation', 'attributable'" in text

    def test_detect_relabels_verdict_diff_clean(self):
        for p in ALL_DETECT:
            text = _read(p)
            assert "DIFF-CLEAN" in text
            assert "SERIOUS ALWAYS SURVIVES" in text
            assert "return { perFile: results, totals, review }" in text

    def test_detect_attributability_clause_present(self):
        for p in ALL_DETECT:
            text = _read(p)
            assert "REVIEW MODE. This audit gates a submit" in text
            assert "attributable: false" in text

    def test_project_doc_skill_documents_review_mode(self):
        text = _read(PROJECT_DOC_SKILL)
        assert "## Review mode" in text
        assert "Verdict is `DIFF-CLEAN`, not `COMPLIANT`" in text
        assert "You materialize the pre-images; the workflow never does." in text
        # review parses in step 1 and overrides the multi-file threshold in step 2
        assert "REVIEW MODE OVERRIDE" in text
        assert "Reject the combination review + non_interactive" in text
