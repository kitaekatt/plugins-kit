"""Convergence gate: CONVERGED / STALLED / CONTINUE verdicts.

Drives a fill -> grade -> select -> apply cycle toward a stopping decision.
Generalizes loc's ``trial.py`` convergence classifier (its CONVERGED / STALLED
verdicts) to a progress-based evaluator with no domain vocabulary:

- **CONVERGED** -- every unit of work is terminal (no outstanding work), and
  that has held for a stability window. There is nothing left to improve.
- **STALLED** -- outstanding work remains but the last N rounds produced no
  new progress: the loop is spinning without locking anything, so a cycle
  budget would only burn.
- **FAILED** -- every unit is terminal but the last round reports units whose
  outcome is a failure (``Round.failed > 0``). Only a caller that sets
  ``Round.failed`` can receive it.
- **CONTINUE** -- neither terminal condition holds; run another cycle.

This is an opt-in component (CRP): a single-pass pipeline never reaches this
module. It is pure -- a fold over a history of :class:`Round` records the
caller supplies (each round: how much NEW work was produced, how much remains
outstanding). The thresholds (stall window, converge window) are parameters,
so a caller tunes the no-progress patience without editing the gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional, Protocol, Sequence, runtime_checkable


class Verdict(str, Enum):
    """A convergence gate's stopping decision."""

    CONVERGED = "converged"
    STALLED = "stalled"
    CONTINUE = "continue"
    FAILED = "failed"


@dataclass(frozen=True)
class Round:
    """One cycle's progress signal.

    - ``produced`` -- units of NEW work locked/produced this round (a per-cycle
      delta, not a cumulative total). Zero means the round made no progress.
    - ``outstanding`` -- non-terminal units still remaining after this round.
      Zero means everything is terminal.
    - ``failed`` -- terminal units whose outcome is a failure. When the gate
      sees a drained round with ``failed > 0`` it returns FAILED, not CONVERGED.
    - ``terminal`` -- terminal units that never succeeded (informational).
    - ``detail`` -- opaque caller data; excluded from hashing and equality-free
      of meaning to the gate.
    - ``total`` -- population size (all units, drained or not), when the caller
      knows it. ``None`` means unknown. ``0`` marks an empty population, which
      :class:`ProgressEvaluator` can treat differently from a drained one. It is
      informational and excluded from equality and hashing, like ``detail``.
    """

    produced: int
    outstanding: int
    failed: int = 0
    terminal: int = 0
    detail: Mapping[str, Any] = field(default_factory=dict, hash=False)
    total: Optional[int] = field(default=None, compare=False)


@runtime_checkable
class ConvergenceGate(Protocol):
    """Maps a round history to a :class:`Verdict`."""

    def evaluate(self, history: Sequence[Round]) -> Verdict:
        ...


@dataclass(frozen=True)
class ProgressEvaluator:
    """Progress-based :class:`ConvergenceGate`.

    Parameters:

    - ``stall_window`` -- number of trailing rounds that must ALL show zero
      progress (``produced == 0``) before declaring STALLED, provided
      outstanding work remains. Matches loc's ``_STALL_WINDOW_K`` (2): real
      progress means at least one recent round locked something.
      ``None`` disables STALLED entirely (the gate never stalls).
    - ``converge_window`` -- number of trailing rounds that must ALL show zero
      outstanding work before declaring CONVERGED. Default 1 (converge as soon
      as outstanding hits zero, loc's behavior); raise it to require the empty
      state to persist for stability.
    - ``empty_is_converged`` -- default True: a round with ``outstanding == 0``
      converges even when the population is empty. When False and the latest
      round reports ``total == 0``, the verdict is CONTINUE (an empty
      population is not success). A round whose ``total`` is ``None`` is
      unaffected.

    Precedence: CONVERGED is checked before STALLED, so a run that both drained
    its outstanding work and stopped producing classifies as converged, not
    stalled.
    """

    stall_window: Optional[int] = 2
    converge_window: int = 1
    empty_is_converged: bool = True

    def evaluate(self, history: Sequence[Round]) -> Verdict:
        """Classify the run given its cycle-by-cycle history.

        An empty history is CONTINUE (nothing has run yet). When
        ``empty_is_converged`` is False and the last round has ``total == 0``
        the verdict is CONTINUE. Otherwise:

        1. When the last ``converge_window`` rounds all have
           ``outstanding == 0`` (and at least that many rounds exist): FAILED
           if the last round has ``failed > 0``, else CONVERGED.
        2. STALLED (unless ``stall_window is None``) when outstanding work remains after the last round AND the
           last ``stall_window`` rounds all have ``produced == 0`` (and at
           least that many rounds exist).
        3. CONTINUE otherwise.
        """
        if not history:
            return Verdict.CONTINUE

        if not self.empty_is_converged and history[-1].total == 0:
            return Verdict.CONTINUE

        if len(history) >= self.converge_window and all(
            r.outstanding == 0 for r in history[-self.converge_window :]
        ):
            return Verdict.FAILED if history[-1].failed > 0 else Verdict.CONVERGED

        last = history[-1]
        if (
            self.stall_window is not None
            and last.outstanding > 0
            and len(history) >= self.stall_window
            and all(r.produced == 0 for r in history[-self.stall_window :])
        ):
            return Verdict.STALLED

        return Verdict.CONTINUE


def evaluate(
    history: Sequence[Round],
    *,
    stall_window: Optional[int] = 2,
    converge_window: int = 1,
) -> Verdict:
    """Convenience: build a :class:`ProgressEvaluator` and evaluate ``history``."""
    return ProgressEvaluator(
        stall_window=stall_window, converge_window=converge_window
    ).evaluate(history)


__all__ = [
    "Verdict",
    "Round",
    "ConvergenceGate",
    "ProgressEvaluator",
    "evaluate",
]
