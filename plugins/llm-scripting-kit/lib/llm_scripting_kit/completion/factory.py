"""Construct completion backends from the shared endpoint configuration."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..model_endpoints import HARNESS_KIND, EndpointEntry
from ..models import (
    EndpointResolveError,
    ModelResolveError,
    default_endpoint_name,
    discover_model_entries,
    load_model_config,
    resolve_endpoint,
    resolve_model,
)
from .backends import ClaudeCliBackend, OpenRouterBackend
from .codex_backend import CodexCliBackend
from .opencode_backend import OpencodeCliBackend
from .types import LLMBackend


@dataclass(frozen=True)
class BackendSelection:
    """A configured backend plus the model and defaults it should receive."""

    endpoint: str
    kind: str
    backend: LLMBackend
    model: str
    effort: Optional[str] = None


def _harness_entry(
    name: str, *, config: dict, project_root: Optional[str]
) -> Optional[EndpointEntry]:
    raw = (config.get("endpoints") or {}).get(name)
    if isinstance(raw, dict):
        if "harness" not in raw:
            return None
        harness = raw.get("harness")
        model = raw.get("model")
        if not isinstance(harness, str) or not harness.strip():
            raise EndpointResolveError(
                f"endpoint '{name}' is a harness entry and has no 'harness'"
            )
        if not isinstance(model, str) or not model.strip():
            raise EndpointResolveError(
                f"endpoint '{name}' is a harness entry and has no 'model'"
            )
        effort = raw.get("effort")
        if effort is not None and (not isinstance(effort, str) or not effort.strip()):
            raise EndpointResolveError(
                f"endpoint '{name}' has an invalid 'effort' ({effort!r})"
            )
        return EndpointEntry(
            id=name, base_url=None, model=model.strip(), kind=HARNESS_KIND,
            harness=harness.strip(), effort=effort.strip() if effort else None,
        )
    return discover_model_entries(config=config, project_root=project_root).get(name)


def create_backend(
    endpoint: Optional[str] = None,
    *,
    model: Optional[str] = None,
    cheap: bool = False,
    project_root: Optional[str | Path] = None,
) -> BackendSelection:
    """Create the backend selected by a configured transport or harness entry.

    ``resolve_endpoint`` deliberately rejects harnesses because it is the HTTP
    resolver.  This factory classifies discovery entries before crossing that
    boundary, preserving that invariant while providing one host-neutral
    dispatch API.
    """
    root = str(project_root) if project_root is not None else None
    config = load_model_config(project_root=root)
    name = endpoint or default_endpoint_name(config)
    entry = _harness_entry(name, config=config, project_root=root)
    if entry is not None and entry.kind == HARNESS_KIND:
        selected_model = model or entry.model
        harness = (entry.harness or "").lower()
        if harness == "claude":
            backend: LLMBackend = ClaudeCliBackend()
        elif harness == "codex":
            backend = CodexCliBackend()
        elif harness == "opencode":
            backend = OpencodeCliBackend()
        else:
            raise EndpointResolveError(
                f"endpoint '{name}' uses unsupported harness '{entry.harness}'"
            )
        return BackendSelection(
            endpoint=name,
            kind=HARNESS_KIND,
            backend=backend,
            model=selected_model,
            effort=entry.effort,
        )

    resolved = resolve_endpoint(name, config=config, project_root=root)
    selected_model = resolve_model(
        model, cheap=cheap, config=config, endpoint=name, project_root=root
    )
    return BackendSelection(
        endpoint=name,
        kind="transport",
        backend=OpenRouterBackend(endpoint=name, project_root=Path(root) if root else None),
        model=selected_model,
        effort=(resolved.get("request_defaults") or {}).get("reasoning_effort"),
    )


def create_transport_backend(
    endpoint: Optional[str] = None,
    *,
    model: Optional[str] = None,
    cheap: bool = False,
    project_root: Optional[str | Path] = None,
) -> BackendSelection:
    """``create_backend`` for a caller that dispatches TRANSPORT entries only.

    A harness entry is refused with ``EndpointResolveError``, which
    ``declaration.describe`` reads as "unroutable here" and skips silently. A
    model alias that does not resolve is reported the same way, so a bad
    per-entry override lands in the floor's itemised list instead of escaping
    as a crash. ``model`` and ``cheap`` are the per-entry override: they pick
    within the entry's own model map and selectors.
    """
    root = str(project_root) if project_root is not None else None
    config = load_model_config(project_root=root)
    name = endpoint or default_endpoint_name(config)
    entry = _harness_entry(name, config=config, project_root=root)
    if entry is not None and entry.kind == HARNESS_KIND:
        raise EndpointResolveError(
            f"entry '{name}' is a {entry.harness} harness entry; this caller "
            "dispatches transport entries only"
        )
    try:
        return create_backend(name, model=model, cheap=cheap, project_root=project_root)
    except ModelResolveError as exc:
        raise EndpointResolveError(str(exc)) from exc


__all__ = ["BackendSelection", "create_backend", "create_transport_backend"]
