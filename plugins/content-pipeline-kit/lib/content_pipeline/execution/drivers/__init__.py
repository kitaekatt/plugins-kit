"""Drivers execute a prepared wave through the durable run store.

A driver claims units from a wave returned by
:func:`~content_pipeline.execution.controller.prepare_run` (or
:func:`~content_pipeline.execution.wave.ready_wave` directly), produces text
for each, and records the result through
:class:`~content_pipeline.execution.store.ExecutionStore`. This phase (A-min.2)
ships exactly one, additive driver:

- :mod:`~content_pipeline.execution.drivers.inline` -- concurrency-one,
  runs in the calling process.

Later phases add a background-session driver and a workflow driver
(``session-recipients-plan.md``, phases B and C); nothing here anticipates
their shape.
"""
