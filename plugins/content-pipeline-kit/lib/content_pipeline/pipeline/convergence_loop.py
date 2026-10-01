"""The grade -> select -> apply -> fill cycle, driven to a verdict.

Grades the candidate population for every work unit, selects the winner(s),
applies the selection, and fills a fresh candidate where a unit is still
unresolved, then repeats until ``llm.convergence`` returns CONVERGED, STALLED
or FAILED. Implemented in the last pre-port phase deliberately -- the seams it
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

Observers (:data:`LoopObserver`) see a :class:`LoopEvent` at every boundary:
loop, cycle and stage start/finish, and a stage failure. They are synchronous
and read-only; the stage order stays structural.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Callable, List, Mapping, Optional, Sequence, Tuple, Union

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

# Progress probe: store -> (produced_this_cycle, outstanding), or a full
# Round. ``produced`` is a per-cycle delta (new readings locked this cycle);
# ``outstanding`` is the count of still-non-terminal units. A tuple is coerced
# to ``Round(produced, outstanding)``.
Measure = Callable[[Any], Union[Tuple[int, int], Round]]

# The four stages, in the fixed order run_cycle runs them.
STAGES: Tuple[str, ...] = ("grade", "select", "apply", "fill")


class LoopEventKind(str, Enum):
    """What a :class:`LoopEvent` reports."""

    LOOP_STARTED = "loop_started"
    CYCLE_STARTED = "cycle_started"
    STAGE_STARTED = "stage_started"
    STAGE_FINISHED = "stage_finished"
    STAGE_FAILED = "stage_failed"
    CYCLE_FINISHED = "cycle_finished"
    LOOP_FINISHED = "loop_finished"


@dataclass(frozen=True)
class LoopEvent:
    """One observation of the loop, delivered synchronously to each observer.

    ``at`` is ``time.time()``; ``elapsed_s`` is monotonic and set on the
    ``*_FINISHED`` kinds and ``STAGE_FAILED``. ``stage`` is one of
    :data:`STAGES`. ``store`` is the store at that moment: observers observe,
    they never replace it.
    """

    kind: LoopEventKind
    cycle: Optional[int] = None
    stage: Optional[str] = None
    store: Any = None
    at: float = 0.0
    elapsed_s: Optional[float] = None
    round: Optional[Round] = None
    verdict: Optional[Verdict] = None
    error: Optional[BaseException] = None


LoopObserver = Callable[[LoopEvent], None]


def _emit(
    observers: Sequence[LoopObserver], kind: LoopEventKind, **fields: Any
) -> None:
    if not observers:
        return
    event = LoopEvent(kind=kind, at=time.time(), **fields)
    for observer in observers:
        observer(event)


def _as_round(measured: Union[Tuple[int, int], Round]) -> Round:
    if isinstance(measured, Round):
        return measured
    produced, outstanding = measured
    return Round(produced=produced, outstanding=outstanding)


@dataclass(frozen=True)
class CycleResult:
    """Outcome of one :func:`run_cycle`.

    - ``cycle`` -- the 1-based cycle index.
    - ``store`` -- the store after all four stages ran.
    - ``round`` -- the convergence :class:`~content_pipeline.llm.convergence.
      Round` measured after the cycle.
    - ``verdict`` -- the gate's verdict given the history through this cycle.
    - ``stage_seconds`` -- monotonic seconds per stage that ran (excluded from
      hashing).
    """

    cycle: int
    store: Any
    round: Round
    verdict: Verdict
    stage_seconds: Mapping[str, float] = field(default_factory=dict, hash=False)


@dataclass
class LoopResult:
    """Outcome of a :func:`run` multi-cycle drive.

    ``cycles`` holds one :class:`CycleResult` per cycle actually run (zero when
    the store was already CONVERGED or FAILED before the first cycle).
    ``verdict`` is the final gate verdict; ``converged`` / ``stalled`` /
    ``failed`` are its terminal convenience flags. ``store`` is the final store.
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

    @property
    def failed(self) -> bool:
        return self.verdict is Verdict.FAILED


def _apply_stage(
    store: Any,
    stage: Optional[Stage],
    cycle: int,
    name: str,
    observers: Sequence[LoopObserver],
    seconds: dict,
) -> Any:
    """Run one optional stage, threading a returned store forward."""
    if stage is None:
        return store
    _emit(observers, LoopEventKind.STAGE_STARTED, cycle=cycle, stage=name, store=store)
    started = time.monotonic()
    try:
        result = stage(store, cycle)
    except BaseException as exc:
        try:
            _emit(
                observers,
                LoopEventKind.STAGE_FAILED,
                cycle=cycle,
                stage=name,
                store=store,
                elapsed_s=time.monotonic() - started,
                error=exc,
            )
        except Exception as observer_error:
            # Never mask the stage error with the observer's.
            raise exc from observer_error
        raise
    elapsed = time.monotonic() - started
    seconds[name] = elapsed
    store = store if result is None else result
    _emit(
        observers,
        LoopEventKind.STAGE_FINISHED,
        cycle=cycle,
        stage=name,
        store=store,
        elapsed_s=elapsed,
    )
    return store


