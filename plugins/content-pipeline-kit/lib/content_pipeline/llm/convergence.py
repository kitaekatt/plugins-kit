"""Convergence gate: CONVERGED / STALLED / CONTINUE verdicts.

Drives a fill -> grade -> select -> apply cycle toward a stopping decision.
Generalizes loc's ``trial.py`` convergence classifier (its CONVERGED / STALLED
verdicts) to a progress-based evaluator with no domain vocabulary:

- **CONVERGED** -- every unit of work is terminal (no outstanding work), and
  that has held for a stability window. There is nothing left to improve.
- **STALLED** -- outstanding work remains but the last N rounds produced no
  new progress: the loop is spinning without locking anything, so a cycle
  budget would only burn.
- **CONTINUE** -- neither terminal condition holds; run another cycle.

This is an opt-in component (CRP): a single-pass pipeline never reaches this
module. It is pure -- a fold over a history of :class:`Round` records the
caller supplies (each round: how much NEW work was produced, how much remains
outstanding). The thresholds (stall window, converge window) are parameters,
so a caller tunes the no-progress patience without editing the gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, Sequence, runtime_checkable


class Verdict(str, Enum):
    """A convergence gate's stopping decision."""

    CONVERGED = "converged"
    STALLED = "stalled"
    CONTINUE = "continue"


@dataclass(frozen=True)
class Round:
    """One cycle's progress signal.

    - ``produced`` -- units of NEW work locked/produced this round (a per-cycle
      delta, not a cumulative total). Zero means the round made no progress.
    - ``outstanding`` -- non-terminal units still remaining after this round.
      Zero means everything is terminal.
    """

    produced: int
    outstanding: int


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
    - ``converge_window`` -- number of trailing rounds that must ALL show zero
      outstanding work before declaring CONVERGED. Default 1 (converge as soon
      as outstanding hits zero, loc's behavior); raise it to require the empty
      state to persist for stability.

    Precedence: CONVERGED is checked before STALLED, so a run that both drained
    its outstanding work and stopped producing classifies as converged, not
    stalled.
    """

    stall_window: int = 2
    converge_window: int = 1

    def evaluate(self, history: Sequence[Round]) -> Verdict:
        """Classify the run given its cycle-by-cycle history.

        An empty history is CONTINUE (nothing has run yet). Otherwise:

        1. CONVERGED when the last ``converge_window`` rounds all have
           ``outstanding == 0`` (and at least that many rounds exist).
        2. STALLED when outstanding work remains after the last round AND the
           last ``stall_window`` rounds all have ``produced == 0`` (and at
           least that many rounds exist).
        3. CONTINUE otherwise.
        """
        if not history:
            return Verdict.CONTINUE

        if len(history) >= self.converge_window and all(
            r.outstanding == 0 for r in history[-self.converge_window :]
        ):
            return Verdict.CONVERGED

        last = history[-1]
        if (
            last.outstanding > 0
            and len(history) >= self.stall_window
            and all(r.produced == 0 for r in history[-self.stall_window :])
        ):
            return Verdict.STALLED

        return Verdict.CONTINUE


def evaluate(
    history: Sequence[Round],
    *,
    stall_window: int = 2,
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
