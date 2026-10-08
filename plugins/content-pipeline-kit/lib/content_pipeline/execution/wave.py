"""Ready-wave materialization: which units may be claimed right now (A-min.2).

This module answers one question over the durable store -- "what is currently
claimable for this run" -- for three work-unit shapes: the two
``content_pipeline.pipeline.workunit`` exposes, and
:class:`~content_pipeline.execution.parallel_graph.ParallelGraphStrategy`:

- **Parallel graph strategies**
  (:class:`~content_pipeline.execution.parallel_graph.ParallelGraphStrategy`)
  name each unit's direct dependencies. The ready wave is every ``PENDING``
  unit whose dependencies are all settled (``SKIPPED``, or ``ACCEPTED`` and
  applied), up to the strategy's ``capacity`` minus the units in flight, with
  ``deferred`` units last; a transitive dependent of a broken unit is gated
  (:func:`gated_units`). That module's docstring states the rule in full. The
  drain-loop rule below applies to it as to the sequential graph.
- **Flat strategies** (anything that is neither a
  :class:`~content_pipeline.pipeline.workunit.GraphWalkStrategy` nor a
  parallel graph) have no structural ordering constraint between units. The ready wave is simply
  every ``PENDING`` unit for the run, in ordinal order, optionally capped by
  ``max_wave_size``.
- **Graph strategies** are treated as strictly ordinal-sequential: only one
  unit may ever be in flight at a time, so the ready wave is empty or exactly
  one unit -- the lowest-ordinal ``PENDING`` unit whose nearest
  non-``SKIPPED`` predecessor (looking back past every ``SKIPPED`` unit) is
  ``ACCEPTED`` *and has been applied* (its last apply-kind attempt is
  ``AttemptKind.APPLY_SUCCEEDED`` -- see "Apply-awareness" below). A unit
  with no non-``SKIPPED`` predecessor (it is first, or every earlier unit was
  skipped) is vacuously ready when it is ``PENDING``.

A ``SKIPPED`` unit (a gate or freshness check decided that unit will never
be generated, see ``execution.controller``) is transparent to the graph
rule: it never carries an ``accepted_text`` and ``finalize_run`` never applies
it, so there is nothing to wait on, and it is neither a dependency nor a
broken link. The rule looks past it to the nearest earlier unit that is not
``SKIPPED`` and judges the successor by that unit's state. A skip therefore
never releases a successor over an earlier unit that is still in flight,
accepted but unapplied, or terminally ``FAILED``. An ``ACCEPTED``
predecessor only satisfies the successor once its apply has actually
succeeded (below).

Deliberate corner case, not spelled out elsewhere: a terminally
``FAILED`` predecessor blocks the chain from ever becoming ready past it. Once
the lowest-ordinal ``PENDING`` unit's predecessor is ``FAILED`` (a terminal
state, per ``execution.model.TERMINAL_STATES``), that unit -- and by
construction everything after it -- can never become ready again through this
function, because ``FAILED`` never transitions back to ``ACCEPTED`` or
``SKIPPED``. This is a fail-closed choice: a graph pipeline with a broken link
stalls rather than skipping ahead.

An expired claim is reclaimable, so :func:`ready_wave` can be asked to treat
it as ready (``reclaim_at``): the lowest unit that is ``CLAIMED`` with a lease
at or before ``reclaim_at`` is released like a ``PENDING`` unit, under the
same predecessor rule. A unit behind a LIVE claim is never released. The
worker's own ``claim`` performs the reclaim (fence + 1, EXPIRE attempt), so
the bounded-reclaim limit still applies. Without ``reclaim_at`` only
``PENDING`` units are ready.

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
accepted into the store at submit time (submit-time acceptance is authoritative); it does not mean
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
A run whose consumer requests interrupts can also be blocked on a person: a
loop must end its pass when the wave is empty, ``finalize_run`` applied
nothing, and ``execution.interrupts.waiting_units`` returns a unit, because
retrying cannot move a ``WAITING`` unit -- only ``resolve_interrupt`` or
``expire_interrupts`` can.

An interrupted apply: a crash between ``record_apply_started`` and
``record_apply_succeeded`` leaves a unit whose last apply-kind attempt is
``APPLY_STARTED`` with no following ``APPLY_SUCCEEDED``. This function
withholds the successor in that state too, but only until the next
``finalize_run``: finalize applies the unit again (``RunAdapter.apply`` is
repeat-safe by contract), records ``APPLY_SUCCEEDED``, and the successor is
released. See ``execution.controller``'s ``finalize_run`` docstring for the
mechanics.
"""

