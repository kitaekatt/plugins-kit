"""The merge algebra: how an ``extensible`` child and its parent combine.

Stated by the dialect, implemented here:

- Scalars: the child's value replaces the parent's.
- Maps: merged BY KEY, recursively. This is the case that makes the construct
  work at all -- a child that sets one key keeps every other key the parent set.
- Lists: the child's list replaces the parent's entirely.
- Records (inline nested shapes): merged field by field, like maps.
- Deletion: not expressible.
- Chains: resolved parent-first, deepest ancestor applied first; a cycle is an
  error naming every record in it.

A map and an inline record are both mappings on disk, so one rule covers both,
and a list is replaced because a list is a value rather than a namespace.
"""

from __future__ import annotations

from typing import Any

from .corpus import Corpus, Record
from .errors import Diagnostic
from .model import TypeSpec


def merge_values(parent: Any, child: Any) -> Any:
    """One step of the algebra over two already-loaded values."""
    if isinstance(parent, dict) and isinstance(child, dict):
        merged: dict[Any, Any] = dict(parent)
        for key, value in child.items():
            merged[key] = merge_values(parent.get(key), value) if key in parent else value
        return merged
    return child


def flatten_type(
    type_spec: TypeSpec, corpus: Corpus
) -> tuple[dict[str, dict[str, Any]], list[Diagnostic]]:
    """Flatten every record of an ``extensible`` type against its ancestors.

    Returns the flattened data keyed by record identity, plus any diagnostic
    the flattening itself produced (a dangling parent, or a cycle).
    """
    diagnostics: list[Diagnostic] = []
    records = {r.identity: r for r in corpus.of_type(type_spec.id) if r.identity is not None}
    if type_spec.extensible is None:
        return {identity: dict(r.data) for identity, r in records.items()}, diagnostics

    via = type_spec.extensible.via
    flattened: dict[str, dict[str, Any]] = {}

    for identity, record in records.items():
        chain, cycle = _ancestry(identity, records, via)
        if cycle:
            diagnostics.append(
                Diagnostic(
                    "'{0}:' forms an inheritance cycle: {1}".format(via, " -> ".join(cycle)),
                    record.file,
                    record=identity,
                    field=via,
                )
            )
            flattened[identity] = dict(record.data)
            continue
        # A dangling parent is NOT reported here. `via:` is a `ref` to this
        # same type, so the ordinary ref check already names the file, the
        # record and the field -- reporting it twice would make one mistake
        # look like two.
        merged: dict[str, Any] = {}
        for ancestor in chain:
            merged = merge_values(merged, records[ancestor].data)
        flattened[identity] = merged
    return flattened, diagnostics


def _ancestry(
    identity: str, records: dict[str, Record], via: str
) -> tuple[list[str], list[str]]:
    """The chain deepest-ancestor-first, or the cycle that stopped it."""
    chain: list[str] = []
    seen: list[str] = []
    current: str | None = identity
    while current is not None:
        if current in seen:
            return [], seen[seen.index(current):] + [current]
        seen.append(current)
        chain.insert(0, current)
        record = records.get(current)
        if record is None:
            break
        parent = record.data.get(via)
        current = None if parent is None else str(parent)
        if current is not None and current not in records:
            break
    return chain, []
