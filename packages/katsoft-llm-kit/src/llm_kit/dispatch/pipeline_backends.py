"""The configured backend factory and the construction builder behind it.

The construction half of the legacy ``content_pipeline.llm.backends``: that
module's thin wrappers each built ONE concrete completion backend lazily, from
constructor options they held, under a double-checked lock. The wrappers keep
their public shape (``llm_kit.pipeline.runtime.backends``); the concrete
construction lives here, behind an injected
:class:`~llm_kit.completion.factory_protocols.BackendFactory`.

This is a NEW builder, not a rename of :func:`llm_kit.dispatch.factory
.create_backend`. That function takes an endpoint name, a model and a project
root, and builds default harness instances. This one takes a full
:class:`~llm_kit.completion.factory_protocols.BackendSpec` and passes exactly
the options that spec carries to exactly the adapter class its backend name
selects.

Two rules, both observable on a captured constructor:

* an OMITTED ``runner`` stays omitted. The concrete constructor is not called
  with ``runner=None``; the adapter keeps its own default runner. The same
  holds for an unspecified timeout and retry option.
* an option the selected backend does not take is refused, not dropped.

The concrete adapter classes are imported at the call that needs them, so
importing this module reaches no SDK and no CLI machinery, and constructing a
:class:`LazyBackend` builds nothing.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional

from llm_kit.completion.capabilities import Capabilities
from llm_kit.completion.factory_protocols import (
    BackendFactory,
    BackendSpec,
    CapabilityProvider,
)
from llm_kit.completion.types import LLMBackend

BACKEND_OPENROUTER = "openrouter"
BACKEND_MODEL_ENDPOINT = "model-endpoint"
BACKEND_CLAUDE_CLI = "claude-cli"
BACKEND_CODEX_CLI = "codex-cli"
BACKEND_OPENCODE_CLI = "opencode-cli"


@dataclass(frozen=True)
class _Plan:
    """Which spec fields a backend's constructor takes, and how."""

    #: Always passed, None included (the legacy wrappers pass them unconditionally).
    always: tuple
    #: Passed only when not None; otherwise the adapter's own default applies.
    unless_none: tuple


_TRANSPORT_PLAN = _Plan(always=("endpoint", "project_root", "client"), unless_none=())

_PLANS: Dict[str, _Plan] = {
    BACKEND_OPENROUTER: _TRANSPORT_PLAN,
    BACKEND_MODEL_ENDPOINT: _TRANSPORT_PLAN,
    BACKEND_CLAUDE_CLI: _Plan(
        always=("diagnostics_dir", "executable"),
        unless_none=("default_timeout_s", "retry_max_attempts", "retry_cooldown_s", "runner"),
    ),
    BACKEND_CODEX_CLI: _Plan(
        always=("argv_prefix",), unless_none=("default_timeout_s", "runner"),
    ),
    BACKEND_OPENCODE_CLI: _Plan(
        always=("argv_prefix",), unless_none=("default_timeout_s", "runner"),
    ),
}

_SPEC_OPTIONS = (
    "endpoint", "project_root", "client", "default_timeout_s", "retry_max_attempts",
    "retry_cooldown_s", "diagnostics_dir", "executable", "argv_prefix", "runner",
)


def _plan_for(backend: str) -> _Plan:
    plan = _PLANS.get(backend)
    if plan is None:
        raise ValueError(
            f"unknown backend {backend!r}; configured backends: {', '.join(sorted(_PLANS))}"
        )
    return plan


def construction_kwargs(spec: BackendSpec) -> Dict[str, Any]:
    """The keyword arguments the concrete constructor receives for ``spec``.

    Raises ``ValueError`` for an unknown backend name, and for a non-None
    option the selected backend does not take.
    """
    plan = _plan_for(spec.backend)
    taken = set(plan.always) | set(plan.unless_none)
    stray = [name for name in _SPEC_OPTIONS if name not in taken and getattr(spec, name) is not None]
    if stray:
        raise ValueError(
            f"backend {spec.backend!r} does not take {', '.join(stray)}"
        )
    kwargs: Dict[str, Any] = {name: getattr(spec, name) for name in plan.always}
    for name in plan.unless_none:
        value = getattr(spec, name)
        if value is not None:
            kwargs[name] = value
    return kwargs


