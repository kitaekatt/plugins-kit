"""Quota-aware selection over a caller's preference order (compatibility layer).

The selection rule lives in :mod:`llm_scripting_kit.declaration`:
:func:`~.declaration.describe` classifies a declaration, removes out-of-quota
entries from the usable set, and orders the rest by PACE
(:func:`~.declaration.order_by_pace`). :func:`choose_endpoint` is a thin caller
of it that keeps this module's :class:`QuotaSelection` return shape, and
:func:`rank_candidates` keeps the two-band rank (available before
under-quota, stated order inside each band) for callers that still import it.
Both names are kept until migration step 12; other callers use ``describe``.

The one rule both layers share: **an out-of-quota endpoint is removed, a
behind-pace endpoint is only moved.** Treating "behind pace" as "unusable"
would drop a model that can still answer, which is the opposite of what pacing
is for.

**This module ranks; it does not dispatch.** Endpoints that do not declare
``conserve_usage`` have no budget and are never moved or removed by it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .usage_budget import Budget

#: Rank of each disposition within the ordering. Lower sorts first. Only two
#: values, because only two things can happen to a usable endpoint: it is
#: preferred, or it is de-prioritized behind its peers.
_RANK_AVAILABLE = 0
_RANK_UNDER_QUOTA = 1


@dataclass(frozen=True)
class Candidate:
    """One endpoint from the caller's preference list, with its verdict.

    ``budget`` is None when the endpoint declares no ``conserve_usage`` (or is
    not a harness entry) -- an unpaced endpoint, which ranks as available.
    ``preference_index`` is the endpoint's position in the ORIGINAL list, kept
    so a caller can see that the order it stated was honored inside its band.
    """

    endpoint: str
    preference_index: int
    budget: Optional[Budget] = None

    @property
    def usable(self) -> bool:
        return self.budget is None or self.budget.usable

    @property
    def deprioritized(self) -> bool:
        return self.budget is not None and self.budget.deprioritized

    def to_json(self) -> Dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "preference_index": self.preference_index,
            "budget": self.budget.to_json() if self.budget is not None else None,
        }


@dataclass(frozen=True)
class QuotaSelection:
    """The outcome. ``chosen`` is the head of ``ranked``, else the ``default``.

    With nothing usable and no ``default``, :func:`choose_endpoint` raises the
    floor instead of returning.

    ``ranked`` is every usable candidate in the order they should be tried, so
    a caller with its own retry loop gets the whole fallback chain rather than
    just the head of it. ``disabled`` carries the out-of-quota ones, reported
    rather than dropped: "we skipped opus because its pool is spent" is a
    different fact from "opus was never a candidate", and only the first
    changes when the window resets.

    ``used_default`` says the answer came from the ``default`` rather than from
    the preference list -- the caller's own fallback, not a ranking decision.
    """

    chosen: Optional[str]
    ranked: tuple[Candidate, ...]
    disabled: tuple[Candidate, ...]
    used_default: bool = False
    reason: str = ""

    def to_json(self) -> Dict[str, Any]:
        return {
            "chosen": self.chosen,
            "used_default": self.used_default,
            "reason": self.reason,
            "ranked": [c.to_json() for c in self.ranked],
            "disabled": [c.to_json() for c in self.disabled],
        }


def rank_candidates(candidates: Sequence[Candidate]) -> "tuple[List[Candidate], List[Candidate]]":
    """DEPRECATED two-band rank; use ``declaration.order_by_pace``. Pure.

    Splits into (ranked usable, disabled): available before under-quota, the
    sort STABLE on ``preference_index`` inside each band. awesome-kit's
    orchestrate renderer probes for this name, so it keeps its behaviour until
    that renderer moves to the pace rule (migration step 4); it is removed at
    step 12.
    """
    usable = [c for c in candidates if c.usable]
    disabled = [c for c in candidates if not c.usable]
    usable.sort(
        key=lambda c: (
            _RANK_UNDER_QUOTA if c.deprioritized else _RANK_AVAILABLE,
            c.preference_index,
        )
    )
    return usable, disabled


def _reason(
    ranked: Sequence[Candidate],
    spent: Sequence[str],
    unknown: Sequence[str],
    other: Sequence[str],
    default: Optional[str],
) -> str:
    """Say why the head was chosen, keeping the exclusion causes apart.

    ``spent`` and ``unknown`` stay separate: an endpoint that names no
    configured entry was excluded by a CONFIGURATION error, and calling that
    "out of quota" is a false claim about the account -- one that would send a
    reader looking at their usage for a typo. ``unknown`` and ``other`` are
    non-empty only on the floor path, the one surface allowed to name a hidden
    id; a successful selection passes rendered entries only.
    """
    causes = []
    if spent:
        causes.append(f"out of quota ({', '.join(spent)})")
    if unknown:
        causes.append(f"not configured ({', '.join(unknown)})")
    if other:
        causes.append(f"not usable here ({', '.join(other)})")
    if not ranked:
        if not causes:
            return "no candidates were given"
        why = "every candidate was excluded: " + "; ".join(causes)
        if default is None:
            return f"{why}; no default was given"
        return f"{why}; fell back to '{default}'"
    head = ranked[0]
    parts = [f"'{head.endpoint}'"]
    passed_over = [c.endpoint for c in ranked[1:] if c.preference_index < head.preference_index]
    if passed_over:
        parts.append(f"higher pace than {', '.join(passed_over)}")
    parts.extend(f"skipping {cause}" for cause in causes)
    return "; ".join(parts)


def choose_endpoint(
    preferences: Sequence[str],
    *,
    default: Optional[str] = None,
    entries: Optional[Mapping[str, Any]] = None,
    project_root: Optional[str] = None,
) -> QuotaSelection:
    """Pick one endpoint from ``preferences``: a thin caller of ``describe``.

    ``preferences`` is a model declaration; the selection is
    :func:`~.declaration.describe` for a process caller, so out-of-quota
    entries leave the chain and the usable ones are ordered by pace (D5).
    Reachability is NOT probed -- every entry is answered from a cache of
    ``unknown``, which counts as usable -- because this API never spawned or
    fetched anything and callers rely on that.

    ``default`` is the caller's own fallback when nothing is usable, returned
    with ``used_default`` set. Without one the floor
    (:class:`~.declaration.NoUsableRoutingTarget`) propagates. Kept until
    migration step 12; other callers use ``describe`` directly.
    """
    from .declaration import (  # noqa: PLC0415 -- declaration imports this package's models
        DISPOSITION_OUT_OF_QUOTA,
        DISPOSITION_UNREACHABLE,
        DISPOSITION_UNRESOLVED,
        DISPOSITION_USABLE,
        NoUsableRoutingTarget,
        describe,
    )
    from .reachability import STATUS_UNKNOWN, Reachability  # noqa: PLC0415

    names = list(preferences)
    if not names:
        return QuotaSelection(
            chosen=default, ranked=(), disabled=(), used_default=default is not None,
            reason=_reason((), (), (), (), default),
        )
    unprobed = {
        name: Reachability(status=STATUS_UNKNOWN, checked="none", detail="not probed by choose_endpoint")
        for name in names
    }
    try:
        ranking = describe(
            names, project_root=project_root, caller="process",
            entries=entries, reachability_cache=unprobed,
        )
        # A success names rendered entries only: out-of-quota ones may be
        # reported as disabled, hidden ones (unresolved, unroutable, ...) are
        # skipped silently and surface only through the floor below.
        dispositions = tuple(
            d for d in ranking.dispositions
            if d.disposition in (DISPOSITION_USABLE, DISPOSITION_OUT_OF_QUOTA, DISPOSITION_UNREACHABLE)
        )
        ranked = tuple(
            Candidate(endpoint=e.id, preference_index=e.declared_index, budget=_pinned_budget(e))
            for e in ranking.rendered_entries
            if e.usable
        )
    except NoUsableRoutingTarget as floor:
        if default is None:
            raise
        dispositions, ranked = floor.dispositions, ()

    spent = [d.id for d in dispositions if d.disposition == DISPOSITION_OUT_OF_QUOTA]
    unknown = [d.id for d in dispositions if d.disposition == DISPOSITION_UNRESOLVED]
    other = [
        d.id for d in dispositions
        if d.disposition not in (DISPOSITION_USABLE, DISPOSITION_OUT_OF_QUOTA, DISPOSITION_UNRESOLVED)
    ]
    disabled = tuple(
        Candidate(endpoint=d.id, preference_index=d.declared_index, budget=None)
        for d in dispositions
        if d.disposition != DISPOSITION_USABLE
    )
    chosen = ranked[0].endpoint if ranked else default
    return QuotaSelection(
        chosen=chosen,
        ranked=ranked,
        disabled=disabled,
        used_default=not ranked and default is not None,
        reason=_reason(ranked, spent, unknown, other, default),
    )


def _pinned_budget(entry: Any) -> Optional[Budget]:
    """The pinned verdict an EntryState was ranked on, as a Budget."""
    if entry.usability is None:
        return None
    return Budget(
        status=entry.usability, pool=entry.pool or "", detail="",
        remaining=entry.remaining, window_remaining=entry.window_remaining,
        resets_at=entry.resets_at,
    )


__all__ = ["Candidate", "QuotaSelection", "choose_endpoint", "rank_candidates"]
