"""Regression pins for exception-aware built-in conventions (skills-kit 0.31.0).

Companion to the H-11 ancestor-convention feature (0.30.0). The md-domain audit
classifiers carry HARDCODED universal-convention checks (a non-ASCII look-alike
or a hardcoded absolute path is a convention-violation FIX). This feature makes
those checks EXCEPTION-AWARE: when an ancestor CLAUDE.md explicitly declares a
scoped exception covering the specific instance, the built-in FIX is suppressed
(demoted to PASS/INFO citing the verbatim exception + source path), and the same
declared rule+exception governs BOTH the ancestor-convention finding and the
built-in check so they can never contradict.

0.32.0 brought the project-doc audit lane to parity with the claude-md and skill
audit lanes (the PD-11 ancestor-convention finding, taxonomy
S_ancestor_convention_violation, plus --review mode), so all three review-capable
lanes are pinned here.

Post-fold (md-domain, 2026-07-29) the content moved but the contract did not:

- the lane prompts are `md-domain/workflow/{claude-md,skill,project-doc}-detect.js`
  (hand-maintained wording -- the generator only fully renders remediate.js);
- the per-artifact rule text is `references/standards/{skill,claude-md,project-doc}-standards.md`
  (taxonomy ids M_ / R_ / S_, deliberately un-unified);
- the single shared prose statement of the carve-out -- the successor of the
  per-member SKILL.md inline detect paths -- is `references/lanes/audit-lane.md`.

The intent is unchanged: keep the three lanes and the two detection paths
(workflow lane + the shared inline procedure) from silently diverging.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MD_DOMAIN = REPO_ROOT / "plugins" / "skills-kit" / "skills" / "md-domain"
WORKFLOW = MD_DOMAIN / "workflow"
REFERENCES = MD_DOMAIN / "references"

CLAUDE_MD_DETECT = WORKFLOW / "claude-md-detect.js"
SKILL_DETECT = WORKFLOW / "skill-detect.js"
PROJECT_DOC_DETECT = WORKFLOW / "project-doc-detect.js"

AUDIT_LANE = REFERENCES / "lanes" / "audit-lane.md"
SKILL_STANDARDS = REFERENCES / "standards" / "skill-standards.md"
CLAUDE_MD_STANDARDS = REFERENCES / "standards" / "claude-md-standards.md"
PROJECT_DOC_STANDARDS = REFERENCES / "standards" / "project-doc-standards.md"

# All three review-capable lanes' detect.js share the exception carve-out.
ALL_DETECT = (CLAUDE_MD_DETECT, SKILL_DETECT, PROJECT_DOC_DETECT)

# The gate the exception carve-out is built on: it is present ONLY when ancestor
# paths were supplied, so absent ancestors == today's behavior.
GATE = "ancestorPaths.length > 0"
EXCEPTION_CONST = "builtinConventionExceptionClause"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _flat(p: Path) -> str:
    """Whitespace-collapsed text, for pins on prose that is hard-wrapped."""
    return " ".join(p.read_text(encoding="utf-8").split())


class TestBuiltinExceptionClauseInDetectJs:
    """All three lanes' detect.js gate the built-in carve-out on ancestor paths."""

    def test_claude_md_detect_declares_exception_clause(self):
        text = _read(CLAUDE_MD_DETECT)
        assert f"const {EXCEPTION_CONST} = {GATE}" in text
        # empty string when no ancestor paths -> behavior exactly as today
        assert f"const {EXCEPTION_CONST} = {GATE}\n    ? " in text
        # injected into the convention-violation FIX bullet
        assert f"${{{EXCEPTION_CONST}}}" in text

    def test_skill_detect_declares_exception_clause(self):
        text = _read(SKILL_DETECT)
        assert f"const {EXCEPTION_CONST} = {GATE}" in text
        assert f"const {EXCEPTION_CONST} = {GATE}\n    ? " in text
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


