# cohesion-principles/ insights

> Preserved decision log of the dissolved cohesion-principles skill (folded
> into md-domain as `references/cohesion-principles.md`, 2026-07-29).

Decision provenance for the cohesion-principles skill (the placement spine).
The canonical content lives in SKILL.md; this file records the decisions about
the skill's own shape so they do not bleed into the SKILL.md body.

```yaml
claude_md:
  _schema_version: "1"
  scope:
    directory: skills/cohesion-principles
    covers:
      - shape decisions about the cohesion-principles SKILL.md itself
    excludes:
      - the placement framework content (lives in SKILL.md)
  insights:
    - id: crp_verdict_keep_whole
      keywords: [crp verdict, no references dir, over threshold, keep whole, split evaluated, tool-call doubling, dec-11]
      summary: The SKILL.md is over the 500-line signal threshold (533 lines at the 2026-06-10 evaluation) with no references/ dir -- evaluated 2026-06-10 and deliberately kept whole; do not re-propose a split without new structure.
      detail: |
        Dec-11 evaluation (size is a signal, CRP is the test): the body is an
        Index facts block that routes into ONE content_allocation framework
        block, consumed as a unit by every reader (md-audit members judge
        against it; md-authoring places content by it). No section can be
        omitted on a typical invocation, so any L2 -> L3 split would produce a
        stub plus an always-co-loaded reference -- a tool-call doubling, not a
        context-efficiency win. Verdict: COMPLIANT as-is; keep whole. Re-run
        the CRP test only if the skill grows a genuinely separable sub-task
        (e.g. a worked-examples corpus readers skip on normal placement calls).
      origin: Arch-review finding S18 (2026-06-09); CRP evaluated during the S2-S18 remediation pass 2026-06-10.
      added: "2026-06-10"
    - id: skill_packaging_razor_added
      keywords: [packaging razor, two-step razor, fold vs separate, vocabulary test, cadence test, execute-or-fetch, md-domain restructure, razor provenance]
      summary: "The skill_packaging_razor record (Index fact + framework block, beside placement_follows_trigger_shape) documents the two-step packaging razor settled with the user 2026-07-28: step 1 execute-or-fetch (static text is never a skill), step 2 fold-into-domain iff vocabulary + cadence tests both pass. Documented ahead of the md-domain restructure that implements it, so the restructure executes a written rule rather than taste."
      detail: |
        Two razor forms were considered and retracted during the 2026-07-28
        discussion before this one was settled (rationale in
        tmp/improve-md-audit/log.md -- gitignored working task); the
        surviving form is the one whose steps are each auditable (see
        skill_packaging_razor.declared_evidence in SKILL.md). The
        declared_evidence fields it names on folded techniques are a
        documentation contract; mechanical checking of those fields is
        part of the planned md-domain restructure, not shipped with the
        razor text.
      origin: |
        User-settled decision 1 + 4 of the skills-kit improvement discussion
        (2026-07-28), following the two-agent accuracy audit of skills-kit
        0.34.0.
      added: "2026-07-28"
    - id: stress_test_total_ownership_extensions
      keywords: [steam-analysis stress test, directory load trigger, file access trigger, readme role, in-code contract doc, generated artifact, asset dependency edge, total ownership]
      summary: "The 2026-07 steam-analysis stress test drove five spine extensions toward total ownership (every md file has a named role): (1) directory/subsystem CLAUDE.md load_trigger models BOTH cwd descent and file access beneath the directory; (2) readme_md surface + role (agent-facing copy is SSOT, README is the derived human brief); (3) in_code_contract_doc surface (validator's in-code doc is SSOT for code-enforced schemas) + the md_restates_code_enforced_contract anti-pattern; (4) generated_artifact role (provenance-only audit); (5) the asset_dependency edge (runtime consumption declared via the asset_dependencies: portable unit, mechanically resolved)."
      detail: |
        Every extension was hit as a real gap when the full framework was applied
        end-to-end on a freshly extracted repo (steam-analysis): a data-directory
        CLAUDE.md was correct in practice but dead weight under the strict-cwd
        model; README/root-CLAUDE.md overlap had to be resolved ad hoc; two
        code-validated schemas had no SSOT rule across docstrings and md; a
        committed generated rendering needed a by-fiat exemption; and a cross-skill
        runtime asset path (a workflow consuming another skill's reference) had no
        audit signal. Direction chosen for (5): an explicit declaration
        (asset_dependencies:) rather than heuristic path-literal scanning --
        scanning false-positives and cannot know intent; a declaration is
        opinionated, schema-validated, and mechanically resolvable. The audits
        (claude-md-audit load-trigger note + C-7; project-doc-audit PD-9/PD-10 +
        taxonomy L/M; audit.py asset resolution) operationalize these roles.
      origin: |
        steam-analysis stress-test feedback
        (~/Dev/steam-analysis/docs/skills-kit-feedback.md (external repo), worked
        2026-07-13): gaps 1, 2, 3, 5, 6 of 8. The framework was applied end-to-end
        on a real repo and every gap names the exact change site plus evidence.
      added: "2026-07-13"
```
