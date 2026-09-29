"""cli -- CLI scaffold, decomposing the monolithic facade pattern.

Both source systems this plugin unifies grew a single multi-thousand-line
CLI facade over time. This package is the reusable scaffold a thin
per-project CLI wires per-command modules onto instead: ``scaffold`` (arg
dispatch, scope filtering, typo did-you-mean), ``budget`` (budget guard /
hard-stop on 429/401, auth-expiry preflight), ``bulk`` (the two-phase
cache-warm bulk worker), and ``unsupported`` (the sticky unsupported-stub
registry -- exclude an entity forever once flagged, no re-paying the same
LLM call every run).

Dependency contract: ``cli`` imports stdlib, ``pyyaml``, ``llm`` (the
``PipelineHaltError`` taxonomy, in ``budget`` and ``bulk``), and ``execution``
(``run`` adapts argv onto ``ExecutionStore``, ``compute_status`` and the worker
protocol).

``budget.guarded_sweep``, ``bulk.run_bulk`` and ``pipeline.single_pass.
run_single_pass`` are deprecated untracked loop helpers: each emits a
``DeprecationWarning`` once per call. The tracked replacement is
``execution.controller.prepare_run`` + ``execution.drivers.inline.run_wave`` +
``execution.controller.finalize_run``, with
``execution.controller.unfinished_units`` recovering the unfinished set after
a halt. ``budget.BudgetStop`` and the other names here are not deprecated.

Deviations from the skeleton / source systems
---------------------------------------------

1. **The bulk / unsupported entry points are generalized off the store.**
   ``bulk.run_bulk(units, worker, *, warm=...)`` replaces the skeleton's
   ``run_bulk(entities, stage, cache_dir)`` -- the ``warm`` callable owns
   whatever shared-cache priming the consumer needs, so this module never owns a
   cache substrate. The bare module-level
   ``unsupported.mark_unsupported`` / ``is_unsupported`` are kept for the
   skeleton signature but wrap a process-default registry; the explicit
   ``UnsupportedRegistry`` (passed by the caller, persistable) is the real
   surface -- module-global mutable state is an anti-pattern for a library.
2. **Halt handling lives once, in ``budget``.** ``budget.guarded_sweep`` catches
   ``llm.PipelineHaltError`` and halts cleanly with partial progress + a resume list;
   ``bulk.run_bulk`` composes it. ``bulk`` imports ``PipelineHaltError`` only to
   re-raise it ahead of its unguarded loop when ``guard_halts=False``.
3. **Did-you-mean and uniform output are shared scaffold primitives.**
   ``scaffold.did_you_mean`` (``difflib``) backs both the unknown-command and
   the unknown-scope-value recovery affordances; ``scaffold.emit_yaml`` +
   ``dispatch`` give every handler uniform YAML output and stable exit codes
   (0 ok / 2 usage / 1 error).
"""
