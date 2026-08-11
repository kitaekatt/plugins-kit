"""Contract pins for the hierarchy verb -- registry, criteria, discovery, and the
structural refusal in the detect lane.

Hierarchy resolves PLACEMENT across a whole CLAUDE.md tree: it takes persisted
coverage reports plus the tree's existing documents and selects exactly one home
per fact. It is REPORT-ONLY and it is not an audit lane.

The property this suite exists to protect is the refusal. A lane that returns a
clean verdict over inputs it did not actually have is the failure the whole
design is a response to, and prose forbidding it is not enforcement -- an agent
that never read the lane doc must still be unable to produce that result. So the
affirmative verdicts are computed from an inventory, the inventory is built by a
script the caller does not supply, and the incomplete-input cases are decided
BEFORE any agent dispatch.

The detect-lane assertions are text-level because the lane is a Workflow script
(top-level `return`, `agent()` / `parallel()` injected at run time), so it cannot
be imported and executed here. The discovery script has no such constraint and is
exercised for real.
"""

import importlib.util
import json
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
MD_DOMAIN = REPO_ROOT / "plugins" / "skills-kit" / "skills" / "md-domain"
DETECT = MD_DOMAIN / "workflow" / "hierarchy-detect.js"
LANE = MD_DOMAIN / "references" / "lanes" / "hierarchy-lane.md"
DISCOVER = MD_DOMAIN / "scripts" / "discover_hierarchy.py"
STANDARDS = MD_DOMAIN / "references" / "standards" / "hierarchy-standards.md"
FRAMEWORK_YAML = MD_DOMAIN / "references" / "audit-framework.yaml"

_spec = importlib.util.spec_from_file_location("hier_discover", DISCOVER)
hier = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hier)


def _detect() -> str:
    return DETECT.read_text(encoding="utf-8")


def _lane() -> str:
    return LANE.read_text(encoding="utf-8")


def _lane_records() -> list:
    text = (MD_DOMAIN / "SKILL.md").read_text(encoding="utf-8")
    for block in re.findall(r"```yaml\n(.*?)\n```", text, re.S):
        if block.lstrip().startswith("lanes:"):
            return yaml.safe_load(block)["lanes"]["records"]
    raise AssertionError("md-domain/SKILL.md has no fenced `lanes:` YAML block")


def _hierarchy_record() -> dict:
    for record in _lane_records():
        if record["id"] == "hierarchy_claude_md_tree":
            return record
    raise AssertionError("no hierarchy_claude_md_tree lane record")


