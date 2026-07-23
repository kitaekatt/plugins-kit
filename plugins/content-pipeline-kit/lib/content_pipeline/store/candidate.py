"""Candidate cell: active / shadow / retired lists with cached grades.

The many-candidates generalization of ``attributed`` -- instead of one
effective value per field, a candidate cell tracks a small population of
candidate values per field, each carrying a cached grade and a set of
deterministic fact riders (see ``validate.riders``) that downstream stages
reuse rather than re-derive. Candidates move between three lists: active
(eligible for selection), shadow (generated but not yet promoted), and
retired (superseded, kept for audit history). The degenerate one-candidate
case collapses to ``attributed``'s single-pick model. Large stores load via
a C-backed YAML parser for throughput.
"""


def promote_candidate(entity_id: str, field: str, candidate_id: str) -> None:
    """Move a candidate from shadow to active, retiring the previous active candidate."""
    raise NotImplementedError
