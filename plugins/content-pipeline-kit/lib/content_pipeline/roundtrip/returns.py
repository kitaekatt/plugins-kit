"""Export-for-review / intake-corrections closed loop.

Exports a batch of generated content into a human-reviewable form (a workbook,
a spreadsheet), then re-ingests corrections made against that export back into
the store as HUMAN-attributed values (so ``store.attributed``'s human-always-
wins precedence protects them from the next machine pass). This is the batch-
shaped sibling of ``questions``'s per-entity loop.

Generalizes the localization workbook export / intake shape. The workbook
FORMAT stays project-side: :func:`export_for_review` takes a ``serialize``
callable and :func:`intake_corrections` a ``parse`` callable, so the xlsx
(or csv, or anything) specifics never leak into this module. What IS generic:

- **Export a snapshot** -- reduce each entity to a review row, hand the rows to
  the caller's serializer.
- **Intake as human-attributed** -- read the returned rows, keep only the rows
  a human actually corrected (a filled correction column), and surface each as
  a :class:`Correction` carrying a ``human`` attribution so the store write can
  land it on the human slice.

Only stdlib is imported here; ``store`` is reachable per the dependency
contract but this module deliberately stays store-shape-agnostic (it emits
:class:`Correction` values; the caller decides how to write them onto its
store, e.g. onto an ``AttributedField.human`` slice).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, List, Mapping, Optional, Sequence


@dataclass(frozen=True)
class ReviewRow:
    """One row of a review snapshot.

    - ``entity_id`` -- the entity this row projects.
    - ``fields`` -- the snapshot columns (source text, current value, context).
    """

    entity_id: str
    fields: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Correction:
    """One human correction ingested from a reviewed artifact.

    - ``entity_id`` -- the corrected entity.
    - ``value`` -- the human-supplied value.
    - ``attribution`` -- always ``"human"`` (the point of the intake is that a
      correction lands on the human slice); kept explicit so a store write site
      cannot mis-file it.
    - ``meta`` -- any extra per-correction fields the reviewer supplied (a
      change category, a comment).
    """

    entity_id: str
    value: Any
    attribution: str = "human"
    meta: Mapping[str, Any] = field(default_factory=dict)


def export_for_review(
    entities: Sequence[Any],
    destination: Any,
    *,
    to_row: Callable[[Any], ReviewRow],
    serialize: Callable[[Any, List[ReviewRow]], None],
) -> List[ReviewRow]:
    """Project ``entities`` into review rows and serialize them to ``destination``.

    ``to_row`` reduces one entity to a :class:`ReviewRow` snapshot; ``serialize``
    writes the row list to ``destination`` in whatever format the consumer
    reviews in (the format is entirely the caller's). Returns the row list that
    was exported (for logging / assertion).
    """
    rows = [to_row(e) for e in entities]
    serialize(destination, rows)
    return rows


def intake_corrections(
    source: Any,
    *,
    parse: Callable[[Any], Sequence[Mapping[str, Any]]],
    to_correction: Callable[[Mapping[str, Any]], Optional[Correction]],
) -> List[Correction]:
    """Read a reviewed artifact and return the human corrections it carries.

    ``parse`` reads ``source`` into row mappings (the format is the caller's --
    e.g. an xlsx reader). ``to_correction`` maps one row to a
    :class:`Correction`, or returns ``None`` for a row the human did not
    correct (an empty correction column) -- so only genuine human edits are
    ingested, never the untouched export values. Returns the corrections in row
    order.
    """
    corrections: List[Correction] = []
    for row in parse(source):
        correction = to_correction(row)
        if correction is not None:
            corrections.append(correction)
    return corrections


def apply_corrections(
    corrections: Sequence[Correction],
    write: Callable[[Correction], None],
) -> int:
    """Write each correction via ``write``; return the count applied.

    The write callable lands the human value on the store (typically onto the
    ``human`` slice of an attributed field, so it wins the do-no-harm
    precedence forever). Kept as a thin loop so the store-shape coupling lives
    at the single caller-supplied ``write`` site.
    """
    count = 0
    for correction in corrections:
        write(correction)
        count += 1
    return count


__all__ = [
    "ReviewRow",
    "Correction",
    "export_for_review",
    "intake_corrections",
    "apply_corrections",
]
