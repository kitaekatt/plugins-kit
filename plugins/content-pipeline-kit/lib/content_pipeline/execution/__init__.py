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
  strategy.
- ``controller`` -- :func:`~content_pipeline.execution.controller.prepare_run`
  / :func:`~content_pipeline.execution.controller.finalize_run` /
  :func:`~content_pipeline.execution.controller.unfinished_units` /
  ``pause_run`` / ``resume_run``, the prepare/finalize lifecycle, plus the
  local ``RunAdapter``-shaped seam ``finalize_run`` calls through.
- ``drivers`` -- driver implementations that execute a prepared wave through
  the store; ``drivers.inline`` is the A-min.2 concurrency-one driver.

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
through to ``LLMBackend`` / ``submit_validated``) and
``content_pipeline.validate.contract``. A consumer that wants only the
store/status surface can still import ``execution.store`` and
``execution.status`` directly without triggering those heavier imports --
each submodule's own dependency footprint is what matters, not this
package's aggregate.

Out of scope for this phase (see the plan of record,
``docs/planning/content-pipeline-kit/session-recipients-plan.md``, phase
A-min.3): the versioned JSON worker protocol, the real ``RunAdapter``
protocol (mountable handlers, adapter identity/version-gated resume), and the
background-session / workflow drivers. A-min.2 ships only the prepare/finalize
controller and the additive inline driver on top of the A-min.1 store.
"""
