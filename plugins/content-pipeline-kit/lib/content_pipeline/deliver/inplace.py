"""In-place mutation delivery: do-no-harm marker + first-class revert.

Writes generated content directly into the authored source, tagging every
machine-written region with a marker so a later pass (or a human) can tell
authored from generated at a glance. Revert is first-class: any marked region
returns to its pre-generation (unmarked, cleared) state without touching the
human-authored content around it. Drives the ``vcs`` seam for the two-phase
changeset choreography (a placeholder changeset up front -- or one the caller
already holds and passes in -- per-item inline moves, a description rebuilt
from the successfully-moved subset, delete-if-empty).

Per-item isolation is collect-and-continue by default (matching the proven
consumer's apply/revert semantics): a single item that fails to apply OR fails
to move into the changeset is recorded and skipped, never aborting the rest of
the batch. Apply failures land on :attr:`ChangesetResult.failed`; VCS failures
(``open_for_edit`` / ``move_into`` raising -- e.g. p4-kit's ``move_into``
raising when a ``p4 reopen`` was a silent no-op or landed the file in the wrong
CL) land on :attr:`ChangesetResult.failed_moves`. Only successfully-moved items
enter :attr:`ChangesetResult.moved`, so the finalize step still rebuilds the
description from the successfully-moved subset alone (the description never
claims an item that did not land in the changeset) and delete-if-empty still
fires when nothing moved.

Three generalizations, each domain-free:

- **Ownership marker schema** (:class:`Marker`, :func:`classify_ownership`) --
  a configurable tag on a row's marker field decides HUMAN vs. MACHINE vs.
  EMPTY. A populated value with the marker is machine-owned; a populated value
  WITHOUT the marker is human-owned (a designer took ownership) and is never
  touched. Generalizes the first-pass ``[FIRST PASS]`` tag schema.
- **Apply purely from the store** (:func:`apply_inplace`) -- marked rows are
  rebuilt as a pure function of the store's projected value; human rows are
  left untouched; rows the store has no value for are skipped. The store is
  the source of truth for machine content, so re-applying is idempotent and
  never invents a value the store does not carry (the source
  ``_apply_assignments`` rule).
- **Revert** (:func:`revert_marked`) -- strip the marker and clear the value on
  exactly the marked rows, returning the exact set of mutated row ids. The
  source ``revert.py`` do-no-harm revert.

The row shape is entirely the caller's: an :class:`InplaceSpec` supplies the
callables that read a row's id / value-presence / marker field and that
produce a mutated row. This module never imports ``vcs`` -- a ``VcsBackend`` is
injected (per the plugin's dependency contract, ``deliver`` takes a backend
instance, it does not construct one).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, List, Optional, Sequence, Tuple


class Marker:
    """A do-no-harm ownership tag on a row's marker field.

    The tag marks a value as machine-produced. Presence of the tag on a
    populated row means "the pipeline owns this row"; absence means a human
    authored it. ``add`` / ``remove`` are idempotent and whitespace-normalizing,
    matching the source tag mutation helpers.
    """

    def __init__(self, tag: str) -> None:
        if not tag:
            raise ValueError("Marker tag must be non-empty")
        self.tag = tag

    def is_marked(self, marker_text: Optional[str]) -> bool:
        """True when ``marker_text`` carries the tag."""
        return bool(marker_text) and self.tag in marker_text

    def add(self, marker_text: Optional[str]) -> str:
        """Return ``marker_text`` with the tag appended (idempotent)."""
        text = marker_text or ""
        if self.tag in text:
            return text
        if not text or text.isspace():
            return self.tag
        return f"{text} {self.tag}"

    def remove(self, marker_text: Optional[str]) -> str:
        """Return ``marker_text`` with the tag removed and whitespace collapsed."""
        text = marker_text or ""
        if self.tag not in text:
            return text
        return " ".join(text.replace(self.tag, "").split())


class Ownership(str, Enum):
    """Who owns a row's value, per the marker schema."""

    EMPTY = "empty"  # no value present
    MACHINE = "machine"  # value present AND marked -- the pipeline owns it
    HUMAN = "human"  # value present WITHOUT the marker -- a human authored it


