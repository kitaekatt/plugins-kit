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
        Precision (spiritcrossing, HELD OUT, C#/C++): 4 true positives,
        2 false positives, 6 correct suppressions. Both false positives were in
        the ENUMERATION half and produced the closed-set and unit-ambiguity
        gates now in the rule -- membership decided by template / attribute /
        reflection dispatch is not enumerable, and a count is not wrong if any
        defensible unit makes it right. CD-2b needed no change, but its
        "document RIGHT, code WRONG" framing was corrected: an incomplete
        catalog is an equally valid reading, so both resolutions are reported
        and the choice is a human's.
        RESIDUAL GAP: no TypeScript, Rust, or Go corpus was available on this
        machine. Generality across dynamic-typed-web, ownership-model, and
        explicit-error-return families is UNVALIDATED. Do not describe these
        criteria as validated across languages.
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
```
