"""Recording a block in the task folder.

A block is the dashboard's one way of saying "a person must look at this":
the task's stored status becomes ``blocked``, the item becomes ``blocked-user``
with a concise ``note``, and one dated log entry records why. It is written
through :class:`~.mutation.TaskMutator`, so it is atomic per file and defends
itself against a concurrent harness edit.

When the item cannot be edited safely (plan.md has no parseable block, or the
id is missing or duplicated), the task is still blocked and the log entry says
the item could not be mutated and why, so the UI can say so truthfully. When
even task.yaml cannot be written the call raises
:class:`~.mutation.MutationError` naming the file: a block that was not
recorded is never reported as recorded.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .mutation import (
    LOG,
    PLAN,
    TASK_YAML,
    MutationError,
    TaskMutator,
    TaskTexts,
    append_log,
    set_item,
    set_task_status,
)

NOTE_LIMIT = 400


@dataclass(frozen=True)
class BlockOutcome:
    item_mutated: bool
    item_error: str | None
    revision: str


def concise(text: str, limit: int = NOTE_LIMIT) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 3].rstrip() + "..."


def record_block(
    mutator: TaskMutator,
    folder: Path,
    *,
    item_id: str | None,
    note: str,
    log_detail: str,
    expected_revision: str | None = None,
) -> BlockOutcome:
    """Block the task (and the item, when it can be edited). See module docstring."""
    state = {"item_error": None}

    def edit(texts: TaskTexts) -> dict[str, str]:
        changes: dict[str, str] = {}
        detail = log_detail
        if item_id is not None:
            try:
                changes[PLAN] = set_item(texts.plan, item_id, state="blocked-user", note=concise(note))
            except MutationError as exc:
                state["item_error"] = str(exc)
                detail += f" (the item could not be marked blocked because its source was invalid: {exc})"
        changes[TASK_YAML] = set_task_status(texts.task_yaml, "blocked")
        changes[LOG] = append_log(texts.log, detail)
        return changes

    revision = mutator.mutate(Path(folder), edit, expected_revision=expected_revision)
    return BlockOutcome(item_mutated=state["item_error"] is None and item_id is not None,
                        item_error=state["item_error"], revision=revision)