def classify_ownership(
    value_present: bool, marker_text: Optional[str], marker: Marker
) -> Ownership:
    """Classify a row: EMPTY, MACHINE (marked), or HUMAN (present, unmarked).

    The marker covers the whole row: a value present without the marker means a
    human took ownership, so it classifies HUMAN even if a machine wrote it
    once -- the do-no-harm boundary.
    """
    if not value_present:
        return Ownership.EMPTY
    if marker.is_marked(marker_text):
        return Ownership.MACHINE
    return Ownership.HUMAN


@dataclass(frozen=True)
class InplaceSpec:
    """Callables that adapt a caller's row shape to in-place delivery.

    - ``marker`` -- the :class:`Marker` schema.
    - ``row_id`` -- ``row -> str`` identity within the target.
    - ``value_present`` -- ``row -> bool``: does the row carry a value?
    - ``marker_text`` -- ``row -> str``: the row's marker-field text.
    - ``store_value`` -- ``(store, row_id) -> value``: the projected pick from
      the store (``None``/empty == the store has no value; the row is skipped).
      Typically ``store.projection.project_cell`` / ``project_field``.
    - ``set_value`` -- ``(row, value) -> row``: return a row with the value set.
    - ``clear_value`` -- ``row -> row``: return a row with the value cleared.
    - ``set_marker`` / ``clear_marker`` -- ``row -> row``: add / remove the tag.
    - ``eligible`` -- optional ``(store, row) -> Optional[str]``: a policy gate
      returning a reason to SKIP the row (a speaker-policy filter, an
      excluded item) or ``None`` to proceed. Runs before the human-ownership
      check, mirroring the source apply's policy gate.
    """

    marker: Marker
    row_id: Callable[[Any], str]
    value_present: Callable[[Any], bool]
    marker_text: Callable[[Any], Optional[str]]
    store_value: Callable[[Any, str], Any]
    set_value: Callable[[Any, Any], Any]
    clear_value: Callable[[Any], Any]
    set_marker: Callable[[Any], Any]
    clear_marker: Callable[[Any], Any]
    eligible: Optional[Callable[[Any, Any], Optional[str]]] = None


@dataclass
class ApplyResult:
    """Outcome of an :func:`apply_inplace` pass over one target's rows.

    - ``rows`` -- the rebuilt row list (a new list; input rows are not mutated
      when the caller's ``set_*`` return new rows).
    - ``written`` -- ids of rows whose value was (re)written from the store.
    - ``skipped_human`` -- ids of populated, unmarked rows left untouched.
    - ``skipped_policy`` -- ``(id, reason)`` for rows an ``eligible`` gate held.
    - ``skipped_no_value`` -- ids the store had no value for.
    """

    rows: List[Any] = field(default_factory=list)
    written: List[str] = field(default_factory=list)
    skipped_human: List[str] = field(default_factory=list)
    skipped_policy: List[Tuple[str, str]] = field(default_factory=list)
    skipped_no_value: List[str] = field(default_factory=list)

    @property
    def mutated_ids(self) -> List[str]:
        """Ids whose row actually changed this pass (the exact write set)."""
        return list(self.written)


