"""Deterministic endpoint selection: the first usable entry of a pace-ordered declaration."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Collection, Mapping, Sequence
from pathlib import Path
from typing import Any, Optional

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


# The version the FRONTIER symbol shipped in, not the oldest symbol's: the
# message names a version the user can act on, so it has to be one that
# actually carries everything probed below. The requirements module's arm_call
# and HaltError.retry_after_s (backpressure retry) are the frontier, 0.61.0.
_MIN_LLM_SCRIPTING_KIT_VERSION = "0.61.0"


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
import inspect as _inspect


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

# arm_call arms a request's controls (text-only mode, a disallowed_tools deny);
# first shipped in llm-scripting-kit 0.61.0.
_REQUIRED_REQUIREMENTS_SYMBOLS = ("arm_call",)

_require_module("llm_scripting_kit.completion", _REQUIRED_COMPLETION_SYMBOLS)
_require_module(
    "llm_scripting_kit.completion.requirements", _REQUIRED_REQUIREMENTS_SYMBOLS
)
# HaltError.retry_after_s (0.61.0) is what the backpressure wait honours; an
# older HaltError has no such argument and every wait would silently fall back
# to backoff, so a too-old lib is refused here instead.
if "retry_after_s" not in _inspect.signature(
    _require_module("llm_scripting_kit.completion", ("HaltError",)).HaltError
).parameters:
    raise SharedLibTooOldError("HaltError.retry_after_s", "llm_scripting_kit.completion")
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


def _selection_requirements(
    requirements: Mapping[str, object],
) -> Mapping[str, object]:
    """Require an advertisement record when no capability is otherwise needed."""
    return dict(requirements) or _ADVERTISED


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
    factory: BackendFactory,
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
    backend_factory: Optional[BackendFactory] = None,
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
    advertised = dict(
        capabilities
        if capabilities is not None
        else (capabilities_provider or adapter_capabilities)()
    )
    factory, resolved = _memoized(backend_factory or create_backend)
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
    "EffortUndeliverableError",
    "SharedLibTooOldError",
    "NoCompatibleEndpointError",
    "NoUsableRoutingTarget",
    "requirements_match",
    "select_endpoint",
    "select_endpoint_with_readings",
    "choose_endpoint",
]
