# md-domain/ insights

Decision provenance for the md-domain skill itself -- why it is one skill rather
than two routers plus eight members. The canonical content lives in SKILL.md and
`references/`; this file records only the shape decisions.

The inherited provenance -- the numbered `dec_N` framework decisions and the
per-skill CLAUDE.md histories of the folded skills -- lives in
`references/provenance/`. Look there for WHY a contract, a rule id, or a standards
statement reads the way it does; look here for why the SKILL tree has this shape.

```yaml
claude_md:
  _schema_version: "1"
  scope:
    directory: plugins/skills-kit/skills/md-domain
    covers:
      - shape decisions about the md-domain skill itself (the fold, the dispatch table, the required lane fields)
      - where the inherited decision-provenance logs live
    excludes:
      - the standards content itself (lives in references/standards/)
      - the placement framework (lives in references/cohesion-principles.md)
      - the inherited dec_N framework decisions and folded-skill histories (live in references/provenance/)
  insights:
    - id: md_domain_fold
      keywords: [md-domain fold, maximal collapse, one front door, dissolved members, md-audit md-authoring retired, packaging razor applied, verb x artifact]
      summary: md-audit, md-authoring, and their member skills folded into this single domain-skill; cohesion-principles folded in as a reference doc. The verb x artifact matrix survives as ONE dispatch table over two shared verb lanes and four per-artifact standards docs.
      detail: |
        Before the fold the matrix was expressed as topology: two router skills,
        four audit members, two authoring members, and a standalone placement
        skill. That topology carried a hand-maintained second copy of every
        standard (each audit member's criteria: block echoed a standards doc) and
        four near-identical copies of one audit procedure, and it made routing a
        prose-only concern -- the routers could co-load, and a member could be
        reached with no artifact classification at all.

        The fold applies cohesion-principles' skill_packaging_razor: step 1, the
        standards docs and the placement spine are static reference text, so they
        are never skills; step 2, the remaining doers share one vocabulary and one
        change cadence, so they fold into one domain rather than staying separate.
        What survives is the matrix as DATA: one dispatch table (verb x artifact
        -> lane record), one audit procedure parameterized by artifact, one
        authoring procedure parameterized by artifact, four standards docs each
        read in both directions.

        Deliberate omissions: no /md-audit or /md-authoring aliases (clean break
        pending a separate alias decision), and no author x references lane
        (cross-references are emergent, not authored).
      origin: |
        Settled decision 2 of the skills-kit improvement discussion (2026-07-28),
        following the two-agent accuracy audit of skills-kit 0.34.0; implemented
        as phase 3 of that plan.
      added: "2026-07-29"
    - id: lane_records_require_two_fields
      keywords: [required fields, invocation_phrasings, change_driver, router enforcement, registry integrity test, lane record invalid, phrasing coverage]
      summary: WHY the two required lane-record fields exist (the fields themselves and their floor are defined in SKILL.md's "Lane records" section, the SSOT) -- they are the machine-checkable answers to the two risks that survive a fold into one skill.
      detail: |
        The audit found routing was enforced only by prose. Collapsing to one
        skill removes the co-loading failure mode structurally, but two risks
        survive a fold: a lane nobody can phrase their way into, and a lane whose
        content quietly accretes because no one declared what it changes with.
        The two required fields are the answers, and they are machine-checkable
        rather than aspirational.

        invocation_phrasings also feeds a judgment rule in the audit criteria --
        does the domain description cover each lane's declared phrasings -- so the
        description and the dispatch table cannot drift apart silently.

        The third enforcement leg is behavioral, not structural: every audit
        detect lane self-applies its artifact shape test when kind is absent and
        declines a non-matching file with NOT-AUDITED plus an IMPROVE routing
        finding. That generalizes the PD-1 decline contract from the project-doc
        lane to all of them; see references/lanes/audit-lane.md.
      origin: |
        Settled decision 3 (required audit-checked fields on folded techniques,
        2026-07-28) plus the router-enforcement section of the phase-3 design.
      added: "2026-07-29"
    - id: references_layout_deviation
      keywords: [refs-one-hop-deep, nested references, accepted deviation, clustered layout, standards lanes provenance, config off, one hop]
      summary: The clustered references/ layout (standards/, lanes/, skill-domain/, authoring-patterns/, provenance/) is an ACCEPTED deviation from the refs-one-hop-deep rule; the rule is turned off for this repo in .claude/skills-kit/config.yaml. The rule itself ships unchanged to consumers.
      detail: |
        refs-one-hop-deep FAILs nested references directories because deeply
        nested files tend to be partially read and unindexed ones become
        invisible. md-domain keeps the nesting anyway: the five clusters ARE
        the architecture (per-artifact standards vs verb lanes vs deep skill
        refs vs content-shape patterns vs provenance), and the load-graph
        property the rule protects is preserved differently -- every cluster
        surface is a first-class SKILL.md index entry, and the standards and
        lane docs are indexed by full path. Decision made 2026-07-29 at the
        phase-3 code review over the alternatives (flatten to a 30-file root;
        amend the rule to exempt indexed-nested files). The rule amendment --
        a nested file with an explicit index.references[].path entry counts
        as one hop -- is the flagged follow-up that would let the rule come
        back on for this repo.
      origin: |
        Phase-3 code review 2026-07-29: the fold made md-domain the only
        skill in the repo failing its own plugin's audit (24 nested files),
        against the merge-gate convention. User chose keep-layout +
        config-off + record-the-deviation.
      added: "2026-07-29"
    - id: contracts_preserved_verbatim_through_the_fold
      keywords: [golden corpus gate, verdict vocabulary, rule ids preserved, model pinning, PD-1 decline, review reducer invariants, no behavior change]
      summary: The fold is a RELOCATION, not a behavior change. Rule and taxonomy ids, the verdict vocabulary, the PD-1 decline contract, the review-reducer invariants, and the detect/remediate model pinning are all preserved verbatim so the golden corpus stays a meaningful gate.
      detail: |
        Preserved verbatim, deliberately: all rule and taxonomy ids (C-*, R-*,
        A-*, H-*, PD-*, CD-*, DD-*, and the per-lane letter taxonomies including
        the inconsistent ancestor-convention ids M_ / R_ / S_ -- unifying them is
        a flagged follow-up, not part of the fold); the verdict vocabulary
        COMPLIANT / NON-COMPLIANT / DIFF-CLEAN / NOT-AUDITED plus the references
        lane's AUTO / DISCUSS / SPECIAL buckets; the PD-1 decline contract on both
        triggers; the review-reducer invariants (NOT-AUDITED passes through
        relabel untouched and is counted apart from diffClean, fan-out threshold 1
        under review, the attributable/SERIOUS keep-rule); and the model pinning
        (detect/classify opus + high effort, remediate sonnet + low effort).

        Consequence for anyone editing this tree: a change that alters any of
        those is not a refactor. It needs its own decision and its own golden-
        corpus re-record.
      origin: |
        The contract-preservation list in the phase-3 design, gated by the golden
        corpus at tests/skills-kit/golden_corpus/ (mechanical goldens byte-compare;
        recorded lane verdicts re-run).
      added: "2026-07-29"
```