def apply_inplace(
    rows: Sequence[Any],
    store: Any,
    spec: InplaceSpec,
) -> ApplyResult:
    """Rebuild the marked rows of ``rows`` purely from ``store``.

    For each row, in order:

    1. **Policy** -- if ``spec.eligible`` returns a reason, skip (record on
       ``skipped_policy``), leaving the row verbatim.
    2. **Store value** -- read ``spec.store_value(store, row_id)``. An
       empty/absent value means the store owns no machine content for this row;
       skip it (``skipped_no_value``), leaving it verbatim.
    3. **Human ownership** -- a populated, UNMARKED row is human-authored; skip
       it (``skipped_human``), never overwriting authored work.
    4. **Write** -- otherwise set the value from the store and add the marker,
       recording the id on ``written``.

    Returns an :class:`ApplyResult` carrying the rebuilt row list and the four
    disposition buckets. Pure over the store: the same store yields the same
    rows every call (idempotent re-apply).
    """
    result = ApplyResult()
    for row in rows:
        rid = spec.row_id(row)

        if spec.eligible is not None:
            reason = spec.eligible(store, row)
            if reason is not None:
                result.skipped_policy.append((rid, reason))
                result.rows.append(row)
                continue

        value = spec.store_value(store, rid)
        if value is None or value == "" or (isinstance(value, str) and not value.strip()):
            result.skipped_no_value.append(rid)
            result.rows.append(row)
            continue

        ownership = classify_ownership(
            spec.value_present(row), spec.marker_text(row), spec.marker
        )
        if ownership is Ownership.HUMAN:
            result.skipped_human.append(rid)
            result.rows.append(row)
            continue

        mutated = spec.set_value(row, value)
        mutated = spec.set_marker(mutated)
        result.written.append(rid)
        result.rows.append(mutated)
    return result


@dataclass
class RevertResult:
    """Outcome of a :func:`revert_marked` pass.

    - ``rows`` -- the rebuilt row list.
    - ``reverted`` -- ids of rows whose value + marker were stripped.
    """

    rows: List[Any] = field(default_factory=list)
    reverted: List[str] = field(default_factory=list)


def revert_marked(
    rows: Sequence[Any],
    spec: InplaceSpec,
    *,
    only_ids: Optional[Sequence[str]] = None,
) -> RevertResult:
    """Strip the marker and clear the value on every marked row.

    A row is reverted when it carries the marker (machine-owned). ``only_ids``,
    when given, restricts the revert to that id set (the "revert one
    conversation inside a shared file" case); the marker check still applies.
    Human-owned rows (populated, unmarked) are never touched. Returns the
    rebuilt rows plus the exact set of mutated ids.
    """
    id_filter = set(only_ids) if only_ids is not None else None
    result = RevertResult()
    for row in rows:
        rid = spec.row_id(row)
        marked = spec.marker.is_marked(spec.marker_text(row))
        in_scope = id_filter is None or rid in id_filter
        if marked and in_scope:
            mutated = spec.clear_value(row)
            mutated = spec.clear_marker(mutated)
            result.reverted.append(rid)
            result.rows.append(mutated)
        else:
            result.rows.append(row)
    return result


# ---------------------------------------------------------------------------
# Changeset choreography (drives the injected VcsBackend)
# ---------------------------------------------------------------------------


@dataclass
class ChangesetResult:
    """Outcome of :func:`deliver_changeset`.

    - ``changeset`` -- the backend's changeset handle (``None`` for a no-op
      backend).
    - ``moved`` -- ``(item_id, path)`` for items whose write + move succeeded.
    - ``failed`` -- ``(item_id, reason)`` for items whose write (``apply_item``)
      raised.
    - ``failed_moves`` -- ``(item_id, path, reason)`` for items whose VCS step
      (``open_for_edit`` / ``move_into``) raised. Collect-and-continue: a
      per-item VCS failure is recorded here and the batch proceeds, so one bad
      move never aborts the rest.
    - ``description`` -- the final description finalized onto the changeset
      (rebuilt from the moved subset), or ``""`` when nothing moved.
    """

    changeset: Any = None
    moved: List[Tuple[str, str]] = field(default_factory=list)
    failed: List[Tuple[str, str]] = field(default_factory=list)
    failed_moves: List[Tuple[str, str, str]] = field(default_factory=list)
    description: str = ""


