"""Convergence gate: CONVERGED / STALLED verdicts.

Drives the fill -> grade -> select -> apply cycle in
``pipeline.convergence_loop`` toward a stopping decision. A run is
CONVERGED when successive cycles stop improving the candidate population by
whatever grading signal the pipeline registered; STALLED when the cycle
budget is exhausted without convergence. This is an opt-in component (CRP):
a single-pass pipeline never reaches this module.
"""


def evaluate(history: list) -> str:
    """Return 'CONVERGED' or 'STALLED' given a cycle-by-cycle grading history."""
    raise NotImplementedError
