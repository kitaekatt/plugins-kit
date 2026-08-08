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
    - id: coverage_is_a_report_only_third_verb
      keywords: [coverage verb, third verb, not an audit lane, not a density lens, report-only, GAPS-FOUND, COVERAGE-ASSESSED, code subject, ambient chain, no remediate workflow, registration is go-live]
      summary: The coverage capability is a REPORT-ONLY third verb (coverage_code_subtree, own coverage-lane.md), not a fifth audit lane and not a --coverage flag. Three contract facts decide it; the report-only narrowing is what makes a third verb cheap.
      detail: |
        Three shapes were considered: (A) a third verb with its own procedure,
        (B) a --coverage lens threaded through audit_claude_md exactly as
        --density is, (C) a fifth audit lane binding into audit-lane.md the way
        audit_references does.

        B is refuted by the NULL CASE. Density's subject is a file that by
        definition exists -- it is a lens over an already-enumerated CLAUDE.md.
        Coverage's canonical instance is a code subtree with NO CLAUDE.md at all,
        so a per-file lens never fires on precisely the case the capability
        exists to catch. discover_claude_md.py returns no subjects when no such
        file exists, and claude-md-detect.js rejects a non-CLAUDE basename as
        NOT-AUDITED, so a virtual-subject workaround is A or C behind a flag.

        C is refuted by three facts, each verified against source:
          - audit-lane.md:19-22 -- everything before the references section
            "applies to the three per-file lanes". audit_references is an
            explicitly carved-out outlier, not a general extension point.
          - audit-lane.md:485-487 -- "The same input produces the same verdict"
            is an audit INVARIANT. Coverage disclaims idempotency (candidate
            selection is a judgment over ~10^4 constructs), so it cannot be an
            audit lane without breaking a contract the audit family relies on.
          - tests/skills-kit/test_domain_members_resolve.py:213-217 forces every
            audit_* lane but references to declare NOT-AUDITED + DIFF-CLEAN, and
            :236 forces every verb == "audit" record to bind a
            workflow_remediate. A third verb satisfies both by not matching them,
            rather than by exception.

        REPORT-ONLY is what keeps A cheap. With no remediation phase there is no
        coverage-remediate.js, the sonnet+low pin does not apply, and
        gen_workflow_js.py -- which assumes per-file edits and
        applied/skipped/failed results -- is not involved at all.

        One place coverage must NOT copy the audit lane: audit-lane.md:110-117
        runs a single-subject job INLINE at the session model. A coverage run
        normally has exactly ONE subtree, so reusing that shortcut would put the
        COMMON case off-pin. The detect workflow is entered regardless of count.

        REGISTRATION WAS THE GO-LIVE SWITCH, and it has been thrown (2026-08-08).
        The verb was deliberately absent from SKILL.md's menu, dispatch table and
        lane registry until its assessment criteria were authored -- a menu entry
        for a verb that cannot assess anything is worse than no entry, the same
        reasoning that kept it off the menu when the analysis vocabulary shipped.
        Criteria landed as references/standards/coverage-standards.md; the lane
        record, menu entry, dispatch row and argument grammar landed with them,
        and SKILL.md's "None of the above reads your source tree" paragraph was
        deleted in the same commit because the entry falsifies it. The two
        contract tests that pinned the switch OFF were inverted in that commit --
        their failure at registration was the designed reminder, not a
        regression.
        GOLDEN CORPUS: NOT re-recorded, deliberately. The obligation recorded
        here anticipated new verdicts changing recorded expectations, but the
        corpus fixtures are markdown FILES and its expected-lanes files are
        per-artifact (claude-md, project-doc, references, skill). Coverage's
        subject is a code subtree plus its ambient chain, so it has no fixture
        shape in this corpus and adds no expectation to any existing one; no
        existing verdict changes meaning. Re-recording would have produced a
        no-op diff. Stated rather than skipped, because
        contracts_preserved_verbatim_through_the_fold requires the decision to
        be deliberate, not merely correct.
      origin: |
        Surface: the coverage-lane spec argued "separate lane" from subject
        enumeration alone. Finding: an adversarial cross-check (gpt-5.6-sol,
        2026-08-08) refuted the fifth-audit-lane reading with the three contract
        facts above; the null-case argument against the lens survived. Follow-up:
        the criteria seam (coverage-detect.js refuses without refs.criteria) and
        the registration/go-live pairing above.
      added: "2026-08-08"
    - id: coverage_depth_asks_rather_than_defaults
      keywords: [basic advanced, analysis depth, intent gate, AskUserQuestion, extreme experience, default disclosure, verdict carries the mode, one dial not two]
      summary: Coverage depth is one dial with two operating points (basic / advanced). When the invocation expresses no depth the intent gate ASKS via AskUserQuestion rather than defaulting -- the rare case where prompting beats a sensible default, because both directions of a silent wrong choice are expensive and invisible.
      detail: |
        The dial moves READ DEPTH and PASS COUNT together: basic is a bounded
        sampled read with a single assessment pass; advanced reads every source
        file completely and adds an invariant-discovery pass before assessment
        and a verification pass after it. Depth and passes are genuinely two
        axes and were deliberately collapsed into one flag -- a caller wanting
        only recall or only precision still gets both, which is worse
        conceptually and better ergonomically.
        Calibration, not quality, names the levels: basic is what a Claude Code
        power user should expect from a routine invocation; advanced is "give me
        the full experience", the shape the generation method itself ran.
        WHY IT ASKS. Defaulting silently is wrong in both directions and the user
        cannot see either error as it happens: choosing advanced opts them into
        an extreme, expensive run they never requested, and choosing basic hands
        someone who wanted exhaustive treatment a bounded sample whose
        COVERAGE-ASSESSED they may read as "verified absent". A prompt costs real
        UX and is justified here by that asymmetry, not by a general preference
        for asking. An explicit flag still runs silently, and a non-interactive
        dispatch takes basic and DISCLOSES it in keyword form (defaults:
        depth=basic) -- disclosure being the fallback for the interactive case,
        since a disclosed default is correctable only after the expensive run.
        CONSEQUENCE FOR THE VERDICT. COVERAGE-ASSESSED means "not found within
        budget" under basic and "verified absent" under advanced, so the report
        must carry the mode; a verdict printed without it is ambiguous.
      origin: |
        Surface: authoring the criteria required settling evidence depth, which
        md-domain-coverage-gaps.md left as a blocking open question with unknown
        relative recall between the alternatives. Finding: the owner reframed
        depth as a parameter rather than a fixed criterion, then ruled that the
        gate should ask rather than default because the default risked opting a
        user into an extreme experience. Follow-up: an earlier "flag only,
        default basic" ruling is SUPERSEDED by this record -- the flag still
        wins, but silence now prompts instead of defaulting.
      added: "2026-08-08"
```