def _write(path: Path, text: str = "x\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _report(path: Path, root: Path, candidates: list) -> None:
    _write(path, json.dumps({"root": str(root), "candidates": candidates}))


def _candidate(fact: str, destination: str, **extra) -> dict:
    return {"fact": fact, "destination": destination, "anchors": ["a.py:1"], **extra}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestArtifactsExist:
    def test_detect_lane_exists(self):
        assert DETECT.is_file()

    def test_procedure_exists(self):
        assert LANE.is_file()

    def test_discover_script_exists(self):
        assert DISCOVER.is_file()

    def test_standards_exist(self):
        assert STANDARDS.is_file()

    def test_no_remediate_lane(self):
        """Report-only: a remediate lane would half-execute an ordered plan."""
        assert not (MD_DOMAIN / "workflow" / "hierarchy-remediate.js").exists()

    def test_generator_does_not_own_a_hierarchy_lane(self):
        """gen_workflow_js.py assumes per-file edits + applied/skipped/failed."""
        gen = REPO_ROOT / "plugins" / "skills-kit" / "scripts" / "gen_workflow_js.py"
        assert "hierarchy" not in gen.read_text(encoding="utf-8").lower()


class TestLaneRecord:
    def test_record_declares_its_subject_axis_not_an_artifact(self):
        record = _hierarchy_record()
        assert record["verb"] == "hierarchy"
        assert record["subject"] == "claude_md_tree"
        assert "artifact" not in record

    def test_required_fields_are_present(self):
        record = _hierarchy_record()
        phrasings = record.get("invocation_phrasings")
        assert isinstance(phrasings, list) and len(phrasings) >= 3
        assert all(isinstance(p, str) and p.strip() for p in phrasings)
        assert isinstance(record.get("change_driver"), str)
        assert record["change_driver"].strip()

    def test_record_is_report_only(self):
        record = _hierarchy_record()
        assert record.get("report_only") is True
        assert "workflow_remediate" not in record

    def test_verdicts_are_the_two_affirmative_ones_only(self):
        """INPUTS-INCOMPLETE is NOT a verdict, so it must not be declared as one."""
        record = _hierarchy_record()
        assert record["verdicts"] == ["CHAIN-COHERENT", "RESOLUTION-PROPOSED"]
        assert "INPUTS-INCOMPLETE" not in record["verdicts"]

    def test_bound_paths_resolve(self):
        record = _hierarchy_record()
        for key in ("standards", "procedure", "discover_script", "workflow_detect"):
            assert (MD_DOMAIN / record[key]).exists(), f"{key} -> {record[key]}"

    def test_dispatch_table_row_exists(self):
        text = (MD_DOMAIN / "SKILL.md").read_text(encoding="utf-8")
        section = text.split("## Dispatch table", 1)[1].split("### Lane records", 1)[0]
        assert "| hierarchy (claude_md_tree) | `hierarchy_claude_md_tree` |" in section


class TestCompositionRegistered:
    def test_claude_md_tree_is_a_registered_composition(self):
        registry = yaml.safe_load(FRAMEWORK_YAML.read_text(encoding="utf-8"))
        ids = [c["id"] for c in registry["compositions"]]
        assert "claude_md_tree" in ids
        assert len(ids) == len(set(ids))

    def test_composition_declares_the_candidate_reports_constituent(self):
        """The one input no other composition has: another lane's persisted output."""
        registry = yaml.safe_load(FRAMEWORK_YAML.read_text(encoding="utf-8"))
        entry = next(c for c in registry["compositions"] if c["id"] == "claude_md_tree")
        kinds = [c.get("kind") for c in entry["contains"]["optional"]]
        assert "candidate_reports" in kinds
        assert "claude_md" in kinds

    def test_standards_applies_to_the_composition(self):
        text = STANDARDS.read_text(encoding="utf-8")
        assert "applies_to: claude_md_tree" in text


# ---------------------------------------------------------------------------
# Criteria
# ---------------------------------------------------------------------------


class TestStandardsCriteria:
    EXPECTED = {
        "one-home-per-fact": "fail",
        "shallowest-true-depth": "judgment",
        "precedent-outranks-hoisting": "judgment",
        "input-inventory-complete": "fail",
        "disposition-re-judged": "judgment",
        "merge-preserves-precision": "judgment",
        "unplaceable-declared": "fail",
    }

    def _criteria(self) -> dict:
        text = STANDARDS.read_text(encoding="utf-8")
        block = re.search(r"```yaml\n(standards_set:.*?)\n```", text, re.S)
        assert block, "hierarchy-standards.md has no standards_set block"
        data = yaml.safe_load(block.group(1))["standards_set"]
        return {c["id"]: c for c in data["criteria"]}

    def test_every_criterion_is_present_with_its_severity(self):
        criteria = self._criteria()
        assert set(criteria) == set(self.EXPECTED)
        for cid, severity in self.EXPECTED.items():
            assert criteria[cid]["severity"] == severity, cid

    def test_every_criterion_carries_a_statement_and_keywords(self):
        for cid, criterion in self._criteria().items():
            assert criterion["statement"].strip(), cid
            assert len(criterion.get("keywords") or []) >= 3, cid

    def test_the_two_mechanical_refusals_are_fail_severity(self):
        """Both are enforced in code; a judgment severity would misdescribe them."""
        criteria = self._criteria()
        assert criteria["input-inventory-complete"]["enforcement"] == "mechanical"
        assert criteria["unplaceable-declared"]["enforcement"] == "mechanical"

    def test_short_ids_are_mapped_to_criterion_ids(self):
        """The lane and the design speak HR-n; the criteria speak slugs."""
        text = STANDARDS.read_text(encoding="utf-8")
        for n, cid in enumerate(self.EXPECTED, start=1):
            assert f"| `{cid}` | HR-{n} |" in text


# ---------------------------------------------------------------------------
# Discovery -- the inventory the refusal is computed from
# ---------------------------------------------------------------------------


class TestLeafEnumeration:
    def test_a_directory_with_code_is_a_leaf(self, tmp_path):
        _write(tmp_path / "src" / "a.py")
        _write(tmp_path / "src" / "ui" / "b.py")
        subject = hier.build_subject(tmp_path / "src", None)
        leaves = {Path(p).name for p in subject["leaves"]}
        assert leaves == {"src", "ui"}

    def test_a_directory_with_no_code_is_not_a_leaf(self, tmp_path):
        _write(tmp_path / "src" / "a.py")
        _write(tmp_path / "src" / "docs" / "notes.md")
        subject = hier.build_subject(tmp_path / "src", None)
        assert {Path(p).name for p in subject["leaves"]} == {"src"}

    def test_documents_are_collected_even_where_there_is_no_code(self, tmp_path):
        _write(tmp_path / "src" / "a.py")
        _write(tmp_path / "src" / "CLAUDE.md", "# root\n")
        _write(tmp_path / "src" / "docs" / "CLAUDE.md", "# docs\n")
        subject = hier.build_subject(tmp_path / "src", None)
        assert len(subject["claudeMdPaths"]) == 2

    def test_structural_exclusions_are_shared_with_coverage(self, tmp_path):
        _write(tmp_path / "src" / "a.py")
        _write(tmp_path / "src" / "node_modules" / "dep.js")
        subject = hier.build_subject(tmp_path / "src", None)
        assert {Path(p).name for p in subject["leaves"]} == {"src"}
        assert any(r["reason"] == "vendored" for r in subject["skipped"])


class TestInputInventory:
    def _tree(self, tmp_path):
        _write(tmp_path / "src" / "a.py")
        _write(tmp_path / "src" / "ui" / "b.py")
        _write(tmp_path / "src" / "net" / "c.py")
        return tmp_path / "src"

    def test_a_leaf_with_candidates_is_status_report(self, tmp_path):
        root = self._tree(tmp_path)
        _report(tmp_path / "reports" / "ui.json", root / "ui",
                [_candidate("f", str(root / "CLAUDE.md"))])
        subject = hier.build_subject(root, tmp_path / "reports")
        rows = {Path(r["leaf"]).name: r["status"] for r in subject["inventory"]}
        assert rows["ui"] == "report"

    def test_an_empty_report_is_assessed_null_not_missing(self, tmp_path):
        """'assessed, nothing found' and 'never assessed' must not look alike."""
        root = self._tree(tmp_path)
        _report(tmp_path / "reports" / "ui.json", root / "ui", [])
        subject = hier.build_subject(root, tmp_path / "reports")
        rows = {Path(r["leaf"]).name: r["status"] for r in subject["inventory"]}
        assert rows["ui"] == "assessed-null"

    def test_a_leaf_with_its_own_document_and_no_report_is_written_doc(self, tmp_path):
        root = self._tree(tmp_path)
        _write(root / "ui" / "CLAUDE.md", "# ui\n")
        subject = hier.build_subject(root, None)
        rows = {Path(r["leaf"]).name: r["status"] for r in subject["inventory"]}
        assert rows["ui"] == "written-doc"

    def test_a_leaf_with_nothing_is_MISSING(self, tmp_path):
        """The row that makes both affirmative verdicts unemittable."""
        root = self._tree(tmp_path)
        _report(tmp_path / "reports" / "ui.json", root / "ui",
                [_candidate("f", str(root / "CLAUDE.md"))])
        subject = hier.build_subject(root, tmp_path / "reports")
        rows = {Path(r["leaf"]).name: r["status"] for r in subject["inventory"]}
        assert rows["net"] == "MISSING"
        assert rows["src"] == "MISSING"

    def test_every_enumerated_leaf_gets_exactly_one_row(self, tmp_path):
        root = self._tree(tmp_path)
        subject = hier.build_subject(root, None)
        assert len(subject["inventory"]) == len(subject["leaves"])
        assert len({r["leaf"] for r in subject["inventory"]}) == len(subject["leaves"])

    def test_a_report_matching_no_leaf_is_surfaced_not_dropped(self, tmp_path):
        """Reports and tree disagreeing about what exists is an inventory failure."""
        root = self._tree(tmp_path)
        _report(tmp_path / "reports" / "ghost.json", root / "nowhere",
                [_candidate("f", str(root / "CLAUDE.md"))])
        subject = hier.build_subject(root, tmp_path / "reports")
        assert len(subject["unmatchedReports"]) == 1
        assert subject["unmatchedReports"][0]["source"] == "ghost"


class TestReportLoading:
    def test_candidates_get_stable_ids_so_dropping_one_is_detectable(self, tmp_path):
        _write(tmp_path / "src" / "a.py")
        root = tmp_path / "src"
        _report(tmp_path / "reports" / "src.json", root,
                [_candidate("one", "X"), _candidate("two", "X")])
        subject = hier.build_subject(root, tmp_path / "reports")
        ids = [c["_id"] for c in subject["reports"][0]["candidates"]]
        assert ids == ["src#0", "src#1"]
        assert len(set(ids)) == 2

    def test_two_reports_of_the_same_fact_stay_distinguishable(self, tmp_path):
        """Duplicate collapse is only checkable while the duplicates have identities.

        Sibling blindness means the same fact arrives once per sibling, correctly.
        Both copies must survive discovery as separate, identified inputs so the
        resolution's collapse can be verified against them rather than asserted.
        """
        _write(tmp_path / "src" / "ui" / "a.py")
        _write(tmp_path / "src" / "net" / "b.py")
        root = tmp_path / "src"
        _report(tmp_path / "reports" / "ui.json", root / "ui", [_candidate("shared", "D")])
        _report(tmp_path / "reports" / "net.json", root / "net", [_candidate("shared", "D")])
        subject = hier.build_subject(root, tmp_path / "reports")
        ids = sorted(
            c["_id"] for r in subject["reports"] for c in r["candidates"]
        )
        assert ids == ["net#0", "ui#0"]
        assert subject["candidateTotal"] == 2

    def test_carriage_fields_survive_into_the_subject(self, tmp_path):
        _write(tmp_path / "src" / "a.py")
        root = tmp_path / "src"
        _report(
            tmp_path / "reports" / "src.json", root,
            [_candidate("f", "D", scope="PROMOTE -> src", sibling_overlap="src/ui/CLAUDE.md")],
        )
        subject = hier.build_subject(root, tmp_path / "reports")
        candidate = subject["reports"][0]["candidates"][0]
        assert candidate["scope"] == "PROMOTE -> src"
        assert candidate["sibling_overlap"] == "src/ui/CLAUDE.md"

    def test_the_perSubject_shape_is_accepted(self, tmp_path):
        _write(tmp_path / "src" / "a.py")
        root = tmp_path / "src"
        _write(
            tmp_path / "reports" / "run.json",
            json.dumps({"perSubject": [{"root": str(root), "candidates": [_candidate("f", "D")]}]}),
        )
        subject = hier.build_subject(root, tmp_path / "reports")
        assert subject["candidateTotal"] == 1

    def test_an_unreadable_report_is_noted_not_swallowed(self, tmp_path):
        _write(tmp_path / "src" / "a.py")
        _write(tmp_path / "reports" / "broken.json", "{not json")
        subject = hier.build_subject(tmp_path / "src", tmp_path / "reports")
        assert any("unreadable report" in n for n in subject["notes"])

    def test_a_report_naming_no_root_is_noted(self, tmp_path):
        _write(tmp_path / "src" / "a.py")
        _write(tmp_path / "reports" / "r.json", json.dumps({"candidates": []}))
        subject = hier.build_subject(tmp_path / "src", tmp_path / "reports")
        assert any("no recognizable coverage subject" in n for n in subject["notes"])

    def test_a_missing_reports_directory_is_noted(self, tmp_path):
        _write(tmp_path / "src" / "a.py")
        subject = hier.build_subject(tmp_path / "src", tmp_path / "nope")
        assert any("does not exist" in n for n in subject["notes"])


# ---------------------------------------------------------------------------
# The structural refusal in the detect lane
# ---------------------------------------------------------------------------


class TestCriteriaWiring:
    def test_detect_refuses_without_criteria(self):
        src = _detect()
        guard = re.search(r"if \(typeof criteriaPath !== 'string'.*?\n\}", src, re.S)
        assert guard, "no criteriaPath type guard found"
        assert "throw new Error" in guard.group(0)

    def test_guard_rejects_truthy_non_paths(self):
        src = _detect()
        assert "typeof criteriaPath !== 'string'" in src
        assert "criteriaPath.trim() === ''" in src

    def test_guard_precedes_any_agent_dispatch(self):
        src = _detect()
        assert src.index("throw new Error") < src.index("await agent(")

    def test_lane_binds_the_criteria_by_absolute_path(self):
        text = _lane()
        assert "### Step 3 -- Resolve" in text
        assert "references/standards/hierarchy-standards.md" in text
        assert "ABSOLUTE path" in text
        assert "refs.criteria" in text


class TestIncompleteInputRefusal:
    """The refusal is decided in code, before assessment -- not by prose."""

    def _preflight_block(self) -> str:
        src = _detect()
        return src[src.index("const missingLeaves"):src.index("const RESOLUTION_SCHEMA")]

    def test_missing_leaves_are_a_preflight_blocker(self):
        block = self._preflight_block()
        assert "row.status === 'MISSING'" in block
        assert "preflightBlockers.push" in block

    def test_unmatched_reports_are_a_preflight_blocker(self):
        assert "unmatchedReports.length" in self._preflight_block()

    def test_zero_input_is_a_preflight_blocker(self):
        """A tree with no documents and no reports has nothing to resolve."""
        block = self._preflight_block()
        assert "reports.length === 0 && documents.length === 0" in block

    def test_zero_leaves_is_a_preflight_blocker(self):
        assert "inventory.length === 0" in self._preflight_block()

    def test_the_refusal_returns_before_any_agent_dispatch(self):
        """A refusal after fan-out would have already spent the tokens -- and
        worse, would let an agent's own verdict reach the reducer."""
        src = _detect()
        refusal = src.index("if (preflightBlockers.length) {")
        dispatch = src.index("await agent(")
        assert refusal < dispatch
        early_return = src.index("return { result: incomplete(preflightBlockers)")
        assert refusal < early_return < dispatch

    def test_inputs_incomplete_is_not_one_of_the_two_verdicts(self):
        src = _detect()
        assert "'INPUTS-INCOMPLETE'" in src
        assert "'CHAIN-COHERENT'" in src
        assert "'RESOLUTION-PROPOSED'" in src

    def test_inputs_incomplete_is_tallied_apart_from_both_verdicts(self):
        """Folding it into either count is exactly the fake pass being refused."""
        src = _detect()
        assert "inputsIncomplete: verdict === 'INPUTS-INCOMPLETE' ? 1 : 0" in src
        assert "chainCoherent: verdict === 'CHAIN-COHERENT' ? 1 : 0" in src
        assert "resolutionProposed: verdict === 'RESOLUTION-PROPOSED' ? 1 : 0" in src

    def test_the_refusal_carries_the_inventory(self):
        """The evidence has to reach the reader, not just the decision."""
        src = _detect()
        incomplete = src[src.index("const incomplete ="):src.index("phase('Hierarchy')")]
        assert "inventory," in incomplete
        assert "unmatchedReports," in incomplete

    def test_never_emits_document_compliance_verdicts(self):
        src = _detect()
        for forbidden in ("'COMPLIANT'", "'NON-COMPLIANT'", "'DIFF-CLEAN'", "'NOT-AUDITED'"):
            assert forbidden not in src

    def test_lane_states_the_refusal_conditions(self):
        text = _lane().lower()
        assert "the verdict is computed" in text
        assert "absence of a report is absence of evidence" in text or (
            "do not treat a missing report as an empty candidate set" in text
        )


class TestVerdictIsComputed:
    def test_verdict_is_derived_from_the_blockers_and_the_plan(self):
        src = _detect()
        assert "const verdict = postBlockers.length" in src
        assert "planIsEmpty ? 'CHAIN-COHERENT' : 'RESOLUTION-PROPOSED'" in src

    def test_the_result_schema_declares_no_verdict_field(self):
        """A verdict the agent could return is a verdict nobody computed."""
        src = _detect()
        schema = src[src.index("const RESOLUTION_SCHEMA"):src.index("// The prompt.")]
        assert "verdict" not in schema

    def test_prompt_forbids_returning_a_verdict(self):
        src = _detect()
        assert "DO NOT emit a verdict" in src

    def test_unread_documents_bar_the_affirmative_verdict(self):
        src = _detect()
        assert "status === 'UNEXTRACTED'" in src
        assert "unlistedDocuments" in src
        assert "postBlockers.push" in src

    def test_unaccounted_inputs_bar_the_affirmative_verdict(self):
        """A silently dropped candidate must not read as a resolved tree."""
        src = _detect()
        assert "unaccountedCandidates" in src
        block = src[src.index("const postBlockers"):src.index("const planIsEmpty")]
        assert "unaccountedCandidates.length" in block
        assert "doubleCounted.length" in block


class TestInputAccounting:
    """Duplicate collapse is only meaningful if every input is accounted for once."""

    def test_each_candidate_is_counted_across_all_three_sinks(self):
        src = _detect()
        block = src[src.index("const seen = new Map()"):src.index("const unaccountedCandidates")]
        assert "destination:" in block
        assert "'rejection'" in block
        assert "'unplaceable'" in block

    def test_a_candidate_placed_twice_is_reported_as_a_one_home_violation(self):
        src = _detect()
        assert "one-home-per-fact is violated" in src

    def test_a_merged_fact_must_name_at_least_one_source(self):
        """An entry with no source is a fact the resolution invented."""
        src = _detect()
        sources = re.search(r"sources: \{[^}]*\}", src)
        assert sources and "minItems: 1" in sources.group(0)

    def test_prompt_requires_collapse_of_duplicate_reporters(self):
        src = _detect()
        assert "DUPLICATE COLLAPSE" in src
        assert "ACCOUNT FOR EVERY CANDIDATE, EXACTLY ONCE" in src

    def test_prompt_states_the_narrower_statement_wins(self):
        """merge-preserves-precision, in the criteria's own words."""
        src = _detect()
        assert "NARROWER verified statement" in src
        assert "constraints" in src


class TestSubtractionsAreDerived:
    def test_subtractions_are_computed_from_the_merged_facts(self):
        """Derived, so 'emitted per source' is true by construction."""
        src = _detect()
        block = src[src.index("const subtractions = []"):src.index("// Per-leaf arithmetic")]
        assert "for (const group of destinations)" in block
        assert "candidateById.get(id)" in block
        assert "source: candidate.leaf" in block

    def test_a_subtraction_is_skipped_when_the_home_did_not_move(self):
        src = _detect()
        assert "norm(candidate.proposedDestination) === norm(group.destination)) continue" in src

    def test_every_subtraction_carries_the_execution_order(self):
        """Deleting a fact before its replacement exists loses it entirely."""
        src = _detect()
        assert "order: 'write-destination-before-subtract-source'" in src
        assert "write-destination-before-subtract-source" in _lane()

    def test_the_schema_has_no_subtractions_field(self):
        """The agent cannot supply them, so it cannot forget them either."""
        src = _detect()
        schema = src[src.index("const RESOLUTION_SCHEMA"):src.index("// The prompt.")]
        assert "subtractions" not in schema


class TestUnplaceableIsDeclaredNotForced:
    def test_the_unplaceable_schema_has_no_destination_property(self):
        """Structurally prevents an unplaceable fact being assigned to the root."""
        src = _detect()
        block = src[src.index("unplaceable: {"):src.index("liftOuts: {")]
        # Strip the comment lines: the block explains WHY there is no such
        # property, and the word must not be read as a declaration of one.
        declarations = "\n".join(
            line for line in block.splitlines() if not line.strip().startswith("//")
        )
        assert not re.search(r"\bdestination\s*:", declarations)

    def test_a_reason_is_required_on_every_unplaceable_item(self):
        src = _detect()
        block = src[src.index("unplaceable: {"):src.index("liftOuts: {")]
        assert "required: ['candidateId', 'fact', 'reason']" in block

    def test_a_reasonless_unplaceable_is_caught_after_the_fact_too(self):
        """An UNPLACEABLE with no reason is a silent drop with a label on it."""
        src = _detect()
        assert "unplaceableWithoutReason" in src
        assert "REASON MISSING" in src
        block = src[src.index("const postBlockers"):src.index("const planIsEmpty")]
        assert "unplaceableWithoutReason.length" in block

    def test_unplaceable_items_are_counted_in_the_totals(self):
        src = _detect()
        assert "unplaceable: unplaceable.length" in src
        assert "UNPLACEABLE (declared, not resolved)" in src

    def test_prompt_forbids_hoisting_to_the_root(self):
        src = _detect()
        assert "Do not force it to" in src and "the root" in src

    def test_lane_declares_the_limit_rather_than_inventing_a_criterion(self):
        text = _lane()
        assert "declares\nUNPLACEABLE and stops" in text or "does not invent a criterion" in text


class TestDispositionsAreDownwardOnly:
    def test_an_upward_flip_is_corrected(self):
        src = _detect()
        assert "d.before === 'NOT-WARRANTED' && d.after === 'WARRANTED'" in src
        assert "an upward disposition flip is not derivable from subtraction" in src

    def test_a_leaf_with_nothing_left_cannot_stay_warranted(self):
        src = _detect()
        assert "after === 0 && row.after === 'WARRANTED'" in src

    def test_the_counts_behind_the_re_judgment_are_derived(self):
        src = _detect()
        assert "candidatesBefore: before" in src
        assert "candidatesAfter: after" in src
        assert "removedByLeaf" in src


class TestReportOnlyPosture:
    def test_lane_declares_no_qa_gate_and_no_remediation(self):
        assert "no Q&A gate and no remediation phase" in _lane()

    def test_lane_states_the_ordering_constraint(self):
        text = _lane().lower()
        assert "write the destination" in text or "write destinations before" in text

    def test_idempotency_is_not_claimed(self):
        assert "idempotency is not claimed" in _lane().lower()

    def test_result_is_described_as_a_sample_of_samples(self):
        assert "sample of samples" in _lane().lower()
        assert "sample of samples" in _detect().lower()

    def test_detect_pins_opus_high_and_no_remediation_tier(self):
        src = _detect()
        assert "model: 'opus'" in src
        assert "effort: 'high'" in src
        assert "model: 'sonnet'" not in src

    def test_lane_documents_the_single_subject_trap(self):
        assert "regardless of subject count" in _lane().lower()


class TestScopeBoundaries:
    def test_prompt_forbids_discovering_facts_from_code(self):
        src = _detect()
        assert "You are NOT discovering facts" in src

    def test_prompt_routes_content_work_away_rather_than_absorbing_it(self):
        src = _detect()
        assert "You are NOT auditing content" in src
        assert "routedTo" in src

    def test_lane_states_why_this_is_not_coverage_and_not_an_audit(self):
        text = _lane()
        assert "Not `coverage`" in text
        assert "Not an audit lane with a bigger selector" in text
