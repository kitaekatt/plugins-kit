"""cli -- CLI scaffold, decomposing the monolithic facade pattern.

Both source systems this plugin unifies grew a single multi-thousand-line
CLI facade over time. This package is the reusable scaffold a thin
per-project CLI wires per-command modules onto instead: ``scaffold`` (arg
dispatch, scope filtering, typo did-you-mean), ``budget`` (budget guard /
hard-stop on 429/401, auth-expiry preflight), and ``unsupported`` (the sticky unsupported-stub
registry -- exclude an entity forever once flagged, no re-paying the same
LLM call every run).

Dependency contract: ``cli`` imports stdlib, ``pyyaml``, ``llm`` (the
``PipelineHaltError`` taxonomy, in ``budget``), and ``execution``
(``run`` adapts argv onto ``ExecutionStore``, ``compute_status`` and the worker
protocol).

The untracked loop helpers ``budget.guarded_sweep``, ``bulk.run_bulk`` and
``pipeline.single_pass.run_single_pass`` are removed. The tracked path is
``execution.controller.prepare_run`` + ``execution.drivers.inline.run_wave`` +
``execution.controller.finalize_run``, with
``execution.controller.unfinished_units`` recovering the unfinished set after
a halt. ``budget.BudgetStop`` remains.

Deviations from the skeleton / source systems
---------------------------------------------

1. **The unsupported entry points are generalized off the store.**
   The bare module-level
   ``unsupported.mark_unsupported`` / ``is_unsupported`` are kept for the
   skeleton signature but wrap a process-default registry; the explicit
   ``UnsupportedRegistry`` (passed by the caller, persistable) is the real
   surface -- module-global mutable state is an anti-pattern for a library.
2. **Halt handling lives once, in ``budget``.** ``budget.preflight_check`` and
   ``budget.check_response`` re-raise ``llm.PipelineHaltError`` as
   ``BudgetStop`` (or raise it from a hard-stop response), so a caller's run
   loop halts cleanly on a dead credential.
3. **Did-you-mean and uniform output are shared scaffold primitives.**
   ``scaffold.did_you_mean`` (``difflib``) backs both the unknown-command and
   the unknown-scope-value recovery affordances; ``scaffold.emit_yaml`` +
   ``dispatch`` give every handler uniform YAML output and stable exit codes
   (0 ok / 2 usage / 1 error).
"""
