"""Discover reachable harness seats around the current model."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .model_endpoints import (
    HARNESS_KIND,
    EndpointEntry,
    EndpointRegistry,
    EndpointRegistryError,
)
from .models import discover_model_entries, load_model_config
from .reachability import (
    DEFAULT_VERIFY_TIMEOUT_S,
    STATUS_REACHABLE,
    STATUS_UNKNOWN,
    Reachability,
    check_many,
)
from .usage_budget import Budget, pinned_evaluate

_BANDS = {1: "small", 2: "workhorse", 3: "strong", 4: "frontier"}


class SeatResolutionError(EndpointRegistryError):
    """The requested self seat is unknown, ambiguous, or not classifiable."""


@dataclass(frozen=True)
class SeatSelf:
    """The classified harness entry used as the discovery reference."""

    endpoint: str
    model: str
    tier: int
    band: str
    family: str
    harness: Optional[str]

    def to_json(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "model": self.model,
            "tier": self.tier,
            "band": self.band,
            "family": self.family,
            "harness": self.harness,
        }


@dataclass(frozen=True)
class Seat:
    """A reachable classified candidate, or an indeterminate candidate."""

    relation: str
    endpoint: str
    model: str
    tier: int
    band: str
    family: str
    harness: Optional[str]
    reachability: Reachability
    budget: Optional[Budget] = None

    def to_json(self) -> dict[str, Any]:
        result = {
            "relation": self.relation,
            "endpoint": self.endpoint,
            "model": self.model,
            "tier": self.tier,
            "band": self.band,
            "family": self.family,
            "harness": self.harness,
            "reachability": self.reachability.to_json(),
        }
        if self.budget is not None:
            result["budget"] = self.budget.to_json()
        return result


@dataclass(frozen=True)
class UnclassifiedEntry:
    """A harness entry missing tier or family metadata."""

    endpoint: str
    model: str
    tier: Optional[int]
    band: Optional[str]
    family: Optional[str]
    harness: Optional[str]

    def to_json(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "model": self.model,
            "tier": self.tier,
            "band": self.band,
            "family": self.family,
            "harness": self.harness,
        }


@dataclass(frozen=True)
class SeatsResult:
    """The self seat, confirmed seats, and excluded discovery candidates."""

    self: SeatSelf
    seats: tuple[Seat, ...]
    unclassified: tuple[UnclassifiedEntry, ...]
    probe_unknown: tuple[Seat, ...]
    out_of_quota: tuple[Seat, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "self": self.self.to_json(),
            "seats": [seat.to_json() for seat in self.seats],
            "unclassified": [entry.to_json() for entry in self.unclassified],
            "probe_unknown": [seat.to_json() for seat in self.probe_unknown],
            "out_of_quota": [seat.to_json() for seat in self.out_of_quota],
        }


def _classified(entry: EndpointEntry) -> bool:
    return (
        isinstance(entry.tier, int)
        and not isinstance(entry.tier, bool)
        and entry.tier in _BANDS
        and isinstance(entry.family, str)
        and bool(entry.family.strip())
    )


def _resolve_self(self_ref: str, entries: dict[str, EndpointEntry]) -> EndpointEntry:
    direct = entries.get(self_ref)
    if direct is not None:
        if direct.kind != HARNESS_KIND:
            raise SeatResolutionError(
                f"self '{self_ref}' resolves to a non-harness endpoint"
            )
        return direct

    matches = [
        entry for entry in entries.values()
        if entry.kind == HARNESS_KIND and entry.model == self_ref
    ]
    if not matches:
        raise SeatResolutionError(f"unknown self endpoint or model '{self_ref}'")
    if len(matches) > 1:
        names = ", ".join(sorted(entry.id for entry in matches))
        raise SeatResolutionError(
            f"ambiguous self model '{self_ref}' (matching endpoints: {names})"
        )
    return matches[0]


def _self_record(entry: EndpointEntry) -> SeatSelf:
    if not _classified(entry):
        raise SeatResolutionError(
            f"self endpoint '{entry.id}' is unclassified; tier and family are required"
        )
    assert entry.tier is not None
    assert entry.family is not None
    return SeatSelf(
        endpoint=entry.id,
        model=entry.model,
        tier=entry.tier,
        band=_BANDS[entry.tier],
        family=entry.family,
        harness=entry.harness,
    )


def _candidate_record(
    entry: EndpointEntry,
    relation: str,
    reachability: Reachability,
    budget: Optional[Budget] = None,
) -> Seat:
    assert entry.tier is not None
    assert entry.family is not None
    return Seat(
        relation=relation,
        endpoint=entry.id,
        model=entry.model,
        tier=entry.tier,
        band=_BANDS[entry.tier],
        family=entry.family,
        harness=entry.harness,
        reachability=reachability,
        budget=budget,
    )


def _candidate_sort_key(entry: EndpointEntry, relation: str) -> tuple[int, int, str]:
    assert entry.tier is not None
    if relation == "UP":
        return (0, -entry.tier, entry.id)
    return (1, 0, entry.id)


def _seat_sort_key(seat: Seat) -> tuple[int, int, int, str]:
    """Order confirmed seats, de-prioritizing the under-quota ones.

    Quota rank sits BELOW relation and ABOVE tier: a seat's relation says
    whether it can do the job at all, which outranks how much of its budget is
    left, while two seats that can both do the job are separated by budget
    before anything else. An under-quota seat therefore stays selectable and
    simply loses to an equally-suitable peer -- which is the whole difference
    between de-prioritizing and disabling.
    """
    relation_rank = 0 if seat.relation == "UP" else 1
    quota_rank = 1 if (seat.budget is not None and seat.budget.deprioritized) else 0
    tier_rank = -seat.tier if seat.relation == "UP" else 0
    return (relation_rank, quota_rank, tier_rank, seat.endpoint)


def discover_seats(
    self_ref: str,
    *,
    project_root: Optional[str | Path] = None,
    timeout: Optional[float] = None,
    registry: Optional[EndpointRegistry] = None,
) -> SeatsResult:
    """Discover reachable UP and BESIDE harness seats.

    This callable first shipped in ``llm-scripting-kit 0.28.0``. It resolves
    ``self_ref`` by endpoint id, then by a unique model id. Only classified
    harness entries are candidates, and every eligible candidate is probed on
    every call through the concurrent reachability checker. Unreachable
    candidates are omitted; indeterminate probes are retained in
    ``probe_unknown``.

    A reachable candidate that declares ``conserve_usage`` is additionally
    checked against its subscription-usage pool
    (:mod:`llm_scripting_kit.usage_budget`), and its verdict has two different
    effects. OUT OF QUOTA disables the seat: it is moved to ``out_of_quota``
    instead of ``seats`` -- reported rather than dropped, so a caller can tell
    "no seat above me" from "the seat above me is spent". UNDER QUOTA merely
    de-prioritizes it: the seat stays in ``seats`` and sorts after any peer of
    the same relation that is not behind pace. Both verdicts are pinned for the
    session, so a seat cannot disappear mid-session.

    ``registry`` is an injectable parsed registry for callers and tests that
    already own a fabricated registry. When omitted, the normal layered
    configuration and user registry are loaded.
    """
    root = str(project_root) if project_root is not None else None
    config = load_model_config(project_root=root)
    discovery = discover_model_entries(
        config=config, project_root=root, registry=registry
    )
    entries = dict(discovery.entries)
    self_entry = _resolve_self(self_ref, entries)
    self_record = _self_record(self_entry)

    unclassified = tuple(
        UnclassifiedEntry(
            endpoint=entry.id,
            model=entry.model,
            tier=entry.tier,
            band=_BANDS.get(entry.tier),
            family=entry.family,
            harness=entry.harness,
        )
        for entry in sorted(entries.values(), key=lambda item: item.id)
        if entry.kind == HARNESS_KIND
        and entry.id != self_entry.id
        and not _classified(entry)
    )

    candidates: list[tuple[EndpointEntry, str]] = []
    for entry in entries.values():
        if entry.id == self_entry.id or entry.kind != HARNESS_KIND or not _classified(entry):
            continue
        assert entry.tier is not None
        assert entry.family is not None
        if entry.tier > self_record.tier:
            candidates.append((entry, "UP"))
        elif entry.tier == self_record.tier and entry.family != self_record.family:
            candidates.append((entry, "BESIDE"))
    candidates.sort(key=lambda item: _candidate_sort_key(item[0], item[1]))

    probe_input = {
        entry.id: {"kind": entry.kind, "harness": entry.harness}
        for entry, _relation in candidates
    }
    checks = check_many(
        probe_input,
        timeout=DEFAULT_VERIFY_TIMEOUT_S if timeout is None else timeout,
        project_root=root,
    )

    confirmed: list[Seat] = []
    probe_unknown: list[Seat] = []
    out_of_quota: list[Seat] = []
    for entry, relation in candidates:
        reachability = checks.get(
            entry.id,
            Reachability(
                status=STATUS_UNKNOWN,
                checked="unknown",
                detail="reachability checker returned no result",
            ),
        )
        # The pacing verdict is computed only for a candidate that is actually
        # reachable: an endpoint already excluded by its probe gains nothing
        # from a second reason, and the read would pin a verdict for the
        # session against a seat this session never had.
        budget = None
        if reachability.status == STATUS_REACHABLE and entry.conserve_usage is not None:
            budget = pinned_evaluate(entry.id, entry.conserve_usage, entry.harness)
        candidate = _candidate_record(entry, relation, reachability, budget)
        if reachability.status == STATUS_REACHABLE:
            # A spent pool DISABLES the seat; being merely behind pace does
            # not. The excluded ones are reported rather than dropped, so a
            # caller reporting "no seat above me" can tell a fleet with no
            # frontier model from one whose frontier model is out of quota --
            # only the second answer changes when the window resets.
            if budget is not None and not budget.usable:
                out_of_quota.append(candidate)
            else:
                confirmed.append(candidate)
        elif reachability.status == STATUS_UNKNOWN:
            probe_unknown.append(candidate)

    confirmed.sort(key=_seat_sort_key)
    return SeatsResult(
        self=self_record,
        seats=tuple(confirmed),
        unclassified=unclassified,
        probe_unknown=tuple(probe_unknown),
        out_of_quota=tuple(out_of_quota),
    )


__all__ = [
    "Budget",
    "Seat",
    "SeatResolutionError",
    "SeatSelf",
    "SeatsResult",
    "UnclassifiedEntry",
    "discover_seats",
]