from __future__ import annotations

from typing import Dict, List, NamedTuple, Optional, Sequence

from content_pipeline.execution.model import (
    TERMINAL_STATES,
    AttemptKind,
    AttemptRecord,
    ExecutionError,
    UnitRecord,
    UnitState,
)
from content_pipeline.execution.parallel_graph import (
    ParallelGraphStrategy,
    is_parallel_graph_strategy,
)
from content_pipeline.execution.scheduler import dependents_closure
from content_pipeline.pipeline.workunit import GraphWalkStrategy, WorkUnitStrategy


class UnsafeGraphParallelismError(ExecutionError):
    """A ``max_wave_size`` greater than 1 was requested against a graph strategy.

    Graph strategies are strictly ordinal-sequential (the one-unit-wave
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


class GraphRegistrationMismatchError(ExecutionError):
    """A parallel graph's node ids differ from the unit ids registered for the run.

    A registered unit outside the graph has no dependency rule, and a graph
    node that was never registered can never settle, so its dependents would
    wait forever. Raised by :func:`ready_wave` and :func:`gated_units`.
    """

    def __init__(self, run_id: str, unregistered: Sequence[str], ungraphed: Sequence[str]) -> None:
        self.run_id = run_id
        self.unregistered = list(unregistered)
        self.ungraphed = list(ungraphed)
        super().__init__(
            f"run {run_id!r}: parallel graph and registered units differ -- "
            f"graph nodes never registered: {self.unregistered!r}; "
            f"registered units outside the graph: {self.ungraphed!r}"
        )


def is_dependency_ordered(strategy: WorkUnitStrategy) -> bool:
    """Return whether ``strategy`` constrains which units may run together.

    True for a sequential graph walk and for a parallel graph; a caller that
    selects units outside :func:`ready_wave` (a wave packer, a reclaim path)
    must narrow its selection to ``ready_wave``'s answer for these.
    """
    return is_graph_strategy(strategy) or is_parallel_graph_strategy(strategy)


def ready_wave(
    store,
    run_id: str,
    strategy: WorkUnitStrategy,
    *,
    max_wave_size: Optional[int] = None,
    reclaim_at: Optional[float] = None,
) -> List[UnitRecord]:
    """Return the units currently claimable for ``run_id`` under ``strategy``.

    See the module docstring for the flat vs. graph semantics. ``max_wave_size``
    caps a flat or parallel-graph wave's length (a parallel graph is also
    capped by its ``capacity``, and ``reclaim_at`` applies to it as to the
    sequential graph); against a graph walk strategy, any ``max_wave_size``
    greater than 1 raises :class:`UnsafeGraphParallelismError` immediately,
    before any store read. ``reclaim_at`` (graph strategies only) is a clock
    reading: a ``CLAIMED`` unit whose lease expired at or before it counts as
    ready (see the module docstring); ``None`` keeps ``PENDING``-only
    readiness.

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
        return _graph_ready_wave(store, run_id, reclaim_at)
    if is_parallel_graph_strategy(strategy):
        wave = _parallel_graph_view(store, run_id, strategy, reclaim_at).ready
        return wave[:max_wave_size] if max_wave_size is not None else wave
    return _flat_ready_wave(store, run_id, max_wave_size)


def _flat_ready_wave(store, run_id: str, max_wave_size: Optional[int]) -> List[UnitRecord]:
    units = sorted(store.list_units(run_id), key=lambda u: u.ordinal)
    pending = [u for u in units if u.state is UnitState.PENDING]
    if max_wave_size is not None:
        pending = pending[:max_wave_size]
    return pending