class TestSharedLaneProseMirrorsTheDetectLanes:
    """audit-lane.md is the ONE inline (non-workflow) detect path after the fold.

    Pre-fold this carve-out was restated in each member SKILL.md's inline
    single-file path; the fold replaced those three copies with one shared
    statement in the audit procedure, which every lane inherits.
    """

    def test_shared_inline_path_has_exception_awareness(self):
        text = _flat(AUDIT_LANE)
        assert "**Exception awareness** (applies to the ancestor check AND to the built-in" in text
        assert "do NOT flag that instance under either check" in text
        assert "when in doubt the check STILL fires" in text

    def test_shared_inline_path_states_scope_and_content_kind(self):
        text = _flat(AUDIT_LANE)
        assert "declares a scoped exception covering the specific instance" in text
        assert "right file scope AND right content kind" in text
        assert "demote it to PASS/INFO and cite the verbatim exception quote + source path" in text

    def test_shared_inline_path_states_precedence(self):
        text = _flat(AUDIT_LANE)
        assert "governs both checks so they never contradict" in text

    def test_shared_inline_path_keeps_verbatim_quote_posture(self):
        text = _flat(AUDIT_LANE)
        assert "quotable VERBATIM from the ancestor" in text
        assert "No inferred or stretched exceptions" in text

    def test_ancestor_check_is_gated_on_supplied_paths(self):
        """Absent ancestors == today's behavior, in the prose as in the code."""
        text = _read(AUDIT_LANE)
        assert "when `ancestorClaudeMdPaths` is non-empty" in text


class TestStandardsDocsDocumentSuppression:
    """The per-artifact rule text now lives in references/standards/."""

    def test_claude_md_h11_section_documents_exception_suppression(self):
        text = _read(CLAUDE_MD_STANDARDS)
        assert "Ancestor-declared exceptions suppress the built-in universal conventions." in text
        assert "demotes to PASS/INFO" in text
        # precedence is spelled out so the doc matches the prompt
        assert "an exception that silences H-11 silences the built-in FIX too" in text

    def test_project_doc_pd11_section_documents_exception_suppression(self):
        text = _flat(PROJECT_DOC_STANDARDS)
        assert "Ancestor-declared exceptions suppress the built-in universal conventions." in text
        assert "demotes to PASS/INFO" in text
        assert "an exception that silences PD-11 silences the built-in FIX too" in text

    def test_ancestor_taxonomy_ids_survive_the_fold_unmodified(self):
        """M_ / R_ / S_ stay inconsistent on purpose -- unifying them is a follow-up."""
        for doc, tax in ((SKILL_STANDARDS, "M_ancestor_convention_violation"),
                         (CLAUDE_MD_STANDARDS, "R_ancestor_convention_violation"),
                         (PROJECT_DOC_STANDARDS, "S_ancestor_convention_violation")):
            text = _read(doc)
            assert tax in text, f"{doc.name} lost its ancestor-convention taxonomy id"
        # and no doc picked up a sibling lane's id in the copy
        assert "R_ancestor_convention_violation" not in _read(SKILL_STANDARDS)
        assert "M_ancestor_convention_violation" not in _read(CLAUDE_MD_STANDARDS)

    def test_ancestor_finding_disposition_is_fix(self):
        for doc in (SKILL_STANDARDS, CLAUDE_MD_STANDARDS, PROJECT_DOC_STANDARDS):
            text = _read(doc)
            line = [l for l in text.splitlines() if "ancestor_convention_violation" in l and "|" in l]
            assert line, f"{doc.name}: no taxonomy table row for the ancestor finding"
            assert any("| FIX |" in l for l in line), (
                f"{doc.name}: the ancestor-convention finding is no longer FIX by default"
            )


class TestReviewModeParityAcrossLanes:
    """All three review-capable lanes carry the same --review contract in detect.js.

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

    def test_audit_lane_documents_review_mode(self):
        text = _read(AUDIT_LANE)
        assert "## Review mode" in text
        assert "**Verdict is `DIFF-CLEAN`, not `COMPLIANT`.**" in text
        assert "### You materialize the pre-images; the workflow never does" in text
        # the threshold override, and the rejected flag combination
        assert "**REVIEW MODE OVERRIDE: the threshold is 1.**" in text
        assert "Reject the combination rather than guessing." in text
