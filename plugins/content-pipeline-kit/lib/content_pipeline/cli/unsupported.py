"""Sticky unsupported-stub registry: exclude forever, no re-pay.

When an entity is determined to be structurally unsupported by a pipeline
(not a transient failure -- a genuine "this pipeline cannot handle this
shape"), it is recorded in a sticky registry and excluded from all future
runs. This prevents a bulk run from re-attempting (and re-paying an LLM call
for) an entity that will never succeed.
"""


def mark_unsupported(entity_id: str, reason: str) -> None:
    """Record an entity as sticky-unsupported with a reason."""
    raise NotImplementedError


def is_unsupported(entity_id: str) -> bool:
    """Return True if entity_id is in the sticky-unsupported registry."""
    raise NotImplementedError
