"""Contract pins for the coverage verb's detect lane and procedure.

Coverage is the discovery phase of generation -- still REPORT-ONLY, still not an
audit lane. Several of its
properties are load-bearing in a way that is invisible at a glance, and each has
a specific way of silently regressing:

  * It must never emit COMPLIANT / NON-COMPLIANT. Those belong to the document
    lanes and answer a different question; conflating them is the exact misread
    the whole capability exists to correct.
  * It must pin opus + high explicitly. A coverage run normally has ONE subject,
    and the audit lane's single-subject shortcut runs inline at whatever model
    the session happens to be on -- so an unpinned lane silently drops tier in
    the common case rather than the rare one.
  * It must have NO remediate lane. Report-only is what keeps the discovery phase
    cheap; a remediate lane would drag in the sonnet+low pin and the generator,
    which assumes per-file edits and applied/skipped/failed results.
  * It must REFUSE to run when the authored criteria path is missing rather than
    improvising. An invented predicate reproduces the hazard sweep two
    adversarial reviews rejected -- and it would look like it was working.

These are text-level assertions because the lane is a Workflow script (top-level
`return`, `agent()` / `parallel()` injected at run time), so it cannot be
imported and executed here.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MD_DOMAIN = REPO_ROOT / "plugins" / "skills-kit" / "skills" / "md-domain"
DETECT = MD_DOMAIN / "workflow" / "coverage-detect.js"
LANE = MD_DOMAIN / "references" / "lanes" / "coverage-lane.md"
DISCOVER = MD_DOMAIN / "scripts" / "discover_coverage.py"
STANDARDS = MD_DOMAIN / "references" / "standards" / "coverage-standards.md"


def _detect() -> str:
    return DETECT.read_text(encoding="utf-8")


def _lane() -> str:
    return LANE.read_text(encoding="utf-8")


def _lane_prompt_body() -> str:
    """Just the lanePrompt template literal, excluding the file header.

    Several contract phrases appear in BOTH the header comment and the prompt;
    asserting over the whole file cannot tell the two apart.
    """
    src = _detect()
    return src[src.index("const lanePrompt"):src.index("phase('Coverage')")]


class TestArtifactsExist:
    def test_detect_lane_exists(self):
        assert DETECT.is_file()

    def test_procedure_exists(self):
        assert LANE.is_file()

    def test_discover_script_exists(self):
        assert DISCOVER.is_file()

    def test_no_remediate_lane(self):
        """Report-only: a remediate lane would be fictitious and costly."""
        assert not (MD_DOMAIN / "workflow" / "coverage-remediate.js").exists()

    def test_generator_does_not_own_a_coverage_lane(self):
        """gen_workflow_js.py assumes per-file edits + applied/skipped/failed."""
        gen = REPO_ROOT / "plugins" / "skills-kit" / "scripts" / "gen_workflow_js.py"
        assert "coverage" not in gen.read_text(encoding="utf-8").lower()


class TestVerdictVocabulary:
    def test_declares_only_the_two_coverage_verdicts(self):
        src = _detect()
        assert "'GAPS-FOUND'" in src
        assert "'COVERAGE-ASSESSED'" in src

    def test_never_emits_document_compliance_verdicts(self):
        """A CLAUDE.md can be COMPLIANT while its subtree is GAPS-FOUND."""
        src = _detect()
        # The words may appear in prose forbidding them; they must never appear
        # as an emittable enum value.
        assert "'COMPLIANT'" not in src
        assert "'NON-COMPLIANT'" not in src
        assert "'DIFF-CLEAN'" not in src
        assert "'NOT-AUDITED'" not in src

    def test_schema_enum_is_exactly_the_two_verdicts(self):
        src = _detect()
        assert "enum: ['GAPS-FOUND', 'COVERAGE-ASSESSED']" in src

    def test_verdict_is_derived_not_trusted(self):
        """The schema cannot express GAPS-FOUND iff candidates is non-empty.

        Without derivation a schema-valid response can carry GAPS-FOUND with
        zero candidates and contradict the lane's own decision rules.
        """
        src = _detect()
        assert "candidates.length ? 'GAPS-FOUND' : 'COVERAGE-ASSESSED'" in src
        assert "verdict: derived" in src


class TestModelPinning:
    def test_pins_opus_high_explicitly(self):
        src = _detect()
        assert "model: 'opus'" in src
        assert "effort: 'high'" in src

    def test_no_sonnet_low_remediation_pin(self):
        src = _detect()
        assert "model: 'sonnet'" not in src

    def test_lane_documents_the_single_subject_trap(self):
        """The reason the pin matters more here than in the document lanes."""
        text = _lane().lower()
        assert "regardless of subject count" in text


class TestCriteriaWiring:
    def test_detect_refuses_without_criteria(self):
        """The throw must be INSIDE the guard, not merely present somewhere."""
        src = _detect()
        guard = re.search(
            r"if \(typeof criteriaPath !== 'string'.*?\n\}",
            src, re.S,
        )
        assert guard, "no criteriaPath type guard found"
        assert "throw new Error" in guard.group(0)

    def test_guard_rejects_truthy_non_paths(self):
        """`true` is truthy and names no document; it must not pass."""
        src = _detect()
        assert "typeof criteriaPath !== 'string'" in src
        assert "criteriaPath.trim() === ''" in src

    def test_guard_precedes_any_agent_dispatch(self):
        """A refusal after fan-out would have already spent the tokens."""
        src = _detect()
        assert src.index("throw new Error") < src.index("agent(lanePrompt")

    def test_refusal_names_the_rejected_design(self):
        """The guard exists to prevent improvising, not merely to be tidy."""
        src = _detect()
        assert "hazard sweep" in src

    def test_lane_marks_step_three_as_filled(self):
        text = _lane()
        assert "### Step 3 -- Assess" in text
        assert "references/standards/coverage-standards.md" in text
        assert "ABSOLUTE path" in text
        assert "refs.criteria" in text
        assert "[SEAM]" not in text


class TestReportOnlyPosture:
    def test_lane_declares_no_qa_gate_and_no_remediation(self):
        text = _lane().lower()
        assert "no q&a gate and no remediation" in text

    def test_idempotency_is_not_claimed(self):
        assert "idempotency is not claimed" in _lane().lower()

    def test_ceiling_is_enforced_in_the_schema_not_only_requested(self):
        """A prompt asking for <= N does not stop a schema-valid N+1."""
        src = _detect()
        assert "maxItems: ceiling" in src

    def test_ceiling_hit_is_announced_in_the_returned_data(self):
        """Announcement must survive a lane that forgets to mention it."""
        src = _detect()
        assert "results are capped, not complete" in src
        assert re.search(r"notes\.push\([^)]*ceiling", src, re.S)

    def test_notes_is_required_so_truncation_cannot_validate_silently(self):
        src = _detect()
        assert "'ceilingReached', 'notes'" in src

    def test_result_is_described_as_a_sample(self):
        assert "SAMPLE" in _lane() or "sample" in _lane().lower()


class TestAnalysisDepth:
    def test_lane_prompts_when_depth_is_unspecified(self):
        text = _lane()
        assert "analysis depth" in text.lower()
        assert "AskUserQuestion" in text
        assert "defaults: depth=basic" in text

    def test_detect_applies_both_depth_contracts(self):
        src = _detect()
        assert "input.depth" in src
        assert "bounded, sampled read and one assessment pass" in src
        assert "invariant-discovery pass" in src
        assert "verification pass" in src

    def test_detect_returns_the_depth_with_the_verdict(self):
        src = _detect()
        assert "verdict: derived, depth" in src
        assert "return { perSubject: results, totals, ceiling, depth }" in src


class TestScopeCorrection:
    def test_detect_prompt_forbids_returning_a_defect_list(self):
        """Assert on the PROMPT body, not the header comment.

        Both phrases also appear in the file header, so a whole-file substring
        check passes even if the prompt stops saying it.
        """
        prompt = _lane_prompt_body()
        assert "not a code-review tool" in prompt
        assert "defect list" in prompt
        assert "You are NOT reviewing this code" in prompt

    def test_severe_deficiency_carve_out_is_present_and_bounded(self):
        src = _detect()
        assert "severeDeficiency" in src
        assert "FOSSILIZE" in src or "fossilize" in src.lower()

    def test_lane_retires_the_old_remediation_routing(self):
        """The spec once routed code-fix -> make-loud -> document."""
        assert "retired" in _lane().lower()


class TestSubjectContract:
    def test_takes_subjects_and_returns_per_subject(self):
        src = _detect()
        assert "input.subjects" in src
        assert "perSubject" in src

    def test_consumes_the_chain_rather_than_deriving_it(self):
        """Behavioural, not a comment.

        The previous version asserted the presence of a comment saying not to
        recompute, which passes against an implementation that recomputes anyway.
        """
        src = _detect()
        assert "s.ambientClaudeMdPaths" in src
        for forbidden in ("readFileSync", "existsSync", "readdir", "path.resolve"):
            assert forbidden not in src, f"lane derives the chain itself: {forbidden}"

    def test_uncovered_tally_reads_the_input_not_the_agent_result(self):
        """The chain size is an INPUT fact.

        Reading it off the agent result made every subtree count as uncovered,
        because the schema has no such field. Pin the SOURCE, not the name.
        """
        src = _detect()
        built = re.search(r"chainSizeByRoot = new Map\((.*?)\n\)", src, re.S)
        assert built, "chainSizeByRoot is not built from a Map literal"
        assert "subjects.map" in built.group(1)
        assert "results" not in built.group(1)
        assert "ambientChainSize" not in src

    def test_empty_ambient_chain_is_the_finding_not_an_error(self):
        src = _detect()
        assert "not an error and not a skip" in src


class TestConversionContractCarriage:
    """CV-4 and CV-7 must be carried by the SCHEMA, not merely asked for.

    Both criteria were once satisfiable at assessment time and dropped before a
    reader saw them: the candidate schema sets additionalProperties:false and
    declared no classification field, so CV-4's "and the classification is
    reported" had nowhere to land; and CV-7 is fail-severity on the evidence
    floor while `anchors` was merely optional. A criterion the schema cannot
    carry is a criterion a run can satisfy on paper and omit in fact.
    """

    def _candidate_schema_block(self) -> str:
        src = _detect()
        start = src.index("candidates: {")
        return src[start:src.index("notes: { type: 'array'", start)]

    def test_candidate_schema_declares_the_tier_field(self):
        """additionalProperties:false means it must be DECLARED, not smuggled."""
        block = self._candidate_schema_block()
        assert "additionalProperties: false" in block
        assert re.search(r"\btier:\s*\{", block), "no tier property on the candidate schema"

    def test_tier_is_an_enum_of_exactly_the_two_conversion_values(self):
        block = self._candidate_schema_block()
        assert (
            "tier: { type: 'string', enum: ['FINDING-CONVERTIBLE', 'CONTEXT-ONLY'] }"
            in block
        )

    def test_tier_and_anchors_are_required_on_every_candidate(self):
        block = self._candidate_schema_block()
        required = re.search(r"required: \[([^\]]*)\]", block)
        assert required, "candidate schema declares no required list"
        names = {n.strip().strip("'") for n in required.group(1).split(",")}
        assert {"fact", "destination", "why", "tier", "anchors"} <= names

    def test_anchors_requires_at_least_one_citation(self):
        """An empty array satisfies a bare `required` while citing nothing."""
        block = self._candidate_schema_block()
        anchors = re.search(r"anchors: \{[^}]*\}", block)
        assert anchors, "no anchors property on the candidate schema"
        assert "minItems: 1" in anchors.group(0)

    def test_prompt_asks_for_both_in_the_criteria_own_words(self):
        prompt = _lane_prompt_body()
        assert "TIER (CV-4)" in prompt
        assert "FINDING-CONVERTIBLE" in prompt and "CONTEXT-ONLY" in prompt
        assert "EVIDENCE (CV-7)" in prompt
        assert "anchors" in prompt

    def test_reducer_preserves_the_candidate_objects(self):
        """The reducer must not rebuild candidates and drop the new fields."""
        src = _detect()
        assert "const candidates = r.candidates || []" in src
        assert "return { ...r, candidates, verdict: derived, depth, notes }" in src

    def test_totals_report_the_tier_split(self):
        """CV-4 requires the classification to be REPORTED, not just recorded."""
        src = _detect()
        assert "c.tier === 'FINDING-CONVERTIBLE'" in src
        assert "c.tier === 'CONTEXT-ONLY'" in src
        assert "findingConvertible: 0" in src and "contextOnly: 0" in src

    def test_summary_line_surfaces_the_tier_split_and_the_evidence_floor(self):
        src = _detect()
        assert "${tierNote}" in src
        assert "${evidenceNote}" in src
        assert "FINDING-CONVERTIBLE, ${totals.contextOnly} CONTEXT-ONLY" in src
        assert "CV-7 evidence floor breached" in src

    def test_report_template_renders_tier_and_evidence_per_candidate(self):
        """The only point a human reads a candidate."""
        text = _lane()
        assert "[<FINDING-CONVERTIBLE | CONTEXT-ONLY>]" in text
        assert "evidence: <file:line>" in text

    def test_standards_still_require_both(self):
        """If the criteria stop requiring them, the schema pins above are stale."""
        text = STANDARDS.read_text(encoding="utf-8")
        assert "id: candidate-tier-classified" in text
        assert "classified finding-convertible or" in text
        assert "id: evidence-floor" in text
        assert "cites a file and line observed in source" in text


class TestDiscoveryFailureRefusal:
    """Structural enforcement: an unrecognized-extension subtree with zero
    recognized code files must never be dispatched to assessment as though it
    were a clean COVERAGE-ASSESSED pass -- it was never read. See
    discover_coverage.py's unknownExtensions and coverage-lane.md's decision
    rule ("Never emit COVERAGE-ASSESSED when codeFiles is empty and
    unknownExtensions is non-empty").
    """

    def test_detect_never_dispatches_a_discovery_failure_to_the_agent(self):
        src = _detect()
        assert "hasNoCodeFiles(s) && hasUnknownExtensions(s)" in src
        # The refusal branch must precede the agent() call inside the same
        # per-subject thunk, so a discovery failure never reaches assessment.
        thunk = src[src.index("subjects.map((s) => () => {"):src.index("}))")]
        assert thunk.index("discoveryFailure(s)") < thunk.index("agent(lanePrompt")

    def test_discovery_failure_verdict_is_not_one_of_the_two_coverage_verdicts(self):
        src = _detect()
        assert "'DISCOVERY-FAILED'" in src

    def test_discovery_failed_subject_is_passed_through_not_rederived(self):
        """It must skip the candidates.length ? GAPS-FOUND : COVERAGE-ASSESSED
        derivation entirely -- deriving over it would read as COVERAGE-ASSESSED."""
        src = _detect()
        guard = src.index("if (r.verdict === 'DISCOVERY-FAILED') {")
        derive = src.index("candidates.length ? 'GAPS-FOUND' : 'COVERAGE-ASSESSED'")
        assert guard < derive

    def test_discovery_failed_is_tallied_apart_from_both_verdicts(self):
        src = _detect()
        assert "acc.discoveryFailed" in src

    def test_lane_states_the_refusal_rule(self):
        text = _lane().lower()
        assert "never emit `coverage-assessed`" in text
        assert "discovery failure" in text

    def test_lane_step_two_surfaces_unknown_extensions_alongside_exclusions(self):
        text = _lane()
        assert "unknownExtensions" in text
        assert "same standing as the" in text.lower()

    def test_report_template_includes_unknown_extensions(self):
        text = _lane()
        assert "Unknown extensions:" in text


class TestCoverageRegisteredWithCriteria:
    """Registration and its criteria binding are one atomic go-live contract."""

    def test_skill_md_advertises_the_coverage_invocation(self):
        skill_md = (MD_DOMAIN / "SKILL.md").read_text(encoding="utf-8")
        assert "/md-domain coverage" in skill_md

    def test_skill_md_registers_coverage_code_subtree(self):
        skill_md = (MD_DOMAIN / "SKILL.md").read_text(encoding="utf-8")
        assert "coverage_code_subtree" in skill_md

    def test_source_tree_disclaimer_is_absent(self):
        skill_md = (MD_DOMAIN / "SKILL.md").read_text(encoding="utf-8")
        assert "None of the above reads your source tree" not in skill_md

    def test_coverage_standards_exist(self):
        assert STANDARDS.is_file()

    def test_lane_record_binds_coverage_standards(self):
        skill_md = (MD_DOMAIN / "SKILL.md").read_text(encoding="utf-8")
        record = skill_md.split("- id: coverage_code_subtree", 1)[1]
        assert "standards: references/standards/coverage-standards.md" in record
