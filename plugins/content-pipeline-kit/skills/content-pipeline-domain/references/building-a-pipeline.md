# Building a Pipeline

> **Status: skeleton -- to be expanded.** This reference is a structural
> outline drawn from the plugin proposal; each section carries only the
> 2-4 sentences the proposal already established. Expand with worked
> examples and command-level detail as pipelines are actually built against
> `content_pipeline`.

## 1. Pick the pipeline shape

Two shapes are available: `pipeline.single_pass` (regenerate-on-stale,
two-phase generate/apply -- the shape most new pipelines start with) and
`pipeline.convergence_loop` (fill -> grade -> select -> apply, cycled to a
CONVERGED/STALLED verdict via `llm.convergence`). Pick single-pass unless
your pipeline genuinely needs to iterate multiple candidates against a
grading signal before picking a winner; convergence-loop is heavier and was
deliberately implemented last (in the pre-port phase) so it is never
speculative.

## 2. Choose a work-unit strategy

Decide what "one unit of work" means for your content: a graph-walk
strategy (`pipeline.workunit`) when structural adjacency matters -- a
cadence between neighboring units, a dependency order -- or a flat-chunk
strategy when work units are independently processable. Both strategies
expose the same iteration interface to `single_pass` / `convergence_loop`,
so this choice does not ripple into the rest of the pipeline's code.

## 3. Register providers

Register every piece of prompt context your generation calls need in
`providers.registry` as a name -> (callable, tier) pair -- `source` tier for
unit-agnostic context, `generation` tier for per-language/per-variant
context. Assemble prompts through `providers.assembly`'s single-owner
slot-syntax filler rather than hand-formatting strings at each call site, so
two call sites needing "the same block" cannot silently drift.

## 4. Select delivery mode and VCS backend

Pick exactly one delivery mode: `deliver.inplace` (mutate authored content
directly, do-no-harm marker, first-class revert) or `deliver.projection`
(append-only artifacts, `.bak` rollback, never overwrite). Pick exactly one
VCS backend implementing `vcs.seam.VcsBackend`: `vcs.null_vcs` for CI/tests,
`vcs.git_vcs` (the implied default), or a Perforce backend from p4-kit if
your project is P4-backed. These are not layered -- a pipeline commits to
one of each.

## 5. Stand up the CLI facade

Wire your project's per-command CLI onto `cli.scaffold` (arg dispatch, scope
filtering, typo did-you-mean) rather than growing a bespoke argparse tree.
Add `cli.budget` for the preflight/hard-stop guard on 429/401 responses,
`cli.bulk` for the two-phase cache-warm bulk worker, and `cli.unsupported`
for the sticky unsupported-stub registry if your pipeline has entities that
are structurally unsupported rather than transiently failing.

## 6. Wire the mock LLM seam for tests

Select the `mock` backend in `llm.backends` for every test that exercises
pipeline logic without making real LLM calls. The mock seam is what lets
`single_pass` / `convergence_loop` be tested end-to-end deterministically;
reserve `openrouter` / `claude-cli` backend selection for real runs.
