# Plan

## Accomplished

- Measured the premise before building: a full-fidelity tree rewrite costs
  +4.8% (tree notation tokenises worse than markdown tables). The saving comes
  from partitioning rationale out and from ordered elimination making the
  "escalate when" column redundant -- not from the notation.
- Established the two-file design source: `lexicon.md` (controlled vocabulary,
  per-term test, `[skill]`/`[concept]`, `render` flag) and `tier-principles.md`
  (criteria stated in those terms, plus an evidence-gap ledger).
- Five clean-room derivations, each by a sub-agent that had read ONLY the two
  documents. Defects per round: 9, 3, 4, then missed instances of
  already-solved problems. Round five's verdict was "build the renderer".
- Validated the Codex ladder by real dispatch: `luna`/`terra`/`sol` are not
  dispatchable; `gpt-5.6-*` are. Shipped as awesome-kit 0.21.1 -- a live bug on
  master that had survived because the policy is prose nobody executes.
- Built the reshape (schema 2 + renderer rewrite + 523 tests), reviewed it with
  `/git-code-review`, fixed four confirmed defects plus one the smoke test
  caught, shipped as awesome-kit 0.22.0 (`23ef7f3`).

- **`drift-guard` built** (`6ff549a`, pushed to dev). `scripts/check_orchestration_drift.py`,
  chained from `scripts/pre-commit-version-check.sh`. Fingerprints the
  canonicalized decision-half YAML SUBTREE (sha256), NOT the render -- the
  render is machine-dependent (Codex ladder present or absent) and, worse,
  `render_decision_tree` consumes `backends[].name` from the MACHINE half, so a
  render-derived fingerprint would move on a machine-half rename. Subtree
  hashing is machine- and machine-half-independent by construction. Baseline:
  `docs/planning/orchestrate-2.0/decision-fingerprint.txt`, excluded from what
  counts as a principles change (or regenerating it would satisfy its own gate).
  Update path: `uv run python scripts/check_orchestration_drift.py --update`,
  named in the failure message. 544 tests pass (was 523).
  **Independently verified, not taken on report:** neutered `check()` to
  `return []` and confirmed all three positive controls fail, then restored.
- **`skill-md-partition` applied** (`20fb227`, pushed to dev). SKILL.md 2,629 -> 1,695
  tokens measured (-35.5%), paid on every invocation. Rationale moved to a new
  `references/why-delegate.md` (823 tokens, on-demand). Verified independently:
  5 anti-pattern records moved WHOLE with all 10 required rationale fields
  intact, reference cited from SKILL.md, mechanical audit COMPLIANT.

- **`machine-half-prose` applied** (`4a3c1ae`, pushed to dev). Machine half 1,205 -> 799
  tokens with Codex (-406), 444 -> 344 without (-100). Fell SHORT of the ~500
  target and was reported as such rather than padded: after removing the two
  genuine duplicates (`codex exec` rule, stdin rule -- both already stated in
  `dispatch` at the point of composing the launch) and compressing
  justification clauses, what remains is dense operational fact. Verified
  independently: all nine protected facts survive, "never bare codex" renders
  exactly once, every diff hunk is under `backends:`, drift check exits 0.

Detail for each of these is in `log.md`.

### Measured position (each figure labelled with its basis)

Do NOT sum a per-invocation figure with a per-render one without saying so.

| basis | pre-2.0 | 0.22.0 | now (on dev, unbumped) |
|---|---:|---:|---:|
| whole render, Codex present | 3,204 | 2,547 | **2,141** |
| whole render, Codex absent | 1,861 | 1,447 | **1,347** |
| machine half only, Codex present | 1,205 | 1,205 | **799** |
| machine half only, Codex absent | 444 | 444 | **344** |
| SKILL.md (per invocation) | 2,629 | 2,629 | **1,695** |

Combined agent-visible cost (SKILL.md + whole render, Codex present), which is
the sum the project has already agreed is meaningful because step 3 mandates
the render: 5,833 -> **3,836**, -34.2%. State the two components whenever this
number is used.

## Forward overview

```yaml
task_items:
  items:
    - id: version-bumps-and-publish
      title: "Two version bumps + publish; consumers see nothing until then"
      state: blocked-user
      priority: P1
      note: "committed+pushed to dev as 6ff549a/4a3c1ae/20fb227, but the cache keys on version -- unbumped work is structurally invisible. TWO bumps (user decision), separate bases. /git-code-review was NOT run on these commits."
    - id: promote-design-docs
      title: "Move principles + lexicon into the skill's references/ once code reads them"
      state: available
      priority: P2
      note: "UNBLOCKED: drift-guard now reads docs/planning/orchestrate-2.0/. But the move is now COUPLED -- check_orchestration_drift.py hardcodes that path and the baseline lives there; move both together or the guard silently stops gating"
    - id: codex-rung-evidence
      title: "Get capability evidence for the gpt-5.6-luna / gpt-5.6-sol split"
      state: deferred
      priority: P3
      note: "the ladder is seated on dispatch shape + absence of a counter-case, not measurement; principles section 7"
    - id: fan-out-no-codex-hole
      title: "Decide what a high-fan-out unit does when Codex is absent"
      state: deferred
      priority: P3
      note: "currently disclosed in one clause rather than answered; a gap in the principles, not the renderer"
```

