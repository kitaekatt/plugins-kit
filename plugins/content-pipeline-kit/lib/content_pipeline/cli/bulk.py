"""Two-phase cache-warm bulk worker.

Runs a bulk operation over many units in two phases: a WARM phase that primes
shared caches once (the source system's "group same-speaker calls so the prompt
cache stays warm" discipline generalized to "prime the shared cache before the
per-unit loop"), then a per-unit WORKER phase that processes each unit with
per-unit error isolation. Separating the phases lets a bulk run be interrupted
and resumed without redoing already-warm work, and keeps one bad unit from
aborting the batch.

Deviation from the skeleton: ``run_bulk(entities, stage, cache_dir)`` is
generalized to :func:`run_bulk(units, worker, *, warm=..., ...)`. The bare
``stage`` / ``cache_dir`` pair assumed the caller wanted this module to own the
cache; instead the WARM callable owns whatever priming the consumer needs
(seeding a ``ResponseCache`` directory, loading a shared glossary), and the
WORKER owns the per-unit call -- so this module stays free of ``llm`` and of any
cache substrate. Halt handling is delegated to ``cli.budget.guarded_sweep``
when the caller opts in, keeping the ``HaltError`` import in one place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Sequence, Tuple

from content_pipeline.cli.budget import BudgetStop, SweepResult, guarded_sweep


@dataclass
class BulkResult:
    """Outcome of a :func:`run_bulk` run.

    - ``warmed`` -- True when the warm phase ran (False when no ``warm`` given).
    - ``done`` -- ``(unit, result)`` for units the worker completed.
    - ``errors`` -- ``(unit, message)`` for isolated per-unit failures.
    - ``halted`` -- the :class:`~content_pipeline.cli.budget.BudgetStop` that
      stopped the run, or ``None``.
    - ``remaining`` -- units not attempted after a halt.
    """

    warmed: bool = False
    done: List[Tuple[Any, Any]] = field(default_factory=list)
    errors: List[Tuple[Any, str]] = field(default_factory=list)
    halted: Optional[BudgetStop] = None
    remaining: List[Any] = field(default_factory=list)

    @property
    def stopped(self) -> bool:
        return self.halted is not None

    @property
    def ok_count(self) -> int:
        return len(self.done)


def run_bulk(
    units: Sequence[Any],
    worker: Callable[[Any], Any],
    *,
    warm: Optional[Callable[[], None]] = None,
    guard_halts: bool = True,
    isolate_errors: bool = True,
) -> BulkResult:
    """Run the two-phase cache-warm bulk worker over ``units``.

    1. **Warm phase** -- when ``warm`` is given, call it ONCE before the loop to
       prime shared caches. A warm-phase exception propagates immediately (a
       failed prime means the run cannot proceed correctly).
    2. **Worker phase** -- process each unit via ``worker``. With ``guard_halts``
       (default) the loop runs through :func:`~content_pipeline.cli.budget.
       guarded_sweep`, so a ``HaltError`` halts cleanly with partial progress
       and the remaining units recorded; without it, a ``HaltError`` propagates.
       A non-halt error is isolated per unit when ``isolate_errors``.

    Returns a :class:`BulkResult`. A resume run passes ``result.remaining`` (or
    re-runs everything -- already-warm work is a cache hit, so re-processing is
    cheap by construction).
    """
    result = BulkResult()

    if warm is not None:
        warm()
        result.warmed = True

    if guard_halts:
        sweep: SweepResult = guarded_sweep(
            units, worker, isolate_errors=isolate_errors
        )
        result.done = sweep.done
        result.errors = sweep.errors
        result.halted = sweep.halted
        result.remaining = sweep.remaining
        return result

    for unit in units:
        try:
            outcome = worker(unit)
        except Exception as exc:  # noqa: BLE001 -- isolate one unit's failure
            if not isolate_errors:
                raise
            result.errors.append((unit, str(exc)))
            continue
        result.done.append((unit, outcome))
    return result


__all__ = ["BulkResult", "run_bulk"]
