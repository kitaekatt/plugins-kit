"""Drift guard for the two code-review skills (single-sourced via a generator).

git-kit:git-code-review and p4-kit:p4-code-review run the same multi-agent
review pipeline (identical review_profiles, subagents, guardrails, issue schema,
submit-gate semantics, narration); only the VCS front-half differs. Historically
the shared back-half drifted by accident -- a fix landed in one kit's SKILL.md
and never reached the other (findings G6/G7 of the 2026-06-09 architecture
review).

Both SKILL.md files, their shared references, and all effort agents are rendered
from shared templates plus per-VCS substitutions in
scripts/gen_code_review_skills.py. This test asserts the committed files are
byte-identical to what the generator renders, so the two kits cannot drift: a
hand-edit to either rendered file fails the byte-identity check, and a template
change that isn't regenerated fails it too. Same enforcement idea as
tests/skills-kit/test_workflow_js_drift.py.

To change either skill: edit the template/fragments in
scripts/gen_code_review_skills.py, run
`uv run python scripts/gen_code_review_skills.py`, and commit every rendered file
together.

Lives in tests/bootstrap/code_review/ because the invariant is the shared
review-pipeline contract embodied by bootstrap_lib/code_review -- neither kit
owns it, mirroring the cross-plugin vendoring drift tests already in
tests/bootstrap/.
"""

import importlib.util
from pathlib import Path

import pytest

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


class TestMechanicalScanContract:
    def test_both_native_skills_require_contract_two_and_allow_masked_errors(self):
        for vcs in ("git", "p4"):
            body = gen.render_skill(vcs)
            assert "Require `bundle.mechanical_contract == 2`" in body
            assert "Preserve each record's `mechanical_contract: 2`" in body
            assert "Later errors hidden by the first diagnostic remain reviewer scope" in body
            assert "hidden errors remain reviewer scope and may be reported" in body
            assert "nor report a hit the scan" not in body

    def test_both_skills_read_per_file_phrases_from_the_bundle(self):
        for vcs in ("git", "p4"):
            body = gen.render_skill(vcs)
            assert "diff_chunks[i].mechanical_scan" in body
            assert "derive the covered-check list from THAT record" in body
            assert "bundle.mechanical_check_phrases" in body
            assert "non_ascii` = non-ASCII characters" not in body
            assert "no mechanical coverage for this file" in body
            assert "covers exactly those two checks" not in body

    def test_both_skills_transport_claimed_scan_to_md_domain(self):
        for vcs in ("git", "p4"):
            body = gen.render_skill(vcs)
            ref = gen.render_md_domain_review(vcs)
            for rendered in (body, ref):
                assert "mechanicalScan" in rendered
                assert "mechanicalCheckPhrases" in rendered
                assert "bundle.mechanical_check_phrases" in rendered
            assert "mechanical_scan.files[0]" in body
            assert "does not" in ref and "audit the file" in ref


class TestCitationVerificationDispatch:
    def test_both_skills_parse_native_lanes_and_pass_bundle_to_endpoints(self):
        for vcs in ("git", "p4"):
            body = gen.render_skill(vcs)
            assert "--bundle <bundle.bundle_dir>/bundle.json" in body
            assert "Parse every NATIVE Agent lane's returned array" in body
            assert "scripts/parse_review_lane.py" in body
            assert "verifies each citation" in body
            assert "Endpoint envelopes already contain output from the same shared parser" in body


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
        # prepare_review.py ships mode 100644 with no shebang, so a bare-path
        # launch exits 126 (permission denied) -- BOTH kits must launch it via
        # an explicit python3 interpreter, at every prepare and ledger-record
        # site. There is no bare-path form left to assert for either kit.
        p4 = gen.render_skill("p4")
        git = gen.render_skill("git")
        for body in (p4, git):
            assert "tool: python3 ${CLAUDE_PLUGIN_ROOT}/scripts/prepare_review.py" in body
            assert "tool: ${CLAUDE_PLUGIN_ROOT}/scripts/prepare_review.py" not in body
        # ledger-record site (@PREPARE_TOOL@ token, shared LEDGER_RECORD_STEP body)
        p4_ledger = gen.render_declined_ledger("p4")
        git_ledger = gen.render_declined_ledger("git")
        for ledger in (p4_ledger, git_ledger):
            assert "python3 ${CLAUDE_PLUGIN_ROOT}/scripts/prepare_review.py --ledger-record" in ledger

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


