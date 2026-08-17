"""execution -- the durable run plane above ``llm.platform``.

A backend-independent run store for LLM-in-the-loop batch work: run, unit,
attempt/event, and lease records over SQLite, with atomic claims, monotonically
increasing per-unit fencing tokens, and a bounded read-only status digest. This
is the substrate a prepare/finalize controller and driver (inline today;
background-session and workflow drivers in later phases) sit on top of --
``LLMBackend`` stays a one-call transport; this package is its caller's
durability layer, never its peer.

Submodules:

- ``model`` -- record dataclasses, the unit state machine, and the error
  taxonomy (``StaleFenceError``, ``RunHaltedError``, ``TerminalStateError``,
  ...). Pure data; no SQLite.
- ``store`` -- :class:`~content_pipeline.execution.store.ExecutionStore`, the
  only place run truth lives. WAL journal mode, a ``busy_timeout`` on every
  connection, connections opened per verb and closed, and a loud (never
  refusing) warning when the database path looks like a network filesystem.
- ``status`` -- :func:`~content_pipeline.execution.status.compute_status`, a
  read-only bounded digest (counts, ages, throughput, capped failure groups,
  halt state) that never contains prompts, unit payloads, or outputs.
- ``wave`` -- :func:`~content_pipeline.execution.wave.ready_wave`, which
  units are currently claimable for a run under a flat or graph work-unit
  strategy, plus :func:`~content_pipeline.execution.wave.graph_block_reason`
  for diagnosing why a graph strategy's wave is empty. For a graph strategy,
  a wave returned by ``prepare_run`` OR ``ready_wave`` directly, an empty
  wave is NOT proof the run is complete -- a caller looping either one must
  interleave ``controller.finalize_run`` and consult
  ``controller.unfinished_units``; see ``wave``'s module docstring, "Looping
  ``ready_wave`` alone does not drain a graph run to completion".
- ``controller`` -- :func:`~content_pipeline.execution.controller.prepare_run`
  / :func:`~content_pipeline.execution.controller.finalize_run` /
  :func:`~content_pipeline.execution.controller.unfinished_units` /
  ``record_halt`` / ``pause_run`` / ``resume_run``, the prepare/finalize
  lifecycle plus the driver-shared D4 halt response, and the local
  ``RunAdapter``-shaped seam both ``drivers.inline.run_wave`` and
  ``finalize_run`` call through.
- ``drivers`` -- driver implementations that execute a prepared wave through
  the store; ``drivers.inline`` is the A-min.2 concurrency-one driver, and
  imports ``controller`` for ``RunAdapter``/``record_halt``.
- ``adapter`` -- :class:`~content_pipeline.execution.adapter.RunAdapter` (A-min.3):
  the consumer's full five-responsibility worker-facing contract (reconstruct
  unit by id, build a prepared request, provide a ``ValidationSpec``, apply a
  payload, optionally reconcile an ``apply_unknown``), plus
  ``require_compatible_adapter`` for D1's incompatible-resume refusal. This is
  the canonical home of ``RunAdapter`` as of A-min.3 -- ``controller.py``
  imports and re-exports it unchanged (widened in place, not duplicated; see
  that module's "RunAdapter-shaped seam" docstring section).
- ``protocol`` -- :func:`~content_pipeline.execution.protocol.build_handlers` /
  :func:`~content_pipeline.execution.protocol.dispatch` (A-min.3): the
  versioned JSON worker protocol (``prepare | claim | read | submit | fail |
  renew | status | pause | resume | finalize``) as mountable handlers a
  consumer wires onto its own entry point. Ships no runnable tool of its own
  -- the no-console-script boundary holds; ``cli.scaffold.dispatch`` remains
  the human-facing helper, this is the machine-facing equivalent for a JSON
  envelope.

Deliberately re-exports NOTHING here, matching the root package's strict-DAG
discipline -- ``from content_pipeline.execution.store import ExecutionStore``,
not ``from content_pipeline.execution import ExecutionStore``.

**Not zero-dependency as a whole package, as of A-min.2.** ``model.py``,
``store.py``, and ``status.py`` remain zero-dependency (stdlib plus
``content_pipeline.execution`` itself) -- a consumer can still adopt just the
durable run store without pulling in anything else. But ``wave.py`` imports
``content_pipeline.pipeline.workunit`` (the work-unit strategy shapes),
``controller.py`` additionally imports ``content_pipeline.pipeline.single_pass``
(the ``Gate`` / ``run_gates`` seam) and ``content_pipeline.freshness.classify``,
and ``drivers/inline.py`` imports ``content_pipeline.llm.platform`` (to call
through to ``LLMBackend`` / ``submit_validated``) and ``controller.py``
itself (for the shared ``RunAdapter`` seam and ``record_halt``), which pulls
in ``controller.py``'s own dependencies transitively (including
``content_pipeline.validate.contract``, for ``RunAdapter.validators``'
type). ``adapter.py`` additionally imports ``content_pipeline.llm.platform``
directly (for ``ValidationSpec``), and ``protocol.py`` imports ``adapter.py``,
``controller.py``, ``status.py``, ``store.py``, ``llm.platform``
(``evaluate_submission``), ``pipeline.single_pass`` (``Gate``, for its
``prepare`` verb's policy parameters), and ``validate.contract``. A consumer
that wants only the store/status surface can still import ``execution.store``
and ``execution.status`` directly without triggering those heavier imports --
each submodule's own dependency footprint is what matters, not this
package's aggregate.

Out of scope for this phase (see the plan of record,
``docs/planning/content-pipeline-kit/session-recipients-plan.md``, phase
A-min.3): the background-session / workflow drivers (phases B and C). A-min.3
ships the worker protocol, the full ``RunAdapter`` (mountable handlers,
adapter identity/version-gated resume), pure evaluation, and cache hardening
on top of the A-min.1 store and A-min.2 prepare/finalize controller.
"""
