"""Export-for-review / intake-corrections closed loop.

Exports a batch of generated content into a human-reviewable form (e.g. a
workbook), then re-ingests corrections made against that export back into
the store as human-attributed values (see ``store.attributed``'s
human-always-wins precedence). This is the batch-shaped sibling of
``questions``'s per-entity loop.
"""


def export_for_review(entities: list, destination) -> None:
    """Export a batch of entities into a human-reviewable artifact."""
    raise NotImplementedError


def intake_corrections(source) -> list:
    """Re-ingest corrections from a reviewed artifact, returning human-attributed updates."""
    raise NotImplementedError
