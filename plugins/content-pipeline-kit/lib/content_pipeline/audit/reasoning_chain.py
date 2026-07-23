"""Per-item reasoning-chain sidecar.

Records, per audited entity, the chain of reasoning that led to a candidate
being selected -- the intermediate grades, the rejected alternatives, the
riders that informed the pick. This is a capability one of the two source
systems this plugin unifies lost during consolidation; shipping it here
rebuilds it for both consumers.
"""


def record_chain(entity_id: str, steps: list) -> None:
    """Persist a reasoning-chain sidecar entry for an audited entity."""
    raise NotImplementedError
