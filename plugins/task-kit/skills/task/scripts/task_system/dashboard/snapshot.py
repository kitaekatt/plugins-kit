"""The JSON the dashboard API serves, built from an inventory snapshot.

Pure functions: the same inventory snapshot and filters always yield the same
payload, which is what lets the client skip a render when ``revision`` is
unchanged. Nothing here reads the disk except :func:`item_detail`, which reads
one task's plan.md on demand.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from . import findings as findings_mod
from . import plan_sections, runfiles
from .inventory import (
    NEEDS_CLASSIFICATIONS,
    InventorySnapshot,
    ItemView,
    ProjectEntry,
    TaskEntry,
)

SCHEMA = 1
RECENT_ACTIVITY_LIMIT = 15
CLOSED_TASKS_SHOWN = 25
PLAN_BODY_LIMIT = 4000

# The sections a task falls in (matches `task.py list`: archived is omitted).
OPEN_CLASSIFICATIONS = ("active", "blocked")

CAPABILITIES = {
    "read": True,
    "run": {
        "available": False,
        "reason": "Autonomous runs arrive in a later increment.",
    },
    "mutations": {
        "available": False,
        "reason": "Task authoring and queue actions need the dashboard server's scheduler.",
    },
    "run_options": {"defaults": None, "workers": {}},
}


def display_path(path: str) -> str:
    """A directory as the page shows it: home-relative (``~/Dev/x``) when it is
    under the user's home, as given otherwise. Rendering only; nothing parses it."""
    home = os.path.normcase(os.path.normpath(os.path.expanduser("~")))
    full = os.path.normcase(os.path.normpath(path))
    if full == home:
        return "~"
    if full.startswith(home + os.sep):
        return "~/" + os.path.normpath(path)[len(home) + 1:].replace(os.sep, "/")
    return path


def section_of(classification: str) -> str:
    if classification in OPEN_CLASSIFICATIONS:
        return "open"
    if classification == "deferred":
        return "deferred"
    if classification == "closed":
        return "closed"
    return "other"


def needs_records(task: TaskEntry) -> list[dict]:
    """The reasons this task needs a person (empty when it does not)."""
    core = task.core
    if core.classification not in NEEDS_CLASSIFICATIONS:
        return []
    base = {
        "project": task.project_id,
        "checkout": task.checkout_id,
        "project_name": task.project_name,
        "stub": task.stub,
        "task": task.id,
        "task_title": core.title or task.stub,
        "last_activity": core.last_activity,
    }
    records = []
    if core.classification == "blocked":
        reason = "The task is blocked."
        if core.blocked_by:
            reason += " Blocked by: " + ", ".join(core.blocked_by) + "."
        records.append(
            {**base, "kind": "task-blocked", "item": None, "title": core.title or task.stub,
             "detail": reason}
        )
    if core.classification == "invalid":
        first = core.errors[0] if core.errors else "The task folder does not validate."
        records.append(
            {**base, "kind": "invalid", "item": None, "title": core.title or task.stub,
             "detail": first}
        )
    for item in core.items:
        if item.ui_state == "blocked":
            records.append(
                {**base, "kind": "item-blocked", "item": item.id, "title": item.title,
                 "detail": item.note or "The item is blocked on you; no note was recorded."}
            )
    return records


def _item_json(item: ItemView) -> dict:
    return {
        "id": item.id,
        "title": item.title,
        "state": item.state,
        "ui_state": item.ui_state,
        "priority": item.priority,
        "note": item.note,
    }


def task_json(task: TaskEntry, *, held: bool = False, run_active: bool = False, helpers: list[str] | None = None) -> dict:
    """One task row. ``held`` (a Claude Code session works the task) and ``run_active`` (a dashboard run does)
    say why authoring is off for it, so the page disables the control with the reason instead of inviting a 409.
    ``helpers`` names the endpoint helpers configured for the task's repository (what a run there is offered)."""
    core = task.core
    return {
        "key": f"{task.checkout_id}|{task.id}",
        "project": task.project_id,
        "checkout": task.checkout_id,
        "repo": task.project_name,
        "working_root": display_path(task.project_root),
        "id": task.id,
        "stub": task.stub,
        "revision": core.revision,
        "held": held,
        "run_active": run_active,
        "helpers": list(helpers or []),
        "location": task.location,
        "title": core.title or task.stub,
        "classification": core.classification,
        "section": section_of(core.classification),
        "priority": core.priority,
        "summary": core.summary,
        "last_activity": core.last_activity,
        "updates": [
            {"date": u.date, "time": u.time, "detail": u.detail} for u in core.updates[:3]
        ],
        "items": [_item_json(i) for i in core.items],
        "has_items_block": core.has_items_block,
        "needs": needs_records(task),
        "handoff": handoff_commands(task),
        "findings": {
            "errors": list(core.errors),
            "warnings": findings_mod.gating(core.warnings),
            "item_errors": list(core.item_errors),
            # Shown as information, never a blocker (see findings.py).
            "info": findings_mod.informational(core.warnings),
        },
    }


