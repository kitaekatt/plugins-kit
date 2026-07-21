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


class TestMdAuditContributorPresent:
    """The subject-lens md-audit wiring must reach BOTH skills verbatim."""

    def test_both_skills_carry_probe_and_fallback(self):
        for vcs in ("git", "p4"):
            body = gen.render_skill(vcs)
            # step-2 claim probe
            assert "Claim probe" in body
            assert "skills-kit:md-audit" in body
            assert "--claim '**/CLAUDE.md' --claim '**/SKILL.md'" in body
            # step-6 launch + version-skew fallback
            assert "Subject-lens md-audit pass" in body
            assert "version-coupling safety valve" in body
            assert "WITHOUT any `--claim` flags" in body

    def test_both_skills_carry_labeled_section_and_notice(self):
        for vcs in ("git", "p4"):
            body = gen.render_skill(vcs)
            assert "## md-audit (subject-lens) findings" in body
            assert "never merge the two" in body
            assert "ruleset changed" in body            # self-reference notice

    def test_both_md_audit_references_render(self):
        git_ref = gen.render_md_audit_review("git")
        p4_ref = gen.render_md_audit_review("p4")
        for ref in (git_ref, p4_ref):
            assert "# Subject-lens md-audit contributor" in ref
            assert "claude-md-audit/workflow/detect.js" in ref
            assert "skill-audit/workflow/detect.js" in ref
            assert "venvPython" in ref
            assert "ancestorClaudeMdPaths" in ref
        # per-VCS pre-image origin seam
        assert "git show" in git_ref and "p4 print" not in git_ref
        assert "p4 print" in p4_ref and "git show" not in p4_ref

    def test_md_audit_reference_targets_exist(self):
        # The generated references are part of the drift-checked target set.
        target_names = {p.name for p in gen.targets()}
        assert "md-audit-review.md" in target_names


class TestVcsSeamsRendered:
    """The per-VCS seams the substitution table exists for must actually land."""

    def test_git_seam(self):
        body = gen.render_skill("git")
        assert "# Git Code Review" in body
        assert "auto-detect" in body           # range auto-detection wording
        assert "Branch: <branch>" in body      # git-only output header
        assert "- n: 10" not in body           # git has no shelf-cleanup step

    def test_p4_seam(self):
        body = gen.render_skill("p4")
        assert "# P4 Code Review" in body
        assert "- n: 10" in body               # p4-only auto-shelf cleanup step
        assert "python3` interpreter" in body  # p4-only launch gotcha
        assert "Branch: <branch>" not in body  # no git output header
