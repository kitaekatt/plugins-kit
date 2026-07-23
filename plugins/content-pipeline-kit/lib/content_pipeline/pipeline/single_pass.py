"""The regenerate-on-stale pipeline shape: two-phase generate / apply.

For every entity ``freshness.classify`` marks stale or missing: a generate
phase produces a candidate, an apply phase writes it through
``deliver``. The two phases are kept distinct so a caller can inspect (or
gate) everything about to be applied before any delivery side effect runs.
This is the shape a single-pass consumer ports onto directly; a
convergence-driven consumer instead composes stages under
``convergence_loop``.
"""


def run(store, stages: list) -> object:
    """Run the two-phase generate/apply cycle once over every stale/missing entity."""
    raise NotImplementedError
