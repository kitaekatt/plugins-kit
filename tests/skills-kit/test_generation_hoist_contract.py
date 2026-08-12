"""Contract pins for the propose-verify-write hoist phase in claude-md-generate.js.

A composition may hoist a fact found in a SINGLE child's document -- the
repetition trigger is gone and wording is the whole test -- but it may not WRITE
one. It proposes into `candidateHoists`, a verification step settles each
proposal against exactly the files the proposal named, and only survivors are
applied. Several properties of that shape are load-bearing and each has a
specific way of silently regressing:

  * `candidateHoists` must be REQUIRED, per the schema's own no-optional-
    disclosure convention. An optional disclosure field is one a run can satisfy
    on paper and omit in fact.
  * `claimedOver` must be required AND non-empty. It is the field that bounds
    verification; a candidate that cannot name the files its claim is about is a
    guess, and an empty array satisfies a bare `required` while naming nothing.
  * The three phase dispositions must stay separable from `not-proposed`.
    "Proposed and refuted" and "never proposed" both yield zero hoists and are
    different results; folding them together makes a composition that proposed
    nothing indistinguishable from one with nothing to propose.
  * An `hoist-unverifiable` candidate must be REFUSED, never written by default.
    That is the case the whole ordering exists to handle.
  * The candidate record must live in the PARENT's own result. That is what keeps
    this distinct from the rejected upward-nomination design mechanically rather
    than culturally: no child ever writes one.

Most assertions are text-level because the lane is a Workflow script (top-level
`return`, `agent()` / `parallel()` injected at run time), so it cannot be
imported and executed here. The schema assertions are NOT text-level: the JS
object literals are extracted and exercised against a minimal validator, so
"accepts a well-formed candidate" and "rejects an empty claimedOver" are answered
by the schema rather than by a substring.
"""

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MD_DOMAIN = REPO_ROOT / "plugins" / "skills-kit" / "skills" / "md-domain"
GENERATE = MD_DOMAIN / "workflow" / "claude-md-generate.js"


def _src() -> str:
    return GENERATE.read_text(encoding="utf-8")


def _prompt_body(start: str, end: str) -> str:
    """A single prompt builder, excluding the file header and the other prompts.

    Contract phrases appear in both the header comment and the prompt text;
    asserting over the whole file cannot tell the two apart.
    """
    src = _src()
    a = src.index(start)
    return src[a:src.index(end, a)]


