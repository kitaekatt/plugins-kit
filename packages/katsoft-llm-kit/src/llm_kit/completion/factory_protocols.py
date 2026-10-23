"""The injected backend-factory and capability-provider protocols.

Consumers that need a concrete completion backend (the pipeline wrappers, jobs,
the front door) receive a :class:`BackendFactory` instead of importing an
adapter, and consumers that match requirements against an adapter's filled
capability record receive it through a :class:`CapabilityProvider`. The default
implementation of both is ``llm_kit.dispatch.pipeline_backends
.ConfiguredBackendFactory``; this module holds only the contracts and the
immutable value that crosses them, so ``llm_kit.completion`` keeps no filled
adapter table and no factory.

:class:`BackendSpec` is the construction spec. It names a backend and carries
every constructor option the pipeline wrappers forward today: endpoint, project
root, client, timeout, retry, diagnostics, executable, argv prefix and runner.
``None`` means "not specified" for the options a concrete adapter already
defaults; for ``runner`` it means the option is OMITTED from the concrete
constructor call, so the adapter keeps its own default runner. Passing
``runner=None`` to an adapter is not the same thing and is never done.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol, runtime_checkable

from llm_kit.completion.capabilities import Capabilities
from llm_kit.completion.types import LLMBackend


@dataclass(frozen=True)
class BackendSpec:
    """An immutable description of one backend to construct.

    ``backend`` is the adapter family name the concrete backend carries
    (``"openrouter"``, ``"claude-cli"``, ``"codex-cli"``, ``"opencode-cli"``)
    or ``"model-endpoint"`` for an OpenAI-compatible completion against a
    registry entry; ``endpoint`` then names that entry.

    ``default_timeout_s``, ``retry_max_attempts`` and ``retry_cooldown_s`` are
    omitted from the constructor call when None, so the adapter's own default
    applies. ``runner`` is omitted when None. ``endpoint``, ``project_root``,
    ``client``, ``diagnostics_dir``, ``executable`` and ``argv_prefix`` are
    forwarded as given, None included, to the adapters that take them.
    """

    backend: str
    endpoint: Optional[str] = None
    project_root: Optional[Path] = None
    client: Any = None
    default_timeout_s: Optional[float] = None
    retry_max_attempts: Optional[int] = None
    retry_cooldown_s: Optional[float] = None
    diagnostics_dir: Optional[Path] = None
    executable: Optional[str] = None
    argv_prefix: Optional[tuple] = None
    runner: Optional[Callable[..., "tuple[str, str, int]"]] = None

    def __post_init__(self) -> None:
        if not isinstance(self.backend, str) or not self.backend.strip():
            raise ValueError("BackendSpec.backend must be a non-empty backend name")


@runtime_checkable
class BackendFactory(Protocol):
    """Builds the concrete completion backend a :class:`BackendSpec` describes."""

    def create(self, spec: BackendSpec) -> LLMBackend:
        """Return a new backend for ``spec``; an unknown backend name raises."""
        ...


@runtime_checkable
class CapabilityProvider(Protocol):
    """Supplies filled adapter capability records, keyed by backend name."""

    def capability_records(self) -> Mapping[str, Capabilities]:
        """Every advertised record, keyed by the backend's ``name``."""
        ...

    def capability_record(self, backend: str) -> Optional[Capabilities]:
        """The record for one backend name, or None when it advertises none."""
        ...


__all__ = ["BackendFactory", "BackendSpec", "CapabilityProvider"]
