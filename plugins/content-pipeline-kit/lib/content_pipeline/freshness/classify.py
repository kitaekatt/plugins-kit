"""The single freshness predicate: human > missing > stale > fresh.

Every "needs regen" check and every coverage-bucket site in a consuming
pipeline delegates to ``classify`` rather than re-deriving its own staleness
logic. Precedence: a human-attributed value is never reclassified as stale
by a hash mismatch (see ``store.attributed``'s human-always-wins rule);
missing beats stale (nothing to compare yet); stale beats fresh (a hash
mismatch was found). One predicate, one place the rule can be wrong or
right.
"""


def classify(record: dict, current_source_hash: str) -> str:
    """Return one of 'human', 'missing', 'stale', 'fresh' for a stored record."""
    raise NotImplementedError
