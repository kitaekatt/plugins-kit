"""The concurrency-one inline driver: runs a prepared wave in this process.

:func:`run_wave` claims each unit of a wave (typically the output of
:func:`~content_pipeline.execution.controller.prepare_run` or
:func:`~content_pipeline.execution.wave.ready_wave`) one at a time, produces
its text one of two ways, and accepts it into the store:

- ``generate`` -- a plain ``Callable[[WorkUnit], str]`` the caller supplies
  directly (no ``LLMBackend`` involved at all -- useful for tests and for
  consumers whose generation step is not an LLM call).
- ``backend`` -- an :class:`~content_pipeline.llm.platform.LLMBackend`, run
  through :func:`~content_pipeline.llm.platform.submit_validated` (the
  validate-until-valid loop). Exactly one of ``generate``/``backend`` must be
  given.

The adapter is one object
----------------------------

The consumer's data contract -- how to reconstruct a ``WorkUnit`` by id
(``unit_for``), how to build the backend-path prompt (``system_for``/
``user_for``), how to recover a payload from accepted text (``parse_fn``),
and what counts as valid (``validators``) -- is supplied as a single
:class:`~content_pipeline.execution.controller.RunAdapter`, not five loose
keyword arguments. This is the same object
:func:`~content_pipeline.execution.controller.finalize_run` calls through,
and the sharing is the point: D1 requires finalize to re-parse a unit's
accepted text with the SAME ``parse_fn`` this driver submitted it under, and
one shared field makes that hold by construction -- a caller cannot
accidentally pass a different ``parse_fn`` to each call, because there is
only one field to pass it in. See ``controller.py``'s module docstring, "The
``RunAdapter``-shaped seam", for the full field list and which A-min.3
responsibilities are still absent from it.

Cache-key stability (D3 / invariant 7) -- READ BEFORE TOUCHING THIS MODULE
----------------------------------------------------------------------------

``backend`` is passed straight through to ``submit_validated`` (which passes
it straight through to
:func:`~content_pipeline.llm.platform.call_llm`) UNCHANGED. Never wrap it in
an adapter, a proxy, or any object with a different ``.name`` --
``call_llm`` builds its cache key via
``build_cache_key(backend=backend.name, ...)``
(``llm/platform.py:457-487``), so any change to what ``backend.name`` resolves
to from this module's call site silently invalidates every consumer's
on-disk response cache the moment they upgrade to a tracked run. There is no
migration path for a silently-changed cache key; the corpus just re-spends
in full. See ``docs/planning/content-pipeline-kit/session-recipients-plan.md``,
decision D3, and ``tests/content-pipeline-kit/test_execution_driver_inline.py``
for the regression test that pins this byte-for-byte against the REAL
``build_cache_key``.

Halt handling (D4)
--------------------

A :class:`~content_pipeline.llm.platform.HaltError` caught while producing a
unit's text (from either path -- ``generate`` may raise it directly, and
``submit_validated``/``call_llm`` raise it internally) is handled as:

1. The store-side response --
   :func:`~content_pipeline.execution.controller.record_halt`: sets the halt,
   then returns the triggering unit to ``PENDING`` (not terminally failed) via
   ``store.fail_unit(..., terminal=False, error=...)`` -- it is unfinished
   work, not a permanent failure, and stays eligible for a future wave once
   the run resumes. This half is shared with every other driver (D4 semantics
   must be byte-identical across all of them), so it lives in ``controller.py``
   rather than being re-derived here.
2. The loop stops: no further unit in this wave is claimed. This half stays
   local to this driver -- "stop claiming" means something different for a
   driver with a different concurrency model, so only the concurrency-one
   ``break`` below lives in this module.

Setting the halt does **not** retroactively affect any unit already accepted
earlier in this same call, and does not prevent a DIFFERENT, already-in-flight
claim (this driver's own next unit, or a concurrent worker's) from accepting
with a still-valid fencing token -- ``store.accept_unit`` never consults halt
state for a valid fence (D4). This driver adds no halt check of its own before
the accept call; it relies entirely on the store's existing behavior, which is
what keeps this guarantee true without re-deriving it here.

Non-halt exceptions (a plain bug in ``generate``, a validation exhaustion
inside ``submit_validated`` that never raises but returns an unaccepted
result, etc.) are the caller's problem: a non-halt exception from ``generate``
propagates out of :func:`run_wave` uncaught (the unit stays ``CLAIMED``, to be
reclaimed on lease expiry), and ``submit_validated`` returning a rejected
:class:`~content_pipeline.llm.platform.SubmitResult` is surfaced via
:class:`UnacceptedSubmissionError` rather than silently accepting empty or
invalid text.

A halt already set when this loop reaches the NEXT unit's claim
------------------------------------------------------------------

The ``HaltError`` handling above covers a halt raised BY this call's own
``generate``/``submit_validated``. It does not cover a halt that is already
set by the time this loop reaches ``store.claim_unit`` for a later unit in
the same ``wave`` -- a peer process calling ``store.set_halt`` directly, or
this call's own previous iteration setting the halt and still returning text
for that unit. ``store.claim_unit`` raises
:class:`~content_pipeline.execution.model.RunHaltedError` in that case (D4:
halt blocks new claims). :func:`run_wave` catches it around the claim,
stopping the loop the same way the ``HaltError`` path does, and returns
whatever was accepted so far -- it does not re-raise or swallow the halt
silently: the run is already durably marked halted (by whoever set it), so
returning the partial ``accepted`` list is the correct, documented behavior
for this path, matching the module's contract of "stop claiming, return what
was accepted."
"""

