"""Cost classes for declared model entries (pure; registry layer, imports nothing outside registry).

Every declared entry is classified as one of three cost classes:

* ``local``        -- a transport the registry declares unmetered AND self-owned
  (``billing: {mode: unmetered, owner: self}``), a front door whose every tier is, or
  a harness entry that names such an endpoint. ``billing.mode`` alone states marginal
  cost, not who owns the hardware, so an unmetered entry without ``owner: self`` is
  ``metered`` (billing unverified).
* ``subscription`` -- a harness running on a subscription session pool (claude or codex
  with SUBSCRIPTION auth).
* ``metered``      -- paid OR unverified billing: anything metered, unknown, undeclared,
  or not provably local/subscription. The class states that billing could not be shown
  to be free; it never claims that a charge occurred.

Classification fails closed: an entry that cannot be shown to be local or subscription
is ``metered``. Auth state for harnesses is supplied by the caller (see
``llm_kit.adapters.harness_auth``); this module performs no I/O. A classification is
information and is always reported with its reason; whether a caller may use a class
is the optional consent layer's business (``llm_kit.registry.consent``).

Classification rules, applied in order:

1. ``entry is None`` -> metered ("no registry entry: billing undeclared").
2. ``kind == "harness"`` with ``h = (entry.harness or "").lower()``: claude or codex
   with ``harness_auth[h].state == SUBSCRIPTION`` is subscription; otherwise (including
   a missing key) metered, with the reason taken from the auth record. Any other harness
   (opencode) that declares ``endpoint: <entry id>`` takes the class of that transport
   entry (a front door counts every tier); an endpoint that is missing from ``entries``
   or is not a transport entry is metered, with a reason naming it. Any other harness
   without an ``endpoint`` is metered.
3. ``kind == "transport"`` with ``frontdoor: true``: tiers are the transport entries in
   ``entries`` whose ``routing.group == entry.model``. Local only if that set is
   non-empty and every tier is unmetered and self-owned; otherwise metered. The entry's
   own ``billing`` does not count.
4. Any other transport is local if unmetered and self-owned, metered otherwise.
5. Any other kind is metered.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Optional

from llm_kit.registry.model_endpoints import (
    AUTH_CLASSIFIED_HARNESSES,
    HARNESS_KIND,
    TRANSPORT_KIND,
    EndpointEntry,
)

# The harness auth states SUBSCRIPTION and METERED share their spelling with the
# cost classes of the same name: a claude or codex harness on SUBSCRIPTION auth is
# class ``subscription``, and METERED auth is class ``metered``. UNKNOWN auth is
# class ``metered`` (fail closed).
LOCAL, SUBSCRIPTION, METERED, UNKNOWN = "local", "subscription", "metered", "unknown"
COST_CLASSES = (LOCAL, SUBSCRIPTION, METERED)

# Billing-diverting environment variables: any of these set and non-empty routes
# Claude billing away from the subscription session pool. Must equal
# ``llm_kit.pipeline.execution.drivers.claude_bg.BILLING_DIVERTING_VARS``.
BILLING_DIVERTING_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "AWS_BEARER_TOKEN_BEDROCK",
    "ANTHROPIC_VERTEX_PROJECT_ID",
)


@dataclass(frozen=True)
class HarnessAuth:
    """How one harness is authenticated: ``state`` is SUBSCRIPTION, METERED or UNKNOWN.

    ``reason`` names fields and variables, never secret values.
    """

    state: str
    reason: str


@dataclass(frozen=True)
class CostVerdict:
    """A cost class (LOCAL, SUBSCRIPTION or METERED) and the reason for it."""

    cost_class: str
    reason: str


def is_unmetered(entry: EndpointEntry) -> bool:
    """True when the entry's billing mode is ``unmetered`` (the single paid/unpaid rule)."""
    return entry.billing_mode == "unmetered"


def is_self_owned(entry: EndpointEntry) -> bool:
    """True when the entry declares ``billing.owner: self`` (the user owns the hardware)."""
    return entry.billing_owner == "self"


def normalize_allow(allow: Optional[Iterable[str]]) -> Optional[frozenset]:
    """Normalize an allow set to a frozenset of cost classes; None stays None.

    Raises ValueError on an unknown cost class. Consumes a generator once.
    """
    if allow is None:
        return None
    if isinstance(allow, str):
        raise ValueError(
            f"allow must be an iterable of cost classes, not the string {allow!r}; "
            f"valid classes: {', '.join(COST_CLASSES)}"
        )
    names = frozenset(allow)
    unknown = sorted(str(n) for n in names if n not in COST_CLASSES)
    if unknown:
        raise ValueError(
            f"unknown cost class {', '.join(repr(u) for u in unknown)}; "
            f"valid classes: {', '.join(COST_CLASSES)}"
        )
    return names


