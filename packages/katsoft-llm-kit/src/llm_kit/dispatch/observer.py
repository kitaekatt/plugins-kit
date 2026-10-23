"""The observer seam of ``declaration.run``.

A caller that wants an execution-event stream passes any object with the
``emit`` method below as ``run(..., observer=...)``. The method is keyword-
compatible with ``bootstrap_lib.execution_event.Emitter.emit``, so an
``Emitter`` bound to the caller's run and unit satisfies it as is. This module
is a leaf: it imports the standard library only.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Protocol, runtime_checkable


@runtime_checkable
class ExecutionObserver(Protocol):
    """Receives the execution events ``run`` reports, one call per event."""

    def emit(
        self,
        event: str,
        *,
        unit_id: Optional[str] = None,
        attempt_id: Optional[str] = None,
        adapter: Optional[str] = None,
        model: Optional[str] = None,
        payload: Optional[Mapping[str, Any]] = None,
        at: Optional[str] = None,
    ) -> Any:
        ...


__all__ = ["ExecutionObserver"]
