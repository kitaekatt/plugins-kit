"""The fill -> grade -> select -> apply cycle, driven to a verdict.

Fills the candidate population for a work unit, grades each candidate,
selects the winner(s), applies the selection, and repeats until
``llm.convergence.evaluate`` returns CONVERGED or STALLED. Implemented in
the last pre-port phase deliberately -- the seams it needs (candidate-store
schema, the Stage protocol, the convergence-gate protocol) are built earlier
so this module is a composition, not a redesign, when its first real
consumer ports onto it.
"""


def run(store, providers, grader, max_cycles: int) -> object:
    """Run fill/grade/select/apply cycles until convergence or max_cycles is reached."""
    raise NotImplementedError
