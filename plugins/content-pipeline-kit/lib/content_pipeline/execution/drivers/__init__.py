"""Drivers execute a prepared wave through the durable run store.

A driver claims units from a wave returned by
:func:`~content_pipeline.execution.controller.prepare_run` (or
:func:`~content_pipeline.execution.wave.ready_wave` directly) and drives each
one from ``PENDING`` to a terminal or halted state through
:class:`~content_pipeline.execution.store.ExecutionStore`. That contract is
all a driver must satisfy -- HOW a unit's text gets produced is not part of
it, and varies by driver:

**Graph-strategy caller, read before looping:** for a graph strategy, an
empty wave from ``ready_wave`` is NOT proof the run is complete -- it also
means "blocked on a predecessor that is ACCEPTED but not yet applied". A
caller driving one of these drivers in a loop must interleave
:func:`~content_pipeline.execution.controller.finalize_run` between waves
and use :func:`~content_pipeline.execution.controller.unfinished_units` to
tell "complete" apart from "blocked". See ``execution.wave``'s module
docstring, "Looping ``ready_wave`` alone does not drain a graph run to
completion", for the full explanation and the required loop shape.

- :mod:`~content_pipeline.execution.drivers.inline` (A-min.2) -- concurrency
  one: claims a unit, produces its text synchronously in the calling process
  (either a plain callable or an ``LLMBackend`` call), and accepts it.
- A later background-session driver (plan phase B) claims a unit and
  dispatches it to an out-of-process worker session, which submits through
  the protocol asynchronously -- the driver itself produces no text; its
  workers do.
- A later workflow driver (plan phase C) is a third shape again.

So "runs a prepared wave" means "claims units and records their outcomes",
not "synchronously produces text for each and records the result" -- the
narrower phrasing described only the first of the three planned lanes and
would misdescribe the other two.

This phase (A-min.2) ships exactly one, additive driver (inline, above).
Nothing here anticipates the other two lanes' shape beyond the definition
above; see ``session-recipients-plan.md``, phases B and C.
"""