class TestEmptyChunksFastPathCannotSkipMdDomainPass:
    """Item 2: step 6's diff_chunks-empty fast path must not skip the md-domain
    subject-lens pass while a non-trivial claimed file exists -- an md-only
    change reviewed with `--claim '**/*.md'` has zero diff_chunks BY
    CONSTRUCTION, so an unconditional "diff_chunks empty -> skip step 6" reads
    as "skip the audit too" and renders zero issues for the only files that
    changed.

    Three cases the fast-path wording must get right:
      - generic-only (diff_chunks non-empty): the fast path never fires --
        it is gated on diff_chunks being empty in the first place.
      - claimed-non-trivial-only (diff_chunks empty, a NON-TRIVIAL claimed
        file exists): must NOT fire -- the md-domain pass still runs.
      - all-trivial-only (diff_chunks empty, every claimed file trivial):
        still fires, covered by the pre-existing MD_DOMAIN_LAUNCH conjunctive
        gate ("skip the reviewer fan-out AND this md-domain pass ENTIRELY").
    """

    def test_generic_only_fast_path_still_gated_on_diff_chunks(self):
        for vcs in ("git", "p4"):
            body = gen.render_skill(vcs)
            assert "If bundle.diff_chunks is empty" in body

    def test_claimed_non_trivial_blocks_the_fast_path(self):
        for vcs in ("git", "p4"):
            body = gen.render_skill(vcs)
            # the fast path must be qualified so a non-trivial claimed file
            # keeps the md-domain pass running even with zero diff_chunks
            assert "no claimed file is NON-TRIVIAL" in body
            # scoped to the reviewer fan-out, not the whole of step 6 (the
            # md-domain launch lives inside step 6 too and must not be implied
            # skipped by this sentence)
            assert "skip the reviewer fan-out and jump to step 9" in body
            assert "skip step 6 and jump to step 9" not in body

    def test_all_trivial_fast_path_still_skips_everything(self):
        for vcs in ("git", "p4"):
            body = gen.render_skill(vcs)
            assert "skip the reviewer fan-out AND this md-domain pass ENTIRELY" in body


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


class TestSkillsKitRootResolvesFromRegistryFirst:
    """Item 3: the skills-kit plugin root must be resolved from the
    installed_plugins.json REGISTRY first, falling back to the highest-semver
    cache directory only when the registry is empty or unreadable.

    The old rule -- always pick the highest semver directory under the plugin
    cache -- is wrong whenever a higher STALE cache directory outlives the
    active install (a downgrade, a scoped install, a dev-tree entry): the
    registry's `[0].installPath` is the ACTIVE install per the platform
    reference, and this machine's registry is observed populated, not the
    permanently-empty state the root CLAUDE.md's registry_v2_empty insight
    asserts. The fallback is kept so the rule stays correct under EITHER
    reading of that contradiction.
    """

    def test_registry_checked_first(self):
        for vcs in ("git", "p4"):
            ref = gen.render_md_domain_review(vcs)
            assert "installed_plugins.json" in ref
            assert "skills-kit@plugins-kit" in ref
            assert "installPath" in ref

    def test_cache_highest_kept_as_explicit_fallback(self):
        for vcs in ("git", "p4"):
            ref = gen.render_md_domain_review(vcs)
            assert "highest semver dir" in ref
            # the fallback must be explicitly conditioned on the registry
            # being empty or unreadable, not offered as the primary rule
            assert "registry" in ref.lower()
            assert "empty or unreadable" in ref or "is empty" in ref