def _classify_transport(entry: EndpointEntry) -> CostVerdict:
    """A plain transport: local only when declared unmetered AND self-owned."""
    if not is_unmetered(entry):
        return CostVerdict(METERED, "billing not declared unmetered (billing unverified)")
    if not is_self_owned(entry):
        return CostVerdict(
            METERED,
            "billing.mode unmetered, but hardware ownership is not declared "
            "(billing.owner: self) (billing unverified)",
        )
    return CostVerdict(LOCAL, "billing.mode unmetered, owner self")


def _classify_harness(
    entry: EndpointEntry,
    entries: Optional[Mapping[str, EndpointEntry]],
    harness_auth: Optional[Mapping[str, HarnessAuth]],
) -> CostVerdict:
    name = (entry.harness or "").lower()
    if name not in AUTH_CLASSIFIED_HARNESSES:
        if entry.endpoint is None:
            return CostVerdict(
                METERED,
                f"harness {entry.harness!r} declares no endpoint and has no subscription rule "
                "(billing unverified)",
            )
        return _classify_harness_endpoint(entry, entries)
    auth = (harness_auth or {}).get(name)
    if auth is None:
        return CostVerdict(METERED, f"no auth state for harness {name} (billing unverified)")
    if auth.state == SUBSCRIPTION:
        return CostVerdict(SUBSCRIPTION, f"{name} on subscription auth: {auth.reason}")
    return CostVerdict(METERED, f"{name} auth is {auth.state} (billing unverified): {auth.reason}")


def _classify_harness_endpoint(
    entry: EndpointEntry, entries: Optional[Mapping[str, EndpointEntry]]
) -> CostVerdict:
    """A harness takes the class of the transport entry it declares it runs on."""
    target_id = entry.endpoint
    target = (entries or {}).get(target_id)
    if target is None:
        return CostVerdict(
            METERED,
            f"harness endpoint {target_id!r} is not a known registry entry (billing unverified)",
        )
    if target.kind != TRANSPORT_KIND:
        return CostVerdict(
            METERED,
            f"harness endpoint {target_id!r} is a {target.kind} entry, not a transport "
            "(billing unverified)",
        )
    inner = _classify_transport_or_frontdoor(target, entries)
    return CostVerdict(inner.cost_class, f"runs on endpoint {target_id}: {inner.reason}")


def _classify_frontdoor(
    entry: EndpointEntry, entries: Optional[Mapping[str, EndpointEntry]]
) -> CostVerdict:
    group = entry.model
    tiers = [
        e
        for e in (entries or {}).values()
        if e.kind == TRANSPORT_KIND and e.routing is not None and e.routing.group == group
    ]
    if not tiers:
        return CostVerdict(METERED, f"no tiers declared for group {group} (billing unverified)")
    for tier in sorted(tiers, key=lambda t: t.id):
        if not is_unmetered(tier):
            return CostVerdict(METERED, f"tier {tier.id} is not declared unmetered (billing unverified)")
        if not is_self_owned(tier):
            return CostVerdict(
                METERED,
                f"tier {tier.id} is unmetered but its hardware ownership is not declared "
                "(billing.owner: self) (billing unverified)",
            )
    return CostVerdict(LOCAL, f"every tier of group {group} is unmetered and owned by self")


def _classify_transport_or_frontdoor(
    entry: EndpointEntry, entries: Optional[Mapping[str, EndpointEntry]]
) -> CostVerdict:
    if entry.frontdoor:
        return _classify_frontdoor(entry, entries)
    return _classify_transport(entry)


def classify(
    entry: Optional[EndpointEntry],
    *,
    entries: Optional[Mapping[str, EndpointEntry]] = None,
    harness_auth: Optional[Mapping[str, HarnessAuth]] = None,
) -> CostVerdict:
    """Classify one entry per the module rules. Pure; never probes auth."""
    if entry is None:
        return CostVerdict(METERED, "no registry entry: billing undeclared")
    if entry.kind == HARNESS_KIND:
        return _classify_harness(entry, entries, harness_auth)
    if entry.kind == TRANSPORT_KIND:
        return _classify_transport_or_frontdoor(entry, entries)
    return CostVerdict(METERED, f"unknown entry kind {entry.kind!r} (billing unverified)")


def cost_class(entry: Optional[EndpointEntry], **kw) -> str:
    """The cost class name only: ``classify(entry, **kw).cost_class``."""
    return classify(entry, **kw).cost_class
