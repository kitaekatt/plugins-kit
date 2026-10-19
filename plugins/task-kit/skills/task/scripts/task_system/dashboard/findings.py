"""Which validation findings gate dashboard work.

``task validate`` warns when a ``dev/tasks`` folder has uncommitted changes.
For the harness that is advice to commit. For the dashboard it is the normal
state of every task it works on: a run never commits (its worker's edits are
uncommitted by design) and the dashboard's own block and Re-check writes touch
``task.yaml`` and ``log.md``. Gating on it would reject every correct run and
refuse the next Queue, so the dashboard disregards exactly that warning when it
gates (queueability, acceptance after a run, Re-check) and shows it as
information instead. Every other error and warning still gates.

The warning is identified by ``validate.UNCOMMITTED_DEV_TASKS``, the leading
clause validate itself exports for this purpose, as a prefix of the warning
text. ``tests/task-kit/test_dashboard_uncommitted_task.py`` produces the real
warning from a dirty git-tracked task and fails if validate stops emitting it
in that form, so a changed message cannot make this filter silently match nothing.
"""

from __future__ import annotations

from ..validate import UNCOMMITTED_DEV_TASKS


def is_informational(finding: str) -> bool:
    return finding.startswith(UNCOMMITTED_DEV_TASKS)


def gating(findings) -> list[str]:
    """The findings that gate work: everything except the uncommitted-folder warning."""
    return [f for f in findings if not is_informational(f)]


def informational(findings) -> list[str]:
    """The findings shown as information only (the uncommitted-folder warning)."""
    return [f for f in findings if is_informational(f)]


def gating_of(result) -> list[str]:
    """Errors then gating warnings of a ``ValidationResult``."""
    return list(result.errors) + gating(result.warnings)
