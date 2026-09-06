"""Candidate cell: active / shadow / retired lists with cached grades.

The many-candidates generalization of ``attributed`` -- instead of one
effective value per field, a candidate *cell* tracks a small population of
candidate values, each carrying a cached grade summary and a set of
deterministic fact riders (see ``validate.riders``) that downstream stages
reuse rather than re-derive. Candidates move between three statuses: active
(the current projection target), shadow (generated but not promoted), and
retired (superseded, kept for audit history). The degenerate one-candidate
case collapses to ``attributed``'s single-pick model.

Vocabulary is neutral: a cell is identified by an opaque ``key`` tuple (the
consumer decides what the coordinates mean); candidates carry an opaque
``value``. No unit / variant / language / atom concept leaks in.

Design points carried from the source systems:

- **Serialization is pluggable.** :func:`cell_to_dict` / :func:`cell_from_dict`
  and :func:`store_to_doc` / :func:`store_from_doc` convert to and from plain
  ``dict`` documents; the actual YAML (de)serialization is the caller's
  choice. A bulk store at scale routes those docs through a C-backed YAML
  loader/dumper rather than a pure-Python round-trip parser -- the source
  system measured an order-of-magnitude slowdown from binding a comment-
  preserving parser to an 11 MB generated store. This module never imports a
  YAML engine; :func:`load_store` / :func:`dump_store` take a ``yaml_load`` /
  ``yaml_dump`` callable.
- **Rider cache keys** are derived via ``freshness.hashing`` (the one
  cross-package dependency this subsystem is permitted): :func:`rider_cache_key`
  hashes a candidate's value (plus optional salt) so a cached rider block can
  be invalidated when the value it was computed from changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from content_pipeline.freshness import hashing


class CandidateStatus(str, Enum):
    """A candidate's position in the cell's lifecycle."""

    ACTIVE = "active"
    SHADOW = "shadow"
    RETIRED = "retired"


VALID_STATUSES: Tuple[str, ...] = tuple(s.value for s in CandidateStatus)


class CandidateError(Exception):
    """Raised when a candidate or cell schema/invariant is violated."""


@dataclass(frozen=True)
class Candidate:
    """One candidate value for a cell.

    - ``id`` -- unique within the cell (the selector / audit trail
      disambiguate by it). Non-empty.
    - ``value`` -- the opaque candidate payload (a string, a block, whatever
      the consumer stores).
    - ``status`` -- one of :class:`CandidateStatus`.
    - ``grade_summary`` -- the cached per-candidate grade output (free-form),
      or ``None`` when never graded.
    - ``riders`` -- the deterministic fact riders computed once and reused
      downstream (see ``validate.riders``), or ``None``.
    - ``extras`` -- any additional per-candidate metadata the consumer wants
      to round-trip without this module modelling it.
    """

    id: str
    value: Any = ""
    status: str = CandidateStatus.SHADOW.value
    grade_summary: Optional[Mapping[str, Any]] = None
    riders: Optional[Mapping[str, Any]] = None
    extras: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id:
            raise CandidateError("Candidate.id must be a non-empty string")
        status = (
            self.status.value
            if isinstance(self.status, CandidateStatus)
            else self.status
        )
        if status not in VALID_STATUSES:
            raise CandidateError(
                f"Candidate {self.id!r}: status={self.status!r} "
                f"not in {VALID_STATUSES}"
            )
        object.__setattr__(self, "status", status)


@dataclass(frozen=True)
class CandidateCell:
    """A population of candidates for one cell ``key``.

    ``entries`` is a tuple of :class:`Candidate` in introduction order. At
    most one entry may be ``active`` (enforced by the mutators and on load).
    ``locked`` is an explicit terminal flag: a locked cell is "done" and
    stages skip it. ``extras`` round-trips any cell-level metadata this module
    does not model (per-line context, counters).
    """

    key: Tuple[Any, ...]
    entries: Tuple[Candidate, ...] = ()
    locked: bool = False
    extras: Mapping[str, Any] = field(default_factory=dict)

    @property
    def active(self) -> Optional[Candidate]:
        """Return the active candidate, or ``None`` if none is active."""
        for entry in self.entries:
            if entry.status == CandidateStatus.ACTIVE.value:
                return entry
        return None

    def get(self, candidate_id: str) -> Optional[Candidate]:
        """Return the candidate with ``candidate_id``, or ``None``."""
        for entry in self.entries:
            if entry.id == candidate_id:
                return entry
        return None

    @property
    def produced_count(self) -> int:
        """Number of DISTINCT non-empty values over non-retired entries.

        The convergence measure: retired entries and empty values do not
        count as produced readings.
        """
        seen = set()
        for entry in self.entries:
            if entry.status == CandidateStatus.RETIRED.value:
                continue
            value = entry.value
            marker = value.strip() if isinstance(value, str) else value
            if marker:
                seen.add(marker if isinstance(marker, str) else repr(marker))
        return len(seen)


def append_candidate(cell: CandidateCell, candidate: Candidate) -> CandidateCell:
    """Append ``candidate`` to ``cell``; return the new cell.

    Raises :class:`CandidateError` if a candidate with the same ``id`` is
    already present -- ids are unique per cell -- or if ``candidate`` is
    ``active`` and the cell already has an active entry: at-most-one-active
    is enforced by every mutator (and on load, by :func:`cell_from_dict`), so
    an append must not be the one path that lets a second active slip
    through and later fail an unrelated load. ``locked`` and ``extras`` are
    preserved (a post-lock append must not silently unlock the cell).
    """
    if cell.get(candidate.id) is not None:
        raise CandidateError(
            f"cell {cell.key!r}: candidate id {candidate.id!r} already present"
        )
    if candidate.status == CandidateStatus.ACTIVE.value and cell.active is not None:
        raise CandidateError(
            f"cell {cell.key!r}: candidate id {cell.active.id!r} is already "
            "active; at most one active entry is allowed"
        )
    return replace(cell, entries=(*cell.entries, candidate))


def promote_candidate(
    cell: CandidateCell,
    candidate_id: str,
    *,
    retire_previous: bool = False,
) -> CandidateCell:
    """Make ``candidate_id`` the cell's active entry; return the new cell.

    Only the entry that WAS active is demoted; every other non-retired
    entry (a shadow that was never promoted) stays exactly as it is.
    ``retire_previous`` selects between the two source semantics (a
    deliberate semantic union) for what the PRIOR ACTIVE becomes:

    - ``False`` (default) -- the prior active flips to ``shadow`` and stays
      eligible. This is the many-candidate loop's behavior: every produced
      reading remains selectable on a later round.
    - ``True`` -- the prior active flips to ``retired`` (the simpler
      promote-and-supersede shape). Retired entries are kept for audit
      history but excluded from selection and ``produced_count``.

    Entries already ``retired`` are left retired either way, and
    ``candidate_id`` itself must not already be retired -- a retired entry
    is excluded from selection, so promoting one is refused with
    :class:`CandidateError` (consistent with the unknown-id case). Raises
    :class:`CandidateError` if ``candidate_id`` is not in the cell.
    """
    target = cell.get(candidate_id)
    if target is None:
        raise CandidateError(
            f"cell {cell.key!r}: candidate id {candidate_id!r} not present"
        )
    if target.status == CandidateStatus.RETIRED.value:
        raise CandidateError(
            f"cell {cell.key!r}: candidate id {candidate_id!r} is retired "
            "and cannot be promoted"
        )
    demoted = (
        CandidateStatus.RETIRED.value
        if retire_previous
        else CandidateStatus.SHADOW.value
    )
    new_entries = []
    for entry in cell.entries:
        if entry.id == candidate_id:
            new_entries.append(replace(entry, status=CandidateStatus.ACTIVE.value))
        elif entry.status == CandidateStatus.ACTIVE.value:
            new_entries.append(replace(entry, status=demoted))
        else:
            new_entries.append(entry)
    return replace(cell, entries=tuple(new_entries))


def retire_candidate(cell: CandidateCell, candidate_id: str) -> CandidateCell:
    """Flip ``candidate_id`` to ``retired``; return the new cell."""
    if cell.get(candidate_id) is None:
        raise CandidateError(
            f"cell {cell.key!r}: candidate id {candidate_id!r} not present"
        )
    new_entries = tuple(
        replace(entry, status=CandidateStatus.RETIRED.value)
        if entry.id == candidate_id
        else entry
        for entry in cell.entries
    )
    return replace(cell, entries=new_entries)


def set_locked(cell: CandidateCell, locked: bool = True) -> CandidateCell:
    """Return a copy of ``cell`` with its ``locked`` flag set."""
    return replace(cell, locked=locked)


# -- rider cache keys ---------------------------------------------------------

def rider_cache_key(value: Any, *, salt: str = "", length: int = hashing.DEFAULT_DIGEST_LENGTH) -> str:
    """Content hash of a candidate ``value`` for keying its cached riders.

    A rider block computed from a value is only valid while that value is
    unchanged; keying the cache on ``content_hash(value)`` invalidates it
    automatically when the value drifts. ``salt`` lets one value key several
    independent rider families without collision. This is the one place
    ``store`` reaches into ``freshness.hashing`` (explicitly permitted).
    """
    if salt:
        return hashing.content_hash(value, salt, length=length)
    return hashing.content_hash(value, length=length)


# -- (de)serialization (pluggable YAML engine) --------------------------------

def candidate_to_dict(candidate: Candidate) -> dict:
    """Convert a :class:`Candidate` to a plain dict for serialization.

    Optional slices (``grade_summary``, ``riders``) are omitted when unset so
    a candidate that carries neither round-trips to a minimal document.
    """
    out: dict = {
        "id": candidate.id,
        "value": candidate.value,
        "status": candidate.status,
    }
    if candidate.grade_summary is not None:
        out["grade_summary"] = dict(candidate.grade_summary)
    if candidate.riders is not None:
        out["riders"] = dict(candidate.riders)
    if candidate.extras:
        out["extras"] = dict(candidate.extras)
    return out


def candidate_from_dict(doc: Mapping[str, Any]) -> Candidate:
    """Build a :class:`Candidate` from a plain dict document."""
    known = {"id", "value", "status", "grade_summary", "riders", "extras"}
    extras = dict(doc.get("extras") or {})
    for key, value in doc.items():
        if key not in known:
            extras[key] = value
    return Candidate(
        id=doc.get("id", ""),
        value=doc.get("value", ""),
        status=doc.get("status", CandidateStatus.SHADOW.value),
        grade_summary=doc.get("grade_summary"),
        riders=doc.get("riders"),
        extras=extras,
    )


def cell_to_dict(cell: CandidateCell) -> dict:
    """Convert a :class:`CandidateCell` to a plain dict document.

    ``key`` is emitted as a list (YAML has no tuple); ``locked`` is emitted
    only when True so an unlocked cell round-trips minimally.
    """
    out: dict = {
        "key": list(cell.key),
        "entries": [candidate_to_dict(e) for e in cell.entries],
    }
    if cell.locked:
        out["locked"] = True
    if cell.extras:
        out["extras"] = dict(cell.extras)
    return out


def cell_from_dict(doc: Mapping[str, Any]) -> CandidateCell:
    """Build a :class:`CandidateCell` from a plain dict document.

    Validates the at-most-one-active and unique-id invariants on load.
    """
    entries = tuple(candidate_from_dict(e) for e in doc.get("entries") or ())
    ids = [e.id for e in entries]
    if len(ids) != len(set(ids)):
        raise CandidateError(f"cell {doc.get('key')!r}: duplicate candidate id")
    active = [e for e in entries if e.status == CandidateStatus.ACTIVE.value]
    if len(active) > 1:
        raise CandidateError(
            f"cell {doc.get('key')!r}: {len(active)} active candidates (max 1)"
        )
    return CandidateCell(
        key=tuple(doc.get("key") or ()),
        entries=entries,
        locked=bool(doc.get("locked", False)),
        extras=dict(doc.get("extras") or {}),
    )


@dataclass
class CandidateStore:
    """In-memory keyed collection of :class:`CandidateCell`.

    ``cells`` is keyed by each cell's ``key`` tuple. A thin container: it
    holds cells and round-trips to/from a plain document. Mutators live as
    free functions returning new cells (the cell is frozen); callers write the
    returned cell back via :meth:`put`.
    """

    cells: Dict[Tuple[Any, ...], CandidateCell] = field(default_factory=dict)

    def get(self, key: Tuple[Any, ...]) -> Optional[CandidateCell]:
        """Return the cell for ``key``, or ``None``."""
        return self.cells.get(tuple(key))

    def put(self, cell: CandidateCell) -> CandidateCell:
        """Insert or replace ``cell``, keyed by its ``key``."""
        self.cells[tuple(cell.key)] = cell
        return cell

    def add(self, cell: CandidateCell) -> CandidateCell:
        """Insert ``cell``; raise if its key is already present."""
        key = tuple(cell.key)
        if key in self.cells:
            raise CandidateError(f"add: cell key {key!r} already in store")
        self.cells[key] = cell
        return cell


def store_to_doc(store: CandidateStore) -> dict:
    """Convert a :class:`CandidateStore` to a plain dict document."""
    return {"cells": [cell_to_dict(c) for c in store.cells.values()]}


def store_from_doc(doc: Optional[Mapping[str, Any]]) -> CandidateStore:
    """Build a :class:`CandidateStore` from a plain dict document.

    Rejects a duplicate cell key. ``None`` / empty document yields an empty
    store (a store loaded from a missing file).
    """
    store = CandidateStore()
    if not doc:
        return store
    for cell_doc in doc.get("cells") or ():
        cell = cell_from_dict(cell_doc)
        key = tuple(cell.key)
        if key in store.cells:
            raise CandidateError(f"duplicate cell key {key!r} in document")
        store.cells[key] = cell
    return store


def load_store(text: str, *, yaml_load: Callable[[str], Any]) -> CandidateStore:
    """Parse ``text`` with the caller's ``yaml_load`` and build the store.

    The YAML engine is injected so a bulk store can use a C-backed loader for
    throughput; this module never binds one.
    """
    return store_from_doc(yaml_load(text))


def dump_store(store: CandidateStore, *, yaml_dump: Callable[[Any], str]) -> str:
    """Serialize ``store`` to text with the caller's ``yaml_dump``."""
    return yaml_dump(store_to_doc(store))