def _default_class(backend: str) -> Callable[..., Any]:
    """The concrete adapter class for a backend name, imported at call time."""
    _plan_for(backend)
    if backend in (BACKEND_OPENROUTER, BACKEND_MODEL_ENDPOINT):
        from llm_kit.adapters.backends import OpenRouterBackend  # noqa: PLC0415

        return OpenRouterBackend
    if backend == BACKEND_CLAUDE_CLI:
        from llm_kit.adapters.backends import ClaudeCliBackend  # noqa: PLC0415

        return ClaudeCliBackend
    if backend == BACKEND_CODEX_CLI:
        from llm_kit.adapters.codex_backend import CodexCliBackend  # noqa: PLC0415

        return CodexCliBackend
    from llm_kit.adapters.opencode_backend import OpencodeCliBackend  # noqa: PLC0415

    return OpencodeCliBackend


class ConfiguredBackendFactory:
    """The default :class:`~llm_kit.completion.factory_protocols.BackendFactory`.

    Maps a spec's backend name to the concrete adapter class and constructs it
    with :func:`construction_kwargs`. It is also the default
    :class:`~llm_kit.completion.factory_protocols.CapabilityProvider`, serving
    the shipped adapter advertisement.

    ``classes`` injects the name -> constructor table (a test seam, and the way
    a caller substitutes an adapter); a name missing from an injected table is
    an error, never a fall-through to the shipped class.
    """

    def __init__(self, classes: Optional[Mapping[str, Callable[..., Any]]] = None) -> None:
        self._classes = None if classes is None else dict(classes)

    def _class_for(self, backend: str) -> Callable[..., Any]:
        if self._classes is None:
            return _default_class(backend)
        _plan_for(backend)
        try:
            return self._classes[backend]
        except KeyError:
            raise ValueError(
                f"no constructor injected for backend {backend!r}; "
                f"injected: {', '.join(sorted(self._classes)) or '<none>'}"
            ) from None

    def create(self, spec: BackendSpec) -> LLMBackend:
        kwargs = construction_kwargs(spec)
        return self._class_for(spec.backend)(**kwargs)

    def capability_records(self) -> Mapping[str, Capabilities]:
        from llm_kit.adapters.capabilities import adapter_capabilities  # noqa: PLC0415

        return adapter_capabilities()

    def capability_record(self, backend: str) -> Optional[Capabilities]:
        return self.capability_records().get(backend)


def build_backend(spec: BackendSpec, factory: Optional[BackendFactory] = None) -> LLMBackend:
    """Construct the backend ``spec`` describes: the public dispatch builder.

    Library users without their own factory call this; everything else injects
    a :class:`~llm_kit.completion.factory_protocols.BackendFactory`.
    """
    return (ConfiguredBackendFactory() if factory is None else factory).create(spec)


class LazyBackend:
    """A backend built from ``spec`` on first use, at most once, thread-safely.

    The mechanism the pipeline wrappers' lazy delegate relies on:

    1. nothing builds when the object is constructed;
    2. the fast path takes no lock once the delegate exists;
    3. the gate opens when the delegate EXISTS, not when a request succeeds;
    4. only the build is serialised, never the requests made through it.

    Under a concurrent first wave every thread would otherwise build its own
    delegate, and each carries its own client and file descriptor.
    """

    def __init__(self, spec: BackendSpec, factory: Optional[BackendFactory] = None) -> None:
        self._spec = spec
        self._factory = ConfiguredBackendFactory() if factory is None else factory
        self._delegate: Optional[LLMBackend] = None
        self._lock = threading.Lock()

    @property
    def spec(self) -> BackendSpec:
        return self._spec

    def get(self) -> LLMBackend:
        if self._delegate is not None:
            return self._delegate
        with self._lock:
            if self._delegate is None:
                self._delegate = self._factory.create(self._spec)
        return self._delegate


__all__ = [
    "BACKEND_CLAUDE_CLI",
    "BACKEND_CODEX_CLI",
    "BACKEND_MODEL_ENDPOINT",
    "BACKEND_OPENCODE_CLI",
    "BACKEND_OPENROUTER",
    "BackendFactory",
    "BackendSpec",
    "CapabilityProvider",
    "ConfiguredBackendFactory",
    "LazyBackend",
    "build_backend",
    "construction_kwargs",
]