class TestGeneratedYamlBlockParses:
    """The rendered SKILL.md contract block must be machine-readable YAML.

    The drift guard above compares the rendered bytes to the template, so a
    template that renders INVALID YAML drifts nothing and passes. That is how
    three unparseable lines reached the published skills: YAML reserves a
    leading backtick, so a plain scalar may not start with one, and a `: `
    inside a plain scalar splits it into a key and a value whose own first
    character is then a backtick. Every consumer of the block --
    skills_kit_lib.document_walker.safe_load_block among them -- returns None
    on such a document rather than raising, so nothing was ever noticed.

    Parse both rendered skills, not one: the shared template means a bad
    gotcha line lands in git-kit and p4-kit at once.
    """

    SKILLS = (
        "plugins/git-kit/skills/git-code-review/SKILL.md",
        "plugins/p4-kit/skills/p4-code-review/SKILL.md",
    )

    @staticmethod
    def _first_yaml_block(text):
        out, inside = [], False
        for line in text.splitlines():
            if not inside and line.startswith("```yaml"):
                inside = True
                continue
            if inside and line.startswith("```"):
                break
            if inside:
                out.append(line)
        return "\n".join(out)

    @pytest.mark.parametrize("rel", SKILLS)
    def test_contract_block_is_parseable_yaml(self, rel):
        import yaml

        path = REPO_ROOT / rel
        block = self._first_yaml_block(path.read_text(encoding="utf-8"))
        assert block.strip(), f"{rel}: no ```yaml block found"
        try:
            data = yaml.safe_load(block)
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            where = ""
            if mark is not None:
                lines = block.splitlines()
                if 0 <= mark.line < len(lines):
                    where = f"\n  offending line {mark.line + 1}: {lines[mark.line].strip()}"
            raise AssertionError(
                f"{rel}: contract block is not valid YAML: "
                f"{getattr(exc, 'problem', exc)}{where}\n"
                "  Fix the template in scripts/gen_code_review_skills.py and regenerate. "
                "A list item may not begin with a backtick, and a plain scalar may not "
                "contain ': ' -- reword to '--' or quote the scalar."
            ) from None
        assert isinstance(data, dict), f"{rel}: contract block is not a mapping"
        assert "technique_skill" in data, f"{rel}: contract block lost technique_skill"


class TestRenderedFilesAreDetectableAsMachineEmitted:
    """The banner is a CONTRACT with two readers, so it needs its own guard.

    The byte-identity checks above compare the rendered files to the generator,
    so they stay green if the banner is dropped from the template AND the files
    are regenerated -- which is exactly how it would be lost. What the banner
    buys is that a code review classifies these files as machine-emitted (and
    looks at the generator instead of auditing output nobody can hand-edit), and
    that the pre-commit guard's exemption has something to exempt. Both of those
    are properties of the CONTENT, so assert them against the real detector.
    """

    def test_every_rendered_file_is_detected_as_machine_emitted(self):
        from bootstrap_lib.code_review.machine_emitted import detect_machine_emitted

        for path in gen.targets():
            rel = path.relative_to(REPO_ROOT).as_posix()
            assert detect_machine_emitted("", str(path)) is not None, (
                f"{rel}: no machine-emitted signature. A code review would audit "
                "its content, where no finding can be acted on. Restore the "
                "banner in gen_code_review_skills.py (BANNER / _with_banner)."
            )

    def test_the_banner_survives_the_bytes_detector_the_guard_uses(self):
        from bootstrap_lib.code_review.machine_emitted import detect_signature_bytes

        for path in gen.targets():
            rel = path.relative_to(REPO_ROOT).as_posix()
            assert detect_signature_bytes(path.read_bytes()) is not None, (
                f"{rel}: the pre-commit guard reads blobs as BYTES, so a banner "
                "the text detector finds but this one does not would exempt a "
                "path with nothing to exempt."
            )


