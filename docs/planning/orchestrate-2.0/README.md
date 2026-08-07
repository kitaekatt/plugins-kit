# orchestrate 2.0 -- design, not yet implemented

Design artifacts for replacing the `orchestrate` skill's prose policy with a derived
decision tree. **Nothing here drives the live skill.** The rendered policy still comes
from `plugins/awesome-kit/skills/orchestrate/defaults/orchestration.yaml` via
`orchestration_guidance.py`.

They live under `docs/planning/` rather than in the skill's `references/` precisely so
they cannot be mistaken for the source of truth while the renderer still reads the
YAML. Moving them into the skill is part of implementing this, not part of designing
it.

## The two documents

- **[lexicon.md](lexicon.md)** -- the controlled vocabulary. Each term carries a test
  answerable at dispatch time, a `[skill]`/`[concept]` marker (does it select a
  branch?), and a `render: bare|glossed` flag (does its natural reading match its
  test?).
- **[tier-principles.md](tier-principles.md)** -- the criteria, stated in those terms,
  plus rationale, dated prices, and an explicit ledger of what is not known.

## Why this shape

The current policy is characterisation prose: it describes each model's strengths and
leaves selection to the reader. Measured at 3,204 tokens rendered.

Converting it to a tree at **full fidelity saves nothing** -- tree notation tokenises
worse than markdown tables, and a like-for-like rewrite measured +4.8%. The saving
comes from *partitioning*: rationale moves to an unrendered file, and ordered
elimination makes the "escalate when" column redundant. Derived variants measured
**~1,550 tokens with Codex and ~1,170 without**, against 3,204 / 1,861 today.

## Method: clean-room derivation

Five rounds. Each round, a subagent that had read *only* these two documents -- never
any prior draft -- derived the tree and reported every place the documents failed to
determine an answer. Defects found per round: 9, 3, 4, then missed instances of
already-solved problems. That last signature is why round five's verdict was "build
the renderer."

The technique is the point: an author cannot test their own specification, because
they will fill gaps from memory without noticing. It also enforces the one-way
authorship rule below -- if the tree is always regenerated from the principles by
something that cannot see the old tree, it cannot drift.

## Rules that survived

- **Authorship is one-way.** Change a principle, then re-derive. Never edit a rendered
  tree and back-fill a principle to match.
- **Terms earn their place by having a test**, and by selecting a branch. A term that
  only justifies a choice already made is `[concept]` and never renders.
- **Validate model identifiers by dispatch.** The shipped policy named `terra` and
  `sol`, neither of which is dispatchable (`gpt-5.6-terra`, `gpt-5.6-sol` are). That
  survived because the policy is prose nobody executes. Fixed in awesome-kit 0.21.1.

## Remaining work

1. Reshape `orchestration.yaml` into principles-derived data plus a machine-data
   manifest (detected backends, versions, capacity, launch mechanics).
2. Rewrite the render half of `orchestration_guidance.py`. It cannot express this
   today: every section is a flat record with named leaf fields, and `fold()` collapses
   multi-line scalars to one line.
3. Move these two documents into the skill's `references/` once the renderer reads
   them, and add a check that fails when a rendered tree changes without a
   corresponding principles change.

## Known gaps, carried deliberately

`fan-out` has no Claude-side answer, so the Codex-absent variant is silent on
high-fan-out work; the volume threshold in P0.3 is unquantified; there are no
capability benchmarks for the Codex rungs; and agent-type selection is unvalidated.
Section 7 of the principles is the full ledger.
