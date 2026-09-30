"""The concurrency-one inline driver: runs a prepared wave in this process.

:func:`run_wave` claims each unit of a wave (typically the output of
:func:`~content_pipeline.execution.controller.prepare_run` or
:func:`~content_pipeline.execution.wave.ready_wave`) one at a time, produces
its text one of two ways, and accepts it into the store:

**Graph-strategy caller, read before looping ``run_wave``:** ``run_wave``
only claims and accepts the units already IN the wave it is given -- it does
not compute the next one. For a graph strategy, a caller that loops
``ready_wave`` -> ``run_wave`` -> ``ready_wave`` -> ... without ever calling
:func:`~content_pipeline.execution.controller.finalize_run` in between will
see the wave go permanently empty after the first accept, because the
successor's predecessor is ACCEPTED but not yet applied -- not because the
run is done. The correct loop interleaves ``finalize_run`` on an empty wave
and checks :func:`~content_pipeline.execution.controller.unfinished_units`
to tell "complete" from "blocked"::

    while True:
        wave = ready_wave(store, run_id, strategy)
        if not wave:
            if not unfinished_units(store, run_id):
                break  # genuinely complete
            finalize_run(store, run_id, adapter)  # unblock and retry
            continue
        run_wave(store, run_id, wave, adapter, generate=..., backend=...)

See ``execution.wave``'s module docstring, "Looping ``ready_wave`` alone
does not drain a graph run to completion", for the full explanation.

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
and the sharing is the point: submit-time acceptance requires finalize to re-parse a unit's
accepted text with the SAME ``parse_fn`` this driver submitted it under, and
one shared field makes that hold by construction -- a caller cannot
accidentally pass a different ``parse_fn`` to each call, because there is
only one field to pass it in. See ``controller.py``'s module docstring, "The
``RunAdapter``-shaped seam", for the full field list and which A-min.3
responsibilities are still absent from it.

Cache-key stability -- READ BEFORE TOUCHING THIS MODULE
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
in full. A regression test pins this byte-for-byte against the REAL
``build_cache_key``.

Halt handling
--------------------

A :class:`~content_pipeline.llm.platform.PipelineHaltError` caught while producing a
unit's text (from either path -- ``generate`` may raise it directly, and
``submit_validated``/``call_llm`` raise it internally) is handled as:

1. The store-side response --
   :func:`~content_pipeline.execution.controller.record_halt`: sets the halt,
   then returns the triggering unit to ``PENDING`` (not terminally failed) via
   ``store.fail_unit(..., terminal=False, error=...)`` -- it is unfinished
   work, not a permanent failure, and stays eligible for a future wave once
   the run resumes. This half is shared with every other driver (halt semantics
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
state for a valid fence (halt blocks claims, never valid-fence submissions). This driver adds no halt check of its own before
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

The ``PipelineHaltError`` handling above covers a halt raised BY this call's own
``generate``/``submit_validated``. It does not cover a halt that is already
set by the time this loop reaches ``store.claim_unit`` for a later unit in
the same ``wave`` -- a peer process calling ``store.set_halt`` directly, or
this call's own previous iteration setting the halt and still returning text
for that unit. ``store.claim_unit`` raises
:class:`~content_pipeline.execution.model.RunHaltedError` in that case (halt
blocks new claims). :func:`run_wave` catches it around the claim,
stopping the loop the same way the ``PipelineHaltError`` path does, and returns
whatever was accepted so far -- it does not re-raise or swallow the halt
silently: the run is already durably marked halted (by whoever set it), so
returning the partial ``accepted`` list is the correct, documented behavior
for this path, matching the module's contract of "stop claiming, return what
was accepted."

A unit that asks for a durable wait
-----------------------------------

A ``generate`` callable (or the adapter callable ``run_wave`` uses to build
the request, ``adapter.build_request``, or the validation spec,
``adapter.validation_spec_for``) may raise
:class:`~content_pipeline.execution.model.InterruptRequested` to ask a person
a typed question instead of returning text. :func:`run_wave` turns the signal
into ``store.request_interrupt`` under the claim's own fencing token, with the
signal's ``on_rejected`` and ``on_expired`` policies and its ``usage``. The
unit is then ``waiting``: it holds no claimant and no lease, it is not
accepted and not in the returned list, and the wave goes on with its next
unit. The inline lane writes no dispatch row, so there is nothing to settle.

The signal must come from ``generate`` or from those two adapter callables. A
``parse_fn`` or validator that raises it is not supported: the
validate-until-valid loop treats an exception from caller code as a
rejection. A consumer that decides after generating asks from ``generate``.

A caller that has not linked the shared libraries gets
``InterruptSupportError`` from ``request_interrupt`` before anything is
written. It propagates out of :func:`run_wave` like any other non-halt
exception, and the unit stays ``CLAIMED`` until its lease lapses.

A caller that raises nothing sees no change: the clause below is never
entered.

Draining a run that has a waiting unit
--------------------------------------

A waiting unit is not in a flat wave and blocks a graph chain, and
:func:`~content_pipeline.execution.controller.unfinished_units` still lists
it. The loop under "Graph-strategy caller" therefore never ends by itself on
a run with a waiting unit. Add a stop rule to its empty-wave branch: when the
wave is empty, ``finalize_run`` applied nothing, and
:func:`~content_pipeline.execution.interrupts.waiting_units` is non-empty,
end the pass. The run is waiting, which is a healthy state, not a failure::

    applied = finalize_run(store, run_id, adapter)
    if not applied and waiting_units(store, run_id):
        break  # waiting for an answer; run the loop again after one arrives

After a resolution (an answer, or a rejection or expiry under the ``release``
policy) the unit is ``pending`` again and the next wave offers it. Its next
``generate`` call reads
:func:`~content_pipeline.execution.interrupts.unit_resolutions` for the
outcome. A unit stopped by the ``stop`` policy is terminal and is never
offered again. ``prepare_run(reclaim_at=...)`` never re-offers a waiting unit.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable, List, Optional, Sequence

from content_pipeline.execution.controller import RunAdapter, record_halt
from content_pipeline.execution.model import (
    ExecutionError,
    InterruptRequested,
    RunHaltedError,
    UnitRecord,
)
from content_pipeline.execution.store import ExecutionStore, lease_for
from content_pipeline.llm.platform import (
    BackendOptions,
    PipelineHaltError,
    LLMBackend,
    submit_validated,
)
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
    lease_seconds: Optional[float] = None,
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
    ``backend`` path additionally requires a way to build the request
    (``adapter.build_request`` or ``adapter.user_for``; ``system_for``
    defaults to an empty system prompt) and a way to validate it
    (``adapter.validation_spec_for`` or ``adapter.parse_fn``), and reads both
    through ``adapter.resolve_prepared_request`` and
    ``adapter.resolve_validation_spec`` -- the same resolvers the protocol
    ``read``/``submit`` verbs and ``finalize_run`` use, so every lane builds
    the same prompt and judges the same response. A ``context`` or
    ``block_soft`` keyword the caller passes still wins over the spec's. ``**submit_kwargs`` forwards to
    :func:`~content_pipeline.llm.platform.submit_validated` (and, through it,
    to ``call_llm`` -- e.g. ``cache_dir``, ``pricing``, ``max_attempts``).

    Returns the ids of units accepted during this call, in the order they
    were processed. Stops early (returning what was accepted so far) on a
    caught :class:`~content_pipeline.llm.platform.PipelineHaltError` -- see the
    module docstring's "Halt handling" section. A unit whose ``generate`` (or
    request or validation-spec builder) raises
    :class:`~content_pipeline.execution.model.InterruptRequested` is left
    ``waiting`` and is not in the returned list; the wave continues -- see
    "A unit that asks for a durable wait".

    ``lease_seconds`` (item 2, A-min.4): ``None`` (the default) derives a
    per-unit lease ceiling from ``adapter.resolve_expected_unit_seconds``
    via :func:`~content_pipeline.execution.store.lease_for` -- an adapter
    declaring no cost falls back to the unchanged 300s default, no warning.
    An explicit value still wins outright over derivation.
    """
    if adapter is None:
        adapter = RunAdapter()
    if (generate is None) == (backend is None):
        raise ValueError("run_wave requires exactly one of `generate` or `backend`")
    if backend is not None and (
        (adapter.build_request is None and adapter.user_for is None)
        or (adapter.validation_spec_for is None and adapter.parse_fn is None)
    ):
        raise ValueError(
            "the `backend` path requires `adapter.build_request` or `adapter.user_for`, "
            "and `adapter.validation_spec_for` or `adapter.parse_fn`"
        )
    if backend is not None:
        # Name this run in the front door's access log, but only as a DEFAULT:
        # a caller that set its own client_id is being more specific than we
        # can be, and overriding it would erase the attribution it wanted.
        options = submit_kwargs.get("options") or BackendOptions()
        if not options.client_id:
            submit_kwargs["options"] = replace(
                options, client_id=f"content-pipeline:{run_id}"
            )
        else:
            submit_kwargs["options"] = options

    accepted: List[str] = []
    for unit in wave:
        work_unit = adapter.unit_for(unit.unit_id)
        effective_lease_seconds = (
            lease_seconds
            if lease_seconds is not None
            else lease_for(adapter.resolve_expected_unit_seconds(work_unit))
        )
        try:
            claim = store.claim_unit(
                run_id, unit.unit_id, worker_id, lease_seconds=effective_lease_seconds, at=at
            )
        except RunHaltedError:
            # Already halted by the time this loop reached this unit's claim
            # (a peer's set_halt, or our own previous iteration setting the
            # halt while still returning text) -- see the module docstring's
            # "A halt already set..." section. Stop claiming; return what was
            # accepted so far, same contract as the PipelineHaltError path below.
            break

        try:
            if generate is not None:
                text = generate(work_unit)
            else:
                request = adapter.resolve_prepared_request(work_unit)
                spec = adapter.resolve_validation_spec(work_unit)
                loop_kwargs = {"context": spec.context, "block_soft": spec.block_soft}
                loop_kwargs.update(submit_kwargs)
                result = submit_validated(
                    backend=backend,  # type: ignore[arg-type]
                    system=request.system,
                    user=request.user,
                    model=model,
                    parse_fn=spec.parse_fn,
                    validators=spec.validators,
                    # The spec's structural contract, or the unit would be
                    # judged as text here while protocol and finalize judge
                    # it as a contract (``None`` for a spec with none).
                    output_contract=spec.output_contract,
                    **loop_kwargs,
                )
                if not result.accepted:
                    raise UnacceptedSubmissionError(unit.unit_id, result.rejections)
                text = result.responses[-1].text
        except PipelineHaltError as exc:
            record_halt(store, run_id, unit.unit_id, claim.fencing_token, exc, at=at)
            break
        except InterruptRequested as signal:
            # The unit asks for a durable wait: request it under this claim's
            # token (which clears the claimant and lease) and go on with the
            # next unit. Never accept it, and keep it out of ``accepted``.
            store.request_interrupt(
                run_id,
                unit.unit_id,
                claim.fencing_token,
                signal.request,
                on_rejected=signal.on_rejected,
                on_expired=signal.on_expired,
                usage=signal.usage,
                at=at,
            )
            continue

        store.accept_unit(run_id, unit.unit_id, claim.fencing_token, text=text, at=at)
        accepted.append(unit.unit_id)

    return accepted


__all__ = [
    "DEFAULT_INLINE_WORKER_ID",
    "UnacceptedSubmissionError",
    "run_wave",
]
