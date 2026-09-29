"""Per-endpoint facts that specialize an adapter family's advertisement.

A :class:`~.capabilities.Capabilities` record describes an adapter FAMILY, and
for ``openrouter`` that family spans servers that disagree about where a
reasoning effort goes. An :class:`EndpointProfile` carries what the registry
says about ONE endpoint, and :func:`endpoint_capabilities` turns the family
record into that endpoint's truthful record.

Every function here reads configuration and never raises for an unresolvable
endpoint: an endpoint that cannot be resolved has a profile whose effort
source is ``"none"``, which specializes nothing.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping, Optional

from ..effort import EffortDelivery, SOURCE_NONE, remap_effort
from ..model_endpoints import EndpointEntry, resolve_effort_style
from .capabilities import Capabilities, ParamCapability

_EFFORT = "effort"


@dataclass(frozen=True)
class EndpointProfile:
    """What the registry says about one endpoint.

    ``declared_effort`` is the entry's ``reasoning_effort`` default (None when
    it declares none). ``effort`` is how a direct call delivers an effort.
    """

    endpoint: Optional[str]
    effort: EffortDelivery
    declared_effort: Optional[str] = None
    # Per-endpoint output-mode facts are the planned next field here.

    @property
    def delivered_effort(self) -> Optional[str]:
        """The value the declared default puts on the wire, or None if it cannot."""
        if self.declared_effort is None or not self.effort.deliverable:
            return None
        return remap_effort(self.declared_effort, self.effort.style)


def profile_from_entry(entry: EndpointEntry) -> EndpointProfile:
    """The profile of a registry (or config-merged) entry."""
    return EndpointProfile(
        endpoint=entry.id,
        effort=resolve_effort_style(entry),
        declared_effort=entry.reasoning_effort,
    )


def profile_from_resolved(
    resolved: Mapping[str, Any], *, endpoint: Optional[str] = None
) -> EndpointProfile:
    """The profile of a ``models.resolve_endpoint`` result."""
    defaults = resolved.get("request_defaults") or {}
    return EndpointProfile(
        endpoint=endpoint if endpoint is not None else resolved.get("name"),
        effort=EffortDelivery(
            resolved.get("effort_style"),
            resolved.get("effort_style_source") or SOURCE_NONE,
        ),
        declared_effort=defaults.get("reasoning_effort"),
    )


def unresolved_profile(endpoint: Optional[str]) -> EndpointProfile:
    """The profile of an endpoint nothing could be resolved for."""
    return EndpointProfile(endpoint=endpoint, effort=EffortDelivery(None, SOURCE_NONE))


def resolve_endpoint_profile(
    endpoint: Optional[str], *, project_root: Optional[str] = None
) -> EndpointProfile:
    """Resolve ``endpoint`` (None: the configured default) to its profile.

    Never raises: an unresolvable endpoint yields :func:`unresolved_profile`.
    """
    from ..models import resolve_endpoint  # noqa: PLC0415 -- import cycle

    try:
        resolved = resolve_endpoint(
            endpoint, project_root=str(project_root) if project_root is not None else None
        )
    except Exception:  # noqa: BLE001 -- an unresolvable endpoint delivers nothing
        return unresolved_profile(endpoint)
    return profile_from_resolved(resolved, endpoint=endpoint or resolved.get("name"))


def _effort_note(delivery: EffortDelivery) -> str:
    note = (
        f"emitted as {delivery.emits} (effort_style {delivery.style}, from "
        f"{delivery.source}); an effort already in extras wins verbatim and an "
        "explicit null there sends none"
    )
    if delivery.to_json().get("remap"):
        note += "; high is sent as xhigh"
    return note


def endpoint_capabilities(
    record: Capabilities, profile: Optional[EndpointProfile]
) -> Capabilities:
    """``record`` specialized to one endpoint's profile.

    When the profile delivers an effort and the record lists ``effort`` as
    conditional, ``effort`` moves into ``params`` with the concrete emission,
    leaves ``dropped_params`` and ``conditional_params``, and ``endpoint`` is
    set. Otherwise ``record`` is returned unchanged -- the family record is
    already the truth for an endpoint that delivers nothing.
    """
    if (
        profile is None
        or not profile.effort.deliverable
        or _EFFORT not in record.conditional_params
    ):
        return record
    params = dict(record.params)
    params[_EFFORT] = ParamCapability(
        type=record.conditional_params[_EFFORT].type,
        emits=profile.effort.emits,
        note=_effort_note(profile.effort),
    )
    return replace(
        record,
        params=params,
        dropped_params=tuple(p for p in record.dropped_params if p != _EFFORT),
        conditional_params={
            k: v for k, v in record.conditional_params.items() if k != _EFFORT
        },
        endpoint=profile.endpoint,
    )


__all__ = [
    "EndpointProfile",
    "profile_from_entry",
    "profile_from_resolved",
    "unresolved_profile",
    "resolve_endpoint_profile",
    "endpoint_capabilities",
]
