"""Deterministic endpoint selection from the llm-scripting-kit advertisement."""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from pathlib import Path
from typing import Optional

from llm_scripting_kit.completion import (
    BackendSelection,
    Capabilities,
    adapter_capabilities,
    create_backend,
    match_capabilities,
)
from llm_scripting_kit.models import EndpointResolveError

from .model import Job


class SelectionError(Exception):
    """Base class for endpoint-selection errors."""


class NoCompatibleEndpointError(SelectionError):
    """No preferred endpoint matched the job requirements."""

    def __init__(self, job_id: str, endpoints: Sequence[str]) -> None:
        self.job_id = job_id
        self.endpoints = tuple(endpoints)
        super().__init__(
            f"job {job_id!r} has no compatible endpoint in preference order "
            f"{list(self.endpoints)!r}"
        )


def requirements_match(capabilities: Capabilities, requirements: object) -> bool:
    """Return whether an advertisement satisfies a job requirement mapping.

    Thin compatibility alias for llm-scripting-kit's
    ``llm_scripting_kit.completion.match_capabilities``, which owns the
    requirement language (the named convenience keys ``params``,
    ``execution_controls``/``controls``, ``dropped_params``,
    ``structured_output`` and ``system_prompt``, plus dotted-path lookups over
    ``Capabilities.to_json()`` for any other key). job-kit keeps this name so
    callers in this package do not have to import from llm-scripting-kit
    directly.
    """
    return match_capabilities(capabilities, requirements)


BackendFactory = Callable[..., BackendSelection]
CapabilitiesProvider = Callable[[], Mapping[str, Capabilities]]


def _make_selection(
    factory: BackendFactory, endpoint: str, project_root: Optional[str | Path]
) -> BackendSelection:
    """Call the endpoint factory with the selected project root."""
    if project_root is None:
        return factory(endpoint)
    return factory(endpoint, project_root=project_root)


def select_endpoint(
    job: Job,
    *,
    halted_endpoints: Collection[str] = (),
    capabilities: Optional[Mapping[str, Capabilities]] = None,
    capabilities_provider: Optional[CapabilitiesProvider] = None,
    backend_factory: Optional[BackendFactory] = None,
    project_root: Optional[str | Path] = None,
) -> BackendSelection:
    """Select the first compatible preferred endpoint.

    The factory resolves endpoint names through llm-scripting-kit's registry;
    the returned backend name keys the advertisement. A persistent halt for an
    endpoint excludes it for later jobs in the same run. No scoring or retry is
    performed.
    """
    advertised = dict(
        capabilities
        if capabilities is not None
        else (capabilities_provider or adapter_capabilities)()
    )
    factory = backend_factory or create_backend
    halted = set(halted_endpoints)
    for endpoint in job.endpoint_preference:
        if endpoint in halted:
            continue
        try:
            selection = _make_selection(factory, endpoint, project_root)
        except EndpointResolveError:
            continue
        backend_name = getattr(selection.backend, "name", None)
        if not isinstance(backend_name, str):
            continue
        record = advertised.get(backend_name)
        if record is None:
            record = advertised.get(endpoint)
        if record is not None and requirements_match(record, job.requirements):
            return selection
    raise NoCompatibleEndpointError(job.id, job.endpoint_preference)


def choose_endpoint(
    job: Job,
    *,
    halted_endpoints: Collection[str] = (),
    capabilities: Optional[Mapping[str, Capabilities]] = None,
    capabilities_provider: Optional[CapabilitiesProvider] = None,
    backend_factory: Optional[BackendFactory] = None,
    project_root: Optional[str | Path] = None,
) -> str:
    """Return only the selected endpoint name."""
    return select_endpoint(
        job,
        halted_endpoints=halted_endpoints,
        capabilities=capabilities,
        capabilities_provider=capabilities_provider,
        backend_factory=backend_factory,
        project_root=project_root,
    ).endpoint


__all__ = [
    "SelectionError",
    "NoCompatibleEndpointError",
    "requirements_match",
    "select_endpoint",
    "choose_endpoint",
]