# The only attempt kinds `_last_apply_kind` ever consults -- shared by
# `_graph_ready_wave` and `graph_block_reason` so each narrows its own
# `store.snapshot` read to exactly this set (see `_fetch_attempt_rows`'s
# `attempt_kinds` filter in `execution.store`) instead of materializing
# every attempt row of the run.
_APPLY_KINDS = (
    AttemptKind.APPLY_STARTED,
    AttemptKind.APPLY_SUCCEEDED,
    AttemptKind.APPLY_REJECTED,
)


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


def _attempts_by_unit(attempts: Sequence[AttemptRecord]) -> Dict[str, List[AttemptRecord]]:
    """Group ``attempts`` by ``unit_id``, preserving each unit's own order.

    Underscore-private (not in ``__all__``) but NOT module-local: imported
    directly by ``execution.controller`` (``_validate_no_unapplied_accepted``),
    which otherwise repeats this exact grouping loop over its own
    ``store.snapshot`` read -- same status as :func:`_last_apply_kind` above,
    which that module also imports rather than reimplementing.
    """
    grouped: Dict[str, List[AttemptRecord]] = {}
    for a in attempts:
        grouped.setdefault(a.unit_id, []).append(a)
    return grouped


class _PendingLookup(NamedTuple):
    """The lowest-ordinal ``PENDING`` unit for a graph-strategy run (or
    ``None`` if there is none), plus its immediate predecessor's state and
    (if that predecessor is ``ACCEPTED``) its last apply-kind attempt.
    Everything :func:`_graph_ready_wave` and :func:`graph_block_reason` need
    to compute their own, differently-shaped return value -- see
    :func:`_next_pending`, the walk shared by both.
    """

    unit: Optional[UnitRecord]
    predecessor_state: Optional[UnitState]
    predecessor_id: Optional[str]
    predecessor_last_apply_kind: Optional[AttemptKind]
    predecessor_lease_expires_at: Optional[float] = None


def _reclaimable(unit: UnitRecord, reclaim_at: Optional[float]) -> bool:
    return (
        reclaim_at is not None
        and unit.state is UnitState.CLAIMED
        and unit.lease_expires_at is not None
        and unit.lease_expires_at <= reclaim_at
    )


def _next_pending(store, run_id: str, reclaim_at: Optional[float] = None) -> _PendingLookup:
    """Walk ``run_id``'s units in ordinal order and locate the lowest-ordinal
    ``PENDING`` one, alongside its immediate predecessor's context.

    Shared by :func:`_graph_ready_wave` and :func:`graph_block_reason`, which
    otherwise each run this identical walk and differ only in what they return
    once it finds (or fails to find) a ``PENDING`` unit -- one maps the
    result to a ``List[UnitRecord]`` wave, the other to a diagnostic string.
    ``store.snapshot``'s ``attempt_kinds`` filter (pushed into the SQL read,
    inside the same read transaction) narrows the attempt rows read to the
    three apply-kinds :func:`_last_apply_kind` ever consults, instead of
    materializing and objectifying every attempt row of the run just to
    discard the rest in Python. Reading units and attempts on two SEPARATE
    connections would still reopen the torn-read window ``snapshot`` exists
    to close (see the module docstring) -- this filter avoids that trade
    rather than taking it: one read transaction, a narrower attempt query.
    """
    _run, all_units, attempts = store.snapshot(run_id, attempt_kinds=_APPLY_KINDS)
    units = sorted(all_units, key=lambda u: u.ordinal)
    attempts_by_unit = _attempts_by_unit(attempts)

    predecessor: Optional[UnitRecord] = None
    for unit in units:
        if unit.state is UnitState.PENDING or _reclaimable(unit, reclaim_at):
            if predecessor is None:
                return _PendingLookup(unit, None, None, None)
            last_apply_kind = (
                _last_apply_kind(attempts_by_unit.get(predecessor.unit_id, []))
                if predecessor.state is UnitState.ACCEPTED
                else None
            )
            return _PendingLookup(
                unit,
                predecessor.state,
                predecessor.unit_id,
                last_apply_kind,
                predecessor.lease_expires_at,
            )
        if unit.state is not UnitState.SKIPPED:
            predecessor = unit
    if predecessor is None:
        return _PendingLookup(None, None, None, None)
    return _PendingLookup(None, predecessor.state, predecessor.unit_id, None)


