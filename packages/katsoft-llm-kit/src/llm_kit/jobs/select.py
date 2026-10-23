"""Deterministic endpoint selection: the first usable entry of a pace-ordered declaration."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Collection, Mapping
from pathlib import Path
from typing import Any, Optional, Union

from llm_kit.completion.capabilities import Capabilities
from llm_kit.completion.factory_protocols import BackendFactory, CapabilityProvider
from llm_kit.completion.requirements import match_capabilities
from llm_kit.dispatch.declaration import (
    CALLER_PROCESS as _CALLER_PROCESS,
    NoUsableRoutingTarget,
    describe as _describe,
)
from llm_kit.dispatch.endpoint_resolver import EndpointResolver
from llm_kit.dispatch.factory import BackendSelection
from llm_kit.dispatch.pipeline_backends import ConfiguredBackendFactory

from .model import Job


class SelectionError(Exception):
    """Base class for endpoint-selection errors."""


class EffortUndeliverableError(SelectionError):
    """The selected entry cannot carry the effort the job states for it.

    Raised before dispatch when a job's ``model_efforts`` names an effort for
    the selected entry and that entry's adapter advertises no delivered
    ``effort`` param. A ``SelectionError``, so the runner terminalizes the one
    job (unroutable, with this message) and keeps the rest of the run going
    rather than running the job at an effort nobody stated.
    """


def adapter_capabilities() -> Mapping[str, Capabilities]:
    """The shipped adapter advertisement, served by the default provider.

    ``ConfiguredBackendFactory`` is the default
    :class:`~llm_kit.completion.factory_protocols.CapabilityProvider`; jobs ask
    it instead of importing the adapter tables. A caller that injects
    ``capabilities`` or ``capabilities_provider`` never reaches this.
    """
    return ConfiguredBackendFactory().capability_records()


class NoCompatibleEndpointError(SelectionError, NoUsableRoutingTarget):
    """The floor: no declared entry is usable for this job.

    It IS dispatch's typed ``NoUsableRoutingTarget`` (so a caller
    catching the floor catches it) and a job-kit ``SelectionError`` (so the
    runner terminalizes the one job and keeps the rest of the run going). It
    itemises every declared id and its disposition in declaration order, and
    it is the only surface allowed to name an id that selection skipped.
    """

    def __init__(self, job_id: str, floor: NoUsableRoutingTarget) -> None:
        self.job_id = job_id
        self.floor = floor
        self.endpoints = tuple(floor.names)
        NoUsableRoutingTarget.__init__(self, floor.names, floor.dispositions, floor.caller)
        self.args = (f"job {job_id!r}: {floor}",)

    def __str__(self) -> str:
        return str(self.args[0])

    def dispositions_text(self) -> str:
        """The itemised floor lines, without the job prefix."""
        return "\n".join("  " + d.describe_line() for d in self.dispositions)


def requirements_match(capabilities: Capabilities, requirements: object) -> bool:
    """Return whether an advertisement satisfies a job requirement mapping.

    Thin compatibility alias for
    ``llm_kit.completion.requirements.match_capabilities``, which owns the
    requirement language (the named convenience keys ``params``,
    ``execution_controls``/``controls``, ``dropped_params``,
    ``structured_output`` and ``system_prompt``, plus dotted-path lookups over
    ``Capabilities.to_json()`` for any other key). job-kit keeps this name so
    callers in this package do not have to import from completion directly.
    """
    return match_capabilities(capabilities, requirements)


CapabilitiesProvider = Callable[[], Mapping[str, Capabilities]]

#: The selection-style callable job-kit's tests and old callers inject:
#: ``(endpoint, **kwargs) -> BackendSelection``. This is the legacy-callable
#: adapter surface: a callable with no ``create`` method is used as given,
#: without an :class:`~llm_kit.dispatch.endpoint_resolver.EndpointResolver`.
LegacySelectionCallable = Callable[..., BackendSelection]

#: What jobs accepts as ``backend_factory``: the injected
#: :class:`~llm_kit.completion.factory_protocols.BackendFactory` (the default is
#: ``ConfiguredBackendFactory``) or, for compatibility, a legacy selection callable.
JobBackendFactory = Union[BackendFactory, LegacySelectionCallable]


class _CallableProvider:
    """A ``CapabilityProvider`` over an injected ``capabilities_provider`` callable."""

    def __init__(self, provider: CapabilitiesProvider) -> None:
        self._provider = provider

    def capability_records(self) -> Mapping[str, Capabilities]:
        return self._provider()

    def capability_record(self, backend: str) -> Optional[Capabilities]:
        return self._provider().get(backend)


def selection_callable(
    backend_factory: Optional[JobBackendFactory] = None,
    capabilities_provider: Optional[CapabilitiesProvider] = None,
) -> LegacySelectionCallable:
    """Return the ``endpoint -> BackendSelection`` callable selection runs on.

    ``None`` means the default ``ConfiguredBackendFactory``. A
    ``BackendFactory`` is wrapped in a fresh
    :class:`~llm_kit.dispatch.endpoint_resolver.EndpointResolver`, which
    constructs the chosen backend through ``factory.create(BackendSpec)``; the
    factory is also the capability provider unless it is not one, in which case
    ``capabilities_provider`` stands in. A legacy selection callable is
    returned as given. Call once per selection: the resolver memoizes the
    candidates it constructs.
    """
    factory = ConfiguredBackendFactory() if backend_factory is None else backend_factory
    if isinstance(factory, BackendFactory):
        if isinstance(factory, CapabilityProvider) or capabilities_provider is None:
            return EndpointResolver(factory)
        return EndpointResolver(factory, _CallableProvider(capabilities_provider))
    if callable(factory):
        return factory
    raise TypeError(
        "backend_factory must be a BackendFactory (with create(spec)) or a "
        f"selection callable, not {type(factory).__name__}"
    )


def advertised_capabilities(
    backend_factory: Optional[JobBackendFactory] = None,
    capabilities_provider: Optional[CapabilitiesProvider] = None,
    default: Optional[CapabilitiesProvider] = None,
) -> dict[str, Capabilities]:
    """The capability table selection matches against.

    An injected ``capabilities_provider`` wins; otherwise the injected factory
    when it is a ``CapabilityProvider``; otherwise ``default`` (the default
    provider, :func:`adapter_capabilities`, when omitted).
    """
    if capabilities_provider is not None:
        return dict(capabilities_provider())
    if isinstance(backend_factory, CapabilityProvider):
        return dict(backend_factory.capability_records())
    return dict((default or adapter_capabilities)())


#: The requirement a job with none of its own still carries: an advertisement
#: record for the resolved backend must EXIST. llm-scripting-kit's matcher
#: treats an empty mapping as match-all and ``describe`` skips the lookup for
#: it, but execution reads the same record (run.py ``_capabilities_for``) and
#: job-kit has never dispatched to a backend nothing advertises. An empty
#: ``params`` list matches every record and no absent one.
_ADVERTISED = {"params": []}

PaceReading = Mapping[str, object]


def _selection_requirements(
    requirements: Mapping[str, object],
) -> Mapping[str, object]:
    """Require an advertisement record when no capability is otherwise needed."""
    return dict(requirements) or _ADVERTISED


def _memoized(
    factory: LegacySelectionCallable,
) -> tuple[LegacySelectionCallable, dict[str, BackendSelection]]:
    """Wrap ``factory`` so the entry describe() resolved is the one dispatched."""
    resolved: dict[str, BackendSelection] = {}

    def wrapper(endpoint: str, **kwargs: object) -> BackendSelection:
        selection = factory(endpoint, **kwargs)
        resolved[endpoint] = selection
        return selection

    return wrapper, resolved


def _pace_readings(ranking: Any) -> tuple[PaceReading, ...]:
    """Return the stable attempt-ledger projection of one ranking."""
    return tuple(
        {"id": entry.id, "pace": entry.pace, "usable": entry.usable}
        for entry in ranking.rendered_entries
    )


def _select_with_model_sidecars(
    job: Job,
    *,
    advertised: Mapping[str, Capabilities],
    factory: LegacySelectionCallable,
    resolved: dict[str, BackendSelection],
    halted_endpoints: Collection[str],
    project_root: Optional[str | Path],
    reachability_cache: Optional[dict],
) -> tuple[BackendSelection, tuple[PaceReading, ...]]:
    """Rank once, then ask the kit to match each entry's requirements.

    The first describe call has no requirements: it establishes pace order,
    quota and reachability once for the whole declaration. Each rendered entry
    is then described alone with its effective requirements. The same
    reachability mapping is passed to every call, so those checks are never
    repeated. Requirement matching remains inside llm-scripting-kit, including
    its transport-entry capability specialization.
    """
    cache = reachability_cache if reachability_cache is not None else {}
    excluded = frozenset(halted_endpoints)
    try:
        ranking = _describe(
            list(job.models),
            project_root=project_root,
            caller=_CALLER_PROCESS,
            requirements=None,
            capabilities=advertised,
            backend_factory=factory,
            exclude=excluded,
            reachability_cache=cache,
        )
    except NoUsableRoutingTarget as floor:
        raise NoCompatibleEndpointError(job.id, floor) from None

    readings = _pace_readings(ranking)
    dispositions = {
        disposition.declared_index: disposition
        for disposition in ranking.dispositions
    }
    for entry in ranking.rendered_entries:
        try:
            checked = _describe(
                [entry.id],
                project_root=project_root,
                caller=_CALLER_PROCESS,
                requirements=_selection_requirements(
                    job.effective_requirements(entry.id)
                ),
                capabilities=advertised,
                backend_factory=factory,
                exclude=excluded,
                reachability_cache=cache,
            )
        except NoUsableRoutingTarget as floor:
            dispositions[entry.declared_index] = dataclasses.replace(
                floor.dispositions[0], declared_index=entry.declared_index
            )
            continue
        chosen = checked.default
        if chosen is None:  # pragma: no cover - describe raises the floor
            raise SelectionError(
                f"job {job.id!r}: describe returned no default entry"
            )
        return resolved[chosen.id], readings

    floor = NoUsableRoutingTarget(
        job.models,
        tuple(dispositions[index] for index in range(len(job.models))),
        _CALLER_PROCESS,
    )
    raise NoCompatibleEndpointError(job.id, floor)


def select_endpoint_with_readings(
    job: Job,
    *,
    halted_endpoints: Collection[str] = (),
    capabilities: Optional[Mapping[str, Capabilities]] = None,
    capabilities_provider: Optional[CapabilitiesProvider] = None,
    backend_factory: Optional[JobBackendFactory] = None,
    project_root: Optional[str | Path] = None,
    reachability_cache: Optional[dict] = None,
) -> tuple[BackendSelection, tuple[PaceReading, ...]]:
    """Select the first usable entry of the job's pace-ordered declaration.

    A job without per-model sidecars uses llm-scripting-kit's single
    ``describe(caller="process")`` call over ``job.models``: every declared id
    is classified (unresolved, requirements mismatch, excluded, out of quota,
    unreachable, usable), the rendered entries are ordered by pace, and the
    first usable one is taken. A job with either sidecar ranks once without
    requirements, then describes each rendered id with its effective
    requirements in that pace order. Skipping is silent.

    ``halted_endpoints`` is passed as ``exclude``; effective requirements
    (with a run's deny floor already applied by the caller) are matched against
    ``capabilities`` keyed by the RESOLVED backend name, the same record
    execution reads. ``reachability_cache`` is read first and receives every
    probe, so a run-scoped mapping probes each entry once per run.

    Returns the selection and the pace readings of the rendered entries it was
    chosen from (``{"id", "pace", "usable"}`` each, in pace order), which the
    runner logs on the attempt. Raises :class:`NoCompatibleEndpointError`,
    the typed floor, when no declared entry is usable.
    """
    advertised = (
        dict(capabilities)
        if capabilities is not None
        else advertised_capabilities(backend_factory, capabilities_provider)
    )
    factory, resolved = _memoized(selection_callable(backend_factory, capabilities_provider))
    if job.uses_model_sidecars:
        return _select_with_model_sidecars(
            job,
            advertised=advertised,
            factory=factory,
            resolved=resolved,
            halted_endpoints=halted_endpoints,
            project_root=project_root,
            reachability_cache=reachability_cache,
        )
    requirements = _selection_requirements(job.requirements)
    try:
        ranking = _describe(
            list(job.models),
            project_root=project_root,
            caller=_CALLER_PROCESS,
            requirements=requirements,
            capabilities=advertised,
            backend_factory=factory,
            exclude=frozenset(halted_endpoints),
            reachability_cache=reachability_cache,
        )
    except NoUsableRoutingTarget as floor:
        raise NoCompatibleEndpointError(job.id, floor) from None
    chosen = ranking.default
    if chosen is None:  # pragma: no cover - describe() raises the floor instead
        raise SelectionError(f"job {job.id!r}: describe returned no default entry")
    return resolved[chosen.id], _pace_readings(ranking)


def select_endpoint(
    job: Job,
    *,
    halted_endpoints: Collection[str] = (),
    capabilities: Optional[Mapping[str, Capabilities]] = None,
    capabilities_provider: Optional[CapabilitiesProvider] = None,
    backend_factory: Optional[JobBackendFactory] = None,
    project_root: Optional[str | Path] = None,
    reachability_cache: Optional[dict] = None,
) -> BackendSelection:
    """Select the first usable entry of the job's pace-ordered declaration.

    See :func:`select_endpoint_with_readings`, which this wraps.
    """
    selection, _ = select_endpoint_with_readings(
        job,
        halted_endpoints=halted_endpoints,
        capabilities=capabilities,
        capabilities_provider=capabilities_provider,
        backend_factory=backend_factory,
        project_root=project_root,
        reachability_cache=reachability_cache,
    )
    return selection


def choose_endpoint(
    job: Job,
    *,
    halted_endpoints: Collection[str] = (),
    capabilities: Optional[Mapping[str, Capabilities]] = None,
    capabilities_provider: Optional[CapabilitiesProvider] = None,
    backend_factory: Optional[JobBackendFactory] = None,
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
    "EffortUndeliverableError",
    "NoCompatibleEndpointError",
    "NoUsableRoutingTarget",
    "requirements_match",
    "select_endpoint",
    "select_endpoint_with_readings",
    "choose_endpoint",
]
