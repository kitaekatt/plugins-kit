# orchestrate 2.0 -- design and derivation history

Design artifacts for replacing the `orchestrate` skill's prose policy with a derived
decision tree. The rendered policy comes from
`plugins/awesome-kit/skills/orchestrate/defaults/orchestration.yaml` via
`orchestration_guidance.py`; the decision half of that file is itself GENERATED from
this directory's `tier-principles.md` plus the shipped `lexicon.md` -- see "The
generator and its commit-time gate" below. The remaining items under "Remaining work"
predate that generator and are stale; kept for derivation history rather than rewritten.

This document and [tier-principles.md](tier-principles.md) are maintainer-only
derivation source: they explain how the decision tree was designed and record our
build history, neither of which means anything on a machine that only installed the
plugin. They live under `docs/reference/orchestrate/` -- outside every published
plugin -- rather than inside `plugins/awesome-kit/skills/orchestrate/references/`,
because everything under `plugins/<name>/` ships to a consumer's plugin cache (see
the plugin-opinion razor's OP-1 criterion,
[docs/reference/plugin-opinion-razor.md](../plugin-opinion-razor.md)). Both files stay
in this one directory deliberately, so their relative links to each other keep
working.

`lexicon.md` is the one document of the original pair that DOES ship, at
[plugins/awesome-kit/skills/orchestrate/references/lexicon.md](../../../plugins/awesome-kit/skills/orchestrate/references/lexicon.md)
-- a consumer reads it as vocabulary reference for the rendered tree, not as build
machinery, and `scripts/generate_orchestration.py` reads it from that location as one
of its two inputs (the other being `tier-principles.md`, here).

## The two documents

- **[lexicon.md](../../../plugins/awesome-kit/skills/orchestrate/references/lexicon.md)**
  -- the controlled vocabulary, shipped inside the published skill. Each term carries a
  test answerable at dispatch time, a `[skill]`/`[concept]` marker (does it select a
  branch?), and a `render: bare|glossed` flag (does its natural reading match its
  test?).
- **[tier-principles.md](tier-principles.md)** -- the criteria, stated in those terms,
  plus rationale, dated prices, and an explicit ledger of what is not known.
  Maintainer-only; not part of any plugin install.

## Why this shape

The current policy is characterisation prose: it describes each model's strengths and
leaves selection to the reader. Measured at 3,204 tokens rendered.

Converting it to a tree at **full fidelity saves nothing** -- tree notation tokenises
worse than markdown tables, and a like-for-like rewrite measured +4.8%. The saving
comes from *partitioning*: rationale moves to an unrendered file, and ordered
elimination makes the "escalate when" column redundant.

### Measured result

| | before | after |
|---|---:|---:|
| whole render, Codex present | 3,204 | **2,547** (-20.5%) |
| whole render, Codex absent | 1,861 | **1,447** (-22.2%) |
| decision half only, Codex present | 1,635 | **1,342** (-17.9%) |
| decision half only, Codex absent | 1,289 | **1,003** (-22.2%) |

**Read the halves separately, and do not compare across them.** An earlier revision of
this file claimed "~1,550 with Codex and ~1,170 without, against 3,204 / 1,861" --
which set a *decision-half* figure against a *whole-render* baseline and overstated
the saving by more than double. The machine half (backend mechanics, the `codex exec`
line, the gotchas, capacity) is 1,205 tokens with Codex and 444 without, and this work
deliberately did not touch it, so a 1,550 total was arithmetically impossible from the
outset.

Roughly 500 further tokens are available in the machine half from prose tightening
alone -- it restates the `codex exec` rule twice -- but that is an edit to the mechanics
section, not a consequence of the tree, and it should be measured as its own change
rather than folded into this one's credit.

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

(A third item used to be listed here: move these two documents into the skill's
`references/`. That was done for a time, then reversed -- see "The generator and its
commit-time gate" below for why.)

## The generator and its commit-time gate

The deferred "derive the tree from the principles at build time" item above is DONE.
`scripts/generate_orchestration.py` compiles the decision half of `orchestration.yaml`
from `tier-principles.md`'s `emits:` blocks plus `lexicon.md`, splicing the result into
the policy's header and machine half as raw bytes so their comments and formatting are
untouched. `--write` regenerates the file; `--check` (chained from
`scripts/pre-commit-version-check.sh`) fails the commit when the staged policy does
not match what the staged principles would produce -- it reads the staged blobs via
`git show :<path>` when anything is staged, so a partial commit (principles staged,
regenerated policy not) is caught rather than compared against an agreeing working
tree.

Because the tree is now genuinely COMPILED from these principles rather than
hand-written and checked for drift, a fingerprint of the decision half can no longer
disagree with them -- there is nothing left for a drift guard to catch that `--check`
does not already catch more strongly. `scripts/check_orchestration_drift.py` and its
`decision-fingerprint.txt` baseline, which enforced the one-way-authorship rule for the
hand-written tree, were retired for this reason (compile-principles step 4). That
retirement is also what let `tier-principles.md` and this file move out of the
published skill's `references/` -- the guard was the only thing requiring them to sit
inside the directory it policed.

**What `--check` does guarantee, precisely.** The generated decision half is a pure
function of `tier-principles.md` and `lexicon.md`; `--check` fails whenever the staged
policy is not that function's output, so a principles change with no regeneration, a
regeneration with no principles change, and a hand-edit of the decision half are all
caught the same way -- by disagreement, not by a moved baseline. It does **not** verify
that the principles' CONTENT is correct, only that the derivation was followed
mechanically; that is a property of the principles being prose-with-rationale, meant to
be audited by a human. It also does not cover the MACHINE half (`backends`,
`capacity`), which is not derived from anything and is expected to differ per machine,
nor a renderer change in `orchestration_guidance.py` that alters the rendered tree
without touching the YAML.

## Known gaps, carried deliberately

`fan-out` has no Claude-side answer, so the Codex-absent variant is silent on
high-fan-out work; the volume threshold in P0.3 is unquantified; there are no
capability benchmarks for the Codex rungs; and agent-type selection is unvalidated.
Section 7 of the principles is the full ledger.
