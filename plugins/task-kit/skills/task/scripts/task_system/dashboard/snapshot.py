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

from . import plan_sections
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
        "reason": "Queue, Cancel, Retry, Defer, New task and Add item arrive in a later increment.",
    },
}


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
        "project_name": task.project_name,
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


def task_json(task: TaskEntry) -> dict:
    core = task.core
    return {
        "key": f"{task.project_id}|{task.id}",
        "project": task.project_id,
        "id": task.id,
        "stub": task.stub,
        "revision": core.revision,
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
        "handoff": handoff_commands(task),
        "findings": {
            "errors": list(core.errors),
            "warnings": list(core.warnings),
            "item_errors": list(core.item_errors),
        },
    }


def _matches(task: TaskEntry, query: str) -> bool:
    core = task.core
    haystack = " ".join(
        [task.stub, core.title or "", task.project_name]
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
                        "project_name": task.project_name,
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
                    "project_name": task.project_name,
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
                "root": entry.root,
                "counts": _counts(list(entry.tasks)),
            }
        )

    closed_seen: dict[str, int] = {}
    task_rows = []
    hidden_closed = 0
    for task in sorted(
        tasks, key=lambda t: t.core.last_activity or "", reverse=True
    ):
        row = task_json(task)
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
        names = {p.id: p.name for p in all_projects}
        shown = {p.id for p in selected}
        latest: dict[tuple, dict] = {}
        for run in runtime["runs"]:  # newest first
            if run.project_id not in shown:
                continue
            row = run_json(run, names.get(run.project_id, ""))
            latest.setdefault((run.project_id, run.task_ref, run.item_id), row)
            if run.state in LIVE_RUN_STATES:
                runs_json.append(row)
            elif len(recent_json) < RECENT_RUNS_LIMIT:
                recent_json.append(row)
        runs_json.sort(key=lambda r: r["seq"])
        for task_row in task_rows:
            for item_row in task_row["items"]:
                item_row["run"] = latest.get((task_row["project"], task_row["id"], item_row["id"]))
        revision = hashlib.sha256(
            json.dumps([inventory.revision, runtime["capabilities"], runs_json, recent_json], sort_keys=True).encode("utf-8")
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


def handoff_commands(task: TaskEntry) -> dict:
    """The commands that open this task in Claude Code. Shown, never run."""

    def quote(path: str) -> str:
        return f'"{path}"' if any(c.isspace() for c in path) else path

    return {
        "task": task.id,
        "project_root": task.project_root,
        "commands": [
            f"cd {quote(task.project_root)}",
            "claude",
            f"/task work {task.id}",
        ],
        "caption": (
            "Run the first two in a terminal, then paste the third at the Claude prompt. "
            "Fix it there; the dashboard picks up the change by itself."
        ),
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
        "project": {"id": task.project_id, "name": task.project_name, "root": task.project_root},
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


LIVE_RUN_STATES = ("queued", "starting", "running")
RECENT_RUNS_LIMIT = 15


def run_json(run, project_name: str) -> dict:
    """One run as the API serves it (``run`` is a :class:`runtime.Run`)."""
    options = run.options
    return {
        "id": run.id,
        "seq": run.seq,
        "project": run.project_id,
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
        "retry_of": run.retry_of,
        "retryable": run.state in ("failed", "timeout"),
    }
