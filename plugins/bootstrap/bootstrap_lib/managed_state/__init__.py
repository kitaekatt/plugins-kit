"""Harness-independent declarative state management.

Projects declare :class:`Resource` objects and pass them to :func:`run` or
:func:`run_cli`.  Harness integrations may wrap these functions, but the state
semantics and operations live here.
"""

from .cli import run_cli
from .model import (
    Declaration,
    Inspection,
    Operation,
    Plan,
    PlanItem,
    Report,
    Resource,
    ResourceApplyError,
    ResourceResult,
    State,
    Status,
)
from .runner import plan, run
from .symlink import Symlink

__all__ = [
    "Inspection",
    "Declaration",
    "Operation",
    "Plan",
    "PlanItem",
    "Report",
    "Resource",
    "ResourceApplyError",
    "ResourceResult",
    "State",
    "Status",
    "Symlink",
    "plan",
    "run",
    "run_cli",
]
