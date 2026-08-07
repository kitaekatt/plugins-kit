# Plan

## Accomplished

- 2026-08-07: Ran the full `skills-kit:md-domain` audit over flecs-ecs (4 lanes,
  109 files, 229 findings applied, 7 commits `16f373b`..`d2073ee`). Result was clean by
  the skill's own criteria.
- 2026-08-07: Commissioned two independent code-review-coverage assessments of the SAME
  corpus, both briefed to use md-domain's standards as a lens but NOT to run its lane
  pipeline. Assessment A (`report-opus.md`) complete; assessment B (`report-sol.md`)
  dispatched to gpt-5.6-sol at max effort.
- 2026-08-07: Triage COMPLETE (4 parallel verification units, read-only against
  flecs-ecs). report-opus 20 VALID / 4 PARTIAL / 1 REJECTED; report-sol 9 VALID /
  3 PARTIAL / 0 REJECTED. Disagreements settled from source: B right on
  `engine/src/` and on the `engine/tests/` narrowing, A right on the tracked-fixture
  deletion, and B's unique findings are a sampling gap in A rather than a blind spot.
- 2026-08-07: Causal analysis COMPLETE -- six named mechanisms (M1 absent-content
  invisibility, M2 classic/code-directory split, M3 no ambient-coverage rule,
  M4 "true in kind" excludes counts + stale comments defeat the fact-check,
  M5 code-violates-stated-invariant unrepresentable, M6 value filter SILENT on
  historical records). Conceptual gap stated. All in `findings.md`.
- 2026-08-07: Established the premise this task investigates -- assessment A found a
  substantial set of code-review-relevant deficiencies that the clean audit did not
  surface, and explicitly confirmed the residual problem is NOT anchor staleness (it
  re-resolved ~40 anchors and found no rot).

## Forward overview

```yaml
task_items:
  items:
    - id: prototype-coverage-lane
      title: "Implement the opt-in coverage lane per coverage-lane-spec.md"
      state: available
      priority: P2
      note: >-
        Spec is written and reviewed (coverage-lane-spec.md); implementation is
        not started. Unit = (code subtree, ambient ancestor chain); verdict
        GAPS-FOUND / COVERAGE-ASSESSED, never touching document COMPLIANT;
        routes candidates to code-fix / test-enforcement BEFORE documentation.
        Gate before shipping: precision on a held-out corpus + the N4 near-miss
        control (MB_HAZARD_CAP / MB_MAX_CHARS).
    - id: validate-other-language-families
      title: "Precision-test the count/CD-2b criteria on TypeScript, Rust or Go"
      state: blocked-user
      priority: P2
      note: >-
        BLOCKED: no TS/Rust/Go repo with >=2 CLAUDE.md files exists on this
        machine (scanned D:/dev). The C#/C++ run found 2 false positives and
        produced 2 new gates, so this is not hypothetical -- each new language
        family has found real overfitting. Needs the user to name a corpus.
    - id: flecs-code-fixes
      title: "Fix the flecs-ecs defects that documentation can only describe"
      state: available
      priority: P3
      note: >-
        The doc pass recorded these as defect records naming the remedy rather
        than as designs to preserve. Strongest candidate: test_rest.sh:46-47
        (two-line scope change removes the tracked-fixture deletion entirely).
        Also archive.py fail-open, native_registry_register silence, and the
        127-coord / MAX_UNITS / matched[1024] silent truncations. Separate repo,
        needs its own authorization.
    - id: follow-up-two-unassigned-findings
      title: "bridge_named_config indentation coupling + tools/lib/api.py YAML-subset coupling"
      state: available
      priority: P3
      note: "Verified-adjacent during triage but outside the assigned 13; not documented."
```

## Working hypotheses (to confirm or kill, not to assume)

Recorded so the analysis has something falsifiable to push against. Each is a GUESS.

1. **The audit reads docs, not code.** Its criteria check that an anchor RESOLVES and
   that a stated claim HOLDS. Both are doc-anchored: they can only evaluate assertions
   the doc already makes. Nothing in the pipeline asks "what does this code do that the
   doc never mentions?" -- so an undocumented hazard is invisible by construction, and
   silence always passes.