def _run_cycle_core(
    store: Any,
    cycle: int,
    grade: Optional[Stage],
    select: Optional[Stage],
    apply: Optional[Stage],
    fill: Optional[Stage],
    measure: Measure,
    observers: Sequence[LoopObserver],
) -> Tuple[CycleResult, float]:
    _emit(observers, LoopEventKind.CYCLE_STARTED, cycle=cycle, store=store)
    started = time.monotonic()
    seconds: dict = {}
    for name, stage in zip(STAGES, (grade, select, apply, fill)):
        store = _apply_stage(store, stage, cycle, name, observers, seconds)
    measured = _as_round(measure(store))
    result = CycleResult(
        cycle=cycle,
        store=store,
        round=measured,
        verdict=Verdict.CONTINUE,
        stage_seconds=seconds,
    )
    return result, time.monotonic() - started


def run_cycle(
    store: Any,
    cycle: int,
    *,
    grade: Optional[Stage],
    select: Optional[Stage],
    apply: Optional[Stage],
    fill: Optional[Stage],
    measure: Measure,
    observers: Sequence[LoopObserver] = (),
) -> CycleResult:
    """Run ONE cycle in the fixed order grade -> select -> apply -> fill.

    The order is structural, not a parameter: GRADE must precede FILL so a
    cold-start store's empty seed is baked gradeable before FILL tries to
    produce the first reading (the cold-start-deadlock guard). ``measure`` is
    read AFTER fill so ``outstanding`` reflects the cycle's end state.

    The gate verdict is computed by the caller (:func:`run`) over the full
    history; ``run_cycle`` fills in :attr:`CycleResult.verdict` as CONTINUE and
    lets the caller overwrite it -- a single cycle in isolation has no window.

    ``observers`` receive a :class:`LoopEvent` synchronously, in order, on this
    thread. An observer exception propagates; when it is raised while handling
    STAGE_FAILED, the original stage exception is re-raised with the observer
    error chained to it, so a stage error is never masked.
    """
    result, elapsed = _run_cycle_core(
        store, cycle, grade, select, apply, fill, measure, observers
    )
    _emit(
        observers,
        LoopEventKind.CYCLE_FINISHED,
        cycle=cycle,
        store=result.store,
        elapsed_s=elapsed,
        round=result.round,
        verdict=result.verdict,
    )
    return result


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
    observers: Sequence[LoopObserver] = (),
) -> LoopResult:
    """Drive up to ``max_cycles`` grade/select/apply/fill cycles to a verdict.

    Pre-loop gate: ``measure(store)`` is read once before any cycle, coerced to
    a :class:`~content_pipeline.llm.convergence.Round` with ``produced=0``
    (``failed`` / ``terminal`` are kept). When the gate says CONVERGED or
    FAILED for that single round, ZERO cycles run (no wasted stage work).

    Otherwise it runs cycles ``start_cycle .. start_cycle + max_cycles - 1``,
    stopping the instant the gate returns CONVERGED, FAILED (outstanding
    drained with failures) or STALLED (no progress across the gate's stall
    window while outstanding work remains). ``gate`` defaults to a
    :class:`~content_pipeline.llm.convergence.ProgressEvaluator`.

    ``observers`` are called synchronously with a :class:`LoopEvent`; their
    return value is ignored and an exception from one stops the loop.
    """
    if max_cycles < 0:
        raise ValueError(f"max_cycles must be >= 0, got {max_cycles}")

    gate = gate if gate is not None else ProgressEvaluator()
    result = LoopResult(store=store)
    loop_started = time.monotonic()
    _emit(observers, LoopEventKind.LOOP_STARTED, store=store)

    def _finish() -> LoopResult:
        _emit(
            observers,
            LoopEventKind.LOOP_FINISHED,
            store=result.store,
            elapsed_s=time.monotonic() - loop_started,
            verdict=result.verdict,
        )
        return result

    # Pre-loop short-circuit: a terminal store may finish with zero cycles.
    # Probe with a single zero-produced round representing "current state, no
    # work done this observation"; failed / terminal survive the replace.
    pre_round = replace(_as_round(measure(store)), produced=0)
    pre_history = [pre_round]
    pre_verdict = gate.evaluate(pre_history)
    if pre_verdict in (Verdict.CONVERGED, Verdict.FAILED):
        result.verdict = pre_verdict
        result.history = pre_history
        return _finish()

    for offset in range(max_cycles):
        cycle = start_cycle + offset
        cycle_result, elapsed = _run_cycle_core(
            store, cycle, grade, select, apply, fill, measure, observers
        )
        store = cycle_result.store
        result.history.append(cycle_result.round)
        verdict = gate.evaluate(result.history)
        cycle_result = replace(cycle_result, verdict=verdict)
        result.cycles.append(cycle_result)
        result.store = store
        result.verdict = verdict
        _emit(
            observers,
            LoopEventKind.CYCLE_FINISHED,
            cycle=cycle,
            store=store,
            elapsed_s=elapsed,
            round=cycle_result.round,
            verdict=verdict,
        )
        if verdict in (Verdict.CONVERGED, Verdict.STALLED, Verdict.FAILED):
            break

    return _finish()


__all__ = [
    "Stage",
    "Measure",
    "STAGES",
    "LoopEventKind",
    "LoopEvent",
    "LoopObserver",
    "CycleResult",
    "LoopResult",
    "run_cycle",
    "run",
]
