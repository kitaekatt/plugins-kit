"""Deterministic fact riders downstream stages reuse.

A rider is a fact a validator derives deterministically from a candidate
(e.g. a width measurement, a token-count, a protected-token check) that a
later stage (selection, delivery, audit) needs again. Riders are computed
once by the validator and carried alongside the candidate rather than
re-derived at each downstream site -- re-deriving risks two sites disagreeing
on a fact that should be singular.
"""


def attach_riders(candidate: dict, riders: dict) -> dict:
    """Attach deterministic fact riders to a candidate record."""
    raise NotImplementedError
