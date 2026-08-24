# standards decisions

Decision provenance for the per-artifact standards docs in
`references/standards/`. Each record follows the surface / finding /
follow-up convention (plugins/skills-kit/CLAUDE.md, conventions). The
canonical rule text lives in the standards doc it amended; this file records
why the rule was tightened so the decision can be rewound.

```yaml
claude_md:
  _schema_version: "1"
  scope:
    directory: plugins/skills-kit/skills/md-domain/references/standards
    covers:
      - decisions that tightened or amended a standards-doc criterion
    excludes:
      - the criteria themselves (live in the standards docs)
      - framework-vocabulary decisions (skill-authoring-decisions.md)
  insights:
    - id: a3_provenance_path_rule
      keywords: [provenance path, origin field, tmp path, gitignored citation, ephemeral artifact, A-3, run disagreement, idempotency]
      summary: "A-3 amended 2026-08-03: a provenance field citing an untracked path (e.g. tmp/...) is a decisive FIX (drop the path, keep description/date/ids), never an accepted historical pattern."
      detail: |
        Surface: back-to-back full audits of plugins/skills-kit (2026-08-03,
        detect lanes opus/high) disagreed on skills_kit_lib/CLAUDE.md line
        175 -- run 1 classified the gitignored tmp/ citation in an origin:
        field SILENT ("accepted structural pattern"), run 2 FAILed it as a
        broken reference. Finding: both readings were defensible under the
        old A-3 text, so the outcome depended on per-run judgment. Follow-up:
        the criterion now decides it -- cite ephemeral work by description,
        date, and finding ids; a path-form citation to an untracked location
        is a loss-free FIX. Rule text: claude-md-standards.md A-3.
      origin: Adjudicated 2026-08-03 after the run-1/run-2 disagreement; user directed that run disagreements be settled by tightening criteria.
      added: "2026-08-03"
    - id: h9_annotation_ceiling_test
      keywords: [H-9, pointer map, annotation ceiling, embedded documentation, error driver, routing payload, surface map, run disagreement]
      summary: "H-9 amended 2026-08-03: a pointer-map annotation may exceed one line only for constraint/error-driver lines not stated at the target; lines summarizing the target's own content/structure trip H-9 regardless of the map's routing value."
      detail: |
        Surface: the same back-to-back audits disagreed on the
        plugin_surface_overview map in plugins/skills-kit/CLAUDE.md -- run 1
        passed H-9 ("the map is the file's declared routing payload, so it
        earns the annotation"), run 2 FAILed it (multi-line recaps of the
        targets' own layout and config format re-embed deferred
        documentation). Finding: "earns the annotation" was a vibe, not a
        test, so the whole map's fate flipped per run. Follow-up: H-9 now
        carries a per-annotation test -- keep lines stating a constraint or
        agent-error driver absent at the target; collapse lines recapping the
        target's structure. Blanket load-bearing claims are explicitly not an
        exemption. Rule text: claude-md-standards.md H-9.
      origin: Adjudicated 2026-08-03 after the run-1/run-2 disagreement; applied to plugins/skills-kit/CLAUDE.md in the same change.
      added: "2026-08-03"
    - id: count_typing_exact_vs_illustrative
      keywords: [count claim, exact enumeration, illustrative magnitude, P_stale_factual_claim scope, CD-4 narrowed, counted magnitudes, enumerate not comment, stale comment confirms doc]
      summary: "Counts typed 2026-08-07: exact-enumeration counts are contractual and verified by enumerating the code; illustrative magnitudes stay exempt. P_stale_factual_claim's scope grew from classic-only to include code-directory files for exact-enumeration counts."
      detail: |
        Surface: an md-domain audit of a project left "the eleven native
        systems" standing against 19 actual registrations, and "13 tests"
        against 16 definitions, in files it had read and edited. Finding: two
        causes, both structural. (1) Those files were dimension
        code-directory, and P_stale_factual_claim was scoped to "a classic
        (non-code-directory) CLAUDE.md", so the only criterion that could see
        a wrong count was not eligible; the CD dimension that DID apply says
        "counted magnitudes are illustrative, not contractual" (section 3.4)
        and CD-4 says "never FAIL on the number", which correctly exempts
        "a 7200-line god object" but wrongly also exempted a count a reader
        relies on as a complete list. (2) P appeared only in the detect
        lane's taxonomy enum and its step-8 mapping line -- no operative
        instruction ever told a lane to look for count-shaped claims, which
        is why it effectively never fired.
        Follow-up: A-3 now types counts (exact-enumeration vs
        illustrative-magnitude, ambiguity resolved as illustrative) and
        extends P to code-directory files for the exact-enumeration kind
        only, at JUDGMENT severity so nothing new gates; 3.4 and CD-4 are
        narrowed IN PLACE rather than overturned, each naming the exception;
        and claude-md-detect.js gained step 4.5, the missing operative
        instruction. Encoded with it, because it is the part that makes the
        check worthless if forgotten: verification is by ENUMERATING
        registrations/definitions/entries, never against adjacent prose --
        the stale "eleven" was echoed in the source's own comment above the
        registration site, so a fact-check against the nearest human-readable
        text CONFIRMS a wrong doc. Rule text: claude-md-standards.md A-3
        ("Count claims"), section 3.4, CD-4, and the P row in 5.2.
      validation: |
        Measured before publishing, 2026-08-07.
        Regression (flecs-ecs, the corpus the criteria were designed against):
        recall 3/4 on the pre-registered count key with every value derived
        correctly; 6/6 answer-key rows across both criteria; all four negative
        controls silent; the stale-comment trap (a wrong figure echoed in the
        source's own comment) defeated by the enumerate-not-adjacent-prose
        clause. One blocking defect found and fixed: enumeration depth was
        unspecified while the disposition is an APPLIED FIX, so a lane could
        write a NEW wrong number (12 rather than 19) into the doc.
        Precision (a large C#/C++ game corpus, HELD OUT): 4 true positives,
        2 false positives, 6 correct suppressions. Both false positives were in
        the ENUMERATION half and produced the closed-set and unit-ambiguity
        gates now in the rule -- membership decided by template / attribute /
        reflection dispatch is not enumerable, and a count is not wrong if any
        defensible unit makes it right. CD-2b needed no change, but its
        "document RIGHT, code WRONG" framing was corrected: an incomplete
        catalog is an equally valid reading, so both resolutions are reported
        and the choice is a human's.
        ACCEPTED LIMITATION (decided 2026-08-08, not an open item): measured on
        C, Python, C# and C++ only. TypeScript / Rust / Go will not be tested --
        no corpus is at hand, and a gate nobody can satisfy erodes the whole
        discipline rather than raising it. Do not describe these criteria as
        validated across languages, and do not re-file this as pending work.
        The enumeration half assumes a greppable closed registration site; it
        broke twice already (C++ trait dispatch, C# partial classes), and the
        closed-set / unit-ambiguity gates are the general defence. A future
        false positive from an unmeasured idiom is a bug report against those
        gates -- widen the unverifiable category, never guess a number.
      origin: Diagnosis of a clean audit coexisting with drifted counts, 2026-08-07 (mechanism M4 of that investigation); both independent reviewers converged on the same type distinction.
      added: "2026-08-07"
    - id: cd2b_invariant_violated_by_code
      keywords: [CD-2b, H2_inverted_absence extended, stated invariant, doc right code wrong, SERIOUS not FAIL, reported never gated, verbatim quote gate, remediation owner code author]
      summary: "CD-2b added 2026-08-07: an invariant the corpus states, violated by cited code, is reported under H2_inverted_absence as SERIOUS at JUDGMENT severity -- above the verdict, never gating, remediation owned by the code author."
      detail: |
        Surface: a root CLAUDE.md stating "No silent fallbacks" sat beside a
        registration function that silently no-ops on an unknown name, and a
        doc stating "Comparability is never assumed" sat beside code failing
        OPEN on missing comparability. Both passed. Finding: H-11 checks a
        subject doc against an ancestor's declared convention (doc vs doc);
        nothing checked CODE against an invariant the corpus states. The
        class was already named -- H2_inverted_absence's remediation says
        "the fix is in the code/repo, not the CLAUDE.md" -- but only for a
        requires-absent ANCHOR, not for a stated invariant.
        Follow-up: extend H2 rather than add a rule-id family. The
        specification the first review of this idea lacked is now explicit:
        subject (a stated invariant vs the code it governs); severity
        JUDGMENT with disposition SERIOUS, never FIX; remediation owner the
        code author, never a doc edit; and two gates -- the invariant must be
        quotable VERBATIM from the subject or a supplied ancestor (H-11
        posture) and the violation must be demonstrable at a cited code
        location. The verdict question is the load-bearing one: a correct doc
        beside a violated invariant has no document defect, so marking it
        NON-COMPLIANT would be false, but dropping the finding loses the most
        important thing on the page. Resolved with the EXISTING precedent
        rather than new vocabulary -- SERIOUS findings are reported above the
        verdict and survive review mode's attributability filter -- giving
        the rule "reported, never gated". Rule text: claude-md-standards.md
        CD-2b (section 3.6), the H2 rows in 3.6 and 5.2, and the
        reported-but-not-gated note in 5.3; operative instruction at
        claude-md-detect.js step 6.5.
      origin: Diagnosis of a clean audit coexisting with code-violated invariants, 2026-08-07 (mechanism M5 of that investigation); the earlier version of this proposal was rejected for leaving subject, severity, verdict interaction, and remediation owner undefined.
      added: "2026-08-07"
    - id: coverage_criteria_desk_validated_precision_unmeasured
      keywords: [coverage criteria, negative controls, validation, held-out corpus, precision unmeasured, accepted limitation, judgment enforcement, MB_HAZARD_CAP]
      summary: The eight coverage criteria satisfy all four of coverage-gap.md's negative controls BY CONSTRUCTION -- each control maps to a fail-severity criterion -- but empirical precision was NOT measured, because no held-out corpus exists. Shipped with the limitation recorded, per the documented rule.
      detail: |
        Control-to-criterion map, each verified by reading the criterion text
        against the control text (coverage-gap.md:173-188):
        (1) a correctly non-ambient fact must not be relocated ->
            no-cross-apply-placement, whose example IS the control's seed-variable
            case: the destination must be an ancestor of every file the fact
            governs AND of no file it does not, so a sibling placement is refused.
        (2) already-ambient facts must not be re-proposed ->
            already-ambient-suppressed, stated absolutely, trigger sites included.
        (3) good prose must not be flagged as low-value ->
            present-content-not-re-audited. Also structural: coverage proposes
            only ABSENT facts and has no verdict capable of flagging present prose.
        (4) the documented near miss must stay silent -> loud-failure-excluded,
            which keys on whether the failure is SILENT (documented AND loud AND
            test-enforced), not on the shape of the construct -- the exact
            inversion the control exists to catch. MB_HAZARD_CAP / MB_MAX_CHARS
            is excluded TWICE, since being documented in the ambient chain also
            trips already-ambient-suppressed. Redundancy on the sharpest control
            is deliberate.
        WHAT THIS DOES NOT ESTABLISH, and the reason to write it down. Every
        criterion is enforcement: judgment, so a passed desk control shows the
        criterion TEXT would direct a correct assessor to reject. It does not
        show that a model applying the text does. The controls were also read
        while authoring the criteria, so passing them is close to recall against
        the deriving corpus -- which the standing rule says means nothing alone.
        WHY IT SHIPPED ANYWAY. Precision on a corpus the criteria were NOT
        designed against was not measured because no such corpus is available:
        flecs-ecs is simultaneously the corpus the criteria were derived from and
        under a standing do-not-touch. The documented rule for exactly this case
        is SHIP and record the limitation rather than block (coverage-gap.md,
        "Scope of the rule"). This entry is that record. The first coverage run
        on any unfamiliar subtree is the real first measurement, and should be
        read as such rather than as a routine result.
      origin: |
        Authoring the coverage criteria, 2026-08-08. Surface: the criteria item
        required validation against the four negative controls before shipping.
        Finding: all four are satisfied by construction, and no held-out corpus
        exists to measure precision. Follow-up: treat the first run on an
        unfamiliar subtree as the measurement, and revisit if it produces
        candidates any of the four controls should have suppressed.
      added: "2026-08-08"
    - id: sr_criteria_skill_reference_subject
      keywords: [SR-1, SR-2, SR-3, SR-4, skill reference, section 10, second subject shape, claim carve-out retired, held-out precision, scope guard, O_broken_inbound_anchor, P_internal_contradiction, Q_overstated_claim, R_maintainer_only_material]
      summary: "skill-standards.md gained section 10 (2026-08-09): the audit_skill lane's SECOND subject shape, a skill's references/*.md, with four prose criteria. Shipped in the same change that dropped the code-review kits' `!**/skills/*/references/*.md` claim exclusion."
      detail: |
        Surface: no md-domain lane read a skill reference document's prose.
        audit_skill audited the owning SKILL.md's contract and load graph;
        audit_project_doc's PD-1 declined anything inside a skills tree and
        returned NOT-AUDITED. Both code-review kits therefore carved the shape
        out of their claim globs so it fell back to the generic reviewers -- a
        placeholder, with the condition written into the kits' reference doc:
        drop the exclusion in the SAME change that ships real criteria.
        Finding: an opus generic reviewer on one changed reference document
        (2026-07-28) found five defects no lane could reach -- a renamed heading
        that broke six citing files, a self-contradicting paragraph, an
        overstated claim, temporal deixis, non-ASCII lines. Only the first three
        needed new criteria: temporal deixis and non-ASCII are already covered
        by the H-11 ancestor-convention check and the classifier's built-in
        universal-convention FIX, which run on any subject the lane audits, so
        section 10.2 NAMES them rather than restating them (summarize-and-
        reference). SR-4 (reader fit) was added beyond the five: it is the one
        criterion that reaches the root CLAUDE.md's OP-1 concern about
        maintainer-only material on a published plugin surface, which that
        insight had recorded as unreachable precisely BECAUSE of the carve-out.
        WHAT THE CRITERIA DO NOT COVER, deliberately: the SKILL.md contract rows
        (no frontmatter, no type, no schema on a reference), CCP/CRP placement
        (the L2->L3 split belongs to the owning SKILL.md's audit, which sees both
        sides), reachability (owned by the SKILL.md subject), and hygiene
        thresholds (a reference being long is the point of L3). Enumerated in
        10.1 so the next reader does not add them back.
        HELD-OUT MEASUREMENT, and it changed the rules twice. Two precision runs
        on seven reference documents from four plugins the criteria were NOT
        derived against (unreal-kit, hue-kit, content-pipeline-kit,
        awesome-kit), each run by a fresh assessor reading only section 10.
        Run 1: 9 findings, 7 false positives (~22% precision), with three
        distinct causes -- SR-1 fired on informal prose pointers that resolved
        unambiguously (4 of its 6 findings); SR-3 fired on a principles
        document's core structural device, where the Principle / Why /
        Embodied-by genre is neither a claim nor an instruction and so fell
        through a binary guard; SR-4's Rule sentence read wider than its Test,
        so an assessor anchoring on the Rule would flag guidance about the
        DESCRIBED SYSTEM as maintainer-only. All three were fixed in the
        criterion text: SR-1 now grades only anchor links and verbatim heading
        quotes as FAIL and names the unambiguous prose pointer as explicitly not
        a violation; SR-3 has a three-genre guard admitting only claims; SR-4's
        Rule is bounded to the document's own production pipeline.
        Run 2 (post-fix, four documents, one of them new): SR-1 zero findings,
        SR-4 zero findings, SR-3's principles document went from a systematic
        misfire surface to zero, and SR-2 produced the run's one TRUE POSITIVE
        -- a real self-contradiction four lines apart in
        unreal-kit script-execution.md about whether every .py in
        Content/Python/ auto-runs or only init_unreal.py. Residual: 3 SR-3
        findings the assessor itself judged as ones a maintainer would not act
        on, which produced the CONSEQUENCE BAR (raise only when a reader
        believing the claim as stated could act wrongly). That third fix is
        NOT itself measured -- iteration stopped at two rounds.
        LIMITATIONS, recorded rather than left as pending actions.
        (a) SR-1 tests RESOLVABILITY, not APTNESS: run 2 found a citation
        pointing at a section that exists but is the wrong one for the capability
        citing it, and SR-1 is blind to it by construction. Judging aptness is a
        different and much noisier criterion; not attempted.
        (b) Every criterion is enforcement: judgment. A passed desk control shows
        the criterion TEXT directs a correct assessor; it does not show the
        production lane, at its own model and effort pin, applies it the same way.
        (c) The corpus is this repo. It is held out from the DERIVATION (a
        different plugin, a different author, documents chosen after the criteria
        were written) but it is not an unfamiliar codebase, and all seven
        documents share this repo's conventions.
        (d) SR-2's "quote both passages" bar is the only mechanically checkable
        guard in the set, and it is the one that demonstrably worked in both runs
        -- it suppressed four one-sided suspicions and admitted the one true
        positive. Prefer that shape when adding a fifth criterion.
      origin: |
        2026-08-09, task item `skill-references-lane`. Surface: the 2026-07-28
        routing gap left direction 2 (real criteria) unbuilt while directions 1
        (the carve-out) and 3 (the NOT-AUDITED verdict) shipped, so the claim
        exclusion was a standing placeholder. Finding: the two held-out precision
        runs above, which rewrote three of the four criteria before they shipped.
        Follow-up: SR-1's aptness blindness and the unmeasured consequence bar are
        limitations (a) and above, not action items.
      added: "2026-08-09"
```
