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


_MISSING = object()


def _lookup(value: object, path: str) -> object:
    """Read a dotted path from the JSON-shaped advertisement."""
    current = value
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return _MISSING
        current = current[part]
    return current


def _matches(actual: object, expected: object) -> bool:
    """Match a requirement against one advertisement value."""
    if expected is True:
        return bool(actual)
    if expected is False:
        return not bool(actual)
    if isinstance(expected, Mapping):
        if not isinstance(actual, Mapping):
            return False
        return all(
            key in actual and _matches(actual[key], requirement)
            for key, requirement in expected.items()
        )
    if isinstance(expected, Sequence) and not isinstance(
        expected, (str, bytes, bytearray)
    ):
        expected_values = tuple(expected)
        if isinstance(actual, Sequence) and not isinstance(
            actual, (str, bytes, bytearray)
        ):
            return all(item in actual for item in expected_values)
        return actual in expected_values
    return actual == expected


def _required_names(value: object) -> tuple[str, ...]:
    """Normalize a list or mapping of named capability requirements."""
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Mapping):
        return tuple(str(name) for name, wanted in value.items() if wanted is not False)
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return tuple(str(item) for item in value)
    return ()


def requirements_match(capabilities: Capabilities, requirements: object) -> bool:
    """Return whether an advertisement satisfies a job requirement mapping.

    The named convenience keys describe the public advertisement shape:
    ``params``, ``execution_controls``/``controls``, ``dropped_params``,
    ``structured_output`` and ``system_prompt``. Other keys are read as dotted
    paths from ``Capabilities.to_json()``, so this function does not carry an
    endpoint or capability table of its own.
    """
    if requirements is None or requirements == {}:
        return True
    if isinstance(requirements, Sequence) and not isinstance(
        requirements, (str, bytes, bytearray)
    ):
        requirements = {"params": list(requirements)}
    if not isinstance(requirements, Mapping):
        raise ValueError("job requirements must be a mapping or list")

    advertised = capabilities.to_json()
    for raw_key, expected in requirements.items():
        key = str(raw_key)
        if key in {"params", "required_params", "honors"}:
            if isinstance(expected, Mapping):
                params = advertised.get("params", {})
                if not isinstance(params, Mapping):
                    return False
                for name, requirement in expected.items():
                    actual = params.get(str(name), _MISSING)
                    if requirement is False:
                        if actual is not _MISSING:
                            return False
                    elif actual is _MISSING or not _matches(actual, requirement):
                        return False
            else:
                params = advertised.get("params", {})
                if not isinstance(params, Mapping):
                    return False
                if any(name not in params for name in _required_names(expected)):
                    return False
            continue

        if key in {"execution_controls", "controls"}:
            controls = advertised.get("execution_controls", [])
            if not isinstance(controls, Sequence):
                return False
            control_ids = {
                item.get("id")
                for item in controls
                if isinstance(item, Mapping) and "id" in item
            }
            if any(name not in control_ids for name in _required_names(expected)):
                return False
            continue

        if key == "dropped_params":
            dropped = advertised.get("dropped_params", [])
            if any(name not in dropped for name in _required_names(expected)):
                return False
            continue

        if key in {"structured_output", "structured"}:
            structured = advertised.get("structured_output", _MISSING)
            if not isinstance(structured, Mapping):
                return False
            if isinstance(expected, str):
                if expected in {"native", "passthrough", "none"}:
                    if structured.get("mode") != expected:
                        return False
                elif structured.get("result") != expected:
                    return False
            elif not _matches(structured, expected):
                return False
            continue

        if key in {"system_prompt", "system_prompt_mode"}:
            system = advertised.get("system_prompt", _MISSING)
            if isinstance(expected, str):
                if not isinstance(system, Mapping) or system.get("mode") != expected:
                    return False
            elif not _matches(system, expected):
                return False
            continue

        actual = _lookup(advertised, key)
        if actual is _MISSING or not _matches(actual, expected):
            return False
    return True


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