def deliver_changeset(
    items: Sequence[Any],
    *,
    vcs: Any,
    item_id: Callable[[Any], str],
    path_of: Callable[[Any], str],
    apply_item: Callable[[Any], None],
    describe: Callable[[Sequence[Tuple[str, str]]], str],
    placeholder: str = "pending: content-pipeline delivery",
    changeset: Any = None,
) -> ChangesetResult:
    """Run the backend-agnostic changeset choreography over ``items``.

    Delivery-mode logic, not backend logic (so it is written once here, not
    per backend), driving the injected ``vcs`` (a ``VcsBackend`` -- git, null,
    or p4-kit's Perforce backend):

    1. **Changeset up front** -- ``vcs.make_changeset(placeholder)`` before any
       item is touched, UNLESS the caller supplied ``changeset`` (see
       *Adopting an existing changeset* below), in which case nothing is
       minted.
    2. **Per-item inline moves** -- for each item: ``open_for_edit(path)``,
       ``apply_item(item)`` (the caller's write), then
       ``move_into(changeset, [path])``. Collect-and-continue isolates every
       item: an ``apply_item`` exception records the item on ``failed``; an
       ``open_for_edit`` / ``move_into`` exception records ``(id, path,
       reason)`` on ``failed_moves`` -- in either case the batch proceeds, so
       one bad item never aborts the rest. Only items that both applied and
       moved are collected on ``moved``.
    3. **Description rebuilt from the moved subset** -- ``describe`` is called
       with ONLY the successfully-moved ``(id, path)`` pairs, and its result is
       finalized via ``finalize_description`` -- so the description never claims
       an item that did not land in the changeset (the source system's
       description-vs-contents drift bug).
    4. **Delete-if-empty** -- ``delete_if_empty(changeset)`` so a batch that
       moved nothing leaves no empty changeset behind.

    Adopting an existing changeset
    ------------------------------

    ``changeset`` (optional, default ``None`` == mint one) lets a caller
    deliver INTO a changeset it already holds, so several passes can land in
    one reviewable unit. This is not a Perforce concept intruding on neutral
    code: ``VcsBackend`` is already modeled on Perforce's pending changelist
    (``GitVcs`` implements ``open_for_edit`` / ``delete_if_empty`` as no-ops and
    ``make_changeset`` as an in-memory object creating no git object -- git is
    the degenerate case adapted TO that model), so a long-lived adoptable
    changeset is the model's central object and always-minting was a gap in it.

    Adoption means the CALLER owns the changeset's lifecycle, so steps 3 and 4
    are skipped when an adopted changeset received nothing this run -- it is
    left exactly as found. Both would be destructive otherwise: the backends'
    ``delete_if_empty`` tests THIS run's moved paths, not the changeset's real
    contents (p4-kit would try ``p4 change -d`` on a changelist that may hold a
    previous pass's files), and finalizing a no-op run would overwrite the
    caller's description with the empty string. When an adopted changeset DOES
    receive items, the choreography is identical to the minting path -- the
    description is still rebuilt from the moved subset -- because that is what
    the caller asked for by delivering into it.

    Everything else is unchanged: with no ``changeset`` argument the behavior
    is exactly what it was, including finalize-then-delete on an empty batch.
    """
    minted = changeset is None
    if minted:
        changeset = vcs.make_changeset(placeholder)
    result = ChangesetResult(changeset=changeset)

    for item in items:
        iid = item_id(item)
        path = path_of(item)
        try:
            vcs.open_for_edit(path)
        except Exception as exc:  # noqa: BLE001 -- isolate one item's VCS step
            result.failed_moves.append((iid, path, str(exc)))
            continue
        try:
            apply_item(item)
        except Exception as exc:  # noqa: BLE001 -- isolate one item's write
            result.failed.append((iid, str(exc)))
            continue
        try:
            vcs.move_into(changeset, [path])
        except Exception as exc:  # noqa: BLE001 -- isolate one item's VCS step
            result.failed_moves.append((iid, path, str(exc)))
            continue
        result.moved.append((iid, path))

    result.description = describe(result.moved) if result.moved else ""
    if minted or result.moved:
        vcs.finalize_description(changeset, result.description)
        vcs.delete_if_empty(changeset)
    return result


__all__ = [
    "Marker",
    "Ownership",
    "classify_ownership",
    "InplaceSpec",
    "ApplyResult",
    "apply_inplace",
    "RevertResult",
    "revert_marked",
    "ChangesetResult",
    "deliver_changeset",
]
