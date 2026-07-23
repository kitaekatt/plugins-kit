"""Deterministic seeding for stochastic gating decisions.

Some freshness-adjacent decisions are intentionally stochastic (e.g. sampling
a subset of stale entities for a bounded regen batch). A flag flip elsewhere
in the pipeline must not perpetually invalidate the hash driving that
sampling -- so the seed is derived deterministically from stable identity
(entity id, not run-local state), making repeated runs against the same
input set reproducible.
"""


def deterministic_seed(entity_id: str, salt: str = "") -> int:
    """Derive a stable, repeatable seed from an entity's identity."""
    raise NotImplementedError