# ---------------------------------------------------------------------------
# Schema extraction. The lane declares its schemas as JS object literals, so
# exercising them means converting one to JSON first. The conversion is narrow on
# purpose -- it handles exactly the subset these literals use, and raises rather
# than guessing, so a schema that grows a construct it cannot read fails loudly
# instead of being silently half-tested.
# ---------------------------------------------------------------------------
def _extract_literal(name: str) -> dict:
    src = _src()
    start = src.index("const " + name + " = {") + len("const " + name + " = ")
    depth, i = 0, start
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    body = src[start:i + 1]

    # Strip line comments. No string in these literals contains "//".
    body = "\n".join(
        line for line in body.splitlines() if not line.strip().startswith("//")
    )
    assert "//" not in body, "unexpected trailing comment in " + name
    assert '"' not in body, "unexpected double-quoted string in " + name
    body = body.replace("'", '"')
    # Bare object keys -> quoted. No colon appears inside a string in these
    # literals, which is what makes a blanket substitution safe here.
    body = re.sub(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*:", r'"\1":', body)
    body = re.sub(r",(\s*[}\]])", r"\1", body)
    return json.loads(body)


class _Invalid(Exception):
    pass


def _validate(schema: dict, value) -> None:
    """Minimal validator over the JSON Schema subset these literals use."""
    t = schema.get("type")
    if t == "object":
        if not isinstance(value, dict):
            raise _Invalid("expected object")
        for key in schema.get("required", []):
            if key not in value:
                raise _Invalid("missing required key: " + key)
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in props:
                    raise _Invalid("additional property: " + key)
        for key, sub in props.items():
            if key in value:
                _validate(sub, value[key])
    elif t == "array":
        if not isinstance(value, list):
            raise _Invalid("expected array")
        if len(value) < schema.get("minItems", 0):
            raise _Invalid("fewer than minItems entries")
        for item in value:
            _validate(schema["items"], item)
    elif t == "string":
        if not isinstance(value, str):
            raise _Invalid("expected string")
        if "enum" in schema and value not in schema["enum"]:
            raise _Invalid("not in enum: " + value)
    elif t == "boolean":
        if not isinstance(value, bool):
            raise _Invalid("expected boolean")


DOC_SCHEMA = _extract_literal("DOC_SCHEMA")
VERIFY_SCHEMA = _extract_literal("VERIFY_SCHEMA")
APPLY_SCHEMA = _extract_literal("APPLY_SCHEMA")

CANDIDATE_SCHEMA = DOC_SCHEMA["properties"]["candidateHoists"]["items"]
DISPOSITION_SCHEMA = VERIFY_SCHEMA["properties"]["dispositions"]["items"]


def _candidate(**overrides) -> dict:
    base = {
        "id": "godot#1",
        "fromClaim": "godot-tests.json#4",
        "fromChildren": ["godot/tests"],
        "wording": "Every scene script registers its autoload before use.",
        "claimedOver": ["godot/tests/test_station.gd"],
        "check": {
            "kind": "mechanical",
            "detail": "grep -l autoload godot/tests/test_station.gd",
            "expected": "every listed file matches",
        },
    }
    base.update(overrides)
    return base


def _document(**overrides) -> dict:
    base = {
        "root": "godot",
        "written": True,
        "path": "godot/CLAUDE.md",
        "sections": ["Autoloads"],
        "droppedCandidates": [],
        "verifications": [],
        "hoists": [],
        "candidateHoists": [],
        "notProposed": [],
        "notes": [],
    }
    base.update(overrides)
    return base


class TestArtifactExists:
    def test_generate_lane_exists(self):
        assert GENERATE.is_file()


class TestCandidateHoistsIsRequiredNotOptional:
    """The schema's own convention: no optional disclosure fields."""

    def test_candidate_hoists_is_in_the_required_list(self):
        assert "candidateHoists" in DOC_SCHEMA["required"]

    def test_not_proposed_is_in_the_required_list(self):
        assert "notProposed" in DOC_SCHEMA["required"]

    def test_a_document_omitting_candidate_hoists_is_rejected(self):
        doc = _document()
        del doc["candidateHoists"]
        with pytest.raises(_Invalid):
            _validate(DOC_SCHEMA, doc)

    def test_a_document_omitting_not_proposed_is_rejected(self):
        doc = _document()
        del doc["notProposed"]
        with pytest.raises(_Invalid):
            _validate(DOC_SCHEMA, doc)


class TestCandidateRecordShape:
    def test_schema_accepts_a_well_formed_candidate(self):
        _validate(DOC_SCHEMA, _document(candidateHoists=[_candidate()]))

    def test_every_candidate_field_is_required(self):
        assert set(CANDIDATE_SCHEMA["required"]) == {
            "id", "fromClaim", "fromChildren", "wording", "claimedOver", "check",
        }

    def test_an_empty_claimed_over_is_rejected(self):
        """The bound, not a formality.

        An empty array satisfies a bare `required` while naming no file at all,
        which is precisely the guess the phase refuses to carry.
        """
        with pytest.raises(_Invalid):
            _validate(DOC_SCHEMA, _document(candidateHoists=[_candidate(claimedOver=[])]))

    def test_a_missing_claimed_over_is_rejected(self):
        c = _candidate()
        del c["claimedOver"]
        with pytest.raises(_Invalid):
            _validate(DOC_SCHEMA, _document(candidateHoists=[c]))

    def test_a_missing_check_is_rejected(self):
        c = _candidate()
        del c["check"]
        with pytest.raises(_Invalid):
            _validate(DOC_SCHEMA, _document(candidateHoists=[c]))

    def test_an_undeclared_candidate_field_is_rejected(self):
        """additionalProperties:false -- a routing field cannot be smuggled in."""
        with pytest.raises(_Invalid):
            _validate(DOC_SCHEMA, _document(
                candidateHoists=[_candidate(destination="godot")]))

    def test_check_kinds_are_exactly_the_two_forms_plus_the_refusal(self):
        kinds = CANDIDATE_SCHEMA["properties"]["check"]["properties"]["kind"]["enum"]
        assert kinds == ["mechanical", "bounded-read", "none"]

    def test_a_check_must_state_its_expected_predicate(self):
        check = CANDIDATE_SCHEMA["properties"]["check"]
        assert set(check["required"]) == {"kind", "detail", "expected"}


class TestSingleChildCandidateIsProposable:
    """The repetition trigger is dropped; one child is enough to propose."""

    def test_one_child_validates(self):
        _validate(DOC_SCHEMA, _document(
            candidateHoists=[_candidate(fromChildren=["godot/tests"])]))

    def test_zero_children_does_not(self):
        with pytest.raises(_Invalid):
            _validate(DOC_SCHEMA, _document(candidateHoists=[_candidate(fromChildren=[])]))

    def test_prompt_no_longer_states_the_repetition_trigger(self):
        prompt = _prompt_body("const lanePrompt", "const verifyPrompt")
        assert "in more than one child moves up" not in prompt
        assert "REPETITION TRIGGERS A HOIST" not in prompt

    def test_prompt_states_the_one_test_form(self):
        prompt = _prompt_body("const lanePrompt", "const verifyPrompt")
        assert "WORDING LICENSES A HOIST, AND IT IS THE ONLY TEST" in prompt
        assert "There is no separate repetition " in prompt

    def test_the_wording_test_is_not_weakened(self):
        """It is now the ONLY test, so the counter-example and the escape clause
        are more load-bearing than before, not less."""
        prompt = _prompt_body("const lanePrompt", "const verifyPrompt")
        assert "2 of 20 children" in prompt
        assert "18 " in prompt
        assert "no such wording exists short of a list of exceptions" in prompt
        assert "DOES NOT HOIST" in prompt

    def test_hoists_comment_forbids_restoring_the_trigger_from_the_plural_name(self):
        src = _src()
        assert "fromChildren MAY name a SINGLE child" in src


class TestTheThreeDispositionsRoundTrip:
    def test_enum_is_exactly_the_three_phase_dispositions(self):
        assert DISPOSITION_SCHEMA["properties"]["disposition"]["enum"] == [
            "hoist-verified", "hoist-rejected", "hoist-unverifiable",
        ]

    @pytest.mark.parametrize(
        "disposition", ["hoist-verified", "hoist-rejected", "hoist-unverifiable"])
    def test_each_disposition_validates(self, disposition):
        _validate(VERIFY_SCHEMA, {
            "root": "godot",
            "dispositions": [{
                "id": "godot#1",
                "disposition": disposition,
                "reason": "checked against the named files",
                "filesRead": ["godot/tests/test_station.gd"],
            }],
            "notes": [],
        })

    def test_a_fourth_disposition_is_rejected(self):
        with pytest.raises(_Invalid):
            _validate(VERIFY_SCHEMA, {
                "root": "godot",
                "dispositions": [{
                    "id": "godot#1",
                    "disposition": "hoist-deferred",
                    "reason": "r",
                    "filesRead": [],
                }],
                "notes": [],
            })

    def test_not_proposed_is_not_a_phase_disposition(self):
        """It is the parent's OWN judgment, with no phase involved.

        Keeping it off VERIFY_SCHEMA is what makes "proposed and refuted"
        separable from "never proposed" -- both produce zero hoists.
        """
        assert "hoist-unproposed" not in json.dumps(VERIFY_SCHEMA)
        assert "not-proposed" not in json.dumps(VERIFY_SCHEMA)
        assert "notProposed" in DOC_SCHEMA["properties"]

    def test_not_proposed_entries_carry_a_reason(self):
        assert set(DOC_SCHEMA["properties"]["notProposed"]["items"]["required"]) == {
            "fromClaim", "reason",
        }

    def test_every_disposition_records_what_it_read(self):
        """filesRead is the read-bound evidence AND the provenance edge."""
        assert "filesRead" in DISPOSITION_SCHEMA["required"]


class TestUnverifiableNeverReachesTheDocument:
    def test_only_verified_candidates_are_offered_to_the_apply_step(self):
        src = _src()
        assert "d.id === c.id && d.disposition === 'hoist-verified'" in src

    def test_hoists_is_derived_from_what_the_apply_step_actually_placed(self):
        """Not from the agent restating it, and not from the verified set alone.

        A verified candidate the apply step left out is not a hoist; deriving
        keeps every hoists entry corresponding to a sentence in the document.
        """
        src = _src()
        assert "const appliedIds = new Set(" in src
        assert ".filter((c) => appliedIds.has(c.id))" in src

    def test_composition_returns_no_hoists_and_the_lane_refuses_one(self):
        src = _src()
        assert "composed.filter((r) => (r.hoists || []).length)" in src
        speculative = src[src.index("const speculative"):src.index("// ---- Step 2")]
        assert "throw new Error" in speculative

    def test_prompt_tells_the_composition_to_return_hoists_empty(self):
        prompt = _prompt_body("const lanePrompt", "const verifyPrompt")
        assert "YOU DO NOT MAKE A HOIST HERE. YOU PROPOSE ONE." in prompt
        assert "the hoists field must come back EMPTY" in prompt

    def test_check_kind_none_is_documented_as_a_refusal_not_an_omission(self):
        prompt = _prompt_body("const lanePrompt", "const verifyPrompt")
        assert "kind none: no admissible check exists" in prompt
        assert "REFUSED" in prompt

    def test_verify_prompt_refuses_to_invent_a_check_for_kind_none(self):
        prompt = _prompt_body("const verifyPrompt", "const applyPrompt")
        assert "hoist-unverifiable" in prompt
        assert "Do not go looking for a check the proposer could not " in prompt


class TestTheReadBound:
    def test_compose_prompt_draws_the_reevaluate_versus_verify_distinction(self):
        prompt = _prompt_body("const lanePrompt", "const verifyPrompt")
        assert "RE-EVALUATING a directory" in prompt
        assert "VERIFYING one claim" in prompt
        assert "forbidden at composition" in prompt

    def test_compose_prompt_forbids_opening_a_child_source_file(self):
        prompt = _prompt_body("const lanePrompt", "const verifyPrompt")
        assert "Do NOT open a child source file to decide what the " in prompt

    def test_verify_prompt_names_mis_scoping_as_a_rejection_not_a_widening(self):
        prompt = _prompt_body("const verifyPrompt", "const applyPrompt")
        assert "MIS-SCOPED" in prompt
        assert "not a licence to widen the " in prompt

    def test_the_bound_is_enforced_by_the_lane_not_only_requested(self):
        """Prompt text asking an agent not to widen its read is exactly the kind
        of instruction a plausible hypothesis talks it out of, and the widening
        leaves no trace in the document."""
        src = _src()
        assert "const claimed = new Set((c.claimedOver || []).map(norm))" in src
        assert "d.disposition = 'hoist-rejected'" in src

    def test_a_check_that_escaped_its_claim_cannot_stay_verified(self):
        src = _src()
        block = src[src.index("for (const d of v.dispositions) {"):src.index("dispositionsByRoot.set")]
        assert "escaped.length && d.disposition === 'hoist-verified'" in block
        assert "hoist-rejected" in block


class TestNotUpwardNomination:
    """A later reader will suspect this is the rejected design returning."""

    def test_the_distinction_is_stated_where_the_field_is_declared(self):
        src = _src()
        assert "THIS IS NOT UPWARD NOMINATION" in src
        assert "no child ever writes a candidate" in src

    def test_the_placement_is_named_as_what_makes_it_mechanical(self):
        src = _src()
        assert "MECHANICAL rather" in src
        assert "Moving this array anywhere a child can write it" in src

    def test_no_destination_or_routing_field_exists_on_a_candidate(self):
        props = set(CANDIDATE_SCHEMA["properties"])
        assert "destination" not in props
        assert "routeTo" not in props


class TestWaveIsComposeVerifyApply:
    def test_the_barrier_checks_resolution_not_merely_processing(self):
        src = _src()
        assert "const unresolved = descendantsOf(root).filter((d) => !resolvedRoots.has(d))" in src
        assert "processedRoots" not in src

    def test_the_barrier_names_not_yet_resolved_as_the_refusal(self):
        src = _src()
        guard = src[src.index("const unresolved ="):src.index("log('Wave ' + w")]
        assert "throw new Error" in guard
        assert "not-yet-resolved" in guard

    def test_all_three_steps_are_inside_the_wave_loop(self):
        src = _src()
        loop = src[src.index("for (let w = 0; w < waves.length; w++)"):src.index("// Totals.")]
        assert loop.index("agent(lanePrompt") < loop.index("agent(verifyPrompt")
        assert loop.index("agent(verifyPrompt") < loop.index("agent(applyPrompt")

    def test_verification_pins_opus_high_like_the_composition(self):
        src = _src()
        verify = src[src.index("agent(verifyPrompt"):src.index("for (const v of verdicts")]
        assert "model: 'opus'" in verify
        assert "effort: 'high'" in verify

    def test_a_root_is_resolved_only_after_the_apply_step(self):
        src = _src()
        assert src.index("// ---- Step 3: APPLY") < src.index("resolvedRoots.add(r.root)")

    def test_proposed_without_dispositions_is_a_failed_run(self):
        """Not a wave that hoisted nothing -- the two are different results."""
        src = _src()
        assert "the verification step returned no dispositions" in src
        assert "That ' +\n        'is a failed run" in src

    def test_every_proposed_candidate_must_carry_a_disposition(self):
        src = _src()
        assert "has candidate(s) with no disposition" in src

    def test_dispositions_for_uninvited_candidate_ids_are_refused(self):
        src = _src()
        assert "that were never proposed" in src


class TestWaveRecord:
    def test_a_record_is_emitted_per_wave(self):
        src = _src()
        assert "waveRecords.push({" in src

    def test_the_record_carries_the_counts_scoring_needs(self):
        src = _src()
        record = src[src.index("waveRecords.push({"):src.index("log('Wave ' + w + ' resolved")]
        for field in ("phaseRan", "proposed", "verified", "rejected",
                      "unverifiable", "notProposed"):
            assert field in record, "wave record omits " + field

    def test_the_record_is_returned_not_only_logged(self):
        src = _src()
        assert "return { perSubject, waves, waveRecords, totals }" in src

    def test_the_absence_of_a_record_is_documented_as_a_failure(self):
        src = _src()
        assert "its ABSENCE is a failure rather than a silent pass" in src


class TestTotalsSplit:
    def test_totals_seed_carries_the_four_new_counters(self):
        src = _src()
        seed = src[src.index("unverified: 0, proposed: 0"):]
        for field in ("proposed", "verified", "rejected", "unverifiable", "notProposed"):
            assert field + ":" in seed[:200], "totals seed omits " + field

    def test_hoists_counts_verified_hoists_only(self):
        src = _src()
        assert "VERIFIED hoists only, because hoists now holds nothing else" in src

    def test_the_proposed_set_is_counted_beside_hoists_not_inside_it(self):
        src = _src()
        assert "acc.proposed += (r.candidateHoists || []).length" in src
        assert "acc.notProposed += (r.notProposed || []).length" in src

    def test_a_zero_refusal_rate_is_surfaced_as_a_rubber_stamp(self):
        src = _src()
        assert "rubber stamp until a sample is checked by hand" in src

    def test_proposing_nothing_without_recording_an_absence_is_surfaced(self):
        src = _src()
        assert "no child claim was recorded as considered" in src

    def test_a_verified_candidate_that_never_landed_is_surfaced(self):
        src = _src()
        assert "totals.verified > totals.hoists" in src

    def test_escalated_counter_is_retained(self):
        """Reviewed and left alone: its subject is a coverage candidate naming an
        ancestor destination, which is unrelated to the hoist phase, and the
        corpus still holds reports of that shape."""
        src = _src()
        assert "acc.escalated += (r.droppedCandidates || []).filter((d) => d.escalateToAncestor).length" in src
        assert "named an ancestor destination and were NOT " in src


class TestApplyStep:
    def test_apply_schema_reports_where_each_sentence_landed(self):
        assert set(APPLY_SCHEMA["properties"]["applied"]["items"]["required"]) == {
            "id", "section",
        }

    def test_apply_prompt_forbids_rewording_a_settled_sentence(self):
        prompt = _prompt_body("const applyPrompt", "// ------")
        assert "The wording is SETTLED" in prompt
        assert "Rewording it here" in prompt

    def test_apply_prompt_does_not_touch_the_child_documents(self):
        prompt = _prompt_body("const applyPrompt", "// ------")
        assert "do NOT touch the " in prompt

    def test_apply_is_an_addition_not_a_filter(self):
        src = _src()
        assert "an ADDITION to a file that was written without any hoist" in src


class TestDriftTestDoesNotOwnThisFile:
    """Verified rather than assumed.

    gen_workflow_js.py generates the four remediate lanes and pins shared chunks
    in the detect/classify lanes; claude-md-generate.js is in neither set, so
    nothing in test_workflow_js_drift.py pinned the old repetition trigger. The
    one thing that file DOES apply to every workflow script is the tagged-
    template check, which is a parse guard rather than a behaviour pin.
    """

    def _gen(self):
        import importlib.util
        path = REPO_ROOT / "plugins" / "skills-kit" / "scripts" / "gen_workflow_js.py"
        spec = importlib.util.spec_from_file_location("gen_workflow_js_probe", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_not_a_generated_remediate_lane(self):
        assert GENERATE not in set(self._gen().remediate_targets().values())

    def test_not_a_shared_chunk_target(self):
        assert GENERATE not in set(self._gen().SHARED_CHUNK_TARGETS)


class TestNoAccidentalTaggedTemplate:
    """A markdown-style backtick-quoted bare word in prompt prose ends the
    template literal early; node accepts the result and the Workflow parser
    rejects the whole script. Same guard as test_workflow_js_drift.py, asserted
    here so a targeted run of this file catches it too."""

    TAGGED = re.compile(r"`[A-Za-z_$][A-Za-z0-9_$.]*`")

    def test_no_tagged_template_outside_comments(self):
        offenders = []
        in_block = False
        for n, line in enumerate(_src().splitlines(), start=1):
            stripped = line.lstrip()
            if in_block:
                if "*/" in line:
                    in_block = False
                continue
            if stripped.startswith("//"):
                continue
            if stripped.startswith("/*"):
                in_block = "*/" not in line
                continue
            if self.TAGGED.search(line):
                offenders.append(str(n) + ": " + stripped)
        assert offenders == [], "\n".join(offenders)


class TestAsciiOnly:
    def test_the_lane_is_ascii(self):
        raw = GENERATE.read_bytes()
        bad = [i for i, b in enumerate(raw) if b > 126]
        assert bad == [], "non-ASCII byte(s) at offset(s) " + str(bad[:5])


class TestCompositionOnlySubjectsAreNotInputless:
    """A directory with no direct code has no coverage report BY DESIGN.

    The COVERAGE subject rule (a directory holding code files directly) and the
    COMPOSITION subject rule (a directory that, or beneath which, code lives)
    describe different sets, and the second strictly contains the first. A
    code-free directory such as a port root is therefore never assessed, never
    has a report, and is composed entirely from its children's finished
    documents.

    The inputless guard predates that distinction and tests only for a missing
    report. Left unamended it fails twice over on exactly the directories the
    enumeration rule exists to reach: it refuses a run made only of them, and it
    logs that they are "written from code alone" -- the precise inverse of the
    truth, since code alone is what they do not have.
    """

    def test_composition_only_subjects_are_excluded_from_inputless(self):
        assert "!s.compositionOnly" in _src()

    def test_a_composition_only_run_is_not_refused(self):
        """The refusal fires on `inputless.length === subjects.length`, so the
        exclusion above is what keeps an all-composition-only run alive."""
        src = _src()
        guard = src.index("const inputless = subjects.filter")
        throw = src.index("inputless.length === subjects.length")
        assert guard < throw
        assert "!s.compositionOnly" in src[guard:throw]

    def test_composition_only_subjects_get_their_own_note(self):
        src = _src()
        assert "const compositionOnly = subjects.filter" in src
        assert "composed from their children" in src

    def test_the_two_notes_are_not_collapsed(self):
        """They state opposite things -- no coverage input versus no direct
        code -- and a reader who sees the wrong one draws the wrong conclusion
        about whether the directory was assessed."""
        src = _src()
        assert "written from code alone" in src
        assert "composed from their children" in src
        assert src.index("written from code alone") != src.index("composed from their children")
