# Log

## 2026-08-07 -- the premise was measured before it was built

The original proposal was "replace the prose policy with a decision tree, it
will be clearer and cheaper". A full-fidelity draft measured **130 characters
shorter and 155 tokens longer** (+4.8%). Tree notation -- indentation, arrows,
short ragged lines -- tokenises worse than markdown tables, which are the
densest structure available. The controlled case was the Codex capability
section: -8 characters, +38 tokens, from re-wrapping alone.

Would re-bite: the saving does NOT come from the notation. It comes from
(a) moving rationale to an unrendered file and (b) ordered elimination making
the "escalate when" column redundant, because "escalate when X" is just the
next question in sequence. A future revision that keeps the tree but restores
per-rung rationale gives the saving straight back.

## 2026-08-07 -- clean-room derivation as the test

An author cannot test their own specification: they fill gaps from memory
without noticing. Every derivation round was run by a sub-agent given ONLY
`lexicon.md` and `tier-principles.md`, explicitly barred from reading any prior
draft, and asked to report every place the documents failed to determine an
answer.

Defects per round: 9, 3, 4, then only missed instances of already-solved
problems. That last signature is what "converged" looked like -- the remaining
bugs were the same defect in a third location, not new defects.

Findings worth keeping:

- **The three-shape taxonomy was a category error.** trivial / specification /
  question mixed two axes -- "trivial" was about statement sufficiency, the
  other two about whether specification is possible. Units satisfied two
  categories at once and no rung accepted "trivial". Fixed by splitting into
  two independent axes (`known`/`open` x `sufficient`). A revision that
  reintroduces a third shape category is reintroducing this.
- **"Take the lower rung" is unimplementable under ordered elimination** -- it
  presumes comparing two rungs elimination never both reaches. Deleted; correct
  rung ORDER encodes the same intent. The same defect then reappeared twice
  more in different guises (the `cross-check` hedge, and "raise luna's effort
  before reaching for sol"). Watch for it: any rule phrased as "before reaching
  for X" cannot execute on a first-match ladder.
- **Vocabulary discipline surfaced a defect that shipped.** `sol`'s criterion
  was "hard, ambiguous, long-horizon work" -- none of which is a vocabulary
  term, and `difficult` is an explicit anti-term the Claude ladder's own guards
  forbid one rung over. It survived four rounds because nothing before the
  lexicon was strict enough to catch it.

## 2026-08-07 -- the Codex model ids never worked

`codex exec -m terra` fails: `Model metadata for 'terra' not found`, then HTTP
400 "not supported when using Codex with a ChatGPT account". Same for `luna`
and `sol`. The qualified `gpt-5.6-*` forms work, produce no warning, and write
the `-o` file.

The shipped policy had carried `model: terra` / `model: sol` while instructing
the reader to select the model with `-m`. Following it verbatim failed every
time, for as long as the ladder had existed.

Would re-bite: **the policy is prose nobody executes.** Any change to a model
identifier must be validated by an actual dispatch, not by inspection. Recorded
as principle P3.0a.

One honest limit on that test: the three models self-reported "GPT-5", "GPT-5"
and "GPT-5.4". Models are unreliable self-identifiers, so the dispatch proves
the identifier is ACCEPTED and nothing more.

## 2026-08-07 -- what the code review caught that a green suite did not

512 tests passed over four real defects. `/git-code-review` confirmed 4 of 5
candidates (the rejected one was a maintenance preference, correctly filtered).

The serious one: `rung_criteria` dropped unresolvable term ids INDIVIDUALLY,
so a disabled or misspelled lexicon term turned an AND group into a strictly
WIDER test -- `novel + load-bearing + unverifiable` degrading to "`open` work
only", silently widening the gate on the most expensive rung. Reachable by an
ordinary user override (disable a term by id). Now fails closed at group
granularity, with a non-terminal rung raising when nothing survives.

**The first fix for it was also wrong**, and a smoke test caught it: after the
group was dropped, `shape` stood in as the whole test, so the top rung matched
every `open` unit. Same widening, different door. `shape` is a NARROWING clause
on a criteria match and no longer stands alone.

Would re-bite: this is the second time in this work that executing the thing
beat reading it (the first was the model ids). Unit tests were written for the
fail-closed behaviour and passed while the bug was still live, because the test
used a rung without a `shape`.

Also fixed: `render_scope: principles-only` was silently ignored on two of five
blocks; a schema-1 override merged cleanly and contributed nothing with no
warning on any path; and a test probed unexpanded `{term}` braces the renderer
always rewrites, so it could never fail.

## 2026-08-07 -- a number was published overstated

The README shipped "~1,550 with Codex and ~1,170 without, against 3,204 /
1,861". That compares a DECISION-HALF measurement against a WHOLE-RENDER
baseline and overstates the saving by more than double. The machine half is
1,205 tokens with Codex / 444 without and was deliberately out of scope, so the
1,550 total was arithmetically impossible from the outset.

Corrected in 0.22.0. Honest figures: whole render 3,204 -> 2,547 (-20.5%) and
1,861 -> 1,447 (-22.2%); decision half 1,635 -> 1,342 and 1,289 -> 1,003.

Would re-bite: the two halves are measured separately throughout this work, and
mixing them is easy because both are "the policy". State which is which.

## 2026-08-07 -- deleting a rung is not a local edit

Removing the haiku rung left behind: a `render: required` guard attached to a
rung that no longer existed, a `[skill]` lexicon term (`mechanical`) that
selected nothing, and a sonnet description naming that term in text that could
not render. The derivation surfaced these one layer down as "the principles did
not determine this" rather than as visible contradictions.

Would re-bite: a rung's criteria, guards, render tags, vocabulary terms and
overturn condition are all dependent text. Sweep them in the same change.

## 2026-08-07 -- decisions deliberately taken, with their reasons

- **Cost is not the driver.** The user ruled this explicitly. The case against
  the `terra` rung rests on nobody being able to name a unit it wins -- the
  10x price gap only explains why the gap is uncomfortable enough to notice.
- **The top rung's guard is severe because it defends a POOL, not a price.**
  fable is only 2x opus by list price, so read against price the three-way
  conjunction plus written-justification ritual looks disproportionate. It is
  defending a separate exhaustible budget that does not degrade gracefully.
  This was undocumented before this work and is the single most likely thing
  for a future auditor to wrongly relax.
- **Prices are NOT rendered.** They expire (sonnet's introductory rate lapses
  2026-08-31) and a stale price in context is wrong silently where nobody
  re-reads it. The tree carries the decision; the principles carry the number
  and its date.
- **The no-Codex `fan-out` hole is disclosed, not papered over.** An earlier
  revision forbade both an invented answer and any acknowledgement -- the worst
  of three options, since the tree teaches a test, makes the reader run it, and
  goes quiet on a positive result. Silence about a known hole reads as an
  oversight and invites the reader to invent the answer.
- 2026-08-07: update: priority = 'P1'; description = "Replace the orchestrate skill's prose model-tier policy with a decision tree derived from an auditable principles source"; skills_to_invoke = ['awesome-kit:orchestrate', 'skills-kit:md-domain']; durable_outputs = ['docs/planning/orchestrate-2.0/README.md', 'docs/planning/orchestrate-2.0/tier-principles.md', 'docs/planning/orchestrate-2.0/lexicon.md']
- 2026-08-07: update: description = "Replace orchestrate's prose policy with a principles-derived decision tree. drift-guard + machine-half + SKILL.md partition built and verified, uncommitted."
