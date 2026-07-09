"""Task Type registry (spec section 2.5).

A Task Type is the pluggable config bundle that defines what varies between
kinds of task: scaffolding template, task.yaml schema, state vocabulary, and
closure policy. v1 ships exactly one registered type, ``hand-off`` (the
default); the ``type:`` field in task.yaml reserves the extension seam, but
the registry-extension mechanism is out of scope for v1.

The closure_policy entries are DATA ONLY in Step 1 -- human-readable
descriptors of what close / archive / delete will physically do. Closure
behavior itself is implemented in later steps.
"""

from __future__ import annotations

from dataclasses import dataclass

from .schemas import TASK_SCHEMA

DEFAULT_TYPE_NAME = "hand-off"


@dataclass(frozen=True)
class TaskType:
    name: str
    scaffolding: tuple[str, ...]
    state_vocabulary: tuple[str, ...]
    priority_pattern: str
    schema: dict
    schema_versions: tuple[str, ...]
    closure_policy: dict
    # Legal states for plan.md task_items entries (task-items design section
    # 4): the in-flight triage buckets promoted to contract. Items share the
    # type's priority_pattern -- one priority vocabulary system-wide.
    item_state_vocabulary: tuple[str, ...] = ()


HAND_OFF = TaskType(
    name="hand-off",
    scaffolding=("CLAUDE.md", "plan.md", "log.md", "task.yaml"),
    state_vocabulary=("active", "blocked", "closed", "archived"),
    priority_pattern=r"^P[1-3]$",
    item_state_vocabulary=("available", "in-flight", "blocked-user", "deferred"),
    schema=TASK_SCHEMA,
    schema_versions=("1",),
    closure_policy={
        "close": "status = closed; keep folder",
        "archive": (
            "tmp: status = archived, keep folder | "
            "non-tmp: delete folder (git is the record)"
        ),
        "delete": "archive, AND delete the folder even when tmp (unconditional removal)",
    },
)

_REGISTRY: dict[str, TaskType] = {HAND_OFF.name: HAND_OFF}


def get_type(name: str) -> TaskType | None:
    """Resolve a registered Task Type by name from the built-in registry.

    Returns None for an unregistered name -- an error condition for validate
    (spec section 9: unknown ``type``).
    """
    return _REGISTRY.get(name)
