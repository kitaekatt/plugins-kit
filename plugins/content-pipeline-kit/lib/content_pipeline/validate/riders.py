"""Deterministic fact riders downstream stages reuse.

A *rider* is a fact derived deterministically from a candidate (a width
measurement, a token-count, a per-atom compliance check, a validator verdict)
that a later stage -- selection, delivery, audit -- needs again. Riders are
computed once and carried alongside the candidate rather than re-derived at
each downstream site; re-deriving risks two sites disagreeing on a fact that
should be singular.

Two producers feed the rider block, matching the source system's split:

- **Pure fact functions** (:func:`compute_riders`): a mapping of
  ``name -> (value, context) -> fact``. Each computes one deterministic fact
  (a length, a width). No LLM, no I/O.
- **Validator output** (:func:`rider_from_kind` / :func:`facts_from_rejections`):
  the deterministic-signal riders derived from a single validation run, so
  the riders and the hard contract can never drift -- the rider block MAPS the
  validator's :class:`content_pipeline.validate.contract.Rejection` kinds into
  ``{ok, detail}`` shape rather than forking the checking logic.

Riders attach to a candidate via :func:`attach_riders`, which is duck-typed:
a dict candidate gets a merged ``riders`` key; a frozen dataclass candidate
(``store.candidate.Candidate``) gets a ``dataclasses.replace``. Cached riders
are read back with :func:`cached_riders`.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Callable, Iterable, Mapping

from content_pipeline.validate.contract import Rejection

# A pure fact function: (candidate value, context) -> a JSON-able fact.
Rider = Callable[[Any, Any], Any]


def compute_riders(
    value: Any,
    context: Any,
    riders: Mapping[str, Rider],
) -> dict:
    """Compute a ``{name: fact}`` block from pure fact functions.

    Each entry in ``riders`` is a deterministic function of the candidate
    value and its context. The result is the fact block computed once at, e.g.,
    fill time and reused downstream.
    """
    return {name: fn(value, context) for name, fn in riders.items()}


def rider_from_kind(rejections: Iterable[Rejection], kind: str) -> dict:
    """Map a single validator kind to an ``{ok, detail}`` rider.

    ``ok`` is True when no rejection of ``kind`` is present; otherwise
    ``detail`` carries the first matching rejection's detail. This is the
    deterministic projection of a validation run into rider shape -- the rider
    does not re-check anything, it reads the verdict the validator already
    produced.
    """
    for rejection in rejections:
        if rejection.kind == kind:
            return {"ok": False, "detail": rejection.detail}
    return {"ok": True, "detail": ""}


def facts_from_rejections(
    rejections: Iterable[Rejection],
    kinds: Iterable[str],
) -> dict:
    """Build ``{kind: {ok, detail}}`` riders for several validator kinds.

    A convenience over :func:`rider_from_kind`: pass the kinds a downstream
    stage cares about, get one rider per kind from a single rejection list.
    """
    rejection_list = list(rejections)
    return {kind: rider_from_kind(rejection_list, kind) for kind in kinds}


def attach_riders(candidate: Any, riders: Mapping[str, Any]) -> Any:
    """Attach (merge) ``riders`` onto a candidate; return the new candidate.

    Duck-typed and non-mutating:

    - A ``dict`` candidate gets a copy with its ``riders`` sub-dict merged
      (existing rider keys are preserved unless overwritten by ``riders``).
    - A dataclass candidate (e.g. ``store.candidate.Candidate``) gets a
      ``dataclasses.replace`` with the merged ``riders`` field.
    """
    existing = cached_riders(candidate) or {}
    merged = {**existing, **dict(riders)}
    if isinstance(candidate, Mapping):
        out = dict(candidate)
        out["riders"] = merged
        return out
    if dataclasses.is_dataclass(candidate) and not isinstance(candidate, type):
        return dataclasses.replace(candidate, riders=merged)
    raise TypeError(
        f"attach_riders: unsupported candidate type {type(candidate).__name__}"
    )


def cached_riders(candidate: Any) -> dict:
    """Return the riders already attached to ``candidate`` (``{}`` if none)."""
    if isinstance(candidate, Mapping):
        value = candidate.get("riders")
    else:
        value = getattr(candidate, "riders", None)
    return dict(value) if value else {}
