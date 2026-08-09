"""Drift guard for the two code-review skills (single-sourced via a generator).

git-kit:git-code-review and p4-kit:p4-code-review run the same multi-agent
review pipeline (identical review_profiles, subagents, guardrails, issue schema,
submit-gate semantics, narration); only the VCS front-half differs. Historically
the shared back-half drifted by accident -- a fix landed in one kit's SKILL.md
and never reached the other (findings G6/G7 of the 2026-06-09 architecture
review).

Both SKILL.md files AND both references/submit-gates.md files are now rendered
from ONE template + a per-VCS substitution table in
scripts/gen_code_review_skills.py. This test asserts the committed files are
byte-identical to what the generator renders, so the two kits cannot drift: a
hand-edit to either rendered file fails the byte-identity check, and a template
change that isn't regenerated fails it too. Same enforcement idea as
tests/skills-kit/test_workflow_js_drift.py.

To change either skill: edit the template/fragments in
scripts/gen_code_review_skills.py, run
`uv run python scripts/gen_code_review_skills.py`, and commit all four rendered
files together.

Lives in tests/bootstrap/code_review/ because the invariant is the shared
review-pipeline contract embodied by bootstrap_lib/code_review -- neither kit
owns it, mirroring the cross-plugin vendoring drift tests already in
tests/bootstrap/.
"""

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
GEN_PATH = REPO_ROOT / "scripts" / "gen_code_review_skills.py"

_spec = importlib.util.spec_from_file_location("gen_code_review_skills", GEN_PATH)
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)


class TestRenderedFilesMatchTemplate:
    def test_every_target_matches_rendered(self):
        for path, rendered in gen.targets().items():
            on_disk = path.read_text(encoding="utf-8")
            assert on_disk == rendered, (
                f"{path} drifted from the canonical template in "
                f"gen_code_review_skills.py -- edit the template/fragments and "
                f"regenerate (do not hand-edit the rendered file)"
            )

    def test_targets_exist(self):
        for path in gen.targets():
            assert path.is_file(), f"missing generated file: {path}"

    def test_check_mode_passes_on_clean_tree(self):
        assert gen.check() == []


class TestDispatchRulePresent:
    """The deterministic dispatch threshold must reach BOTH skills verbatim."""

    def test_both_skills_carry_the_lane_rule(self):
        for vcs in ("git", "p4"):
            body = gen.render_skill(vcs)
            assert "lanes = R x K" in body
            assert "If lanes <= 6" in body
            assert "If lanes > 6" in body
            assert "Workflow tool" in body


