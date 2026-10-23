"""Resolve an endpoint name to a :class:`BackendSelection` through an injected factory.

The selection-style seam (``endpoint -> BackendSelection``) that
:func:`llm_kit.dispatch.declaration.describe` and the jobs runner consume, built
on the spec-style seam every product receives:
:class:`~llm_kit.completion.factory_protocols.BackendFactory`.

:func:`llm_kit.dispatch.factory.create_backend` resolves an endpoint and builds
default harness and transport instances itself. :class:`EndpointResolver`
performs the same registry resolution and the same model, effort and capability
choices, but constructs the chosen backend by calling
``factory.create(BackendSpec(...))`` once per resolved endpoint, so a caller
that injects a factory controls construction. Ranking, requirements, model and
effort stay selection policy: they are never carried on the spec.

Dependencies: ``llm_kit.completion``, ``llm_kit.registry`` and
``llm_kit.adapters`` (endpoint-profile specialization only), plus the sibling
dispatch modules ``factory`` (the selection value and harness classification)
and ``pipeline_backends`` (backend names).
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional, Tuple

from llm_kit.adapters.endpoint_profile import endpoint_capabilities, profile_from_resolved
from llm_kit.completion.factory_protocols import (
    BackendFactory,
    BackendSpec,
    CapabilityProvider,
)
from llm_kit.dispatch.factory import BackendSelection, _harness_entry
from llm_kit.dispatch.pipeline_backends import (
    BACKEND_CLAUDE_CLI,
    BACKEND_CODEX_CLI,
    BACKEND_OPENCODE_CLI,
    BACKEND_OPENROUTER,
)
from llm_kit.registry.model_endpoints import HARNESS_KIND
from llm_kit.registry.models import (
    EndpointResolveError,
    default_endpoint_name,
    load_model_config,
    resolve_endpoint,
    resolve_model,
)

#: Harness name (lower-cased) -> the BackendSpec backend name that builds it.
HARNESS_BACKENDS: Dict[str, str] = {
    "claude": BACKEND_CLAUDE_CLI,
    "codex": BACKEND_CODEX_CLI,
    "opencode": BACKEND_OPENCODE_CLI,
}

_Key = Tuple[Optional[str], Optional[str], bool, Optional[str]]


class EndpointResolver:
    """``endpoint -> BackendSelection`` over an injected ``BackendFactory``.

    ``factory`` builds the backend; ``capabilities`` supplies the transport
    family record that is specialized to each transport endpoint. When
    ``capabilities`` is omitted, ``factory`` must itself be a
    :class:`~llm_kit.completion.factory_protocols.CapabilityProvider` (the
    default ``ConfiguredBackendFactory`` is both); otherwise construction
    raises ``TypeError``.

    One instance memoizes by ``(endpoint, model, cheap, project_root)``: an
    endpoint resolved again with the same arguments returns the selection
    already built, so a selection that describes the same declaration more
    than once still constructs each candidate once. Create one resolver per
    selection; a failed resolution is not cached.
    """

    def __init__(
        self,
        factory: BackendFactory,
        capabilities: Optional[CapabilityProvider] = None,
    ) -> None:
        provider = capabilities if capabilities is not None else factory
        if not isinstance(provider, CapabilityProvider):
            raise TypeError(
                "EndpointResolver needs a CapabilityProvider: pass `capabilities`, "
                f"or a factory that provides capability records (got {type(factory).__name__})"
            )
        self._factory = factory
        self._capabilities: CapabilityProvider = provider
        self._memo: Dict[_Key, BackendSelection] = {}

    def __call__(
        self,
        endpoint: Optional[str] = None,
        *,
        model: Optional[str] = None,
        cheap: bool = False,
        project_root: Optional[str | Path] = None,
    ) -> BackendSelection:
        root = str(project_root) if project_root is not None else None
        key: _Key = (endpoint, model, cheap, root)
        cached = self._memo.get(key)
        if cached is not None:
            return cached
        selection = self._resolve(endpoint, model=model, cheap=cheap, root=root)
        self._memo[key] = selection
        return selection

    def _resolve(
        self, endpoint: Optional[str], *, model: Optional[str], cheap: bool, root: Optional[str]
    ) -> BackendSelection:
        config = load_model_config(project_root=root)
        name = endpoint or default_endpoint_name(config)
        entry = _harness_entry(name, config=config, project_root=root)
        if entry is not None and entry.kind == HARNESS_KIND:
            backend_name = HARNESS_BACKENDS.get((entry.harness or "").lower())
            if backend_name is None:
                raise EndpointResolveError(
                    f"endpoint '{name}' uses unsupported harness '{entry.harness}'"
                )
            backend = self._factory.create(BackendSpec(backend=backend_name))
            return BackendSelection(
                endpoint=name,
                kind=HARNESS_KIND,
                backend=backend,
                model=model or entry.model,
                effort=entry.effort,
                capabilities=getattr(backend, "capabilities", None),
            )

        resolved = resolve_endpoint(name, config=config, project_root=root)
        selected_model = resolve_model(
            model, cheap=cheap, config=config, endpoint=name, project_root=root
        )
        family = self._capabilities.capability_record(BACKEND_OPENROUTER)
        if family is None:
            raise LookupError(
                f"the capability provider advertises no {BACKEND_OPENROUTER!r} record"
            )
        backend = self._factory.create(
            BackendSpec(
                backend=BACKEND_OPENROUTER,
                endpoint=name,
                project_root=Path(root) if root else None,
            )
        )
        return BackendSelection(
            endpoint=name,
            kind="transport",
            backend=backend,
            model=selected_model,
            effort=(resolved.get("request_defaults") or {}).get("reasoning_effort"),
            capabilities=endpoint_capabilities(
                family, profile_from_resolved(resolved, endpoint=name)
            ),
        )


__all__ = ["EndpointResolver", "HARNESS_BACKENDS"]