from __future__ import annotations

from typing import Any, Callable, List, Optional, Sequence

from content_pipeline.execution.controller import RunAdapter, record_halt
from content_pipeline.execution.model import ExecutionError, RunHaltedError, UnitRecord
from content_pipeline.execution.store import DEFAULT_LEASE_SECONDS, ExecutionStore
from content_pipeline.llm.platform import HaltError, LLMBackend, submit_validated
from content_pipeline.pipeline.workunit import WorkUnit

DEFAULT_INLINE_WORKER_ID = "inline"


class UnacceptedSubmissionError(ExecutionError):
    """``submit_validated`` exhausted its attempts without an accepted result.

    Raised by the ``backend`` path when
    :attr:`~content_pipeline.llm.platform.SubmitResult.accepted` is ``False``
    after the loop ends -- a non-halt, non-exception failure mode
    (``submit_validated`` returns rather than raises on exhaustion) that this
    driver must not silently treat as success by accepting an empty string.
    """

    def __init__(self, unit_id: str, rejections: Sequence[Any]) -> None:
        self.unit_id = unit_id
        self.rejections = list(rejections)
        super().__init__(
            f"unit {unit_id!r}: submit_validated exhausted its attempts without "
            f"an accepted result ({len(self.rejections)} outstanding rejection(s))"
        )


def run_wave(
    store: ExecutionStore,
    run_id: str,
    wave: Sequence[UnitRecord],
    adapter: Optional[RunAdapter] = None,
    *,
    worker_id: str = DEFAULT_INLINE_WORKER_ID,
    generate: Optional[Callable[[WorkUnit], str]] = None,
    backend: Optional[LLMBackend] = None,
    model: str = "",
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
    at: Optional[float] = None,
    **submit_kwargs: Any,
) -> List[str]:
    """Claim, generate, and accept each unit of ``wave``, serially (concurrency 1).

    ``adapter`` carries the consumer's data contract -- see the module
    docstring's "The adapter is one object" section. Defaults to a bare
    ``RunAdapter()`` (its ``unit_for`` default reconstructs a payload-less
    ``WorkUnit`` from the id alone), the right shape for a ``generate``
    callable that needs neither a real ``unit_for`` nor any backend-path
    field.

    Exactly one of ``generate`` or ``backend`` must be supplied. The
    ``backend`` path additionally requires ``adapter.parse_fn`` and
    ``adapter.user_for`` (``adapter.system_for`` defaults to an empty system
    prompt). ``**submit_kwargs`` forwards to
    :func:`~content_pipeline.llm.platform.submit_validated` (and, through it,
    to ``call_llm`` -- e.g. ``cache_dir``, ``pricing``, ``max_attempts``).

    Returns the ids of units accepted during this call, in the order they
    were processed. Stops early (returning what was accepted so far) on a
    caught :class:`~content_pipeline.llm.platform.HaltError` -- see the
    module docstring's "Halt handling" section.
    """
    if adapter is None:
        adapter = RunAdapter()
    if (generate is None) == (backend is None):
        raise ValueError("run_wave requires exactly one of `generate` or `backend`")
    if backend is not None and (adapter.parse_fn is None or adapter.user_for is None):
        raise ValueError(
            "the `backend` path requires both `adapter.parse_fn` and `adapter.user_for`"
        )

    accepted: List[str] = []
    for unit in wave:
        try:
            claim = store.claim_unit(
                run_id, unit.unit_id, worker_id, lease_seconds=lease_seconds, at=at
            )
        except RunHaltedError:
            # Already halted by the time this loop reached this unit's claim
            # (a peer's set_halt, or our own previous iteration setting the
            # halt while still returning text) -- see the module docstring's
            # "A halt already set..." section. Stop claiming; return what was
            # accepted so far, same contract as the HaltError path below.
            break
        work_unit = adapter.unit_for(unit.unit_id)

        try:
            if generate is not None:
                text = generate(work_unit)
            else:
                system = adapter.system_for(work_unit) if adapter.system_for is not None else ""
                user = adapter.user_for(work_unit)  # type: ignore[misc]
                result = submit_validated(
                    backend=backend,  # type: ignore[arg-type]
                    system=system,
                    user=user,
                    model=model,
                    parse_fn=adapter.parse_fn,  # type: ignore[arg-type]
                    validators=adapter.validators,
                    **submit_kwargs,
                )
                if not result.accepted:
                    raise UnacceptedSubmissionError(unit.unit_id, result.rejections)
                text = result.responses[-1].text
        except HaltError as exc:
            record_halt(store, run_id, unit.unit_id, claim.fencing_token, exc, at=at)
            break

        store.accept_unit(run_id, unit.unit_id, claim.fencing_token, text=text, at=at)
        accepted.append(unit.unit_id)

    return accepted


__all__ = [
    "DEFAULT_INLINE_WORKER_ID",
    "UnacceptedSubmissionError",
    "run_wave",
]
