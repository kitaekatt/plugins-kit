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
  predecessor (the previous unit by ordinal) is ``ACCEPTED``. The first unit
  (no predecessor) is vacuously ready when it is ``PENDING``.

A predecessor in state ``SKIPPED`` (A-min.2 -- a gate or freshness check
decided that unit will never be generated, see ``execution.controller``) is
treated exactly like ``ACCEPTED``: it satisfies the successor. A skip is not a
broken link, it is a unit the run intentionally will not produce, so it must
not permanently block everything ordinally after it the way a ``FAILED``
predecessor does below.

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
    """The most recent apply-related attempt kind, or ``None`` if never applied."""
    last: Optional[AttemptKind] = None
    for attempt in attempts:
        if attempt.kind in (AttemptKind.APPLY_STARTED, AttemptKind.APPLY_SUCCEEDED):
            last = attempt.kind
    return last


def _graph_ready_wave(store, run_id: str) -> List[UnitRecord]:
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


__all__ = [
    "UnsafeGraphParallelismError",
    "is_graph_strategy",
    "ready_wave",
]