def _graph_ready_wave(
    store, run_id: str, reclaim_at: Optional[float] = None
) -> List[UnitRecord]:
    lookup = _next_pending(store, run_id, reclaim_at)
    if lookup.unit is None:
        return []
    if lookup.predecessor_state is None:
        return [lookup.unit]
    if (
        lookup.predecessor_state is UnitState.ACCEPTED
        and lookup.predecessor_last_apply_kind is AttemptKind.APPLY_SUCCEEDED
    ):
        return [lookup.unit]
    return []


def graph_block_reason(
    store, run_id: str, strategy: WorkUnitStrategy, *, at: Optional[float] = None
) -> Optional[str]:
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
    - a predecessor whose apply was interrupted (``APPLY_STARTED`` with no
      following ``APPLY_SUCCEEDED``) -- names rerunning ``finalize_run`` as
      the fix (see the module docstring's "An interrupted apply").
    - a terminally ``FAILED`` predecessor -- names the block as permanent.
    - a ``WAITING`` predecessor -- names the open interrupt as the cause.
    - an ``OPERATOR_REJECTED`` or ``INTERRUPT_EXPIRED`` predecessor -- names
      the block as permanent, as for ``FAILED``.
    - a ``CLAIMED`` predecessor whose lease expired at or before ``at`` --
      names it as expired and reclaimable rather than in flight (``at`` is
      the caller's clock reading; without it every claim reads as live).
    - any other non-terminal predecessor state (e.g. ``CLAIMED``) -- names
      the state as still in flight.

    Read-only: performs exactly one ``store.snapshot(run_id)`` read, the same
    call :func:`_graph_ready_wave` makes (via the shared :func:`_next_pending`
    walk), and never raises on its own account.
    """
    if not is_graph_strategy(strategy):
        return None
    lookup = _next_pending(store, run_id)
    unit = lookup.unit
    if unit is None:
        return None  # no PENDING unit at all; nothing to diagnose
    predecessor_state = lookup.predecessor_state
    predecessor_id = lookup.predecessor_id
    if predecessor_state is None:
        return None  # actually ready; nothing to diagnose
    if predecessor_state is UnitState.FAILED:
        return (
            f"unit {unit.unit_id!r} is blocked: predecessor "
            f"{predecessor_id!r} is terminally FAILED, which "
            "permanently blocks the chain"
        )
    if predecessor_state is UnitState.WAITING:
        return (
            f"unit {unit.unit_id!r} is blocked: predecessor "
            f"{predecessor_id!r} is waiting on an open interrupt; nothing "
            "behind it is released until the interrupt is resolved"
        )
    if predecessor_state in (UnitState.OPERATOR_REJECTED, UnitState.INTERRUPT_EXPIRED):
        return (
            f"unit {unit.unit_id!r} is blocked: predecessor "
            f"{predecessor_id!r} is terminally "
            f"{predecessor_state.value.upper()} (its interrupt closed under "
            "the stop policy), which permanently blocks the chain"
        )
    if predecessor_state is UnitState.ACCEPTED:
        last = lookup.predecessor_last_apply_kind
        if last is AttemptKind.APPLY_SUCCEEDED:
            return None  # actually ready; nothing to diagnose
        if last is AttemptKind.APPLY_STARTED:
            return (
                f"unit {unit.unit_id!r} is blocked: predecessor "
                f"{predecessor_id!r} previous apply has no recorded "
                "success; rerun finalize_run"
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
    if (
        predecessor_state is UnitState.CLAIMED
        and at is not None
        and lookup.predecessor_lease_expires_at is not None
        and lookup.predecessor_lease_expires_at <= at
    ):
        return (
            f"unit {unit.unit_id!r} is blocked: predecessor "
            f"{predecessor_id!r} is claimed but its lease expired; it is "
            "reclaimable and the next wave will re-offer it"
        )
    return (
        f"unit {unit.unit_id!r} is blocked: predecessor "
        f"{predecessor_id!r} is {predecessor_state.value} (not yet "
        "ACCEPTED or SKIPPED)"
    )


class _ParallelGraphView(NamedTuple):
    ready: List[UnitRecord]
    gated: List[UnitRecord]


_BROKEN_STATES = (UnitState.FAILED, UnitState.OPERATOR_REJECTED, UnitState.INTERRUPT_EXPIRED)


def _parallel_graph_view(
    store, run_id: str, strategy: ParallelGraphStrategy, reclaim_at: Optional[float]
) -> _ParallelGraphView:
    """Ready and gated units of a parallel-graph run, from one snapshot read."""
    _run, all_units, attempts = store.snapshot(run_id, attempt_kinds=_APPLY_KINDS)
    units = sorted(all_units, key=lambda u: u.ordinal)
    graph = strategy.dependencies
    registered = {u.unit_id for u in units}
    if registered != set(graph):
        raise GraphRegistrationMismatchError(
            run_id,
            [n for n in graph if n not in registered],
            [u.unit_id for u in units if u.unit_id not in graph],
        )
    attempts_by_unit = _attempts_by_unit(attempts)
    settled = set()
    broken = set()
    for u in units:
        if u.state is UnitState.SKIPPED:
            settled.add(u.unit_id)
        elif u.state is UnitState.ACCEPTED:
            last = _last_apply_kind(attempts_by_unit.get(u.unit_id, []))
            if last is AttemptKind.APPLY_SUCCEEDED:
                settled.add(u.unit_id)
            elif last is AttemptKind.APPLY_REJECTED:
                broken.add(u.unit_id)
        elif u.state in _BROKEN_STATES:
            broken.add(u.unit_id)
    blocked = dependents_closure(graph, broken) - broken
    gated = [u for u in units if u.unit_id in blocked and u.state not in TERMINAL_STATES]
    in_flight = sum(
        1
        for u in units
        if u.state is UnitState.CLAIMED and not _reclaimable(u, reclaim_at)
    )
    candidates = [
        u
        for u in units
        if (u.state is UnitState.PENDING or _reclaimable(u, reclaim_at))
        and u.unit_id not in blocked
        and all(dep in settled for dep in graph[u.unit_id])
    ]
    candidates.sort(key=lambda u: 1 if u.unit_id in strategy.deferred else 0)
    slots = max(0, strategy.capacity - in_flight)
    return _ParallelGraphView(candidates[:slots], gated)


def gated_units(store, run_id: str, strategy: WorkUnitStrategy) -> List[UnitRecord]:
    """Unfinished units of a parallel-graph run that can never be released.

    A unit is gated when a dependency, direct or transitive, is broken
    (``FAILED``, ``OPERATOR_REJECTED``, ``INTERRUPT_EXPIRED``, or ``ACCEPTED``
    with its apply refused). A drain loop is done when every unit
    :func:`~content_pipeline.execution.controller.unfinished_units` returns
    is gated. Read-only, one snapshot read. Raises ``TypeError`` for any
    strategy that is not a
    :class:`~content_pipeline.execution.parallel_graph.ParallelGraphStrategy`
    (a sequential graph's block is diagnosed by :func:`graph_block_reason`).
    """
    if not is_parallel_graph_strategy(strategy):
        raise TypeError("gated_units needs a ParallelGraphStrategy")
    return _parallel_graph_view(store, run_id, strategy, None).gated


__all__ = [
    "UnsafeGraphParallelismError",
    "GraphRegistrationMismatchError",
    "is_graph_strategy",
    "is_dependency_ordered",
    "ready_wave",
    "graph_block_reason",
    "gated_units",
]
