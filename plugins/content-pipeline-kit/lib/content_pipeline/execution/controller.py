"""Prepare / finalize lifecycle over the durable run store (A-min.2).

Two entry points bracket a run:

- :func:`prepare_run` -- runs the gate sequence and a freshness check over
  every still-``PENDING`` unit, records terminal skips for the ones that
  will never be generated, and returns the currently claimable wave (via
  :func:`~content_pipeline.execution.wave.ready_wave`).
- :func:`finalize_run` -- applies every ``ACCEPTED`` unit's recorded text,
  serially, in ordinal order, recording ``apply_started``/``apply_succeeded``
  around each call (plan D6). It never re-adjudicates a verdict (D1,
  invariant 5): the adapter's ``parse_fn`` is called mechanically to recover
  the payload object from the durably recorded ``accepted_text``, and no
  validator ever runs again.

Also here: :func:`unfinished_units` (every unit without a terminal state, a
SET with holes preserved, the halt-triggering unit included) and the thin
``pause_run`` / ``resume_run`` wrappers over ``store.set_halt`` /
``store.clear_halt`` (D4 -- an operator pause is just another halt kind; no
new store method exists for it).

The ``RunAdapter``-shaped seam
-------------------------------

:class:`RunAdapter` here is a **local, minimal** dataclass of callables
(``parse_fn``, ``apply``, optional ``reconcile``) -- NOT the versioned JSON
worker protocol A-min.3 ships (``execution/adapter.py`` /
``execution/protocol.py`` do not exist yet; do not build them here). It exists
only so :func:`finalize_run` has something typed to call.

``parse_fn`` MUST be deterministic and store-independent for tracked runs:
finalize re-runs it on text recorded at submit time, potentially long after
and in a different process, so any dependence on ambient state (clock,
filesystem, network) would make a replay diverge from what the worker that
recorded the text actually saw (plan D1's adapter contract).

The gate seam: a direct import, not a re-derived shape
--------------------------------------------------------

``prepare_run`` runs gates through
:class:`content_pipeline.pipeline.single_pass.Gate` and
:func:`content_pipeline.pipeline.single_pass.run_gates` directly, rather than
redefining an equivalent local shape. ``pipeline/single_pass.py`` is
read-only for A-min.2 (no edits, no behavior change to the untracked
``run_single_pass`` loop), but importing its ``Gate`` dataclass and
``run_gates`` helper is not an edit -- it is reuse, and it is the only way a
consumer's existing gate list (already built against that exact shape) works
unchanged against the tracked path. Redefining an equivalent local
``Gate``/`run_gates`` would fork the shape for no benefit and cost every
consumer a second, subtly-different gate protocol to satisfy. This import is
why this package's docstring can no longer claim "depends on no other
subpackage" -- see ``execution/__init__.py``.

Terminal skips: store-API-only, with a documented cost
----------------------------------------------------------

A unit a gate or freshness check decides will never be generated is recorded
as a **terminal failure** -- ``store.claim_unit`` then
``store.fail_unit(terminal=True, error="skip:...")`` -- because A-min.2 adds
no new store state for "skipped". Error-string convention, so
``execution.status``'s failure-code classifier groups skips sensibly instead
of hashing indistinguishable text:

- ``"skip:gate:<gate-name>:<reason>"`` -- a non-sticky gate fired.
- ``"skip:unsupported:<gate-name>:<reason>"`` -- a sticky gate fired (the
  unit is structurally unsupported, mirroring ``Disposition.UNSUPPORTED``).
- ``"skip:up_to_date"`` -- freshness said no generation is needed.

**Accepted, documented trade-off:** a skip is not a real failure, but it
counts toward ``RunStatus.counts_by_state["failed"]`` and toward
``recent_failures`` exactly like one, because the store has only one terminal
failure state. The ``skip:`` prefix is what lets a reader (or a future
status-layer enhancement) tell the two apart; nothing here hides the
overlap.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Sequence

from content_pipeline.execution.model import (
    AttemptKind,
    AttemptRecord,
    ExecutionError,
    TERMINAL_STATES,
    UnitRecord,
    UnitState,
)
from content_pipeline.execution.store import ExecutionStore
from content_pipeline.execution.wave import ready_wave
from content_pipeline.freshness.classify import FreshnessState, needs_generation
from content_pipeline.pipeline.single_pass import Gate, run_gates
from content_pipeline.pipeline.workunit import WorkUnit, WorkUnitStrategy

DEFAULT_PREPARE_WORKER_ID = "prepare"
DEFAULT_FINALIZE_WORKER_ID = "finalize"


class ApplyUnknownError(ExecutionError):
    """Finalize refuses to proceed while any unit is ``apply_unknown`` (D6).

    Raised when the adapter supplies no ``reconcile`` hook (fail closed) --
    or when ``reconcile`` is supplied but this unit still resolves to
    ``apply_unknown`` after asking it (i.e. ``reconcile`` returned ``False``
    and finalize chose to re-apply rather than silently proceeding without
    ever closing the record -- see :func:`finalize_run`).
    """

    def __init__(self, unit_id: str) -> None:
        self.unit_id = unit_id
        super().__init__(
            f"unit {unit_id!r} is apply_unknown (an APPLY_STARTED attempt with "
            "no following APPLY_SUCCEEDED) and the adapter supplies no "
            "reconcile hook; finalize refuses to proceed (D6, fail closed)"
        )


@dataclass
class RunAdapter:
    """The local, minimal seam :func:`finalize_run` calls through.

    - ``parse_fn`` -- ``text -> payload``. Called mechanically on the
      durably recorded ``accepted_text``; never re-validates (D1).
    - ``apply`` -- ``(unit_id, payload) -> None``. The consumer's delivery
      side effect (e.g. a ``deliver.*`` write).
    - ``reconcile`` -- optional ``unit_id -> bool``. Answers "did this
      unit's apply already land" for a unit found ``apply_unknown``. Absent
      means finalize refuses to proceed past any ``apply_unknown`` unit
      (D6, fail closed).
    """

    parse_fn: Callable[[str], Any]
    apply: Callable[[str, Any], None]
    reconcile: Optional[Callable[[str], bool]] = None


# ---------------------------------------------------------------------------
# prepare_run
# ---------------------------------------------------------------------------


def _record_terminal_skip(
    store: ExecutionStore,
    run_id: str,
    unit_id: str,
    worker_id: str,
    error: str,
    *,
    at: Optional[float] = None,
) -> None:
    """Claim, then terminally fail with a ``skip:...`` error (see module docstring)."""
    claim = store.claim_unit(run_id, unit_id, worker_id, at=at)
    store.fail_unit(run_id, unit_id, claim.fencing_token, error=error, terminal=True, at=at)


def prepare_run(
    store: ExecutionStore,
    run_id: str,
    strategy: WorkUnitStrategy,
    work_units: Sequence[WorkUnit],
    *,
    gates: Sequence[Gate] = (),
    freshness_of: Optional[Callable[[WorkUnit], FreshnessState]] = None,
    include_stale: bool = True,
    mark_unsupported: Optional[Callable[[str, str], None]] = None,
    max_wave_size: Optional[int] = None,
    worker_id: str = DEFAULT_PREPARE_WORKER_ID,
    at: Optional[float] = None,
) -> List[UnitRecord]:
    """Evaluate gates and freshness, record terminal skips, materialize a wave.

    ``work_units`` supplies the ``WorkUnit`` (payload + context) for every
    unit currently registered ``PENDING`` in the store, keyed by
    ``WorkUnit.id`` matching the store's ``unit_id``. A ``PENDING`` unit with
    no matching entry in ``work_units`` is left untouched (out of scope for
    this prepare call -- e.g. a caller preparing one chunk of a larger run).

    For each matched ``PENDING`` unit, in the order ``store.list_units``
    returns (ordinal order):

    1. Run ``gates`` via :func:`~content_pipeline.pipeline.single_pass.run_gates`.
       A firing gate records a terminal skip (see module docstring for the
       error-string convention) and, if ``sticky``, also calls
       ``mark_unsupported(unit_id, reason)`` when supplied.
    2. Otherwise, when ``freshness_of`` is supplied, classify the unit and
       skip (terminally, ``"skip:up_to_date"``) when
       :func:`~content_pipeline.freshness.classify.needs_generation` says no
       generation is needed.

    Neither step is store-invented: it is exactly ``claim`` then
    ``fail_unit(terminal=True, ...)`` against the existing store API.

    Returns the ready wave via
    :func:`~content_pipeline.execution.wave.ready_wave` computed AFTER all
    skips above have landed, so a just-skipped unit is not reported as
    claimable to a caller reading the returned wave.
    """
    by_id = {wu.id: wu for wu in work_units}
    for unit in store.list_units(run_id):
        if unit.state is not UnitState.PENDING:
            continue
        work_unit = by_id.get(unit.unit_id)
        if work_unit is None:
            continue

        fired = run_gates(gates, work_unit)
        if fired is not None:
            gate, reason = fired
            if gate.sticky:
                if mark_unsupported is not None:
                    mark_unsupported(unit.unit_id, reason)
                error = f"skip:unsupported:{gate.name}:{reason}"
            else:
                error = f"skip:gate:{gate.name}:{reason}"
            _record_terminal_skip(store, run_id, unit.unit_id, worker_id, error, at=at)
            continue

        if freshness_of is not None:
            state = freshness_of(work_unit)
            if not needs_generation(state, include_stale=include_stale):
                _record_terminal_skip(
                    store, run_id, unit.unit_id, worker_id, "skip:up_to_date", at=at
                )
                continue

    return ready_wave(store, run_id, strategy, max_wave_size=max_wave_size)


# ---------------------------------------------------------------------------
# finalize_run
# ---------------------------------------------------------------------------


def _last_apply_kind(attempts: Sequence[AttemptRecord]) -> Optional[AttemptKind]:
    """The most recent apply-related attempt kind, or ``None`` if never applied."""
    last: Optional[AttemptKind] = None
    for attempt in attempts:
        if attempt.kind in (AttemptKind.APPLY_STARTED, AttemptKind.APPLY_SUCCEEDED):
            last = attempt.kind
    return last


def finalize_run(
    store: ExecutionStore,
    run_id: str,
    adapter: RunAdapter,
    *,
    worker_id: str = DEFAULT_FINALIZE_WORKER_ID,
    at: Optional[float] = None,
) -> List[str]:
    """Apply every ``ACCEPTED`` unit's recorded text, serially, in ordinal order.

    Idempotent (invariant 3): apply state is derived, never stored, by
    scanning :meth:`~content_pipeline.execution.store.ExecutionStore.list_attempts`
    for the last apply-kind attempt per unit (see :func:`_last_apply_kind`):

    - No apply-kind attempt -- not yet applied; apply now.
    - Last is ``APPLY_SUCCEEDED`` -- already applied; skipped (never replayed).
    - Last is ``APPLY_STARTED`` with no following ``APPLY_SUCCEEDED`` --
      ``apply_unknown``. Refuses via :class:`ApplyUnknownError` unless
      ``adapter.reconcile`` is supplied. When it is: ``reconcile(unit_id)``
      returning ``True`` means the apply already landed -- record
      ``apply_succeeded`` and move on WITHOUT calling ``apply`` again (D6:
      never risk a duplicate side effect once reconciliation confirms it
      landed). Returning ``False`` means it did not land -- fall through to
      a normal re-apply (a fresh ``apply_started``/``apply_succeeded`` pair),
      since only the record, not the side effect, was confirmed absent.

    Returns the ids of units whose ``adapter.apply`` was actually invoked
    during THIS call -- a reconciled-as-landed unit is not included, since
    its side effect was not (re)run here.

    Never re-adjudicates a verdict (D1, invariant 5): ``adapter.parse_fn`` is
    called mechanically on the durably recorded ``accepted_text`` to recover
    the payload object; no validator runs again.
    """
    applied: List[str] = []
    units = sorted(store.list_units(run_id), key=lambda u: u.ordinal)
    for unit in units:
        if unit.state is not UnitState.ACCEPTED:
            continue

        attempts = store.list_attempts(run_id, unit.unit_id)
        last_kind = _last_apply_kind(attempts)

        if last_kind is AttemptKind.APPLY_SUCCEEDED:
            continue  # already applied; idempotence

        if last_kind is AttemptKind.APPLY_STARTED:
            if adapter.reconcile is None:
                raise ApplyUnknownError(unit.unit_id)
            landed = adapter.reconcile(unit.unit_id)
            if landed:
                store.record_apply_succeeded(run_id, unit.unit_id, at=at)
                continue
            # Not landed: fall through to a fresh apply below.

        payload = adapter.parse_fn(unit.accepted_text)
        store.record_apply_started(run_id, unit.unit_id, at=at)
        adapter.apply(unit.unit_id, payload)
        store.record_apply_succeeded(run_id, unit.unit_id, at=at)
        applied.append(unit.unit_id)

    return applied


# ---------------------------------------------------------------------------
# unfinished_units
# ---------------------------------------------------------------------------


def unfinished_units(store: ExecutionStore, run_id: str) -> List[UnitRecord]:
    """Every unit WITHOUT a terminal state, ordinal order, holes included.

    A SET, not a queue: the halt-triggering unit (returned to ``PENDING`` by
    the driver on halt, per D4/invariant 2) is included, and a unit's
    original ordinal is preserved regardless of which ordinals around it are
    terminal -- there is no renumbering, so a caller can report "unit 7 of
    12 is unfinished" meaningfully even when units 3 and 5 are done.
    """
    units = sorted(store.list_units(run_id), key=lambda u: u.ordinal)
    return [u for u in units if u.state not in TERMINAL_STATES]


# ---------------------------------------------------------------------------
# pause_run / resume_run
# ---------------------------------------------------------------------------


def pause_run(
    store: ExecutionStore, run_id: str, *, detail: str = "", at: Optional[float] = None
) -> None:
    """Halt ``run_id`` with kind ``"pause"`` (D4: an operator pause is just
    another halt kind -- new claims stop; a valid-fence submission already
    in flight is still accepted; no new store method exists for this)."""
    store.set_halt(run_id, kind="pause", detail=detail, at=at)


def resume_run(store: ExecutionStore, run_id: str) -> None:
    """Clear any halt (pause or otherwise) on ``run_id``."""
    store.clear_halt(run_id)


__all__ = [
    "ApplyUnknownError",
    "RunAdapter",
    "prepare_run",
    "finalize_run",
    "unfinished_units",
    "pause_run",
    "resume_run",
]
