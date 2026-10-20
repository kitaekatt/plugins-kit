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

Tool-level denials (``TOOLS_DENIED_ALWAYS``, ``TOOLS_DENIED_WHEN_OFF``) ride in
the same comma-joined argument after the Bash patterns. They stop the tools the
Bash patterns cannot see: dynamic workflows, worktrees, remote and scheduled
agents, and (while native sub-agents are off) the sub-agent tool itself.

Every guard here is a TRIPWIRE against a cooperating or careless worker, the same
class as the deny floor under ``bypassPermissions``: none of it is confinement.
"""

from __future__ import annotations

import os
from typing import Callable, Mapping

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
    "claude --print", "codex exec", "task-kit", "llm-kit jobs", "llm-kit pipeline", "llm-kit completion",
)


def _bash(prefix: str) -> str:
    return f"Bash({prefix}:*)"


PATTERNS: tuple[str, ...] = (
    tuple(_bash(f"git {verb}") for verb in _GIT_DENIED)
    + tuple(_bash(name) for name in _DELETE_DENIED)
    + tuple(_bash(command) for command in _OTHER_DENIED)
)

#: Tools denied in every run: workflow fan-out outside the sub-agent limits, worktrees (the shared write
#: policy forbids them, but this tool is invisible to the Bash patterns), and remote or scheduled agents
#: that outlive the run's process tree. Claude Code's deny rule syntax takes a bare tool name.
TOOLS_DENIED_ALWAYS: tuple[str, ...] = ("Workflow", "EnterWorktree", "RemoteTrigger", "CronCreate", "ScheduleWakeup")
#: Denied while native sub-agents are off (a current characterization record turns them on). Claude Code's init event lists
#: the sub-agent tool as ``Task`` on some builds; the deny rule name ``Agent`` covers both (``Task(...)`` is an alias).
TOOLS_DENIED_WHEN_OFF: tuple[str, ...] = ("Agent",)
#: The names the sub-agent tool carries in a CLI-authored tool list.
SUBAGENT_TOOL_NAMES: tuple[str, ...] = ("Agent", "Task")

#: Environment every run's command gets. ``TASK_KIT_AGENT_DEPTH`` marks a dashboard run's process tree (the
#: launcher refuses when it sees it); ``CLAUDE_CODE_DISABLE_WORKFLOWS`` is Claude Code's documented switch
#: for dynamic workflows, and turning workflows off also makes ultracode unavailable.
RUN_ENV: dict[str, str] = {"TASK_KIT_AGENT_DEPTH": "0", "CLAUDE_CODE_DISABLE_WORKFLOWS": "1"}
#: Claude Code's documented per-session sub-agent controls (positive integers only; a zero is ignored). Only a run
#: with native sub-agents ON sets them; a dashboard-built probe environment never inherits a user's own values.
SUBAGENT_DEPTH_ENV = "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH"
SUBAGENT_CONCURRENT_ENV = "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS"
#: Nesting depth of an ON run: children launch, grandchildren cannot.
SUBAGENT_DEPTH = 1
#: Variables a dashboard-built subprocess environment never inherits from the server's own environment.
RUN_ENV_STRIPPED: tuple[str, ...] = ("TASK_KIT_AGENT_DEPTH", "TASK_KIT_RUN_TOKEN", SUBAGENT_DEPTH_ENV, SUBAGENT_CONCURRENT_ENV)


def deny_tools(*, sub_agents_on: bool = False) -> tuple[str, ...]:
    """The tool-level denials of one run: every run's list, plus the sub-agent tool while sub-agents are off."""
    return TOOLS_DENIED_ALWAYS if sub_agents_on else TOOLS_DENIED_ALWAYS + TOOLS_DENIED_WHEN_OFF


def subagent_env(max_concurrent: int) -> dict[str, str]:
    """The two documented controls an ON run adds to :data:`RUN_ENV`: nesting depth and sub-agents at once."""
    return {SUBAGENT_DEPTH_ENV: str(SUBAGENT_DEPTH), SUBAGENT_CONCURRENT_ENV: str(max_concurrent)}


def disallowed_tools_value(tools: tuple[str, ...] | list[str] = ()) -> str:
    """The ONE comma-joined ``--disallowedTools`` value: the Bash floor, then the tool-level denials."""
    return ",".join(PATTERNS + tuple(tools))


def run_environment(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment a dashboard-built subprocess gets: ``base`` (default: this process's) without the
    run markers, plus the run environment. Built here, never inherited blindly."""
    env = dict(os.environ if base is None else base)
    for name in RUN_ENV_STRIPPED:
        env.pop(name, None)
    env.update(RUN_ENV)
    return env


#: The value of ``Job.options.disallowed_tools`` with no tool-level denial (the Bash floor alone).
CLAUDE_DISALLOWED_TOOLS = disallowed_tools_value()


class FloorPlacementError(ValueError):
    """The job document puts the deny floor where it must not be, or omits it."""


def check_job_document(document: Mapping, *, deny_tools: tuple[str, ...] | list[str] = ()) -> None:
    """Raise unless ``document`` (a job file mapping) carries the floor correctly.

    - no run-level ``disallowed_tools`` (it would change routing);
    - every job's ``options.disallowed_tools`` names every floor pattern and every tool in ``deny_tools``.
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
        missing = [p for p in (*PATTERNS, *deny_tools) if p not in present]
        if missing:
            raise FloorPlacementError(f"job {job.get('id')!r} is missing deny patterns: {missing}")


def check_worker_models(document: Mapping, entry_kind: Callable[[str], str | None]) -> None:
    """Raise unless every model a job names is a HARNESS entry (a second gate behind translation).

    ``entry_kind(name)`` answers ``"harness"``, ``"transport"`` or None (unknown). A transport entry is a
    text-only endpoint: it cannot be a top-level worker, so a job naming one is refused before any file exists.
    """
    jobs = document.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise FloorPlacementError("the job document has no jobs")
    for job in jobs:
        models = job.get("models") if isinstance(job, Mapping) else None
        if not isinstance(models, list) or not models:
            raise FloorPlacementError(f"job {job.get('id')!r} names no model entry")
        for name in models:
            kind = entry_kind(name)
            if kind != "harness":
                raise FloorPlacementError(
                    f"job {job.get('id')!r} names model entry {name!r}, which is "
                    f"{'not in the model registry' if kind is None else f'a {kind} entry'}; only harness entries can be workers"
                )
