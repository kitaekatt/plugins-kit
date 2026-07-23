"""Canonical store -> consumer-visible projected result.

The canonical store carries attribution metadata, candidate history, and
intermediary state a downstream consumer does not need. Projection strips
that machinery down to the plain value shape a delivery mode or an external
reader expects, applying the effective-value precedence from ``attributed``
(and, for multi-candidate fields, the active-candidate pick from
``candidate``) along the way.

This module owns *computing* the projected view -- reducing an attributed
field to its effective value, and a candidate cell to its active value. It
does NOT write anything: the actual materialization of the projected view
(in-place mutation, append-only projection file) lives in ``deliver``. That
seam -- consumers read only the projected result while all the machinery
stays upstream -- is the canonical-store-plus-projection pattern both source
systems converged on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from content_pipeline.store.attributed import Present, effective_value
from content_pipeline.store.candidate import CandidateCell


def project_field(
    sourced: object = None,
    machine: object = None,
    human: object = None,
    *,
    present: Optional[Present] = None,
) -> object:
    """Project one attributed field to its effective value.

    A thin composition over ``attributed.effective_value`` so a projection
    site does not import two modules for the common single-field case.
    """
    return effective_value(sourced, machine, human, present=present)


def project_cell(cell: Optional[CandidateCell], *, default: Any = None) -> Any:
    """Project a candidate cell to its active candidate's value.

    Returns ``default`` when the cell is ``None`` or has no active candidate
    -- the consumer sees a plain value (or the default), never the
    active/shadow/retired machinery.
    """
    if cell is None:
        return default
    active = cell.active
    return active.value if active is not None else default


@dataclass(frozen=True)
class GroupSlices:
    """Which record fields hold the three slices of one attributed group.

    ``sourced`` / ``machine`` / ``human`` are field names on the store
    record; any may be ``None`` (a group with no sourced slice, say).
    ``present`` is the optional presence predicate (block precedence).
    """

    sourced: Optional[str] = None
    machine: Optional[str] = None
    human: Optional[str] = None
    present: Optional[Present] = None


@dataclass(frozen=True)
class ProjectionSpec:
    """Declares how to reduce a store record to its projected view.

    - ``attributed`` -- ``output_key -> GroupSlices``: each output field is
      the effective value of an attributed group.
    - ``cells`` -- ``output_key -> record_field``: each output field is the
      active-candidate value of a :class:`CandidateCell` on the record.
    - ``passthrough`` -- record fields copied to the output verbatim.
    """

    attributed: Mapping[str, GroupSlices] = field(default_factory=dict)
    cells: Mapping[str, str] = field(default_factory=dict)
    passthrough: tuple = ()


def _record_get(record: Any, name: str, default: object = None) -> object:
    """Duck-typed field read (dict or attribute)."""
    if isinstance(record, Mapping):
        return record.get(name, default)
    return getattr(record, name, default)


def project(store_record: Any, spec: ProjectionSpec) -> dict:
    """Reduce a canonical store record to its consumer-visible projected form.

    For each attributed group, the effective value (human > machine >
    sourced) is written under its output key; for each candidate-cell field,
    the active candidate's value; passthrough fields are copied verbatim. The
    result is a plain dict carrying none of the attribution/candidate
    machinery.
    """
    out: dict = {}
    for out_key, slices in spec.attributed.items():
        sourced = _record_get(store_record, slices.sourced) if slices.sourced else None
        machine = _record_get(store_record, slices.machine) if slices.machine else None
        human = _record_get(store_record, slices.human) if slices.human else None
        out[out_key] = effective_value(sourced, machine, human, present=slices.present)
    for out_key, field_name in spec.cells.items():
        out[out_key] = project_cell(_record_get(store_record, field_name))
    for name in spec.passthrough:
        out[name] = _record_get(store_record, name)
    return out
