"""3-way attributed fields with human-always-wins precedence.

Every stored field carries three slices: ``sourced`` (the authored/original
value), ``machine`` (the last machine-generated value), and ``human`` (a
human correction, if any). The effective value is resolved by a fixed
precedence -- human, when present, always wins over machine, which always
wins over sourced. This is the do-no-harm boundary baked into the data model
itself: a regeneration pass can never silently clobber a human edit, because
the precedence rule is structural, not a runtime check a caller could forget.

Two shapes of precedence exist and both are supported:

- **Scalar slice precedence** (:func:`effective_value`): three scalar
  slices, return the highest-priority *present* one. This is the localization
  ``translation_human > machine`` shape and the first-pass
  ``conversation_direction_human > conversation_direction_claude`` shape.
- **Block (designer-ownership) precedence** (via a ``present`` predicate on a
  compound block): when the human slice is a *block* (a dict of sub-fields),
  "present" means the block as a whole is claimed -- so a human block wins
  *wholesale* even if one of its sub-fields is empty. This is the first-pass
  ``effective_pick`` rule (a human ``{body, face}`` block with an empty
  ``body`` still wins, yielding the empty body -- the designer has taken
  ownership of the line). The caller supplies the presence predicate; the
  module does not hardcode any sub-field names.

:func:`merge_preserved_fields` carries the do-no-harm boundary across a
regeneration: given the previously-saved record and a freshly-regenerated
one, it copies human overrides forward unconditionally, copies machine blocks
+ their freshness hashes forward when the driving inputs are unchanged, and
retains orphaned human answers -- so a structural regen never drops work a
human (or a still-valid machine pass) already produced. The rules are
declared as data (:class:`MergePolicy` / :class:`CollectionMerge`), never
hardcoded to any field name, so a consumer maps its own schema.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Generic, Mapping, MutableMapping, Optional, Sequence, TypeVar

T = TypeVar("T")

# A presence predicate: True when a slice counts as "authored" and should win
# its precedence rung. The default treats any truthy value as present, which
# is correct for scalar string slices; a block (dict) slice passes a custom
# predicate (e.g. "any sub-field truthy") so an all-empty block does not win.
Present = Callable[[object], bool]


def _truthy(value: object) -> bool:
    """Default presence predicate: a value is present when it is truthy."""
    return bool(value)


def effective_value(
    sourced: object = None,
    machine: object = None,
    human: object = None,
    *,
    present: Optional[Present] = None,
) -> object:
    """Resolve the 3-way attributed value: human > machine > sourced.

    Returns the highest-priority slice for which ``present`` is true, falling
    through to ``sourced`` (the base) when neither ``human`` nor ``machine``
    is present. ``present`` defaults to a truthiness test, which is correct
    for scalar string slices. For block (designer-ownership) precedence, pass
    a predicate that inspects the block's sub-fields, e.g.::

        effective_value(machine=claude_block, human=human_block,
                        present=lambda b: bool(b) and any(b.values()))

    so a human block wins wholesale when any sub-field is set, even if the
    winning value it yields is one of the block's empty sub-fields.
    """
    is_present = present or _truthy
    if is_present(human):
        return human
    if is_present(machine):
        return machine
    return sourced


@dataclass(frozen=True)
class AttributedField(Generic[T]):
    """One field's three attribution slices, resolvable by precedence.

    ``sourced`` is the authored/original value, ``machine`` the last
    machine-generated value, ``human`` a human correction. :meth:`resolve`
    applies the human > machine > sourced precedence. Frozen -- a correction
    produces a new instance rather than mutating in place, mirroring the
    do-no-harm boundary at the type level.
    """

    sourced: Optional[T] = None
    machine: Optional[T] = None
    human: Optional[T] = None

    def resolve(self, *, present: Optional[Present] = None) -> Optional[T]:
        """Return the effective value (human > machine > sourced)."""
        return effective_value(
            self.sourced, self.machine, self.human, present=present
        )


@dataclass(frozen=True)
class CollectionMerge:
    """How to merge one keyed sub-collection (a list of item dicts) on regen.

    A record may carry list-valued sub-collections (per-line data, questions)
    whose items are matched across the old and new record by an id field. For
    each matched item the same three preservation rules as the top level
    apply; unmatched *existing* items are retained only when they still carry
    authored work.

    - ``id_key`` -- the field on each item that identifies it across regens.
    - ``human_fields`` -- item fields carried from the old item whenever they
      are present (never clobbered by regen).
    - ``carry_fields`` -- item fields carried verbatim from the old item when
      present (machine blocks whose downstream freshness check, not this
      merge, decides validity; and per-item hashes).
    - ``conditional_fields`` -- item fields carried from the old item only
      when ``unchanged(old_item, new_item)`` holds (machine output that is
      reused while its driving text is unchanged, re-derived otherwise).
    - ``unchanged`` -- per-item predicate gating ``conditional_fields``.
    - ``keep_orphans_when`` -- retain an existing item that has no match in
      the new collection when any of these fields is present on it (e.g. a
      question that already carries a human answer).
    """

    id_key: str
    human_fields: Sequence[str] = ()
    carry_fields: Sequence[str] = ()
    conditional_fields: Sequence[str] = ()
    unchanged: Optional[Callable[[Mapping, Mapping], bool]] = None
    keep_orphans_when: Sequence[str] = ()


@dataclass(frozen=True)
class MergePolicy:
    """Declares which fields survive a regeneration, and how.

    Every rule is a field-name list so a consumer maps its own schema without
    this module knowing any domain field name.

    - ``human_fields`` -- top-level fields carried from the old record when
      present (human overrides; never clobbered).
    - ``carry_fields`` -- top-level fields carried verbatim from the old
      record when present (freshness hashes and machine blocks that are
      always reused across a structural regen).
    - ``conditional_fields`` -- top-level fields carried from the old record
      only when ``unchanged(old, new)`` holds (machine output reused while
      the driving inputs are unchanged).
    - ``unchanged`` -- record-level predicate gating ``conditional_fields``.
    - ``collections`` -- per keyed sub-collection merge rules.

    ``human_fields`` and ``carry_fields`` behave identically (carry-when-
    present); they are kept distinct so the policy documents intent -- one is
    "a human said this", the other is "a machine produced this and it is still
    good". Only ``conditional_fields`` gates on ``unchanged``.
    """

    human_fields: Sequence[str] = ()
    carry_fields: Sequence[str] = ()
    conditional_fields: Sequence[str] = ()
    unchanged: Optional[Callable[[Mapping, Mapping], bool]] = None
    collections: Mapping[str, CollectionMerge] = field(default_factory=dict)


def _carry_present(source: Mapping, target: MutableMapping, keys: Sequence[str]) -> None:
    """Copy each present ``keys`` value from ``source`` onto ``target``."""
    for key in keys:
        value = source.get(key)
        if value:
            target[key] = value


def _merge_item(old: Mapping, new: MutableMapping, spec: CollectionMerge) -> None:
    """Apply one collection's preservation rules to a matched item pair."""
    _carry_present(old, new, spec.human_fields)
    _carry_present(old, new, spec.carry_fields)
    if spec.conditional_fields and (spec.unchanged is None or spec.unchanged(old, new)):
        _carry_present(old, new, spec.conditional_fields)