class TestMdDomainContributorPresent:
    """The subject-lens md-domain wiring must reach BOTH skills verbatim."""

    def test_both_skills_carry_probe_and_fallback(self):
        for vcs in ("git", "p4"):
            body = gen.render_skill(vcs)
            # step-2 claim probe: one `**/*.md` glob supersedes the two-glob form
            assert "Claim probe" in body
            assert "skills-kit:md-domain" in body
            assert "--claim '**/*.md'" in body
            assert ".md.html" in body            # Markdeep is NOT claimed
            # step-6 launch: three-way routing + three-tier version-skew fallback
            assert "Subject-lens md-domain pass" in body
            assert "routed THREE ways by basename" in body
            assert "`audit_project_doc`" in body
            # (assertions repaired at the md-domain cutover: they had gone stale
            # against the shipped prose, which states the broad-skew re-run in
            # short form; the fallback became THREE-TIER when skill references
            # entered the claim)
            assert "THREE-TIER fallback" in body
            assert "version skew" in body
            # broad skew re-runs without --claim; project-doc-only skew keeps the two globs
            assert "broad skew re-runs with no\n            `--claim`" in body
            # ...and the reference doc carries the long form of both tiers
            ref = gen.render_md_domain_review(vcs)
            assert "WITHOUT any `--claim` flags" in ref
            assert "--claim '**/CLAUDE.md' --claim '**/SKILL.md'" in ref
            assert "--claim '**/CLAUDE.md' --claim '**/SKILL.md'" in body

    def test_skill_references_are_claimed_and_routed_to_the_skill_lane(self):
        """The 2026-07-28 carve-out is RETIRED, and both halves must move together.

        The `!**/skills/*/references/*.md` exclusion existed only because no
        md-domain lane read a skill reference's prose. The `audit_skill` lane now
        owns that shape (skill-standards.md section 10), so the exclusion is gone
        and the routing sends the shape to `skill-detect.js`. This pins BOTH: an
        exclusion that comes back without criteria is the fake gate returning, and
        a dropped exclusion without the routing sends the file to a lane that
        declines it.
        """
        exclusion = "--claim '!**/skills/*/references/*.md'"

        # The DEFAULT claim -- the step-2 decision -- must not carry it.
        assert exclusion not in gen.CLAIM_PROBE, (
            "the step-2 claim probe reinstated the skill-reference exclusion as "
            "the default. It is retired -- the audit_skill lane audits that shape "
            "now. Do not reintroduce it without also removing the section-10 criteria."
        )

        for vcs in ("git", "p4"):
            body = gen.render_skill(vcs)
            ref = gen.render_md_domain_review(vcs)

            # the routing rule that gives the claimed shape a destination
            assert "*/skills/<name>/references/" in body, (
                f"{vcs} SKILL.md: step 6 no longer routes a claimed skill reference "
                "to the audit_skill lane -- claimed with no destination is a decline"
            )
            assert "skill reference document" in ref

            # Tier 3 keeps the exclusion available as a COMPATIBILITY SHIM for an
            # installed audit_skill lane that predates the subject shape. That skew
            # is invisible to the other two tiers -- an older skills-kit ships the
            # same entry point with the same args contract -- so the probe is a
            # capability marker in the installed standards doc, not a version.
            assert "skill-reference skew" in ref
            assert "## 10. Skill reference documents" in ref
            assert "COMPATIBILITY shim" in ref
            assert "skill-reference skew" in body

            # ...and the shim is the ONLY place the exclusion survives.
            tier3 = ref.split("**skill-reference skew**")[1]
            assert exclusion in tier3
            assert ref.count(exclusion) == 1, (
                f"{vcs} md-domain-review.md: the exclusion appears outside the "
                "skill-reference skew tier -- it is a compatibility shim, not a "
                "default claim"
            )

    def test_both_skills_carry_labeled_section_and_notice(self):
        for vcs in ("git", "p4"):
            body = gen.render_skill(vcs)
            assert "## md-domain (subject-lens) findings" in body
            assert "never merge the two" in body
            assert "ruleset changed" in body            # self-reference notice

    def test_both_md_domain_references_render(self):
        git_ref = gen.render_md_domain_review("git")
        p4_ref = gen.render_md_domain_review("p4")
        for ref in (git_ref, p4_ref):
            assert "# Subject-lens md-domain contributor" in ref
            assert "skills/md-domain/workflow/claude-md-detect.js" in ref
            assert "skills/md-domain/workflow/skill-detect.js" in ref
            assert "skills/md-domain/workflow/project-doc-detect.js" in ref  # third lane
            assert "venvPython" in ref
            assert "ancestorClaudeMdPaths" in ref
            # three-way routing + three-tier fallback documented
            assert "three-way by basename" in ref
            assert "project-doc-only skew" in ref
            assert "**/*.md" in ref
        # per-VCS pre-image origin seam
        assert "git show" in git_ref and "p4 print" not in git_ref
        assert "p4 print" in p4_ref and "git show" not in p4_ref

    def test_md_domain_reference_targets_exist(self):
        # The generated references are part of the drift-checked target set.
        target_names = {p.name for p in gen.targets()}
        assert "md-domain-review.md" in target_names
        assert "md-audit-review.md" not in target_names  # renamed at the md-domain cutover


class TestDeclinedLedgerPresent:
    """The declined-findings ledger collapse + record steps must reach BOTH skills."""

    def test_both_skills_carry_collapse_and_record(self):
        for vcs in ("git", "p4"):
            body = gen.render_skill(vcs)
            # step-9 collapse region
            assert "Declined-findings ledger" in body
            assert "bundle.ledger_hits" in body
            assert "previously declined (N):" in body
            assert "SERIOUS-severity md-domain finding" in body
            assert "NEVER collapsed" in body
            # post-decision record step
            assert "--ledger-record" in body
            assert "bundle.change_id" in body
            assert "bundle.ledger_baseline" in body
            # bundle field wiring
            assert "ledger_baseline" in body
            assert "ledger_hits" in body

    def test_record_step_uses_correct_launch_prefix(self):
        # p4 must launch via python3; git via the bare plugin-root path.
        p4 = gen.render_skill("p4")
        git = gen.render_skill("git")
        assert "python3 ${CLAUDE_PLUGIN_ROOT}/scripts/prepare_review.py" in p4
        assert "tool: ${CLAUDE_PLUGIN_ROOT}/scripts/prepare_review.py" in git

    def test_both_ledger_references_render(self):
        git_ref = gen.render_declined_ledger("git")
        p4_ref = gen.render_declined_ledger("p4")
        for ref in (git_ref, p4_ref):
            assert "# Declined-findings ledger" in ref
            assert "bootstrap_lib.code_review.ledger" in ref
            assert "normalized anchor" in ref
            assert "SERIOUS" in ref
            assert "Limits" in ref
        # per-VCS baseline seam
        assert "range base SHA" in git_ref and "shelf fingerprint" not in git_ref
        assert "shelf fingerprint" in p4_ref and "range base SHA" not in p4_ref

    def test_ledger_reference_targets_exist(self):
        target_names = {p.name for p in gen.targets()}
        assert "declined-ledger.md" in target_names


