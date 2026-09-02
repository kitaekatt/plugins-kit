"""Ready-wave materialization: which units may be claimed right now (A-min.2).

This module answers one question over the durable store -- "what is currently
claimable for this run" -- for the two work-unit shapes
``content_pipeline.pipeline.workunit`` exposes:

- **Flat strategies** (anything that is not a
  :class:`~content_pipeline.pipeline.workunit.GraphWalkStrategy`) have no
  structural ordering constraint between units. The ready wave is simply
  every ``PENDING`` unit for the run, in ordinal order, optionally capped by
  ``max_wave_size``.
- **Graph strategies** are treated as strictly ordinal-sequential: only one
  unit may ever be in flight at a time, so the ready wave is empty or exactly
  one unit -- the lowest-ordinal ``PENDING`` unit whose immediate
  predecessor (the previous unit by ordinal) is ``SKIPPED``, or is
  ``ACCEPTED`` *and has been applied* (its last apply-kind attempt is
  ``AttemptKind.APPLY_SUCCEEDED`` -- see "Apply-awareness" below). The first
  unit (no predecessor) is vacuously ready when it is ``PENDING``.

A predecessor in state ``SKIPPED`` (A-min.2 -- a gate or freshness check
decided that unit will never be generated, see ``execution.controller``) is
actually a STRONGER signal than ``ACCEPTED``, not an equivalent one: it
satisfies the successor unconditionally, with no apply-kind check at all,
because a ``SKIPPED`` unit never carries an ``accepted_text`` and
``finalize_run`` never applies a ``SKIPPED`` unit -- there is simply no apply
for the successor to wait on. An ``ACCEPTED`` predecessor, by contrast, only
satisfies the successor once its apply has actually succeeded (below). A skip
is not a broken link, it is a unit the run intentionally will not produce, so
it must not permanently block everything ordinally after it the way a
``FAILED`` predecessor does below.

Deliberate corner case, not spelled out by the plan of record: a terminally
``FAILED`` predecessor blocks the chain from ever becoming ready past it. Once
the lowest-ordinal ``PENDING`` unit's predecessor is ``FAILED`` (a terminal
state, per ``execution.model.TERMINAL_STATES``), that unit -- and by
construction everything after it -- can never become ready again through this
function, because ``FAILED`` never transitions back to ``ACCEPTED`` or
``SKIPPED``. This is a fail-closed choice: a graph pipeline with a broken link
stalls rather than skipping ahead.

This module treats ANY ``GraphWalkStrategy`` instance as sequential/dependent,
never conditioning on whether ``context_of`` is set. An ordered walk with no
explicit context hook still encodes an order (via ``order`` /
``predecessors_of``) that the flat shape deliberately asserts away, so the
graph path is the correct behavior even for the ``context_of=None`` case.

The graph path additionally requires apply-awareness (2026-08-17, closing the
"second unguarded door" alongside ``prepare_run``'s own
``UnappliedPredecessorError`` refusal): an ``ACCEPTED`` predecessor satisfies
its successor only once its last apply-kind attempt is
``AttemptKind.APPLY_SUCCEEDED``. ``ACCEPTED`` means only that the text was
accepted into the store at submit time (D1); it does not mean
``finalize_run`` has applied it. Without this, ``ready_wave`` ->
``run_wave`` (accept) -> ``ready_wave`` would release the successor before
its predecessor's payload has landed, even though ``prepare_run`` refuses
the same case loudly via ``UnappliedPredecessorError``. Readiness is a
query, not a gate: this function returns ``[]`` rather than raising --
``prepare_run`` is where the named exception lives, as the diagnostic that
tells a caller WHY nothing was released.

The graph path reads units and attempts together via
:meth:`~content_pipeline.execution.store.ExecutionStore.snapshot` (one read
transaction), so a peer's write landing between "read units" and "read
attempts" cannot be seen by one read and not the other.

Looping ``ready_wave`` alone does not drain a graph run to completion
------------------------------------------------------------------------

For a graph strategy, an empty wave (``[]``) is NOT proof a run is complete
-- it is also what this function returns while the next unit is blocked on a
predecessor that is ``ACCEPTED`` but not yet applied (see "Apply-awareness"
above). A caller that runs ``prepare_run`` once and then simply loops
``ready_wave`` -> a driver's ``run_wave`` -> ``ready_wave`` -> ... without
ever calling :func:`~content_pipeline.execution.controller.finalize_run` in
between passes the apply-awareness guard exactly once (against a clean,
ACCEPTED-free state), then sees every subsequent ``ready_wave`` return ``[]``
forever -- even though most of the run's units are still ``PENDING``.
Nothing raises; the loop simply stops claiming and looks like it finished.

A graph consumer MUST interleave ``finalize_run`` -- that is what actually
applies an ``ACCEPTED`` unit's payload and records
``AttemptKind.APPLY_SUCCEEDED``, unblocking the chain -- between waves, and
use :func:`~content_pipeline.execution.controller.unfinished_units` to tell
"complete" apart from "blocked" when a wave comes back empty::

    while True:
        wave = ready_wave(store, run_id, strategy)
        if not wave:
            if not unfinished_units(store, run_id):
                break  # genuinely complete
            finalize_run(store, run_id, adapter)  # unblock and retry
            continue
        run_wave(store, run_id, wave, adapter, ...)

A run with an empty wave and no unfinished units is done; a run with an
empty wave and any unfinished unit is blocked -- most often on an unapplied
``ACCEPTED`` predecessor, diagnosable with :func:`graph_block_reason` below.

Escape hatch for ``apply_unknown``: a crash between ``record_apply_started``
and ``record_apply_succeeded`` leaves a unit ``apply_unknown`` (its last
apply-kind attempt is ``APPLY_STARTED`` with no following
``APPLY_SUCCEEDED``). This function withholds the successor forever in that
state too -- it is not a deadlock, but nothing on THIS module's path
recovers it. ``finalize_run`` recovers it: either by re-applying, or, when
the adapter supplies a ``reconcile`` hook (D6), by confirming the apply
already landed without re-invoking ``adapter.apply``. See
``execution.controller``'s ``ApplyUnknownError`` and ``finalize_run``
docstring for the mechanics.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from content_pipeline.execution.model import (
    AttemptKind,
    AttemptRecord,
    ExecutionError,
    UnitRecord,
    UnitState,
)
from content_pipeline.pipeline.workunit import GraphWalkStrategy, WorkUnitStrategy


class UnsafeGraphParallelismError(ExecutionError):
    """A ``max_wave_size`` greater than 1 was requested against a graph strategy.

    Graph strategies are strictly ordinal-sequential (D1's one-unit-wave
    consequence for store-dependent validators): a wave of more than one unit
    would let two dependent units be claimed concurrently, which the sequential
    contract never allows. Raised eagerly -- before any store read -- so a
    misconfigured caller fails immediately rather than after touching the
    store.
    """

    def __init__(self, max_wave_size: int) -> None:
        self.max_wave_size = max_wave_size
        super().__init__(
            f"max_wave_size={max_wave_size} is unsafe against a graph strategy: "
            "graph waves are strictly sequential and may contain at most one "
            "unit at a time"
        )


def is_graph_strategy(strategy: WorkUnitStrategy) -> bool:
    """Return whether ``strategy`` is a dependency-carrying graph walk.

    ``isinstance(strategy, GraphWalkStrategy)`` is sufficient from outside
    ``pipeline.workunit`` to detect the graph shape -- see that module's
    ``GraphWalkStrategy`` dataclass.
    """
    return isinstance(strategy, GraphWalkStrategy)


def ready_wave(
    store,
    run_id: str,
    strategy: WorkUnitStrategy,
    *,
    max_wave_size: Optional[int] = None,
) -> List[UnitRecord]:
    """Return the units currently claimable for ``run_id`` under ``strategy``.

    See the module docstring for the flat vs. graph semantics. ``max_wave_size``
    caps a flat wave's length; against a graph strategy, any ``max_wave_size``
    greater than 1 raises :class:`UnsafeGraphParallelismError` immediately,
    before any store read.

    **Graph strategies only:** an empty return is NOT proof the run is
    complete -- it may mean the next unit is blocked on a predecessor that is
    ``ACCEPTED`` but not yet applied. See the module docstring's "Looping
    ``ready_wave`` alone does not drain a graph run to completion" section
    for the required loop shape (interleave
    :func:`~content_pipeline.execution.controller.finalize_run`) and how to
    tell "complete" apart from "blocked"
    (:func:`~content_pipeline.execution.controller.unfinished_units`,
    :func:`graph_block_reason`).
    """
    if is_graph_strategy(strategy):
        if max_wave_size is not None and max_wave_size > 1:
            raise UnsafeGraphParallelismError(max_wave_size)
        return _graph_ready_wave(store, run_id)
    return _flat_ready_wave(store, run_id, max_wave_size)


def _flat_ready_wave(store, run_id: str, max_wave_size: Optional[int]) -> List[UnitRecord]:
    units = sorted(store.list_units(run_id), key=lambda u: u.ordinal)
    pending = [u for u in units if u.state is UnitState.PENDING]
    if max_wave_size is not None:
        pending = pending[:max_wave_size]
    return pending


def _last_apply_kind(attempts: Sequence[AttemptRecord]) -> Optional[AttemptKind]:
    """The most recent apply-related attempt kind, or ``None`` if never applied.

    Underscore-private (not in ``__all__``) but NOT module-local: imported
    directly by ``execution.controller`` (``finalize_run`` and
    ``_validate_no_unapplied_accepted``), which depend on it computing
    apply-state identically to this module's own ``_graph_ready_wave`` and
    ``graph_block_reason``. Kept private rather than promoted to the public
    surface -- it is an implementation detail of "derive apply-state from
    attempts" that happens to be shared, not a stable API a third module
    should reach for; ``execution.controller`` is the one sanctioned
    cross-module import. If you rename or change this function's contract,
    update both call sites in ``controller.py`` in the same change.
    """
    last: Optional[AttemptKind] = None
    for attempt in attempts:
        if attempt.kind in (
            AttemptKind.APPLY_STARTED,
            AttemptKind.APPLY_SUCCEEDED,
            AttemptKind.APPLY_REJECTED,
        ):
            last = attempt.kind
    return last


def _graph_ready_wave(store, run_id: str) -> List[UnitRecord]:
    # NOTE (deliberate trade, do not "fix"): `snapshot` materializes EVERY
    # attempt row for the whole run on every call, even though only one
    # unit's attempts are ever consulted below -- an N-unit graph run is
    # O(N^2 * k) row reads over its lifetime. `list_attempts(run_id, unit_id)`
    # exists and would be O(N * k), but reading units and attempts on two
    # separate connections reopens the exact torn-read window `snapshot`
    # exists to close (see the module docstring). Atomicity is the point;
    # this is the cost of it, not a bug to optimize away.
    _run, all_units, attempts = store.snapshot(run_id)
    units = sorted(all_units, key=lambda u: u.ordinal)
    attempts_by_unit: Dict[str, List[AttemptRecord]] = {}
    for a in attempts:
        attempts_by_unit.setdefault(a.unit_id, []).append(a)

    predecessor_state: Optional[UnitState] = None
    predecessor_id: Optional[str] = None
    for unit in units:
        if unit.state is UnitState.PENDING:
            if predecessor_state is None or predecessor_state is UnitState.SKIPPED:
                return [unit]
            if predecessor_state is UnitState.ACCEPTED:
                predecessor_attempts = attempts_by_unit.get(predecessor_id, [])
                if _last_apply_kind(predecessor_attempts) is AttemptKind.APPLY_SUCCEEDED:
                    return [unit]
            return []
        predecessor_state = unit.state
        predecessor_id = unit.unit_id
    return []


def graph_block_reason(store, run_id: str, strategy: WorkUnitStrategy) -> Optional[str]:
    """Diagnose why a graph-strategy :func:`ready_wave` is returning ``[]``.

    Companion to :func:`~content_pipeline.execution.controller.unfinished_units`
    for the drain-loop pitfall documented in the module docstring
    ("Looping ``ready_wave`` alone does not drain a graph run to
    completion"): ``unfinished_units`` tells a caller a run IS blocked (an
    empty wave with unfinished units left); this tells them WHY, so a stuck
    graph run is diagnosable without reading the store by hand.

    Returns ``None`` when ``strategy`` is not a graph strategy, when there is
    no ``PENDING`` unit at all, or when the next ``PENDING`` unit is actually
    ready (nothing is blocked -- a caller would not normally call this in
    that case). Otherwise returns a short, human-readable string naming the
    blocked unit, its predecessor, and the predecessor's state:

    - an ``ACCEPTED`` predecessor not yet applied -- names ``finalize_run``
      as the fix.
    - an ``apply_unknown`` predecessor (``APPLY_STARTED`` with no following
      ``APPLY_SUCCEEDED``) -- names ``finalize_run`` with an
      ``adapter.reconcile`` hook as the fix (see the module docstring's
      "Escape hatch for ``apply_unknown``").
    - a terminally ``FAILED`` predecessor -- names the block as permanent.
    - any other non-terminal predecessor state (e.g. ``CLAIMED``) -- names
      the state as still in flight.

    Read-only: performs exactly one ``store.snapshot(run_id)`` read, the same
    call :func:`_graph_ready_wave` makes, and never raises on its own account.
    """
    if not is_graph_strategy(strategy):
        return None
    _run, all_units, attempts = store.snapshot(run_id)
    units = sorted(all_units, key=lambda u: u.ordinal)
    attempts_by_unit: Dict[str, List[AttemptRecord]] = {}
    for a in attempts:
        attempts_by_unit.setdefault(a.unit_id, []).append(a)

    predecessor_state: Optional[UnitState] = None
    predecessor_id: Optional[str] = None
    for unit in units:
        if unit.state is UnitState.PENDING:
            if predecessor_state is None or predecessor_state is UnitState.SKIPPED:
                return None  # actually ready; nothing to diagnose
            if predecessor_state is UnitState.FAILED:
                return (
                    f"unit {unit.unit_id!r} is blocked: predecessor "
                    f"{predecessor_id!r} is terminally FAILED, which "
                    "permanently blocks the chain"
                )
            if predecessor_state is UnitState.ACCEPTED:
                last = _last_apply_kind(attempts_by_unit.get(predecessor_id, []))
                if last is AttemptKind.APPLY_SUCCEEDED:
                    return None  # actually ready; nothing to diagnose
                if last is AttemptKind.APPLY_STARTED:
                    return (
                        f"unit {unit.unit_id!r} is blocked: predecessor "
                        f"{predecessor_id!r} is apply_unknown (an "
                        "APPLY_STARTED attempt with no following "
                        "APPLY_SUCCEEDED) -- finalize_run with an "
                        "adapter.reconcile hook can recover it"
                    )
                if last is AttemptKind.APPLY_REJECTED:
                    return (
                        f"unit {unit.unit_id!r} is blocked: predecessor "
                        f"{predecessor_id!r} apply was refused; plan another run"
                    )
                return (
                    f"unit {unit.unit_id!r} is blocked: predecessor "
                    f"{predecessor_id!r} is ACCEPTED but not yet applied -- "
                    "call finalize_run to apply it"
                )
            return (
                f"unit {unit.unit_id!r} is blocked: predecessor "
                f"{predecessor_id!r} is {predecessor_state.value} (not yet "
                "ACCEPTED or SKIPPED)"
            )
        predecessor_state = unit.state
        predecessor_id = unit.unit_id
    return None  # no PENDING unit at all; nothing to diagnose


__all__ = [
    "UnsafeGraphParallelismError",
    "is_graph_strategy",
    "ready_wave",
    "graph_block_reason",
]
