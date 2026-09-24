"""Deterministic endpoint selection: the first usable entry of a pace-ordered declaration."""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from pathlib import Path
from typing import Optional

from .model import Job


class SelectionError(Exception):
    """Base class for endpoint-selection errors."""


# The version the FRONTIER symbol shipped in, not the oldest symbol's: the
# message names a version the user can act on, so it has to be one that
# actually carries everything probed below. describe() (llm-scripting-kit's
# declaration API) is the frontier.
_MIN_LLM_SCRIPTING_KIT_VERSION = "0.46.0"


class SharedLibTooOldError(SelectionError, ImportError):
    """The installed llm-scripting-kit shared lib is missing a required symbol.

    Bootstrap's shared-lib linker pins no version, so a job-kit venv can be
    linked against an llm-scripting-kit older than the one job-kit was
    written against. Raised at import time of job_kit.select so the failure
    names the owning plugin and the fix instead of surfacing as a bare
    ImportError/AttributeError deep in a job run. An ABSENT llm-scripting-kit
    is a different state with a different remedy (install, not update); the
    package's ``__init__`` diagnoses that one.
    """

    def __init__(self, symbol: str, module: str) -> None:
        self.symbol = symbol
        self.module = module
        super().__init__(
            f"job-kit requires llm-scripting-kit >= {_MIN_LLM_SCRIPTING_KIT_VERSION} "
            f"({module!r} has no {symbol!r}). Update the llm-scripting-kit plugin "
            "(bootstrap will provision the newer shared lib, or run "
            "`claude plugin update llm-scripting-kit@plugins-kit`)."
        )


# A missing PACKAGE (unlinked or uninstalled shared lib) propagates as the
# plain ModuleNotFoundError so the message names the package; only a present
# package lacking a module or symbol is diagnosed as "too old".
import importlib as _importlib


def _require_module(name: str, symbols: tuple[str, ...]) -> object:
    """Import ``name`` and probe it for ``symbols``, diagnosing a too-old lib."""
    try:
        module = _importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name != name:
            raise
        raise SharedLibTooOldError(symbols[0], name) from exc
    for symbol in symbols:
        if not hasattr(module, symbol):
            raise SharedLibTooOldError(symbol, name)
    return module


# subjects_for_disallowed_tools is the newest completion symbol: run.py uses it
# to turn a deny floor into the guarantee subjects it asks for. HALT_QUOTA is
# the halt kind a spent subscription pool is classified as.
_REQUIRED_COMPLETION_SYMBOLS = (
    "BackendSelection",
    "Capabilities",
    "adapter_capabilities",
    "create_backend",
    "match_capabilities",
    "subjects_for_disallowed_tools",
    "HALT_QUOTA",
)
# describe is the FRONTIER symbol: selection is llm-scripting-kit's declaration
# API, called with requirements, capabilities, backend_factory, exclude and a
# run-scoped reachability_cache, all of which shipped with describe itself.
_REQUIRED_DECLARATION_SYMBOLS = ("describe", "NoUsableRoutingTarget", "CALLER_PROCESS")
# quota_pool_key is the FRONTIER symbol: it shipped with record_observed_halt's
# ``entries`` argument, which run.py passes so a halt spends the whole pool.
_REQUIRED_USAGE_SYMBOLS = ("record_observed_halt", "quota_pool_key")

_require_module("llm_scripting_kit.completion", _REQUIRED_COMPLETION_SYMBOLS)
_declaration = _require_module(
    "llm_scripting_kit.declaration", _REQUIRED_DECLARATION_SYMBOLS
)
_require_module("llm_scripting_kit.usage_budget", _REQUIRED_USAGE_SYMBOLS)

from llm_scripting_kit.completion import (
    BackendSelection,
    Capabilities,
    adapter_capabilities,
    create_backend,
    match_capabilities,
)

NoUsableRoutingTarget = _declaration.NoUsableRoutingTarget
_describe = _declaration.describe
_CALLER_PROCESS = _declaration.CALLER_PROCESS


class NoCompatibleEndpointError(SelectionError, NoUsableRoutingTarget):
    """The floor: no declared entry is usable for this job.

    It IS llm-scripting-kit's typed ``NoUsableRoutingTarget`` (so a caller
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


#: The requirement a job with none of its own still carries: an advertisement
#: record for the resolved backend must EXIST. llm-scripting-kit's matcher
#: treats an empty mapping as match-all and ``describe`` skips the lookup for
#: it, but execution reads the same record (run.py ``_capabilities_for``) and
#: job-kit has never dispatched to a backend nothing advertises. An empty
#: ``params`` list matches every record and no absent one.
_ADVERTISED = {"params": []}

PaceReading = Mapping[str, object]


def _memoized(
    factory: BackendFactory,
) -> tuple[BackendFactory, dict[str, BackendSelection]]:
    """Wrap ``factory`` so the entry describe() resolved is the one dispatched."""
    resolved: dict[str, BackendSelection] = {}

    def wrapper(endpoint: str, **kwargs: object) -> BackendSelection:
        selection = factory(endpoint, **kwargs)
        resolved[endpoint] = selection
        return selection

    return wrapper, resolved


def select_endpoint_with_readings(
    job: Job,
    *,
    halted_endpoints: Collection[str] = (),
    capabilities: Optional[Mapping[str, Capabilities]] = None,
    capabilities_provider: Optional[CapabilitiesProvider] = None,
    backend_factory: Optional[BackendFactory] = None,
    project_root: Optional[str | Path] = None,
    reachability_cache: Optional[dict] = None,
) -> tuple[BackendSelection, tuple[PaceReading, ...]]:
    """Select the first usable entry of the job's pace-ordered declaration.

    Selection is llm-scripting-kit's ``describe(caller="process")`` over
    ``job.models``: every declared id is classified (unresolved, requirements
    mismatch, excluded, out of quota, unreachable, usable), the rendered
    entries are ordered by pace, and the first usable one is taken. Skipping
    is silent. ``halted_endpoints`` is passed as ``exclude``;
    ``job.requirements`` (with a run's deny floor already applied by the
    caller) is matched against ``capabilities`` keyed by the RESOLVED backend
    name, the same record execution reads. ``reachability_cache`` is read
    first and receives every probe, so a run-scoped mapping probes each entry
    once per run.

    Returns the selection and the pace readings of the rendered entries it was
    chosen from (``{"id", "pace", "usable"}`` each, in pace order), which the
    runner logs on the attempt. Raises :class:`NoCompatibleEndpointError`,
    the typed floor, when no declared entry is usable.
    """
    advertised = dict(
        capabilities
        if capabilities is not None
        else (capabilities_provider or adapter_capabilities)()
    )
    factory, resolved = _memoized(backend_factory or create_backend)
    requirements = dict(job.requirements) or _ADVERTISED
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
    readings = tuple(
        {"id": entry.id, "pace": entry.pace, "usable": entry.usable}
        for entry in ranking.rendered_entries
    )
    return resolved[chosen.id], readings


def select_endpoint(
    job: Job,
    *,
    halted_endpoints: Collection[str] = (),
    capabilities: Optional[Mapping[str, Capabilities]] = None,
    capabilities_provider: Optional[CapabilitiesProvider] = None,
    backend_factory: Optional[BackendFactory] = None,
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
    "SharedLibTooOldError",
    "NoCompatibleEndpointError",
    "NoUsableRoutingTarget",
    "requirements_match",
    "select_endpoint",
    "select_endpoint_with_readings",
    "choose_endpoint",
]
