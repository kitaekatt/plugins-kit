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

Deliberately re-exports NOTHING here, matching the root package's strict-DAG
discipline -- ``from content_pipeline.execution.store import ExecutionStore``,
not ``from content_pipeline.execution import ExecutionStore``. This package
may import ``content_pipeline`` stdlib-adjacent nothing else: it depends on no
other subpackage, so a consumer can adopt the run store without pulling in
``llm``, ``store``, or ``vcs``.

Out of scope for this phase (see the plan of record,
``docs/planning/content-pipeline-kit/session-recipients-plan.md``, phase
A-min.1): prepare/finalize orchestration, the worker protocol, a
``RunAdapter``, and any driver. Those are later A-min sub-phases built on top
of this store, not inside it.
"""
