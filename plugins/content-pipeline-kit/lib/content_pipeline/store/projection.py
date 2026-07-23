"""Canonical store -> consumer-visible projected result.

The canonical store carries attribution metadata, candidate history, and
intermediary state a downstream consumer does not need. Projection strips
that machinery down to the plain value shape a delivery mode or an external
reader expects, applying the effective-value precedence from ``attributed``
(and, for multi-candidate fields, the active-candidate pick from
``candidate``) along the way.
"""


def project(store_record: dict) -> dict:
    """Reduce a canonical store record to its consumer-visible projected form."""
    raise NotImplementedError
