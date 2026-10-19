"""The unattended-worker deny floor, and the rule for where it may live.

An autonomous Claude worker runs with ``--permission-mode bypassPermissions``,
which approves everything, so the only restriction it has is the tools denied
in its argv. This module owns the one list of denied tool patterns and the one
check that a job document places it correctly.

The floor lives ONLY in ``Job.options.disallowed_tools``. llm-kit also accepts
a run-level ``disallowed_tools`` on the job file, but a run-level floor makes
the runner require every candidate model to GUARANTEE the denied effects, which
routes an agentic job into text-only mode (no tools at all) or leaves it
unroutable. A floor in the wrong place is therefore refused here, not repaired.

Patterns use Claude Code's ``Bash(<prefix>:*)`` rule syntax and are joined with
commas into the single string the job option carries. A delete cannot be
expressed as "only outside the checkout", so the whole delete command family is
denied.
"""

from __future__ import annotations

from typing import Mapping

# git operations that move HEAD, the index, a branch or the history.
_GIT_DENIED = (
    "commit", "push", "reset", "rebase", "stash", "clean", "restore", "checkout",
    "switch", "branch", "worktree", "add", "rm", "mv", "merge", "cherry-pick",
    "revert", "tag", "am", "apply", "pull", "fetch", "-C", "-c",
)
# Delete-capable commands (path-inexpressible: the family is denied).
_DELETE_DENIED = ("rm", "rmdir", "del", "erase", "rd", "Remove-Item", "unlink", "shred")
# Publication and nested-driver commands.
_OTHER_DENIED = (
    "python scripts/publish.py", "uv run python scripts/publish.py", "gh", "claude -p",
    "claude --print", "codex exec", "task-kit", "llm-kit jobs", "llm-kit pipeline",
)


def _bash(prefix: str) -> str:
    return f"Bash({prefix}:*)"


PATTERNS: tuple[str, ...] = (
    tuple(_bash(f"git {verb}") for verb in _GIT_DENIED)
    + tuple(_bash(name) for name in _DELETE_DENIED)
    + tuple(_bash(command) for command in _OTHER_DENIED)
)

#: The value of ``Job.options.disallowed_tools``.
CLAUDE_DISALLOWED_TOOLS = ",".join(PATTERNS)


class FloorPlacementError(ValueError):
    """The job document puts the deny floor where it must not be, or omits it."""


def check_job_document(document: Mapping) -> None:
    """Raise unless ``document`` (a job file mapping) carries the floor correctly.

    - no run-level ``disallowed_tools`` (it would change routing);
    - every job's ``options.disallowed_tools`` names every floor pattern.
    """
    if document.get("disallowed_tools") not in (None, ""):
        raise FloorPlacementError(
            "the deny floor must be set on each job's options.disallowed_tools, never on the "
            "job file (a run-level floor changes model routing and can leave an agentic job unroutable)"
        )
    jobs = document.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise FloorPlacementError("the job document has no jobs")
    for job in jobs:
        options = job.get("options") if isinstance(job, Mapping) else None
        value = options.get("disallowed_tools") if isinstance(options, Mapping) else None
        if not isinstance(value, str):
            raise FloorPlacementError(f"job {job.get('id')!r} has no options.disallowed_tools floor")
        present = {part.strip() for part in value.split(",")}
        missing = [p for p in PATTERNS if p not in present]
        if missing:
            raise FloorPlacementError(f"job {job.get('id')!r} is missing deny patterns: {missing}")
