"""The pre-generation gate shape: :class:`Gate` and :func:`run_gates`.

A gate returns a reason to stop or ``None`` to pass; the first firing gate
short-circuits. Both the untracked ``single_pass.run_single_pass`` loop and the
tracked ``execution.controller.prepare_run`` consume this shape, so it lives in
its own module that imports nothing from ``single_pass``.
``pipeline.single_pass`` re-exports both names, so
``from content_pipeline.pipeline.single_pass import Gate, run_gates`` keeps
working.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from content_pipeline.pipeline.workunit import WorkUnit


@dataclass(frozen=True)
class Gate:
    """One ordered pre-generation gate.

    - ``name`` -- diagnostic label (surfaced on the outcome).
    - ``predicate`` -- ``WorkUnit -> Optional[str]``: a reason string stops the
      unit; ``None`` passes it to the next gate.
    - ``sticky`` -- when True, a firing gate marks the unit unsupported (a
      structural "this pipeline cannot handle this shape" verdict) rather than
      a transient skip.
    """

    name: str
    predicate: Callable[[WorkUnit], Optional[str]]
    sticky: bool = False


def run_gates(gates: Sequence[Gate], unit: WorkUnit) -> Optional[tuple]:
    """Run ``gates`` in order; return the first ``(Gate, reason)`` that fires.

    Returns ``None`` when every gate passes -- the unit proceeds to the
    freshness check. First-firing gate wins (order is significant: the source
    pipeline runs its override marker before its structural checks so the
    override reason wins when both apply).
    """
    for gate in gates:
        reason = gate.predicate(unit)
        if reason is not None:
            return gate, reason
    return None


__all__ = ["Gate", "run_gates"]
