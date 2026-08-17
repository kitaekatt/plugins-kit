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

1. ``store.set_halt(run_id, kind=exc.kind, detail=exc.detail)``.
2. The triggering unit is returned to ``PENDING`` (not terminally failed) via
   ``store.fail_unit(..., terminal=False, error=...)`` -- it is unfinished
   work, not a permanent failure, and stays eligible for a future wave once
   the run resumes.
3. The loop stops: no further unit in this wave is claimed.

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
"""

from __future__ import annotations

from typing import Any, Callable, List, Optional, Sequence

from content_pipeline.execution.model import ExecutionError, UnitRecord
from content_pipeline.execution.store import DEFAULT_LEASE_SECONDS, ExecutionStore
from content_pipeline.llm.platform import HaltError, LLMBackend, submit_validated
from content_pipeline.pipeline.workunit import WorkUnit
from content_pipeline.validate import contract

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


def _default_unit_for(unit_id: str) -> WorkUnit:
    return WorkUnit(id=unit_id)


def run_wave(
    store: ExecutionStore,
    run_id: str,
    wave: Sequence[UnitRecord],
    *,
    unit_for: Callable[[str], WorkUnit] = _default_unit_for,
    worker_id: str = DEFAULT_INLINE_WORKER_ID,
    generate: Optional[Callable[[WorkUnit], str]] = None,
    backend: Optional[LLMBackend] = None,
    model: str = "",
    system_for: Optional[Callable[[WorkUnit], str]] = None,
    user_for: Optional[Callable[[WorkUnit], str]] = None,
    parse_fn: Optional[Callable[[str], Any]] = None,
    validators: Sequence[contract.Validator] = (),
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
    at: Optional[float] = None,
    **submit_kwargs: Any,
) -> List[str]:
    """Claim, generate, and accept each unit of ``wave``, serially (concurrency 1).

    Exactly one of ``generate`` or ``backend`` must be supplied. The
    ``backend`` path additionally requires ``parse_fn`` and ``user_for``
    (``system_for`` defaults to an empty system prompt). ``**submit_kwargs``
    forwards to :func:`~content_pipeline.llm.platform.submit_validated`
    (and, through it, to ``call_llm`` -- e.g. ``cache_dir``, ``pricing``,
    ``max_attempts``).

    Returns the ids of units accepted during this call, in the order they
    were processed. Stops early (returning what was accepted so far) on a
    caught :class:`~content_pipeline.llm.platform.HaltError` -- see the
    module docstring's "Halt handling" section.
    """
    if (generate is None) == (backend is None):
        raise ValueError("run_wave requires exactly one of `generate` or `backend`")
    if backend is not None and (parse_fn is None or user_for is None):
        raise ValueError(
            "the `backend` path requires both `parse_fn` and `user_for`"
        )

    accepted: List[str] = []
    for unit in wave:
        claim = store.claim_unit(run_id, unit.unit_id, worker_id, lease_seconds=lease_seconds, at=at)
        work_unit = unit_for(unit.unit_id)

        try:
            if generate is not None:
                text = generate(work_unit)
            else:
                system = system_for(work_unit) if system_for is not None else ""
                user = user_for(work_unit)  # type: ignore[misc]
                result = submit_validated(
                    backend=backend,  # type: ignore[arg-type]
                    system=system,
                    user=user,
                    model=model,
                    parse_fn=parse_fn,  # type: ignore[arg-type]
                    validators=validators,
                    **submit_kwargs,
                )
                if not result.accepted:
                    raise UnacceptedSubmissionError(unit.unit_id, result.rejections)
                text = result.responses[-1].text
        except HaltError as exc:
            store.set_halt(run_id, kind=exc.kind, detail=exc.detail, at=at)
            store.fail_unit(
                run_id,
                unit.unit_id,
                claim.fencing_token,
                error=f"halt:{exc.kind}",
                terminal=False,
                at=at,
            )
            break

        store.accept_unit(run_id, unit.unit_id, claim.fencing_token, text=text, at=at)
        accepted.append(unit.unit_id)

    return accepted


__all__ = [
    "DEFAULT_INLINE_WORKER_ID",
    "UnacceptedSubmissionError",
    "run_wave",
]
