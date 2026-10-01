"""Consumer-owned cell classification, tallied into convergence rounds.

The library knows four outcomes for a cell and nothing about why a consumer
assigns one. A consumer implements :class:`CellPolicy` over its own cell type
(any type, not only ``store.candidate.CandidateCell``); :func:`tally` counts
the outcomes and :func:`measure_from` turns a store into the per-cycle
:class:`~content_pipeline.llm.convergence.Round` that
``pipeline.convergence_loop.run`` consumes.

The terminal classification is derived here from the cells each time, never
stored, so it cannot drift from them.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Iterable, Optional, Protocol, TypeVar

from content_pipeline.llm.convergence import Round

C = TypeVar("C")
C_contra = TypeVar("C_contra", contravariant=True)


class CellOutcome(str, Enum):
    """Where one cell stands."""

    OPEN = "open"
    LOCKED = "locked"
    TERMINAL = "terminal"
    FAILED = "failed"


class CellPolicy(Protocol[C_contra]):
    """The consumer's rule: classify one cell."""

    def outcome(self, cell: C_contra) -> CellOutcome: ...


@dataclass(frozen=True)
class Tally:
    """Counts of cells per outcome."""

    total: int
    open: int
    locked: int
    terminal: int
    failed: int

    def to_round(self, previous: Optional["Tally"] = None) -> Round:
        """Round for this tally.

        ``previous=None`` is a baseline: ``produced=0``. Otherwise
        ``produced = max(0, locked - previous.locked)``. ``outstanding`` is
        ``open``; ``failed`` and ``terminal`` are copied.
        """
        produced = 0 if previous is None else max(0, self.locked - previous.locked)
        return Round(
            produced=produced,
            outstanding=self.open,
            failed=self.failed,
            terminal=self.terminal,
        )


def tally(cells: Iterable[C], policy: CellPolicy[C]) -> Tally:
    """Count ``cells`` by the outcome ``policy`` assigns each."""
    counts = {o: 0 for o in CellOutcome}
    total = 0
    for cell in cells:
        counts[CellOutcome(policy.outcome(cell))] += 1
        total += 1
    return Tally(
        total=total,
        open=counts[CellOutcome.OPEN],
        locked=counts[CellOutcome.LOCKED],
        terminal=counts[CellOutcome.TERMINAL],
        failed=counts[CellOutcome.FAILED],
    )


def measure_from(
    cells_of: Callable[[Any], Iterable[C]], policy: CellPolicy[C]
) -> Callable[[Any], Round]:
    """Build a stateful ``measure`` for ``convergence_loop.run``.

    The first call (the pre-loop probe) records the baseline and reports
    ``produced=0``; each later call reports the locked delta against the
    previous call. One instance serves exactly one ``run`` call; do not share
    it across runs.
    """
    previous: list = []

    def measure(store: Any) -> Round:
        current = tally(cells_of(store), policy)
        rnd = current.to_round(previous[0] if previous else None)
        previous[:] = [current]
        return rnd

    return measure


__all__ = ["CellOutcome", "CellPolicy", "Tally", "tally", "measure_from"]
