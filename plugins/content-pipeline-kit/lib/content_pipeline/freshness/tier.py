"""The two-tier freshness model: source tier, generation tier, cross-ref.

Both source systems separate two hashes with different invalidation costs:

- The **source tier** hashes a unit's source content. A change here
  invalidates the *cheap*, always-regenerated derived artifact (a brief, a
  direction note) -- no expensive machine call is implied yet.
- The **generation tier** hashes the per-item inputs that drove an
  expensive machine output. A change here invalidates just that one output.

The link between them is a **cross-reference**: the derived artifact records
the source hash it was built from. If the unit's current source hash differs
from the recorded one, the derived artifact is stale even though its own
bytes have not been re-examined -- which is what lets a single stored digest
stand in for walking every item (see ``hashing.corpus_hash``).

This is modeled as two small frozen dataclasses and a pure predicate, not an
inheritance tree: the tiers are *data* (two strings), and the staleness rule
is a *function*, so a consumer composes them without subclassing anything.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SourceTier:
    """A unit's source-content hash -- invalidates the cheap derived artifact."""

    hash: str


@dataclass(frozen=True)
class GenerationTier:
    """A per-item generation-inputs hash -- invalidates the expensive output."""

    hash: str


@dataclass(frozen=True)
class TwoTierHashes:
    """The paired tiers for one item, for callers that carry both together."""

    source: SourceTier
    generation: GenerationTier


def is_cross_ref_stale(recorded_source_hash: str, current_source_hash: str) -> bool:
    """True when a derived artifact's recorded source hash no longer matches.

    ``recorded_source_hash`` is the source hash the derived artifact stored
    when it was built; ``current_source_hash`` is the freshly recomputed one.
    A mismatch means upstream source content changed -- the derived artifact
    is stale. An empty recorded hash (a legacy artifact written before the
    cross-ref existed) is treated as a mismatch, forcing one rebuild so the
    field gets populated.
    """
    if not recorded_source_hash:
        return True
    return recorded_source_hash != current_source_hash
