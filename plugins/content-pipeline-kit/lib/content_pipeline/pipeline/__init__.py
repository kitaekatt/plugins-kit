"""pipeline -- stage orchestration for both pipeline shapes.

``stage`` declares the Stage protocol every orchestration shape composes:
a pure function over the store. ``single_pass`` is the regenerate-on-stale
shape (two-phase generate / apply) -- the shape a single-pass consumer
ports onto first. ``convergence_loop`` is the fill -> grade -> select ->
apply cycle, driven to a verdict by ``llm.convergence`` -- built as a seam
in the core phases but implemented last, in the pre-port phase, so it is
never speculative. ``workunit`` is the pluggable work-unit strategy: a
graph-walk (structural cadence) or a flat-chunk split, selected per
pipeline.

Deviations from the skeleton / source systems
---------------------------------------------

1. **The two pipeline entry points take explicit callables, not one opaque
   ``stages`` / ``grader`` placeholder.** ``single_pass.run_single_pass``
   iterates ``WorkUnit`` with an ordered gate sequence, a ``freshness_of``
   classifier, a ``generate``, and an optional ``apply`` (the ``deliver`` seam)
   -- the two-phase generate/apply the source ``generate_conversation`` /
   ``apply_conversation`` split actually runs, not a fold of anonymous stages.
   The skeleton ``run(store, stages)`` is retained as a thin ``stage.compose``
   fold for the trivial case.
2. **Grade-first ordering is structural in ``convergence_loop.run_cycle``, not a
   caller parameter.** The four stages run grade -> select -> apply -> fill in a
   fixed order so a cold-start store's empty seed is baked gradeable before FILL
   runs -- the cold-start-deadlock guard the source corpus loop depends on. The
   skeleton's ``run(store, providers, grader, max_cycles)`` is replaced by a
   signature taking the four stage callables plus a progress ``measure`` and a
   ``ConvergenceGate``; a single opaque ``grader`` never captured the four-stage
   shape.
3. **Sticky-unsupported and deterministic seeding are wired as seams, not
   baked.** A sticky gate calls the caller's ``mark_unsupported`` hook (see
   ``cli.unsupported``); ``single_pass.seed_for`` derives a per-unit RNG seed
   from ``freshness.seed`` so stochastic stage decisions never churn a freshness
   hash. Both are opt-in (CRP), reached only by a pipeline that wants them.
"""