class TestMdDomainCaseInsensitiveGuard:
    """Deliverable 2 (b): the md-domain reference tells consumers to compare case-insensitively."""

    def test_both_references_mention_case_insensitive_compare(self):
        for vcs in ("git", "p4"):
            ref = gen.render_md_domain_review(vcs)
            assert "case-INSENSITIVELY on Windows" in ref


class TestLaunchNarrationPresent:
    """Deliverable 1: the file-type-driven launch message table reaches BOTH skills."""

    def test_both_skills_carry_the_launch_table(self):
        for vcs in ("git", "p4"):
            body = gen.render_skill(vcs)
            name = "git-code-review" if vcs == "git" else "p4-code-review"
            assert "launch_message:" in body
            # canonical style line, VCS-named
            assert f"Running {name}: this audits .md file changes against project standards" in body
            # all four base rows + the trivial/skip row
            assert "all_md:" in body and "all_data:" in body
            assert "mixed:" in body and "all_code:" in body
            assert "md_trivial:" in body
            assert "mechanical (typo-sized)" in body
            # emitted at launch, from step 2
            assert "emit the launch rationale line ONCE" in body

    def test_launch_message_documents_banned_anti_patterns(self):
        for vcs in ("git", "p4"):
            body = gen.render_skill(vcs)
            assert "Negative direction" in body          # anti-pattern (a)
            assert "Asserting it is not a mistake" in body  # anti-pattern (b)
            assert "let the reader draw the" in body


class TestTrivialityGatePresent:
    """Deliverable 2: the pure-mechanical triviality skip rule reaches BOTH skills."""

    def test_both_skills_carry_the_gate_and_honest_labeling(self):
        for vcs in ("git", "p4"):
            body = gen.render_skill(vcs)
            assert "Triviality gate" in body
            assert "trivial_reasons" in body
            # only non-trivial files are audited
            assert "NON-TRIVIAL claimed file" in body
            # honest skip section, never DIFF-CLEAN / never an audit
            assert "## Mechanical checks (audit skipped)" in body
            assert "never label a skipped file DIFF-CLEAN" in body
            # nothing to the ledger for skipped files
            assert "NEVER written to the ledger" in body or "write NOTHING to the ledger" in body
            # override on explicit request
            assert "asks for the full review" in body

    def test_reference_documents_the_gate(self):
        for vcs in ("git", "p4"):
            ref = gen.render_md_domain_review(vcs)
            assert "Triviality gate" in ref
            assert "trivial_checks" in ref
            assert "fails CLOSED" in ref


class TestVcsSeamsRendered:
    """The per-VCS seams the substitution table exists for must actually land."""

    def test_git_seam(self):
        body = gen.render_skill("git")
        assert "# Git Code Review" in body
        assert "auto-detect" in body           # range auto-detection wording
        assert "Branch: <branch>" in body      # git-only output header
        assert "auto-created shelf" not in body  # git has no shelf-cleanup step
        # git's ledger-record step is step 10 (no shelf-cleanup step precedes it).
        assert "- n: 10" in body               # ledger-record step
        assert "- n: 11" not in body           # ...and nothing beyond it

    def test_p4_seam(self):
        body = gen.render_skill("p4")
        assert "# P4 Code Review" in body
        assert "- n: 10" in body               # p4-only auto-shelf cleanup step
        assert "- n: 11" in body               # p4 ledger-record step (after cleanup)
        assert "auto-created shelf" in body     # p4-only cleanup step content
        assert "python3` interpreter" in body  # p4-only launch gotcha
        assert "Branch: <branch>" not in body  # no git output header