def _merge_collection(
    existing: Sequence[Mapping],
    incoming: Sequence[MutableMapping],
    spec: CollectionMerge,
) -> list:
    """Merge one keyed sub-collection, preserving matched items and orphans."""
    old_by_id = {
        item.get(spec.id_key): item
        for item in existing
        if isinstance(item, Mapping) and item.get(spec.id_key) is not None
    }
    new_ids = set()
    merged: list = []
    for item in incoming:
        if not isinstance(item, MutableMapping):
            merged.append(item)
            continue
        item_id = item.get(spec.id_key)
        new_ids.add(item_id)
        old = old_by_id.get(item_id)
        if old is not None:
            _merge_item(old, item, spec)
        merged.append(item)
    # Retain orphaned existing items that still carry authored work.
    if spec.keep_orphans_when:
        for item in existing:
            if not isinstance(item, Mapping):
                continue
            if item.get(spec.id_key) in new_ids:
                continue
            if any(item.get(k) for k in spec.keep_orphans_when):
                merged.append(dict(item))
    return merged


def merge_preserved_fields(
    existing: Optional[Mapping],
    incoming: MutableMapping,
    *,
    policy: MergePolicy,
) -> MutableMapping:
    """Merge a freshly generated record onto an existing one, preserving work.

    ``incoming`` (the fresh regen) is mutated in place and returned. When
    ``existing`` is ``None`` (a first-ever run) ``incoming`` is returned
    unchanged. Otherwise, per ``policy``:

    1. Human overrides (``human_fields``) and always-reused machine blocks +
       hashes (``carry_fields``) are copied from ``existing`` when present.
    2. Conditional machine output (``conditional_fields``) is copied only
       when ``policy.unchanged(existing, incoming)`` holds.
    3. Each keyed sub-collection is merged item-by-item under its
       :class:`CollectionMerge`, retaining orphaned authored items.

    The precedence order matters: human/carry copies happen regardless of
    drift; conditional copies happen only on no-drift -- exactly the "carry
    human overrides + machine blocks + hashes forward across regeneration"
    boundary both source systems converged on.
    """
    if existing is None:
        return incoming
    _carry_present(existing, incoming, policy.human_fields)
    _carry_present(existing, incoming, policy.carry_fields)
    if policy.conditional_fields and (
        policy.unchanged is None or policy.unchanged(existing, incoming)
    ):
        _carry_present(existing, incoming, policy.conditional_fields)
    for coll_key, spec in policy.collections.items():
        incoming[coll_key] = _merge_collection(
            existing.get(coll_key) or (), incoming.get(coll_key) or (), spec
        )
    return incoming
