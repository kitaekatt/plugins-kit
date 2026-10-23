"""Policy-facing re-export of the adapter endpoint profiles.

Endpoint profiles (``EndpointProfile`` and the profile and capability
specialisation functions) are owned by ``llm_kit.adapters.endpoint_profile``.
Policy callers that may import dispatch but not adapters reach them here; the
objects are the adapters' own, not copies.
"""
from __future__ import annotations

from llm_kit.adapters.endpoint_profile import (
    EndpointProfile,
    endpoint_capabilities,
    profile_from_entry,
    profile_from_resolved,
    resolve_endpoint_profile,
    unresolved_profile,
)

__all__ = [
    "EndpointProfile",
    "endpoint_capabilities",
    "profile_from_entry",
    "profile_from_resolved",
    "resolve_endpoint_profile",
    "unresolved_profile",
]
