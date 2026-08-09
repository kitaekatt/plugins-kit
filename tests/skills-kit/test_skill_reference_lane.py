"""Contract guard: the `audit_skill` lane owns skill reference documents.

The `skill` artifact has TWO subject shapes -- the `SKILL.md` contract root and
the skill's own `references/*.md` documents. Before this contract existed, no
md-domain lane read a reference document's prose: `audit_skill` audited the
owning SKILL.md's contract and load graph, and `audit_project_doc` declined
anything inside a skills tree. Both code-review kits therefore carved the shape
out of their claim globs so it fell back to the generic reviewers.

This module pins the three halves that had to ship together, because any one of
them alone reproduces the original defect:

1. `skill-standards.md` section 10 -- real criteria for the shape (SR-1..SR-4).
2. `workflow/skill-detect.js` -- the shape test admits it, the criteria set is
   selected per subject, and the SKILL.md-only rows are explicitly not applied.
3. `references/lanes/audit-lane.md` -- the decline contract's per-lane shape
   test says so, so the prose and the code cannot drift.

The claim-glob half lives in tests/bootstrap/code_review/test_skill_drift.py
(the kits are generated, so their guard belongs with the generator's).
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MD_DOMAIN = REPO_ROOT / "plugins" / "skills-kit" / "skills" / "md-domain"
SKILL_DETECT = MD_DOMAIN / "workflow" / "skill-detect.js"
SKILL_STANDARDS = MD_DOMAIN / "references" / "standards" / "skill-standards.md"
PROJECT_DOC_STANDARDS = MD_DOMAIN / "references" / "standards" / "project-doc-standards.md"
AUDIT_LANE = MD_DOMAIN / "references" / "lanes" / "audit-lane.md"
DISCOVER_SKILL = MD_DOMAIN / "scripts" / "discover_skill.py"

# The four reference-prose criteria and their taxonomy ids. Renaming one is a
# contract break for the workflow lane, the standards doc and any consumer that
# has recorded a verdict -- the same "do not rename them" rule section 7 states.
SR_TAXONOMY = (
    "O_broken_inbound_anchor",
    "P_internal_contradiction",
    "Q_overstated_claim",
    "R_maintainer_only_material",
)


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


class TestStandardsCarryTheCriteria:
    def test_section_10_exists(self):
        text = _read(SKILL_STANDARDS)
        assert "## 10. Skill reference documents (`references/*.md`)" in text, (
            "skill-standards.md lost the skill-reference section -- the criteria "
            "half of the contract. Without it the claim must be carved out again."
        )

    @pytest.mark.parametrize("sr", ["SR-1", "SR-2", "SR-3", "SR-4"])
    def test_each_criterion_is_stated(self, sr):
        assert f"#### {sr}." in _read(SKILL_STANDARDS)

    @pytest.mark.parametrize("taxonomy", SR_TAXONOMY)
    def test_taxonomy_id_is_in_the_table(self, taxonomy):
        assert f"`{taxonomy}`" in _read(SKILL_STANDARDS)

    def test_no_recommended_tier(self):
        """2.4: patterns are required, conditionally required, or prohibited."""
        text = _read(SKILL_STANDARDS)
        section = text.split("## 10. Skill reference documents")[1]
        assert "recommended" not in section.lower() or "no recommended tier" in section, (
            "section 10 introduced a 'recommended' criterion -- the framework has "
            "no such tier (2.4)"
        )

    @pytest.mark.parametrize(
        "guard",
        [
            "**Not a violation, and this is the load-bearing half:**",   # SR-1
            "THREE genres, only the first is in scope",                  # SR-3
            "**Consequence bar.**",                                      # SR-3
            "**Scope guard:** guidance addressed to someone maintaining the SYSTEM",  # SR-4
        ],
    )
    def test_measured_false_positive_guards_survive(self, guard):
        """Each guard was added because a held-out run measured the over-fire."""
        assert guard in _read(SKILL_STANDARDS)

    def test_code_review_boundary_is_stated(self):
        """The lane must not drift into reporting defects in the described system."""
        section = _read(SKILL_STANDARDS).split("## 10. Skill reference documents")[1]
        assert "do NOT judge the code" in section
        assert "code-review finding" in section

    def test_shared_criteria_are_referenced_not_restated(self):
        section = _read(SKILL_STANDARDS).split("## 10. Skill reference documents")[1]
        for shared in (
            "M_ancestor_convention_violation",
            "N_user_standard_violation",
            "adp_back_reference",
            "audit_references",
        ):
            assert shared in section, (
                f"section 10 no longer names {shared} -- either it was restated "
                "(a second source of truth) or the coverage claim is now false"
            )

    def test_inapplicable_rows_are_enumerated(self):
        section = _read(SKILL_STANDARDS).split("## 10. Skill reference documents")[1]
        assert "### 10.1 What the lane does NOT apply to a reference document" in section
        for row in ("required_frontmatter", "yaml_contract_block", "hygiene_thresholds"):
            assert row in section


class TestDetectLaneAdmitsTheShape:
    def test_shape_test_accepts_a_skill_reference_path(self):
        src = _read(SKILL_DETECT)
        assert "const isSkillRefPath" in src, (
            "skill-detect.js has no path test for the reference subject shape"
        )
        assert "skill_reference" in src

    def test_branch_one_does_not_decline_a_skill_reference(self):
        src = _read(SKILL_DETECT)
        assert (
            "const routingClause = f.kind && f.kind !== 'skill' && f.kind !== 'skill_reference'"
            in src
        ), (
            "an explicit kind of `skill_reference` would be DECLINED -- the caller "
            "classification the project-doc lane emits would bounce straight back"
        )

    def test_branch_three_self_applies_both_shapes(self):
        """A kind-less review-mode call must recognise the reference shape."""
        src = _read(SKILL_DETECT)
        assert "TWO subject shapes" in src
        assert r"*/skills/<name>/references/" in src

    @pytest.mark.parametrize("taxonomy", SR_TAXONOMY)
    def test_taxonomy_enum_admits_the_new_ids(self, taxonomy):
        src = _read(SKILL_DETECT)
        assert f"'{taxonomy}'" in src, (
            f"{taxonomy} is not in the lane's taxonomy enum, so a schema-validated "
            "finding carrying it cannot be returned at all"
        )

    def test_validator_is_not_run_on_a_reference(self):
        """A reference has no contract; running the validator invents findings."""
        src = _read(SKILL_DETECT)
        assert "NOT APPLICABLE to a skill reference document" in src
        assert 'do NOT emit a "validator unavailable" finding' in src

    def test_reference_subject_skips_the_skill_md_only_rows(self):
        src = _read(SKILL_DETECT)
        assert "NOT APPLICABLE to this subject, do not emit: taxonomies A, B, C, D, E" in src

    def test_sr3_scope_guard_is_present(self):
        """The two measured over-fire modes: instructions, and design principles.

        A held-out precision run (2026-08-09, three reference documents from
        plugins the criteria were not derived against) showed SR-3 firing on a
        principles document's core structural device. Both exemptions are what
        stops the criterion turning such a document into wall-to-wall findings.
        """
        src = _read(SKILL_DETECT)
        assert "SCOPE GUARD, load-bearing -- THREE genres" in src
        assert "never hand-edit this file" in src              # the INSTRUCTION genre
        assert "NORMATIVE DESIGN PRINCIPLE" in src             # the INVARIANT genre
        assert "CONSEQUENCE BAR" in src                        # the rhetorical-universal bar

    def test_sr1_does_not_fire_on_an_unambiguous_prose_pointer(self):
        """Same run: 4 of 6 SR-1 findings were informal-but-resolvable pointers."""
        src = _read(SKILL_DETECT)
        assert "NOT A VIOLATION, and this is the load-bearing half" in src
        assert "resolves UNAMBIGUOUSLY to exactly one place" in src
        assert "CLASSIFY EACH CITATION BY FORM before judging it" in src

    def test_sr4_scope_guard_bounds_the_rule_to_the_production_pipeline(self):
        """The Rule sentence must not read wider than the Test."""
        src = _read(SKILL_DETECT)
        assert "whose ONLY reader is someone maintaining the document's own PRODUCTION PIPELINE" in src
        assert "maintaining the SYSTEM the document describes is content the reader NEEDS" in src

    def test_sr1_and_sr2_require_both_halves(self):
        src = _read(SKILL_DETECT)
        assert "do NOT raise it without both halves" in src      # SR-1
        assert "MUST quote BOTH passages with line numbers" in src  # SR-2


class TestProseContractMatchesTheCode:
    def test_audit_lane_shape_test_admits_references(self):
        lane = _read(AUDIT_LANE)
        assert "Accepted `kind` values: `skill` and `skill_reference`." in lane

    def test_audit_lane_states_the_validator_exemption(self):
        assert "**Not on a skill reference subject.**" in _read(AUDIT_LANE)

    def test_project_doc_standards_route_to_the_skill_lane(self):
        text = _read(PROJECT_DOC_STANDARDS)
        assert "audited by the skill lane under `skill-standards.md` section 10" in text
        assert "audited transitively via their owning SKILL.md" not in text, (
            "PD-1's neighbour text still says a skill reference is audited only "
            "through its owning SKILL.md -- that is the pre-section-10 claim"
        )


class TestDiscoveryEnumeratesReferences:
    def test_references_flag_lists_reference_documents(self, tmp_path):
        import subprocess
        import sys

        skill = tmp_path / "plugins" / "demo" / "skills" / "demo-skill"
        (skill / "references").mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\nname: demo-skill\nskill-type: technique-skill\n---\n# demo\n",
            encoding="utf-8",
        )
        (skill / "references" / "one.md").write_text("# one\n", encoding="utf-8")

        def run(*extra):
            return subprocess.run(
                [sys.executable, str(DISCOVER_SKILL), "--cwd", str(tmp_path), "--json", *extra],
                capture_output=True, text=True, check=True,
            ).stdout

        import json

        without = json.loads(run())
        with_refs = json.loads(run("--references"))

        assert [r["kind"] for r in without] == ["skill"], (
            "the default listing changed shape -- the skill roster consumes it"
        )
        kinds = sorted(r["kind"] for r in with_refs)
        assert kinds == ["skill", "skill_reference"]
        ref = next(r for r in with_refs if r["kind"] == "skill_reference")
        assert ref["name"] == "demo-skill", "a reference must carry its owning skill"
