"""Cost classes for declared model entries (pure; registry layer, imports nothing outside registry).

Every declared entry is classified as one of three cost classes:

* ``free``  -- an unmetered transport, or a front door whose every tier is unmetered.
* ``usage`` -- a harness running on a subscription session pool (claude or codex
  with SUBSCRIPTION auth).
* ``spend`` -- anything metered, unknown, undeclared, or not provably free/usage.

Classification fails closed: an entry that cannot be shown to be free or usage is
``spend``. Auth state for harnesses is supplied by the caller (see
``llm_kit.adapters.harness_auth``); this module performs no I/O.

Classification rules, applied in order:

1. ``entry is None`` -> spend ("no registry entry: billing undeclared").
2. ``kind == "harness"`` with ``h = (entry.harness or "").lower()``: claude or codex
   with ``harness_auth[h].state == SUBSCRIPTION`` is usage; otherwise (including a
   missing key) spend, with the reason taken from the auth record; opencode or any
   other harness is spend.
3. ``kind == "transport"`` with ``frontdoor: true``: tiers are the transport entries in
   ``entries`` whose ``routing.group == entry.model``. Free only if that set is
   non-empty and every tier is unmetered; otherwise spend. The entry's own
   ``billing`` does not count.
4. Any other transport is free if unmetered, spend otherwise.
5. Any other kind is spend.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Optional

from llm_kit.registry.model_endpoints import HARNESS_KIND, TRANSPORT_KIND, EndpointEntry

FREE, USAGE, SPEND = "free", "usage", "spend"
COST_CLASSES = (FREE, USAGE, SPEND)

SUBSCRIPTION, METERED, UNKNOWN = "subscription", "metered", "unknown"

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
    """A cost class (FREE, USAGE or SPEND) and the reason for it."""

    cost_class: str
    reason: str


def is_unmetered(entry: EndpointEntry) -> bool:
    """True when the entry's billing mode is ``unmetered`` (the single paid/unpaid rule)."""
    return entry.billing_mode == "unmetered"


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


def _classify_harness(
    entry: EndpointEntry, harness_auth: Optional[Mapping[str, HarnessAuth]]
) -> CostVerdict:
    name = (entry.harness or "").lower()
    if name not in ("claude", "codex"):
        return CostVerdict(SPEND, f"harness {entry.harness!r} has no subscription rule")
    auth = (harness_auth or {}).get(name)
    if auth is None:
        return CostVerdict(SPEND, f"no auth state for harness {name}")
    if auth.state == SUBSCRIPTION:
        return CostVerdict(USAGE, f"{name} on subscription auth: {auth.reason}")
    return CostVerdict(SPEND, f"{name} auth is {auth.state}: {auth.reason}")


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
        return CostVerdict(SPEND, f"no tiers declared for group {group}")
    for tier in sorted(tiers, key=lambda t: t.id):
        if not is_unmetered(tier):
            return CostVerdict(SPEND, f"tier {tier.id} metered")
    return CostVerdict(FREE, f"every tier of group {group} is unmetered")


def classify(
    entry: Optional[EndpointEntry],
    *,
    entries: Optional[Mapping[str, EndpointEntry]] = None,
    harness_auth: Optional[Mapping[str, HarnessAuth]] = None,
) -> CostVerdict:
    """Classify one entry per the module rules. Pure; never probes auth."""
    if entry is None:
        return CostVerdict(SPEND, "no registry entry: billing undeclared")
    if entry.kind == HARNESS_KIND:
        return _classify_harness(entry, harness_auth)
    if entry.kind == TRANSPORT_KIND:
        if entry.frontdoor:
            return _classify_frontdoor(entry, entries)
        if is_unmetered(entry):
            return CostVerdict(FREE, "billing.mode unmetered")
        return CostVerdict(SPEND, "billing not declared unmetered")
    return CostVerdict(SPEND, f"unknown entry kind {entry.kind!r}")


def cost_class(entry: Optional[EndpointEntry], **kw) -> str:
    """The cost class name only: ``classify(entry, **kw).cost_class``."""
    return classify(entry, **kw).cost_class