### drift-guard -- fail a check when the tree and the principles disagree

The one-way authorship rule ("change a principle, then re-derive") is currently
a note at the top of a file. That is the same category of guidance as the
top-rung justification gate: it works only if something makes violating it
visible. Today nothing does -- the renderer reads `orchestration.yaml`, and
`docs/planning/orchestrate-2.0/` is read by no code at all.

**DECIDED (user, this session): shape (2), check-do-not-derive.** Shape (1),
derive-at-build-time, is not rejected on merit -- it is deferred until the
schema has stopped moving, because it needs a parseable principles format and
the principles are deliberately prose-with-rationale so a human can audit them.

The two shapes, for the record:

1. **Derive at build time** (DEFERRED). The principles become the input; the
   YAML decision half becomes generated. Disagreement is impossible rather than
   detected.
2. **Check, do not derive** (CHOSEN). Keep both, and fail a pre-commit check
   when the rendered tree's decision half changes without a corresponding
   change under `docs/planning/orchestrate-2.0/`. The repo already does this
   shape for `marketplace.json` drift (`scripts/pre-commit-version-check.sh`).

**The weakness is accepted, not solved:** shape (2) catches "changed without",
not "disagrees with". Say so honestly wherever the check is documented; an
oversold guarantee is worse than a disclosed gap.

**Machine-independence constraint.** The RENDERED output is machine-dependent --
the Codex ladder renders only when `codex` is on PATH -- so a fingerprint taken
over the render is not reproducible across boxes. The baseline must be machine-
independent by construction (canonicalized decision-half YAML subtree) or render
against stubbed backend detection. Whichever is chosen, what it does NOT catch
is part of the deliverable.

### machine-half-prose -- tighten the mechanics section

~1,205 tokens with Codex, 444 without; deliberately untouched by the reshape.
It states the `codex exec`-never-bare-`codex` rule twice, and several gotchas
carry explanation that could compress. Roughly 500 tokens available.

This is prose editing, not a structural change -- no schema work, no renderer
change. Measure before and after with the render commands in CLAUDE.md's
Environment section and report against the whole-render baseline.

### skill-md-partition -- audit the SKILL.md

2,612 tokens, injected on every invocation, never reviewed in this work. The
same test applies as everywhere else: does a line change what the orchestrator
DOES? The economics paragraph and the anti-pattern rationales are candidates;
the step-1 "delegate by context footprint, not difficulty" rule is genuinely
load-bearing and should survive.

Note the interaction: SKILL.md and the rendered policy are two halves of what
the agent sees (5,861 tokens together). Cutting SKILL.md is a bigger relative
win than anything left in the policy half.

**Audited (this session).** 2,629 tokens / 187 lines measured; ~1,730 estimated
after partition. Three findings worth keeping:

- **Schema trap.** `why_it_seems_right` and `why_it_is_wrong` are REQUIRED
  fields in `ANTI_PATTERNS_RULE` (`skills_kit_lib/rule_fragments.py`). Stripping
  the rationale FIELDS -- the obvious reading of "partition the rationale out"
  -- is a contract FAIL. Anti-pattern records move WHOLE or not at all. Safe
  only because the caution floor is an OR and gotchas 1-2 remain.
- **Not standards-mandated.** At 2,629 tokens / 187 lines the file is under both
  hygiene signals (3,000 tokens, 500 lines), so no audit rule flags it. This is
  a per-invocation cost optimisation justified on its own terms. Do not credit
  it to compliance.
- **The premise that looks like rationale.** `policy.reading_it`'s "exhaustive
  by construction" clause reads as justification but is the premise its
  following instruction depends on. Cut it and the instruction becomes an
  arbitrary prohibition an agent rationalises past. Tighten, never remove.

Destination for the partitioned-out rationale: a new
`references/why-delegate.md` (neither existing reference fits its audience).

Open for review: the aggressive variant was taken -- all five anti-pattern
records move. The hedge is to keep `remembered_policy` and
`inline_footprint_work` in SKILL.md, since they guard the two failure modes the
skill exists to prevent; that costs ~324 of the ~629-token saving.