def _matches(task: TaskEntry, query: str) -> bool:
    core = task.core
    haystack = " ".join(
        [task.stub, core.title or "", task.project_name, f"{task.project_name}:{task.stub}"]
        + [f"{i.title} {i.id}" for i in core.items]
    ).lower()
    return query in haystack


def _counts(tasks: list[TaskEntry]) -> dict:
    counts = {
        "open": 0, "deferred": 0, "closed": 0, "other": 0,
        "available": 0, "in-flight": 0, "blocked": 0, "item-deferred": 0, "needs": 0,
    }
    for task in tasks:
        counts[section_of(task.core.classification)] += 1
        counts["needs"] += len(needs_records(task))
        if task.core.classification in OPEN_CLASSIFICATIONS:
            for item in task.core.items:
                if item.ui_state == "available":
                    counts["available"] += 1
                elif item.ui_state == "in-flight":
                    counts["in-flight"] += 1
                elif item.ui_state == "blocked":
                    counts["blocked"] += 1
                elif item.ui_state == "deferred":
                    counts["item-deferred"] += 1
    return counts


def dashboard_payload(
    inventory: InventorySnapshot,
    *,
    project: str | None = None,
    query: str = "",
    needs_only: bool = False,
    server: dict | None = None,
    runtime: dict | None = None,
) -> dict:
    """The ``GET /api/v1/dashboard`` body for the given filters."""
    query = query.strip().lower()
    all_projects = inventory.projects
    selected: list[ProjectEntry] = [
        p for p in all_projects if not project or p.id == project
    ]

    def visible(task: TaskEntry) -> bool:
        if query and not _matches(task, query):
            return False
        if needs_only and not needs_records(task):
            return False
        return True

    tasks: list[TaskEntry] = []
    for entry in selected:
        tasks.extend(t for t in entry.tasks if visible(t))

    needs = []
    for task in tasks:
        needs.extend(needs_records(task))
    needs.sort(key=lambda n: n["last_activity"] or "", reverse=True)

    in_flight = []
    for task in tasks:
        if task.core.classification not in OPEN_CLASSIFICATIONS:
            continue
        for item in task.core.items:
            if item.ui_state == "in-flight":
                in_flight.append(
                    {
                        "project": task.project_id,
                        "checkout": task.checkout_id,
                        "project_name": task.project_name,
                        "stub": task.stub,
                        "task": task.id,
                        "task_title": task.core.title or task.stub,
                        "item": item.id,
                        "title": item.title,
                        "priority": item.priority,
                        "note": item.note,
                    }
                )

    activity = []
    for task in tasks:
        for update in task.core.updates:
            activity.append(
                {
                    "timestamp": update.timestamp,
                    "project": task.project_id,
                    "checkout": task.checkout_id,
                    "project_name": task.project_name,
                    "stub": task.stub,
                    "task": task.id,
                    "task_title": task.core.title or task.stub,
                    "detail": update.detail,
                }
            )
    activity.sort(key=lambda a: a["timestamp"], reverse=True)

    projects = []
    for entry in all_projects:
        projects.append(
            {
                "id": entry.id,
                "name": entry.name,
                "checkouts": [display_path(r) for r in entry.roots],
                "counts": _counts(list(entry.tasks)),
            }
        )

    closed_seen: dict[str, int] = {}
    held_keys = (runtime or {}).get("held") or set()
    active_keys = (runtime or {}).get("run_active") or set()
    helper_names = (runtime or {}).get("helpers_by_checkout") or {}
    task_rows = []
    hidden_closed = 0
    for task in sorted(
        tasks, key=lambda t: t.core.last_activity or "", reverse=True
    ):
        key = os.path.normcase(os.path.realpath(task.folder))
        row = task_json(task, held=key in held_keys, run_active=key in active_keys, helpers=helper_names.get(task.checkout_id))
        if row["section"] == "closed":
            seen = closed_seen.get(task.project_id, 0)
            if seen >= CLOSED_TASKS_SHOWN:
                hidden_closed += 1
                continue
            closed_seen[task.project_id] = seen + 1
        task_rows.append(row)

    runs_json: list[dict] = []
    recent_json: list[dict] = []
    revision = inventory.revision
    if runtime is not None:
        # A run belongs to the checkout it executes in (its project_id is the
        # checkout id), so match on the checkout, not on the repository.
        names = {t.checkout_id: t.project_name for p in all_projects for t in p.tasks}
        shown = {t.checkout_id for p in selected for t in p.tasks}
        latest: dict[tuple, dict] = {}
        # "Queued #n": the run's place among ALL queued entries, in the order the scheduler starts them
        # (``Scheduler.queue_view``), with the reason it is not starting now. Without a view (no scheduler) the
        # place falls back to queue order and carries no reason.
        queue_view = runtime.get("queue_view")
        if queue_view is None:
            queue_view = {r.id: (n, None) for n, r in enumerate(sorted((r for r in runtime["runs"] if r.state == "queued"),
                                                                       key=lambda r: r.seq), start=1)}
        for run in runtime["runs"]:  # newest first
            if run.project_id not in shown:
                continue
            row = run_json(run, names.get(run.project_id, ""), (runtime.get("phases") or {}).get(run.id),
                           queue_position=(queue_view.get(run.id) or (None, None))[0],
                           waiting=(queue_view.get(run.id) or (None, None))[1],
                           subagents=_run_subagents(run))
            latest.setdefault((run.project_id, run.task_ref, run.item_id), row)
            if run.state in LIVE_RUN_STATES:
                runs_json.append(row)
            elif len(recent_json) < RECENT_RUNS_LIMIT:
                recent_json.append(row)
        # Running first (oldest first); queued entries follow in the order the scheduler will start them.
        runs_json.sort(key=lambda r: (1, r["queue_position"] or 0, r["seq"]) if r["state"] == "queued" else (0, 0, r["seq"]))
        for task_row in task_rows:
            for item_row in task_row["items"]:
                item_row["run"] = latest.get((task_row["checkout"], task_row["id"], item_row["id"]))
        revision = hashlib.sha256(
            json.dumps([inventory.revision, runtime["capabilities"], runs_json, recent_json,
                        sorted(held_keys), sorted(active_keys), helper_names], sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]

    return {
        "schema": SCHEMA,
        "revision": revision,
        "generation": inventory.generation,
        "generated_at": inventory.generated_at,
        "server": server or {},
        "capabilities": runtime["capabilities"] if runtime is not None else CAPABILITIES,
        "runs": runs_json,
        "recent_runs": recent_json,
        "filters": {"project": project or "", "q": query, "needs": needs_only},
        "errors": list(inventory.errors),
        "projects": projects,
        "tasks": task_rows,
        "hidden_closed_tasks": hidden_closed,
        "needs": needs,
        "in_flight": in_flight,
        "activity": activity[:RECENT_ACTIVITY_LIMIT],
        "totals": {
            "projects": len(all_projects),
            "tasks": sum(len(p.tasks) for p in all_projects),
            "needs": sum(len(needs_records(t)) for p in all_projects for t in p.tasks),
        },
    }


_SHELL_SPECIAL = set('\\"$`!\n\r')


def shell_quote(text: str) -> str:
    """``text`` as one shell word: double-quoted (so it reads like the commands a person
    types, and works in cmd.exe too), or POSIX single-quoted when it holds a character
    that is live inside double quotes (backslash, quote, $, backtick, !) or a newline."""
    if not any(c in _SHELL_SPECIAL for c in text):
        return f'"{text}"'
    return "'" + text.replace("'", "'\\''") + "'"


# The Claude Code command that runs the task skill's work verb for a task. The
# namespaced form is the one that resolves from any project.
TASK_WORK_COMMAND = "/task-kit:task work"


def handoff_commands(task: TaskEntry) -> dict:
    """The two ways to open this task in Claude Code. Shown for copying, never run.

    ``commands`` is exactly two entries: a shell line that starts Claude Code in the
    task's working root with the work command already queued, and the bare slash
    command for a session that is already running.
    """
    slash = f"{TASK_WORK_COMMAND} {task.id}"
    return {
        "task": task.id,
        "project_root": task.project_root,
        "commands": [
            {
                "label": "Start Claude Code on this task",
                "command": f"cd {shell_quote(task.project_root)} && claude {shell_quote(slash)}",
            },
            {"label": "Or paste into a running session", "command": slash},
        ],
    }


def item_detail(task: TaskEntry, item_id: str) -> dict | None:
    """The ``GET /api/v1/items/...`` body, or None when the item is unknown."""
    item = next((i for i in task.core.items if i.id == item_id), None)
    if item is None:
        return None
    plan_path = Path(task.folder) / "plan.md"
    try:
        plan_text = plan_path.read_text(encoding="utf-8")
        plan_error = None
    except (OSError, UnicodeDecodeError) as exc:
        plan_text = ""
        plan_error = f"cannot read {plan_path}: {exc}"
    section = plan_sections.find_item_section(plan_text, item_id)
    body = section.body
    truncated = len(body) > PLAN_BODY_LIMIT
    return {
        "schema": SCHEMA,
        "project": {
            "id": task.project_id,
            "name": task.project_name,
            "checkout": task.checkout_id,
            "root": task.project_root,
            "working_root": display_path(task.project_root),
        },
        "task": {
            "id": task.id,
            "stub": task.stub,
            "title": task.core.title or task.stub,
            "summary": task.core.summary,
            "classification": task.core.classification,
            "updates": [
                {"date": u.date, "time": u.time, "detail": u.detail}
                for u in task.core.updates[:5]
            ],
        },
        "item": _item_json(item),
        "plan_section": {
            "status": section.status,
            "heading": section.heading,
            "body": body[:PLAN_BODY_LIMIT],
            "truncated": truncated,
            "error": plan_error,
            "source": os.path.join(task.id, "plan.md").replace(os.sep, "/"),
        },
        "handoff": handoff_commands(task),
    }


LIVE_RUN_STATES = ("queued", "starting", "running", "cancelling")
CANCELLABLE_RUN_STATES = ("queued", "starting", "running")
RETRYABLE_RUN_STATES = ("failed", "timeout", "cancelled")
RECENT_RUNS_LIMIT = 15


_RECORDED_SUBAGENTS: dict[tuple[str, str], dict | None] = {}


def recorded_subagents(run) -> dict | None:
    """The sub-agent decision a run started with, from its ``spec.json`` (None until it has one).

    The file is written once, before the runner is spawned, so a run's answer is read once and kept for this process.
    """
    if not run.run_dir:
        return None
    cache_key = (run.id, run.run_dir)
    if cache_key in _RECORDED_SUBAGENTS:
        return _RECORDED_SUBAGENTS[cache_key]
    status, spec = runfiles.read_json(Path(run.run_dir) / runfiles.SPEC_FILE)
    if status == runfiles.MISSING:
        return None
    if status == runfiles.UNREADABLE:
        return {"state": "unreadable", "reason": f"the run's spec.json cannot be read ({spec})"}
    found = spec.get("subagents")
    found = found if isinstance(found, dict) else None
    _RECORDED_SUBAGENTS[cache_key] = found
    return found


def _run_subagents(run) -> dict | None:
    """The part of a run's recorded decision the page shows: on or off, why, and the limits."""
    found = recorded_subagents(run)
    if found is None:
        return None
    return {"state": found.get("state"), "reason": found.get("reason"), "limits": found.get("limits")}


def run_json(run, project_name: str, phase: str | None = None, queue_position: int | None = None,
             waiting: dict | None = None, subagents: dict | None = None) -> dict:
    """One run as the API serves it (``run`` is a :class:`runtime.Run`).

    ``phase`` is the live ledger's state word for an active run (read-only; None when the run has no
    ledger yet or is not active). ``cancellable`` is whether a cancel request would be accepted now;
    a run that is already ``cancelling`` accepts one too but has nothing left to ask for. ``waiting`` is
    ``{"code", "detail"}`` for a queued run that cannot start yet (and why), else None. ``subagents`` is the
    decision the run started with (state, reason, limits), else None.
    """
    options = run.options
    return {
        "id": run.id,
        "seq": run.seq,
        "project": run.project_id,
        "checkout": run.project_id,
        "project_name": project_name,
        "task": run.task_ref,
        "item": run.item_id,
        "item_title": run.item_title,
        "state": run.state,
        "code": run.code,
        "detail": run.detail,
        "queued_at": run.queued_at,
        "started_at": run.started_at,
        "ended_at": run.ended_at,
        "worker": options.get("worker"),
        "entry": options.get("entry"),
        "effort": options.get("effort"),
        "timeout_s": options.get("timeout_s"),
        "queue_position": queue_position,
        "waiting": waiting,
        "retry_of": run.retry_of,
        "retryable": run.state in RETRYABLE_RUN_STATES,
        "cancellable": run.state in CANCELLABLE_RUN_STATES,
        "phase": phase,
        "subagents": subagents,
    }
