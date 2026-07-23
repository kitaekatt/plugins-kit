"""The grade -> select -> apply -> fill cycle, driven to a verdict.

Grades the candidate population for every work unit, selects the winner(s),
applies the selection, and fills a fresh candidate where a unit is still
unresolved, then repeats until ``llm.convergence`` returns CONVERGED or
STALLED. Implemented in the last pre-port phase deliberately -- the seams it
needs (the candidate-store schema in ``store.candidate``, the convergence-gate
protocol in ``llm.convergence``) are built earlier, so this module is a
composition, not a redesign, when its first real consumer ports onto it.

Grade-first ordering is load-bearing and NOT caller-controllable (the cold-
start-deadlock regression). On a blank cold-start store, GRADE runs first and
bakes the empty-seed's generation template (via the no-LLM empty fast path) so
the following FILL is *eligible* to produce the first reading. If FILL ran
before GRADE on a cold store, no cell would ever be gradeable and the loop
would deadlock producing nothing. The four stages therefore run in the fixed
order grade -> select -> apply -> fill inside :func:`run_cycle`; a caller
supplies the four stage callables but cannot reorder them.

Deviation from the skeleton: the placeholder ``run(store, providers, grader,
max_cycles)`` is replaced by :func:`run`, which takes the four stage callables
plus a progress ``measure`` and an optional :class:`~content_pipeline.llm.
convergence.ConvergenceGate`. The single opaque ``grader`` / ``providers``
placeholder never captured the four-stage shape both source loops actually
run; the real signature makes each stage explicit and the ordering structural.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Tuple

from content_pipeline.llm.convergence import (
    ConvergenceGate,
    ProgressEvaluator,
    Round,
    Verdict,
)

# A cycle stage: (store, cycle_index) -> anything (may mutate the store in
# place and return None, or return a new store). The loop threads the returned
# store forward when non-None, so both the mutate-in-place and functional
# stage styles compose.
Stage = Callable[[Any, int], Any]

# Progress probe: store -> (produced_this_cycle, outstanding). ``produced`` is
# a per-cycle delta (new readings locked this cycle); ``outstanding`` is the
# count of still-non-terminal units. Both feed a convergence ``Round``.
Measure = Callable[[Any], Tuple[int, int]]


@dataclass(frozen=True)
class CycleResult:
    """Outcome of one :func:`run_cycle`.

    - ``cycle`` -- the 1-based cycle index.
    - ``store`` -- the store after all four stages ran.
    - ``round`` -- the convergence :class:`~content_pipeline.llm.convergence.
      Round` measured after the cycle.
    - ``verdict`` -- the gate's verdict given the history through this cycle.
    """

    cycle: int
    store: Any
    round: Round
    verdict: Verdict


@dataclass
class LoopResult:
    """Outcome of a :func:`run` multi-cycle drive.

    ``cycles`` holds one :class:`CycleResult` per cycle actually run (zero when
    the store was already CONVERGED before the first cycle). ``verdict`` is the
    final gate verdict; ``converged`` / ``stalled`` are its two terminal
    convenience flags. ``store`` is the final store.
    """

    store: Any
    cycles: List[CycleResult] = field(default_factory=list)
    verdict: Verdict = Verdict.CONTINUE
    history: List[Round] = field(default_factory=list)

    @property
    def cycles_run(self) -> int:
        return len(self.cycles)

    @property
    def converged(self) -> bool:
        return self.verdict is Verdict.CONVERGED

    @property
    def stalled(self) -> bool:
        return self.verdict is Verdict.STALLED


def _apply_stage(store: Any, stage: Optional[Stage], cycle: int) -> Any:
    """Run one optional stage, threading a returned store forward."""
    if stage is None:
        return store
    result = stage(store, cycle)
    return store if result is None else result


def run_cycle(
    store: Any,
    cycle: int,
    *,
    grade: Optional[Stage],
    select: Optional[Stage],
    apply: Optional[Stage],
    fill: Optional[Stage],
    measure: Measure,
) -> CycleResult:
    """Run ONE cycle in the fixed order grade -> select -> apply -> fill.

    The order is structural, not a parameter: GRADE must precede FILL so a
    cold-start store's empty seed is baked gradeable before FILL tries to
    produce the first reading (the cold-start-deadlock guard). ``measure`` is
    read AFTER fill so ``outstanding`` reflects the cycle's end state.

    The gate verdict is computed by the caller (:func:`run`) over the full
    history; ``run_cycle`` fills in :attr:`CycleResult.verdict` as CONTINUE and
    lets the caller overwrite it -- a single cycle in isolation has no window.
    """
    store = _apply_stage(store, grade, cycle)
    store = _apply_stage(store, select, cycle)
    store = _apply_stage(store, apply, cycle)
    store = _apply_stage(store, fill, cycle)
    produced, outstanding = measure(store)
    return CycleResult(
        cycle=cycle,
        store=store,
        round=Round(produced=produced, outstanding=outstanding),
        verdict=Verdict.CONTINUE,
    )


def run(
    store: Any,
    *,
    grade: Optional[Stage] = None,
    select: Optional[Stage] = None,
    apply: Optional[Stage] = None,
    fill: Optional[Stage] = None,
    measure: Measure,
    max_cycles: int,
    gate: Optional[ConvergenceGate] = None,
    start_cycle: int = 1,
) -> LoopResult:
    """Drive up to ``max_cycles`` grade/select/apply/fill cycles to a verdict.

    Pre-loop gate: ``measure(store)`` is read once before any cycle; if the
    store is already terminal (``outstanding == 0``) the gate is consulted with
    a single zero-produced round and, when it says CONVERGED, ZERO cycles run
    (no wasted stage work) -- the source loop's "already-converged store runs
    no cycle" short-circuit.

    Otherwise it runs cycles ``start_cycle .. start_cycle + max_cycles - 1``,
    stopping the instant the gate returns CONVERGED (outstanding drained) or
    STALLED (no progress across the gate's stall window while outstanding work
    remains -- continuing would only burn budget). ``gate`` defaults to a
    :class:`~content_pipeline.llm.convergence.ProgressEvaluator`.
    """
    if max_cycles < 0:
        raise ValueError(f"max_cycles must be >= 0, got {max_cycles}")

    gate = gate if gate is not None else ProgressEvaluator()
    result = LoopResult(store=store)

    # Pre-loop short-circuit: an already-terminal store may converge with zero
    # cycles. Probe with a single zero-produced round representing "current
    # state, no work done this observation".
    _produced, outstanding = measure(store)
    pre_history = [Round(produced=0, outstanding=outstanding)]
    if gate.evaluate(pre_history) is Verdict.CONVERGED:
        result.verdict = Verdict.CONVERGED
        result.history = pre_history
        return result

    for offset in range(max_cycles):
        cycle = start_cycle + offset
        cycle_result = run_cycle(
            store,
            cycle,
            grade=grade,
            select=select,
            apply=apply,
            fill=fill,
            measure=measure,
        )
        store = cycle_result.store
        result.history.append(cycle_result.round)
        verdict = gate.evaluate(result.history)
        cycle_result = CycleResult(
            cycle=cycle_result.cycle,
            store=store,
            round=cycle_result.round,
            verdict=verdict,
        )
        result.cycles.append(cycle_result)
        result.store = store
        result.verdict = verdict
        if verdict in (Verdict.CONVERGED, Verdict.STALLED):
            break

    return result


__all__ = [
    "Stage",
    "Measure",
    "CycleResult",
    "LoopResult",
    "run_cycle",
    "run",
]