2. **CD-5 is a one-way filter.** The value filter asks whether present content EARNS its
   ambient cost. There appears to be no converse criterion asking whether absent content
   should be there. That asymmetry would explain a clean audit over a doc that is
   accurate about everything it happens to mention.
3. **"Sufficient for code review" is never the question asked.** The lane's verdicts are
   COMPLIANT / NON-COMPLIANT against per-criterion rules. A doc can satisfy every
   criterion and still not equip a reviewer, because fitness-for-review is not among them.
4. **The corpus is treated as the subject; the code is only a fact-check oracle.** Lanes
   open source to verify claims, never to enumerate hazards. That is a scoping decision
   in the lane, not an oversight in a criterion.

If (1) and (2) hold, the gap is structural rather than a missing rule, and the proposed
fix has to add a code-derived input to the lane -- which is exactly where the anti-bloat
objection bites hardest.

## Where the two assessments disagree (pre-triage, unverified)

Both reports are COMPLETE. Both reach the same verdict -- coverage is NOT sufficient for
code review -- and both independently condemn the monkey-baiting concentration as review
context and the port journals as excess. The disagreements below are the reason both were
run; they are recorded here as INPUT to `reconcile-report-disagreements`, not as settled.

1. **`engine/src/` as a CLAUDE.md candidate -- direct contradiction.** A rejected it
   ("`main.c:103`, `document_root`, and the two-place native-system registration are all
   already correctly anchored elsewhere"). B validated it, on the structural ground that
   `main.c` / `native_registry.c` / `platform.h` "do not inherit either sibling CLAUDE".
   Both cannot be right. B's argument is about the LOAD GRAPH (which file is ambient for
   those three sources), A's is about whether the facts exist SOMEWHERE. That distinction
   may itself be the conceptual gap this task is chasing.

2. **`engine/tests/` scope -- B narrows A.** A called it the single strongest finding and
   proposed a broad file. B validated it only NARROWLY and rejected several of A's
   proposed contents as already-ambient duplicates: the test macros are at root
   CLAUDE.md:48, the `ecs_iter_fini` rule at root:115, and `FLECS_SEED` is an avk-specific
   contract (avk/CLAUDE.md:40) that does not appear under `engine/tests` at all. Checkable,
   and if B is right it is a caution against the obvious fix.

3. **The tracked-fixture deletion.** A's #1 finding -- `engine/tests/test_rest.sh:47-48`
   does `rm -rf ./sandboxes/clean` over two git-tracked files, which is visibly why the
   working tree shows those deletions. B did not mention it. Verify independently; a miss
   by one reviewer is not evidence against it.

4. **Findings unique to B, none contradicted by A.** REST request-thread ownership (four
   civetweb threads vs `rest_select_world` rewriting the shared `engine->world`); an
   undocumented 127-coordinate ceiling in WebSocket matrix spawning (`128*128` arrays,
   1-based coords); the avk WASM bridge silently truncating unit export at 65,536 while
   the largest config is 47,943; console REST-vs-direct `GameState` parity being FALSE
   (`took_damage` never parsed in REST mode); matrix sprite tinting contradicting the
   parent's white-source-art requirement; `tools/lib/api.py`'s bespoke YAML-subset
   coupling to the REST layer.

5. **B found drift that SURVIVED the audit.** `sandboxes/monkey-baiting/CLAUDE.md:149`
   says `mb_engine.c` hosts "the eleven native systems" against 12 match + 7 campaign
   registrations; :1399 says `test_mb_wasm` has 13 tests against 16 in `main`. If these
   hold, they are the most direct evidence available for `diagnose-why-audit-missed`:
   count-shaped claims about code, in a file the audit read and edited, left wrong.

Not an open item -- recorded only so a re-dispatch is not tripped up by it. The shipped
`awesome-kit:orchestrate` `defaults/orchestration.yaml` names the Codex tiers `sol` and
`terra`, which Codex rejects; the real ids are `gpt-5.6-sol` and `gpt-5.6-terra`. **That
bug is being fixed independently -- do not raise it, and do not treat it as work here.**
Until the fix lands in the installed plugin version, use the full ids when re-dispatching
(the recipe in `report-sol.md` already does).