class TestRenderedSkillDoesNotClaimDiskFreeOperation:
    """Item 4: prepare_review writes diff chunks, bundle.json, materialized
    pre-images, and a durable ledger.json under the plugin data root -- the
    ledger outlives the review by design. The rendered SKILL.md must not claim
    "no persistence to disk" (both intros do this in the SAME sentence that
    also says the diff is "partitioned on disk into chunks", which is a
    self-contradiction) or otherwise claim disk-free operation.
    """

    def test_intro_does_not_claim_no_disk_persistence(self):
        for vcs in ("git", "p4"):
            body = gen.render_skill(vcs)
            assert "no persistence to disk" not in body
            assert "disk-free" not in body

    def test_intro_states_what_actually_persists(self):
        for vcs in ("git", "p4"):
            body = gen.render_skill(vcs)
            # the transient review bundle vs. the durable ledger must both be
            # named, so the reader is told what is retained and where -- not
            # just that "something" is written
            assert "bundle_dir" in body or "bundle.bundle_dir" in body
            assert "ledger" in body.lower()


class TestStaleOpenRenderedOnP4Only:
    """p4-kit surfaces bundle.stale_open (a CL that already owns a depot path
    reconcile flags: opened for edit then deleted locally, or opened for
    delete then recreated) beside bundle.unresolved under one not-submittable
    heading. git-kit has no open-action concept -- git tracks index state, not
    a per-file open action -- so the rendered git skill must not carry any of
    this."""

    def test_p4_skill_renders_the_stale_open_section(self):
        # Assert on strings the RENDER BLOCK alone carries. "stale_open" by
        # itself is not one of them -- the step-2 expected-key list and the
        # step-9 checklist line both name the key, so that substring stays
        # present when the render block is deleted and would pin nothing.
        body = gen.render_skill("p4")
        assert "## CL is not in a submittable state -- fix before review" in body
        assert "`p4 reconcile <path>` flips the CL's open action" in body
        assert "`p4 revert <path>` discards the CL's" in body
        assert "bundle.stale_open` is non-empty, list each entry's depot" in body

    def test_git_skill_has_none_of_it(self):
        body = gen.render_skill("git")
        assert "stale_open" not in body
        assert "## CL is not in a submittable state -- fix before review" not in body
        assert "p4 reconcile <path>" not in body


class TestP4ClaimProbeSubstitution:
    """The p4 claim probe overrides the shared probe's single-invocation
    wording, because a foreign-client CL refuses `--claim` and the skill then
    runs prepare a second time without it. The two texts must not agree.

    A byte-identity check between artifact and generator cannot protect this:
    regenerating moves both sides together, so a substitution that stopped
    matching its source would leave the generator, the rendered file and that
    check all consistent, and the p4 skill would carry an instruction its own
    on_failure block contradicts."""

    def test_p4_probe_drops_the_single_invocation_wording(self):
        assert gen.P4_CLAIM_PROBE != gen.CLAIM_PROBE
        assert "only ONCE" not in gen.P4_CLAIM_PROBE
        assert "Do NOT run prepare" not in gen.P4_CLAIM_PROBE

    def test_substitute_refuses_a_source_it_cannot_find(self):
        with pytest.raises(ValueError, match="substitution source not found"):
            gen._substitute("some text", "absent needle", "replacement")

    def test_only_the_p4_skill_carries_the_fallback_wording(self):
        fallback = "once unless the foreign-client fallback below applies"
        assert fallback in gen.render_skill("p4")
        assert fallback not in gen.render_skill("git")


class TestP4PendingChangeLookup:
    """The picker asks p4 for the effective user's pending changes."""

    def test_uses_the_effective_user_directly(self):
        body = gen.P4_SKILL.read_text(encoding="utf-8")
        assert 'input: "p4 changes --me -s pending -m 20"' in body

    def test_does_not_derive_the_user_from_configured_variables(self):
        body = gen.P4_SKILL.read_text(encoding="utf-8")
        assert "p4 set -q P4USER" not in body
