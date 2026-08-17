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
        generation procedure parameterized by artifact, four standards docs each
        read in both directions.

        Deliberate omissions: no /md-audit or /md-authoring aliases (clean break
        pending a separate alias decision), and no generate x references lane
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
    - id: coverage_is_report_only_and_not_an_audit_lane
      keywords: [coverage verb, third verb, peer verb superseded, discovery phase, not an audit lane, not a density lens, report-only, GAPS-FOUND, COVERAGE-ASSESSED, code subject, ambient chain, no remediate workflow, registration is go-live, coverage_is_a_report_only_third_verb]
      summary: The coverage capability is REPORT-ONLY and has its own procedure (coverage_code_subtree, own coverage-lane.md) -- it is not a fifth audit lane and not a --coverage flag. Three contract facts decide that; the report-only narrowing is what keeps a separate procedure cheap. The further claim that it is a PEER THIRD VERB is superseded (see the amendment at the end).
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

        AMENDMENT 2026-08-09 -- the PEER-VERB inference is superseded; the three
        contract facts are NOT. This record was formerly id
        `coverage_is_a_report_only_third_verb`, and its summary called coverage
        "a REPORT-ONLY third verb". The framework-vocabulary decision
        (references/provenance/skill-authoring-decisions.md,
        dec_20_audit_and_generation_vocabulary) leaves every verified fact above
        intact and overturns exactly one step of reasoning.

        What survives, verbatim and unweakened -- all three are arguments that
        coverage is not a fifth AUDIT lane, and the framework agrees with every
        one of them:
          - audit-lane.md:19-22 -- the pre-references material "applies to the
            three per-file lanes"; audit_references is a carved-out outlier, not
            a general extension point.
          - audit-lane.md:485-487 -- idempotency is an audit INVARIANT, and
            coverage disclaims it.
          - tests/skills-kit/test_domain_members_resolve.py:213-217 and :236 --
            every audit_* lane but references must declare NOT-AUDITED +
            DIFF-CLEAN and bind a workflow_remediate; coverage satisfies both by
            not matching them.

        What is overturned is only the step FROM those facts TO "therefore a peer
        verb". "Not an audit" does not establish peer status. Under the framework
        the producing side is generation (and regeneration where the asset already
        exists), and coverage sits inside that family: it reads code, discovers
        facts, and proposes where they belong -- code introspection leading to fact
        discovery and documentation, which dec_20 places in generation/regeneration
        rather than audit. It is that family's DISCOVERY step, not a third peer.

        The report-only contract does not move an inch, and must not be read as
        moving. Coverage writes nothing (coverage-lane.md:70 "Nothing is ever
        applied", :189 "Then STOP", coverage-standards.md:231-232), which is
        precisely why it is discovery rather than generation proper. Re-homing it
        under the generation family is a FRAMING change; the mechanism that pins
        report-only is unchanged (no bound workflow_remediate, pinned at
        test_domain_members_resolve.py:253-256).
      origin: |
        Surface: the coverage-lane spec argued "separate lane" from subject
        enumeration alone. Finding: an adversarial cross-check (gpt-5.6-sol,
        2026-08-08) refuted the fifth-audit-lane reading with the three contract
        facts above; the null-case argument against the lens survived. Follow-up:
        the criteria seam (coverage-detect.js refuses without refs.criteria) and
        the registration/go-live pairing above.
      added: "2026-08-08"
    - id: hierarchy_is_the_resolution_phase_over_a_tree
      keywords: [hierarchy verb, claude_md_tree, placement resolution, tree unit, report only, INPUTS-INCOMPLETE, computed verdict, sibling blindness, depth invisible per leaf, one home per fact, opt-in phase, RETIRED, lane deleted, shallowest_true_depth re-homed, provenance only]
      summary: "RETIRED 2026-08-17 -- the lane, its two reference documents, its discover script, its workflow and its contract test are deleted; this record is provenance only. Both justifications for a tree-scale unit are supplied by parent composition in generation-lane.md. `shallowest-true-depth` survived the delete, re-homed into cohesion-principles.md."
      detail: |
        WHY A NEW UNIT RATHER THAN A WIDER SELECTOR. Two properties of the
        existing lanes force it, and both are correct behaviour rather than
        defects to fix in place:
          - SIBLING BLINDNESS. A sibling's CLAUDE.md is not ambient for a
            subtree, so per-subtree coverage rightly re-reports a shared fact
            once per sibling. Only a pass whose subject is the whole tree can
            see both reporters at once and collapse them.
          - DEPTH IS INVISIBLE PER LEAF. Whether a fact belongs at the leaf or at
            a parent cannot be judged from inside the leaf's subject; the
            judgment belongs to a lane whose subject contains the parent.
        WHY NOT COVERAGE. coverage-standards.md's present-content-not-re-audited
        is fail-severity -- coverage judges ABSENT facts only. Hierarchy judges
        the PLACEMENT of present facts, so folding it in breaks coverage's own
        hardest boundary. WHY NOT AN AUDIT LANE. The audit criteria are
        per-document; every hierarchy criterion is a relation BETWEEN documents,
        and an audit has no artifact to render a verdict on when the input is a
        set of proposals.
        THE VERDICT IS COMPUTED, AND THAT IS THE WHOLE POINT. The false pass here
        is a resolution handed 10 of 18 leaf reports treating the other 8 as
        empty candidate sets. So the lane enumerates the leaves ITSELF (a
        caller-supplied list cannot notice what it already forgot), builds a
        per-leaf inventory, and decides the incomplete-input cases in
        hierarchy-detect.js BEFORE any agent dispatch. INPUTS-INCOMPLETE is
        deliberately NOT in the lane record's `verdicts` -- it is what the lane
        reports INSTEAD of a verdict -- and it is tallied apart from both
        affirmative verdicts, the same posture as coverage's DISCOVERY-FAILED
        and review mode's NOT-AUDITED.
        FOUR THINGS ARE DERIVED RATHER THAN TRUSTED, because a schema can require
        a list but cannot express these: the subtraction table (computed from the
        merged facts, so "emitted per source" is true by construction and the
        agent schema carries no subtractions field at all); input accounting
        (every candidate appears in exactly one of destination / rejection /
        unplaceable, checkable only because discover_hierarchy.py assigns each
        candidate a stable id); the downward-only disposition flip; and the
        verdict itself. The unplaceable item schema declares NO destination
        property, so an unplaceable fact cannot be quietly assigned to the root.
        REPORT-ONLY IS A HARD PROPERTY OF THE ENTRY POINT, for three reasons that
        each break concretely: the plan spans lanes with an ordering constraint
        (write the destination before subtracting the source, or a fact exists
        nowhere and nothing greps for an absence); it contains editorial
        rejections a user must overrule per item; and disposition re-judgment is
        a judgment call on the least stable quantity in the chain.
        SCOPE AS SHIPPED: the resolution over persisted reports. The pure
        chain-audit face (extracting facts from written prose with no reports) is
        the harder judgment problem and is not in this lane's contract -- a run
        with no reports and no documents reports INPUTS-INCOMPLETE rather than
        improvising one. The phase is OPT-IN: it is not wired as an implied first
        phase of tree-scale generation, and it changes nothing about CV-3,
        sibling reach, or reachability.
        ONE DESIGN TENSION RESOLVED HERE. The coverage candidate record gains
        `scope` and `sibling_overlap`, and both are OPTIONAL. Requiring coverage
        to emit `scope` would make it read a parent or a sibling -- a read outside
        its own subject, and the very judgment finding 2 above assigns to this
        lane. The fields exist so a judgment a CALLER already made survives into
        the persisted report; hierarchy makes the judgment itself when they are
        absent.

        AMENDMENT -- this record's PREMISE is superseded by
        `the_subject_is_one_directory_not_a_subtree`. Read that record first.
        Both justifications for a tree-scale unit rested on the coverage subject
        being a SUBTREE: sibling blindness (a subtree cannot see its siblings)
        and depth-invisible-per-leaf (a subtree cannot judge whether a fact
        belongs at its parent). Under the settled model a parent's composition
        reads every child CLAUDE.md directly, so the parent SEES both reporters
        and makes the depth judgment itself, with the documents in hand rather
        than from one-line proposals. Both properties this lane existed to
        supply are now supplied by the pass that consumes them.
        The `scope` / `sibling_overlap` fields are RETIRED (read-only
        compatibility surface); the criterion that licensed a candidate to name
        an ancestor destination is rewritten as
        `fact-scoped-to-this-directory`, which forbids it. Nothing may
        reintroduce nomination from below.
        What is NOT overturned: the computed-verdict discipline and the
        report-only entry point are sound and worth preserving wherever the
        equivalent judgments now live -- an affirmative verdict must still be
        computed from an inventory the pass builds itself rather than asserted,
        and a plan spanning an ordering constraint must still be presented rather
        than applied.
        STATUS: RETIRED 2026-08-17, by owner decision after reviewing the evidence
        above. Deleted: the lane record, the dispatch row, the greeting entry, the
        argument grammar (`hierarchy` verb, `--reports`), the `claude_md_tree`
        composition in audit-framework.yaml, both reference documents
        (hierarchy-standards.md, hierarchy-lane.md), scripts/discover_hierarchy.py,
        workflow/hierarchy-detect.js, and tests/skills-kit/test_hierarchy_lane_contract.py.
        The earlier reading -- "retiring it versus leaving it dormant is a cosmetic
        call that gates nothing" -- was overtaken by a front-door rewrite. Once the
        greeting is rebuilt around the surviving verbs, a lane whose criteria
        describe a retired model stops being dormant and becomes reachable, so the
        hazard this record already named (a caller getting a confident answer built
        on retired assumptions) turns live rather than latent.
        CARRIED OUT BEFORE THE DELETE, and not to be lost again:
          - `shallowest-true-depth` (HR-2), the one criterion hierarchy-standards.md
            itself said "remains sound IN SUBSTANCE", is re-homed as
            `shallowest_true_depth` in references/cohesion-principles.md under
            `principles_applied_to_placement`, carrying the wording test and the
            never-nominated-from-below rule. generation-lane.md's parent-composition
            step cites it there; it previously cited hierarchy-standards.md:78-92,
            which would have become a dangling reference.
          - The computed-verdict discipline and the report-only entry point are
            sound independent of the retired model. They live on in the coverage
            lane (DISCOVERY-FAILED computed from a self-built inventory) and must
            not be regressed.
        NOT deleted: `walk_tree`. It lives in discover_coverage.py and
        discover_composition.py still consumes it; only discover_hierarchy.py's
        consumption went away. The two test classes that exercised it through the
        hierarchy module were re-pointed at discover_coverage.py rather than dropped.
      origin: |
        Surface: tree-scale CLAUDE.md work was being done by hand-rolled merge
        plans, with the depth and de-duplication judgments living in a
        caller-side brief rather than in any lane. Finding: the two candidate
        analyses (merge/promotion resolution over proposals, and chain
        de-duplication over written documents) are ONE analysis, because a
        written fact is a candidate whose current location is its proposed
        destination -- and a real tree mid-work is mixed. Follow-up: the criteria
        seam (hierarchy-detect.js refuses without refs.criteria) and the
        computed-verdict refusal above.
      added: "2026-08-10"
    - id: the_subject_is_one_directory_not_a_subtree
      keywords: [non-recursive subject, direct code files, one directory, not a subtree, child claude.md as input, hoisting at the parent, no nomination from below, bottom-up order, regenerating commits to descendants, vcs ignore exclusion, task folder claude.md, scope in the sentence, fact-scoped-to-this-directory, promotion retired, unit mismatch]
      summary: A coverage subject is ONE DIRECTORY'S OWN DIRECT code files, never a subtree. A parent gets its content by reading its children's finished CLAUDE.md files and hoisting what a wording test licenses at its depth -- placement is never nominated from below. This SUPERSEDES the promotion machinery (`scope`, `sibling_overlap`, an assessment naming an ancestor destination).
      detail: |
        THE DEFECT THAT FORCED IT. Three components each defined "a code
        directory" differently, and nothing reconciled them: coverage's subject
        was a whole subtree RECURSIVELY (`discover_coverage.py <dir>` on a
        directory with 4 direct files reported 125); the hierarchy lane's leaf
        was a directory DIRECTLY holding code; and a campaign's hand-kept ledger
        used a third definition. So a directory holding code AND code-bearing
        descendants could not produce a per-leaf report without re-reading its
        subtree and duplicating every descendant's facts -- and any
        de-duplication downstream then compared facts against copies of
        themselves.

        THE RESOLUTION IS A NEW MODEL, NOT A PATCH TO EITHER UNIT. Both
        candidate patches were rejected. Narrowing the leaf definition left
        coverage still recursive. Making the subject non-recursive ALONE loses
        content, because a parent's own scope then has no source. The owner's
        model supplies the missing input: composing D reads D's direct code PLUS
        every child CLAUDE.md. That second input is what makes the non-recursive
        subject lossless rather than merely narrower, and it is the load-bearing
        part -- omit it and the model is strictly worse than what it replaced.

        WHY PLACEMENT MOVED TO THE PARENT. The retired criterion invited an
        assessment to name a destination above itself, which it cannot justify:
        it read only its own directory, so it cannot know whether the fact holds
        of code it never opened. Hoisting happens where the evidence is -- the
        parent, comparing documents it has actually read. A fact found in a
        child's document hoists when the wording test licenses it at the
        parent's depth, whether one child stated it or many. This also
        removes an inference the resolver was making from one-line facts without
        access to the evidence behind them.

        WORDING IS THE ONLY TEST; THERE IS NO SEPARATE REPETITION TRIGGER. This
        record used to gate a hoist on repetition first and treat wording as a
        second check. That trigger was dropped by owner decision on 2026-08-12,
        because this same record already conceded the gap it left open: "a fact
        true of every child that only one child noticed never triggers at all"
        -- and that concession is the evidence the decision acts on, not a new
        observation. The failure direction the old trigger guarded against is
        still real and still governs the test: a fact in 2 of 20 children
        hoisted verbatim becomes ambient for 18 directories it does not govern.
        The resolution is that a hoisted fact must be WORDED so it is true as
        stated at its new depth, usually by naming its subjects ("Tools and
        stack-traces both ..."). Scope lives in the sentence, not in a separate
        mechanism. The escape clause -- it stays in the children when no such
        wording exists short of a list of exceptions -- protects the observed
        case of a mirrored-directory rule whose honest wording at the parent
        would enumerate fifteen exclusions.

        ORDERING IS A HARD DEPENDENCY, WITH TWO CONSEQUENCES THAT MUST BE STATED.
        Bottom-up: regenerating D commits to regenerating every descendant first.
        So a root regeneration is a whole-corpus operation -- there is no cheap
        root refresh -- and a STALE child document silently corrupts its parent.

        EXCLUSION IS BY THE PROJECT'S VCS, and it is not cosmetic. Reading every
        child CLAUDE.md unconditionally otherwise ingests documents that are not
        directory guidance at all: a task folder's CLAUDE.md is about a piece of
        WORK. Detecting what the VCS ignores answers that mechanically instead of
        asking, which is the preferred shape for an observable fact.

        COMPATIBILITY. `destination` is pinned to the subject directory --
        degenerate, kept so reports written before this model stay loadable.
        `scope` and `sibling_overlap` are read-only for the same reason and must
        not be emitted or reintroduced under another name.
      origin: |
        Surface: applying the coverage -> generation chain to a real corpus, a
        hierarchy run could not satisfy its own input inventory for any directory
        holding both code and code-bearing descendants. Finding: the two lanes
        disagreed about what a directory is, and the ledger supplied a third
        definition; the recursive subject was the root cause rather than the
        inventory check. Follow-up: `applies_to: code_subtree` and the lane id
        `coverage_code_subtree` are now imprecise names for a
        single-directory subject -- the rename is deliberately NOT bundled here,
        because it touches the dispatch table, the framework registry, the lane
        records and their tests, and a partial rename is the worse failure. It is
        recorded as its own change.
      added: "2026-08-11"
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
