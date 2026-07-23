"""The single freshness predicate: human > excluded > missing > stale > fresh.

Every "needs generation" check and every coverage-bucket site in a consuming
pipeline delegates to :func:`classify` rather than re-deriving its own
staleness logic. One predicate, one place the rule can be wrong or right --
so the "needs regen" set and the coverage report can never drift apart.

States (:class:`FreshnessState`), in strict priority order:

- ``HUMAN`` -- a human-authored value is present. It always wins and is
  never reclassified as stale by a hash mismatch (the do-no-harm boundary,
  mirrored by ``store.attributed``'s human-always-wins precedence).
- ``EXCLUDED`` -- the unit/item is not applicable to generation at all
  (generalizing the source systems' "player choice" / "held pose" lines).
  Whether an item is excluded is a domain decision, so the caller passes it
  in as ``excluded=...`` rather than this module inspecting item shape.
- ``MISSING`` -- no machine value yet; a full generation is needed.
- ``STALE`` -- a machine value exists but the generation-inputs hash it
  recorded no longer matches the current expected hash; regenerate this item.
- ``FRESH`` -- a machine value exists and its recorded hash matches; zero
  cost on the next run.

The predicate does NOT re-hash: the caller computes the expected
generation-inputs hash (via ``hashing.combined_hash`` / ``content_hash``)
and passes it in. This keeps ``classify`` a pure function over a record and
one string, trivially testable, and free of any per-domain hashing knowledge.
"""

from __future__ import annotations

from enum import Enum
from typing import Dict, Iterable


class FreshnessState(Enum):
    """The union of both source systems' generation-state vocabularies."""

    HUMAN = "human"
    EXCLUDED = "excluded"
    MISSING = "missing"
    STALE = "stale"
    FRESH = "fresh"


def _field(record, name: str, default):
    """Duck-typed accessor: works for dataclasses, dicts, and ``None``."""
    if record is None:
        return default
    if isinstance(record, dict):
        return record.get(name, default)
    return getattr(record, name, default)


def _nonempty_str(value) -> bool:
    """True when ``value`` is a non-empty, non-whitespace string."""
    return isinstance(value, str) and bool(value.strip())


def classify(
    record,
    expected_hash: str,
    *,
    excluded: bool = False,
    human_field: str = "human",
    machine_field: str = "machine",
    hash_field: str = "generation_hash",
) -> FreshnessState:
    """Classify one stored record into a :class:`FreshnessState`.

    ``record`` is duck-typed (dataclass, dict, or ``None``); the three
    slices it is read for are configurable by field name so a consumer can
    map its own schema without reshaping data:

    - ``human_field`` -- the human-authored value (non-empty string wins).
    - ``machine_field`` -- the last machine-generated value (empty/absent
      => ``MISSING``).
    - ``hash_field`` -- the generation-inputs hash recorded alongside the
      machine value; compared against ``expected_hash`` for staleness.

    ``excluded`` is the caller's domain verdict that this item does not
    participate in generation. It is checked *after* ``human_field`` so a
    human value wins even on an otherwise-excluded item.

    Priority: ``HUMAN > EXCLUDED > MISSING > STALE > FRESH``.
    """
    if _nonempty_str(_field(record, human_field, "")):
        return FreshnessState.HUMAN
    if excluded:
        return FreshnessState.EXCLUDED
    if not _nonempty_str(_field(record, machine_field, "")):
        return FreshnessState.MISSING
    recorded = _field(record, hash_field, "") or ""
    if recorded != expected_hash:
        return FreshnessState.STALE
    return FreshnessState.FRESH


def needs_generation(
    state: FreshnessState, *, include_stale: bool = True
) -> bool:
    """True when ``state`` calls for a (re)generation.

    ``MISSING`` always needs generation. ``STALE`` needs it only when
    ``include_stale`` is True -- the default full sweep regenerates drift;
    a missing-only bulk pass (``include_stale=False``) leaves stale items
    alone and touches only never-generated ones. ``HUMAN``, ``EXCLUDED``,
    and ``FRESH`` never need generation.
    """
    if state is FreshnessState.MISSING:
        return True
    if state is FreshnessState.STALE:
        return include_stale
    return False


def bucket_counts(states: Iterable[FreshnessState]) -> Dict[FreshnessState, int]:
    """Tally an iterable of states into a per-state count.

    Every :class:`FreshnessState` is present in the result (zero when
    unseen), so a coverage report can index any bucket without a
    ``KeyError``. Because the caller derives ``states`` from the same
    :func:`classify` predicate the "needs generation" set uses, the buckets
    and the regen set cannot disagree.
    """
    counts: Dict[FreshnessState, int] = {state: 0 for state in FreshnessState}
    for state in states:
        counts[state] += 1
    return counts
