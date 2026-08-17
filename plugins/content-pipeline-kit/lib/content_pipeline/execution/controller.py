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

Terminal skips: a dedicated terminal state, ``UnitState.SKIPPED``
----------------------------------------------------------------------

A unit a gate or freshness check decides will never be generated is recorded
as a terminal **skip**, not a terminal **failure**: ``store.claim_unit`` then
``store.fail_unit(terminal=True, terminal_state=UnitState.SKIPPED,
error="skip:...")``. Landing in its own terminal state (rather than
``UnitState.FAILED``, the original A-min.2 shape) is what fixes two defects
that shape had:

- ``execution.wave``'s graph-strategy readiness treats a ``FAILED``
  predecessor as a permanent block. Before ``SKIPPED`` existed, a skipped
  unit 0 (up-to-date / gated / unsupported) permanently wedged every
  ordinally-later unit in a ``GraphWalkStrategy`` run -- they could never
  become claimable. ``execution.wave`` now treats ``SKIPPED`` exactly like
  ``ACCEPTED`` for this purpose: it satisfies a successor.
- ``execution.status``'s digest counted a skip as a real failure --
  inflating ``counts_by_state["failed"]``, ``failed_in_window``, and burning
  a ``recent_failures`` slot with skip noise. With its own state,
  ``counts_by_state["skipped"]`` carries it instead (a plain ``Counter`` over
  unit state already tells the two apart, no status.py logic change needed
  for that field); the attempt-level counts still needed an explicit
  exclusion, since a skip is still recorded through the same
  ``AttemptKind.FAIL`` write as a real failure -- see ``execution.status``.

The error-string convention is unchanged (``model.SKIP_ERROR_PREFIX``, so
``execution.status`` can recognize it without importing this module), and
still exists so ``execution.status``'s failure-code classifier groups skips
sensibly instead of hashing indistinguishable text, and so a reader of
``list_attempts`` can tell a skip's reason apart from a real failure's:

- ``"skip:gate:<gate-name>:<reason>"`` -- a non-sticky gate fired.
- ``"skip:unsupported:<gate-name>:<reason>"`` -- a sticky gate fired (the
  unit is structurally unsupported, mirroring ``Disposition.UNSUPPORTED``).
- ``"skip:up_to_date"`` -- freshness said no generation is needed.

``UnitState`` is a ``str`` Enum over a ``TEXT`` column, so adding
``SKIPPED`` needed no store migration -- see ``execution.model``'s
``UnitState`` docstring.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Sequence

from content_pipeline.execution.model import (
    AttemptKind,
    AttemptRecord,
    ExecutionError,
    SKIP_ERROR_PREFIX,
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


class MissingAcceptedTextError(ExecutionError):
    """Finalize refuses an ACCEPTED unit with no recorded ``accepted_text`` (D6, fail closed).

    ``store.accept_unit`` still permits omitting ``text`` (an optional
    parameter -- see its docstring -- and a pre-0.7.2 row migrated to a
    ``NULL`` ``accepted_text`` column). Calling ``adapter.parse_fn(None)``
    and treating whatever it returns as a real payload would be fail-OPEN:
    a unit could be marked applied with no payload ever having existed.
    Raised before ``adapter.parse_fn`` is called and before
    ``record_apply_started`` -- so ``adapter.apply`` is never invoked, and no
    apply-attempt row is written, for a unit refused this way. Mirrors
    :class:`ApplyUnknownError`'s refusal shape.
    """

    def __init__(self, unit_id: str) -> None:
        self.unit_id = unit_id
        super().__init__(
            f"unit {unit_id!r} is ACCEPTED with no recorded accepted_text; "
            "finalize refuses to apply a None payload (D6, fail closed)"
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
    """Claim, then terminally SKIP with a ``skip:...`` error (see module docstring)."""
    claim = store.claim_unit(run_id, unit_id, worker_id, at=at)
    store.fail_unit(
        run_id,
        unit_id,
        claim.fencing_token,
        error=error,
        terminal=True,
        terminal_state=UnitState.SKIPPED,
        at=at,
    )


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
                error = f"{SKIP_ERROR_PREFIX}unsupported:{gate.name}:{reason}"
            else:
                error = f"{SKIP_ERROR_PREFIX}gate:{gate.name}:{reason}"
            _record_terminal_skip(store, run_id, unit.unit_id, worker_id, error, at=at)
            continue

        if freshness_of is not None:
            state = freshness_of(work_unit)
            if not needs_generation(state, include_stale=include_stale):
                _record_terminal_skip(
                    store, run_id, unit.unit_id, worker_id, f"{SKIP_ERROR_PREFIX}up_to_date", at=at
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

    Fails closed on a ``None`` payload (D6): an ACCEPTED unit whose
    ``accepted_text`` is ``None`` (``accept_unit`` permits omitting ``text``;
    a pre-0.7.2 row may also have migrated to ``NULL``) raises
    :class:`MissingAcceptedTextError` rather than calling
    ``adapter.parse_fn(None)`` and applying whatever it returns.
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

        if unit.accepted_text is None:
            raise MissingAcceptedTextError(unit.unit_id)

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
    "MissingAcceptedTextError",
    "RunAdapter",
    "prepare_run",
    "finalize_run",
    "unfinished_units",
    "pause_run",
    "resume_run",
]
