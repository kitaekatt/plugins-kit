"""Contract pins for the coverage verb's detect lane and procedure.

The coverage verb is a REPORT-ONLY third verb, not an audit lane. Several of its
properties are load-bearing in a way that is invisible at a glance, and each has
a specific way of silently regressing:

  * It must never emit COMPLIANT / NON-COMPLIANT. Those belong to the document
    lanes and answer a different question; conflating them is the exact misread
    the whole capability exists to correct.
  * It must pin opus + high explicitly. A coverage run normally has ONE subject,
    and the audit lane's single-subject shortcut runs inline at whatever model
    the session happens to be on -- so an unpinned lane silently drops tier in
    the common case rather than the rare one.
  * It must have NO remediate lane. Report-only is what keeps a third verb cheap;
    a remediate lane would drag in the sonnet+low pin and the generator, which
    assumes per-file edits and applied/skipped/failed results.
  * It must REFUSE to run without authored criteria rather than improvising. An
    invented predicate reproduces the hazard sweep two adversarial reviews
    rejected -- and it would look like it was working.

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


class TestCriteriaSeam:
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

    def test_lane_marks_step_three_as_a_seam(self):
        assert "SEAM" in _lane()


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


class TestNotRegisteredUntilCriteriaLand:
    """Registration is the go-live switch, so it must not precede the criteria.

    A menu entry for a verb that cannot assess anything is worse than no entry --
    the same reasoning that kept `coverage` off the menu when the vocabulary
    shipped.
    """

    def test_skill_md_does_not_advertise_coverage_yet(self):
        skill_md = (MD_DOMAIN / "SKILL.md").read_text(encoding="utf-8")
        assert "/md-domain coverage" not in skill_md
        assert "coverage_code_subtree" not in skill_md

    def test_skill_md_states_that_nothing_on_the_menu_reads_the_source_tree(self):
        """The disclaimer that must be deleted in the same commit as go-live."""
        skill_md = (MD_DOMAIN / "SKILL.md").read_text(encoding="utf-8")
        assert "None of the above reads your source tree" in skill_md
