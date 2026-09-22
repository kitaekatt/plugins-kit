"""Shared task-list projection used by the ``list`` and ``review`` verbs."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .discovery import (
    DEFAULT_USER_ROOT,
    OPEN_CLASSIFICATIONS,
    TaskRecord,
    TaskUpdate,
    discover,
    read_task_block,
    read_task_updates,
)


SUMMARY_METADATA_KEYS = frozenset(("summary", "summary_fingerprint", "summary_updated"))


@dataclass(frozen=True)
class TaskView:
    """The list/review projection for one discovered task."""

    id: str
    status: str
    priority: str | None
    last_update: str | None
    title: str | None
    summary: str | None
    summary_status: str
    summary_fingerprint: str | None
    current_fingerprint: str | None
    host: str | None
    updates: tuple[TaskUpdate, ...] = ()


@dataclass(frozen=True)
class TaskListing:
    """A serializable discovery result with diagnostics kept separate."""

    scope: str
    effective_root: Path
    views: tuple[TaskView, ...]
    warnings: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def summary_source_fingerprint(folder: Path, block: dict[str, Any]) -> str:
    """Fingerprint the task material a generated summary is based on."""
    task_material = {
        key: value for key, value in block.items() if key not in SUMMARY_METADATA_KEYS
    }
    activity_log = "\n".join(
        f"{entry.date}: {entry.detail}" for entry in reversed(read_task_updates(folder))
    )
    material = {
        "task": task_material,
        "CLAUDE.md": _read_text(folder / "CLAUDE.md"),
        "plan.md": _read_text(folder / "plan.md"),
        "activity_log": activity_log,
    }
    encoded = json.dumps(material, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _view_record(record: TaskRecord, project_root: Path) -> TaskView:
    folder = project_root / record.id
    if record.classification == "remote" or not folder.is_dir():
        return TaskView(
            id=record.id,
            status=record.classification,
            priority=record.priority,
            last_update=record.last_update,
            title=record.title,
            summary=None,
            summary_status="unavailable",
            summary_fingerprint=None,
            current_fingerprint=None,
            host=record.host,
        )
    block = read_task_block(folder)
    updates = read_task_updates(folder)
    if block is None:
        return TaskView(
            id=record.id,
            status=record.classification,
            priority=record.priority,
            last_update=record.last_update,
            title=record.title,
            summary=None,
            summary_status="unavailable",
            summary_fingerprint=None,
            current_fingerprint=None,
            host=record.host,
            updates=updates,
        )
    raw_summary = block.get("summary")
    summary = raw_summary.strip() if isinstance(raw_summary, str) and raw_summary.strip() else None
    stored_fingerprint = block.get("summary_fingerprint")
    stored_fingerprint = stored_fingerprint if isinstance(stored_fingerprint, str) else None
    current_fingerprint = summary_source_fingerprint(folder, block)
    if summary is None:
        summary_status = "missing"
    elif stored_fingerprint and stored_fingerprint != current_fingerprint:
        summary_status = "stale"
    else:
        summary_status = "present"
    return TaskView(
        id=record.id,
        status=record.classification,
        priority=record.priority,
        last_update=record.last_update,
        title=record.title,
        summary=summary,
        summary_status=summary_status,
        summary_fingerprint=stored_fingerprint,
        current_fingerprint=current_fingerprint,
        host=record.host,
        updates=updates,
    )


def collect_listing(
    scope: str,
    project_root: Path,
    *,
    target: str | None = None,
    status: str | None = None,
    priority: str | None = None,
    user_root: Path | None = None,
) -> TaskListing:
    notes: list[str] = []
    effective_root = (
        user_root if user_root is not None else DEFAULT_USER_ROOT
        if scope == "user"
        else project_root
    )
    records = discover(
        scope,
        project_root,
        target=target,
        status=status,
        priority=priority,
        user_root=user_root,
        notes=notes,
    )
    views = tuple(_view_record(record, effective_root) for record in records)
    warnings: list[str] = []
    for view in views:
        if view.summary_status == "missing":
            warnings.append(f"task {view.id} is missing task.summary")
        elif view.summary_status == "stale":
            warnings.append(f"task {view.id} task.summary is stale")
        elif view.summary_status == "unavailable":
            warnings.append(
                f"task {view.id} has no locally readable task summary ({view.status})"
            )
        elif view.summary is not None and "\n" in view.summary:
            warnings.append(f"task {view.id} task.summary is not a single line")
    return TaskListing(
        scope=scope,
        effective_root=effective_root,
        views=views,
        warnings=tuple(warnings),
        notes=tuple(notes),
    )


def section_views(views: tuple[TaskView, ...]) -> dict[str, list[TaskView]]:
    """Group views without losing non-standard classifications."""
    return {
        "open": [view for view in views if view.status in OPEN_CLASSIFICATIONS],
        "closed": [view for view in views if view.status == "closed"],
        "other": [
            view
            for view in views
            if view.status not in OPEN_CLASSIFICATIONS
            and view.status not in ("closed", "archived")
        ],
    }


def review_sort_key(view: TaskView) -> tuple[str, str]:
    """Sort newest activity first; missing dates are oldest."""
    return (view.last_update or "", view.id)


def _view_dict(view: TaskView) -> dict[str, Any]:
    return {
        "id": view.id,
        "status": view.status,
        "priority": view.priority,
        "last_update": view.last_update,
        "title": view.title,
        "summary": view.summary,
        "summary_status": view.summary_status,
        "summary_missing": view.summary_status in ("missing", "stale"),
        "summary_fingerprint": view.summary_fingerprint,
        "current_fingerprint": view.current_fingerprint,
        "host": view.host,
        "updates": [{"date": update.date, "detail": update.detail} for update in view.updates],
    }


def listing_data(listing: TaskListing) -> dict[str, Any]:
    groups = section_views(listing.views)
    diagnostics = []
    for message in listing.warnings:
        if "is stale" in message:
            code = "stale_summary"
        elif "is missing task.summary" in message:
            code = "missing_summary"
        elif "is not a single line" in message:
            code = "malformed_summary"
        else:
            code = "summary_unavailable"
        diagnostics.append({"code": code, "severity": "notice", "message": message})
    return {
        "schema_version": "1",
        "scope": listing.scope,
        "tasks": [_view_dict(view) for view in listing.views],
        "sections": {
            name: [view.id for view in views]
            for name, views in groups.items()
        },
        "diagnostics": diagnostics,
        "warnings": list(listing.warnings),
        "notes": list(listing.notes),
    }


def serialize_listing(listing: TaskListing, output_format: str) -> str:
    data = listing_data(listing)
    if output_format == "json":
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if output_format == "yaml":
        return yaml.safe_dump(data, sort_keys=False, allow_unicode=False)
    raise ValueError(f"unsupported listing format: {output_format}")