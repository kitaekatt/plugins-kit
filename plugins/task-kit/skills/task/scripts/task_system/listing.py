"""Shared task-list projection used by the list and review verbs."""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from . import answer_store, questions
from .answers_types import Catalog
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
SUMMARY_ELIGIBLE_STATUSES = frozenset(("active", "blocked", "closed", "deferred"))
# The list projection's version: "2" adds answers, coverage and summary_source, serves summary from the
# orientation answer before the legacy task.summary, and adds the summary_status values invalid and
# not-applicable.
PROJECTION_VERSION = "2"
ORIENTATION_QUESTION = "orientation"
SUMMARY_SOURCE_ANSWER = "answer"
SUMMARY_SOURCE_LEGACY = "legacy"

# Folded into summary_source_fingerprint's hashed material below. Bump this
# whenever summary_ops's generation contract changes (system prompt wording,
# section structure, or the character cap) so every fingerprint stored under
# the old contract mismatches the freshly computed one and reads as stale on
# the next `task.py review`. Owned here (rather than in summary_ops) because
# this module defines summary_source_fingerprint and summary_ops already
# imports from listing -- putting the version constant in summary_ops instead
# would require listing to import it back, creating a cycle.
SUMMARY_PROMPT_VERSION = 2


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
    summary_updated: str | None
    summary_fingerprint: str | None
    current_fingerprint: str | None
    host: str | None
    project_name: str
    project_root: Path
    updates: tuple[TaskUpdate, ...] = ()
    last_activity: str | None = None  # YYYY-MM-DD HH:MM of the newest update
    # Working-session registrations of this task (sessions.Record, this machine's running first); empty without any.
    sessions: tuple[Any, ...] = ()
    # "answer" (the orientation answer's headline), "legacy" (task.summary) or None.
    summary_source: str | None = None
    # {question id: AnswerState.as_dict()} and the task's coverage; empty/None without a readable folder or catalog.
    answers: Mapping[str, Any] | None = None
    coverage: Mapping[str, int] | None = None
    answer_notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class TaskListing:
    """A serializable discovery result with diagnostics kept separate."""

    scope: str
    effective_root: Path
    views: tuple[TaskView, ...]
    warnings: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProjectGroup:
    """Tasks belonging to one project after physical-folder deduplication."""

    name: str
    root: Path
    views: tuple[TaskView, ...]
    last_update: str | None
    last_activity: str | None


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
        "prompt_version": SUMMARY_PROMPT_VERSION,
        "task": task_material,
        "CLAUDE.md": _read_text(folder / "CLAUDE.md"),
        "plan.md": _read_text(folder / "plan.md"),
        "activity_log": activity_log,
    }
    encoded = json.dumps(
        material, sort_keys=True, ensure_ascii=False, default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _last_activity(updates: tuple[TaskUpdate, ...]) -> str | None:
    return max((update.timestamp for update in updates), default=None)


def activity_key(view: TaskView) -> str | None:
    """The view's newest activity: its timestamp, else its date alone."""
    return view.last_activity or view.last_update


@dataclass(frozen=True)
class AnswerContext:
    """What a listing needs to read answers: the catalog, the repository logs and the read time."""

    catalog: Catalog
    logs: answer_store.RepoLogs
    now: datetime.datetime  # naive local time, as log.md stamps are


def _view_record(record: TaskRecord, fallback_root: Path, context: AnswerContext | None = None) -> TaskView:
    root = record.project_root or fallback_root
    folder = root / record.id
    if record.classification == "remote" or not folder.is_dir():
        return TaskView(
            id=record.id,
            status=record.classification,
            priority=record.priority,
            last_update=record.last_update,
            title=record.title,
            summary=None,
            summary_status="unavailable",
            summary_updated=None,
            summary_fingerprint=None,
            current_fingerprint=None,
            host=record.host,
            project_name=record.project_name or root.name,
            project_root=root,
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
            summary_updated=None,
            summary_fingerprint=None,
            current_fingerprint=None,
            host=record.host,
            project_name=record.project_name or root.name,
            project_root=root,
            updates=updates,
            last_activity=_last_activity(updates),
        )
    raw_summary = block.get("summary")
    summary = (
        raw_summary.strip()
        if isinstance(raw_summary, str) and raw_summary.strip()
        else None
    )
    stored_fingerprint = block.get("summary_fingerprint")
    stored_fingerprint = (
        stored_fingerprint if isinstance(stored_fingerprint, str) else None
    )
    current_fingerprint = summary_source_fingerprint(folder, block)
    raw_summary_updated = block.get("summary_updated")
    try:
        summary_updated = date.fromisoformat(raw_summary_updated).isoformat()
    except (TypeError, ValueError):
        summary_updated = None
    if summary is None:
        summary_status = "missing"
    elif stored_fingerprint and stored_fingerprint != current_fingerprint:
        summary_status = "stale"
    else:
        summary_status = "present"
    if record.classification not in SUMMARY_ELIGIBLE_STATUSES and summary_status in (
        "missing",
        "stale",
    ):
        summary_status = "unavailable"
    summary_source = SUMMARY_SOURCE_LEGACY if summary is not None else None
    answers: dict[str, Any] | None = None
    coverage_data: dict[str, int] | None = None
    answer_notes: tuple[str, ...] = ()
    if context is not None:
        top = context.logs.top_of(folder)
        found = answer_store.task_answers(
            context.catalog, folder, block, record.classification,
            repo_top=top, log=context.logs.log_for(top), now=context.now,
        )
        answers = {question_id: state.as_dict() for question_id, state in found.states.items()}
        coverage_data = found.coverage.as_dict()
        answer_notes = found.notes
        from_answer = answer_store.orientation_headline(found.states)
        orientation = found.states.get(ORIENTATION_QUESTION)
        if from_answer is not None:
            summary, answer_state_name, summary_updated = from_answer
            summary_status = "present" if answer_state_name == answer_store.STATE_CURRENT else "stale"
            summary_source = SUMMARY_SOURCE_ANSWER
        elif summary is None and record.classification in SUMMARY_ELIGIBLE_STATUSES and orientation is not None:
            if orientation.state == answer_store.STATE_INVALID:
                summary_status = "invalid"
            elif orientation.state == answer_store.STATE_NOT_APPLICABLE:
                summary_status = "not-applicable"
    return TaskView(
        id=record.id,
        status=record.classification,
        priority=record.priority,
        last_update=record.last_update,
        title=record.title,
        summary=summary,
        summary_status=summary_status,
        summary_updated=summary_updated,
        summary_fingerprint=stored_fingerprint,
        current_fingerprint=current_fingerprint,
        host=record.host,
        project_name=record.project_name or root.name,
        project_root=root,
        updates=updates,
        last_activity=_last_activity(updates),
        summary_source=summary_source,
        answers=answers,
        coverage=coverage_data,
        answer_notes=answer_notes,
    )


def _attach_sessions(views: tuple[TaskView, ...], notes: list[str]) -> tuple[TaskView, ...]:
    """Fill each locally readable task's ``sessions`` from its task root's ``.sessions`` files (read-only).

    One read per physical task root; nothing is read, and no machine identified, for a project whose task roots
    hold no ``.sessions`` directory.
    """
    from .sessions_cli import SessionReader, location_of  # noqa: PLC0415 -- the session store is read lazily

    reader = SessionReader()
    attached = []
    for view in views:
        found = location_of(view.id) if view.status != "remote" else None
        records = reader.records_for(view.project_root, found[0], found[1]) if found else ()
        attached.append(dataclasses.replace(view, sessions=records) if records else view)
    notes.extend(f"sessions: {note}" for note in dict.fromkeys(reader.notes))
    return tuple(attached)


def collect_listing(
    scope: str,
    project_root: Path,
    *,
    target: str | None = None,
    status: str | None = None,
    priority: str | None = None,
    user_root: Path | None = None,
    devroot: Path | None = None,
    catalog: Catalog | None = None,
    now: datetime.datetime | None = None,
) -> TaskListing:
    """Discover tasks and project them, with their answers.

    ``catalog`` defaults to the shipped questions plus the fleet layer; a catalog that cannot load is a note
    (``answers: ...``) and the listing carries no answers. ``now`` is the naive local time scripts read.
    """
    notes: list[str] = []
    context: AnswerContext | None = None
    if catalog is None:
        try:
            catalog = questions.load_catalog(questions.default_shipped_dir(), questions.default_fleet_dir())
        except questions.QuestionError as exc:
            notes.append(f"answers: the question catalog cannot be loaded: {exc}")
    logs = answer_store.RepoLogs()
    if catalog is not None:
        context = AnswerContext(catalog=catalog, logs=logs, now=now if now is not None else datetime.datetime.now())
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
        devroot=devroot,
    )
    views = tuple(
        sorted(
            (_view_record(record, effective_root, context) for record in records),
            key=review_sort_key,
            reverse=True,
        )
    )
    views = _attach_sessions(views, notes)
    notes.extend(f"answers: {note}" for note in dict.fromkeys(note for view in views for note in view.answer_notes))
    notes.extend(f"answers: {note}" for note in logs.notes)
    warnings: list[str] = []
    for view in views:
        if view.status not in SUMMARY_ELIGIBLE_STATUSES:
            continue
        label = (
            f"{view.project_name}/{view.id}"
            if scope == "all"
            else view.id
        )
        if view.summary_status == "missing":
            warnings.append(f"task {label} is missing task.summary")
        elif view.summary_status == "stale":
            warnings.append(
                f"task {label} orientation answer is stale"
                if view.summary_source == SUMMARY_SOURCE_ANSWER
                else f"task {label} task.summary is stale"
            )
        elif view.summary_status == "invalid":
            warnings.append(f"task {label} has an invalid orientation answer file")
        elif view.summary_status == "unavailable":
            warnings.append(
                f"task {label} has no locally readable task summary ({view.status})"
            )
        elif view.summary is not None and "\n" in view.summary:
            warnings.append(f"task {label} task.summary is not a single line")
    return TaskListing(
        scope=scope,
        effective_root=effective_root,
        views=views,
        warnings=tuple(warnings),
        notes=tuple(notes),
    )


def section_views(views: tuple[TaskView, ...]) -> dict[str, list[TaskView]]:
    """Group views without losing non-standard classifications.

    ``deferred`` (purposefully put on hold, intended to be resumed later) is
    its own group -- neither ``open`` nor ``closed`` -- sectioned between
    them by every caller (list's text output, the JSON/YAML projection, and
    the review HTML)."""
    return {
        "open": [view for view in views if view.status in OPEN_CLASSIFICATIONS],
        "deferred": [view for view in views if view.status == "deferred"],
        "closed": [view for view in views if view.status == "closed"],
        "other": [
            view
            for view in views
            if view.status not in OPEN_CLASSIFICATIONS
            and view.status not in ("closed", "archived", "deferred")
        ],
    }


def project_groups(listing: TaskListing) -> tuple[ProjectGroup, ...]:
    """Group tasks by project, most recently updated project first.

    A project's last activity is the newest activity among its open tasks;
    projects with no dated open task sort last, then by name.
    """
    groups: dict[tuple[str, Path], list[TaskView]] = {}
    for view in listing.views:
        groups.setdefault((view.project_name, view.project_root), []).append(view)
    projects = []
    for (name, root), views in groups.items():
        last_activity = project_last_activity(views)
        projects.append(
            ProjectGroup(
                name=name,
                root=root,
                views=tuple(views),
                last_update=last_activity[:10] if last_activity else None,
                last_activity=last_activity,
            )
        )
    projects.sort(key=lambda group: (group.name.casefold(), group.name, str(group.root)))
    projects.sort(
        key=lambda group: (group.last_activity is not None, group.last_activity or ""),
        reverse=True,
    )
    return tuple(projects)


def project_last_activity(views: Iterable[TaskView]) -> str | None:
    """Return the newest activity among open tasks, or None."""
    return max(
        (
            activity
            for view in views
            if view.status in OPEN_CLASSIFICATIONS
            and (activity := activity_key(view))
        ),
        default=None,
    )


def task_key(listing: TaskListing, view: TaskView) -> str:
    """Return a collision-free key for structured output and HTML."""
    if listing.scope == "all":
        return f"{view.project_name}::{view.id}"
    return view.id


def review_sort_key(view: TaskView) -> tuple[bool, str]:
    """Sort newest activity first; missing dates are oldest."""
    activity = activity_key(view)
    return (activity is not None, activity or "")


def _view_dict(listing: TaskListing, view: TaskView) -> dict[str, Any]:
    from .sessions_cli import record_dict  # noqa: PLC0415

    return {
        "key": task_key(listing, view),
        "id": view.id,
        "status": view.status,
        "priority": view.priority,
        "last_update": view.last_update,
        "last_activity": activity_key(view),
        "title": view.title,
        "summary": view.summary,
        "summary_source": view.summary_source,
        "summary_status": view.summary_status,
        "summary_missing": view.summary_status in ("missing", "stale", "invalid"),
        "answers": dict(view.answers) if view.answers is not None else {},
        "coverage": dict(view.coverage) if view.coverage is not None else None,
        "summary_fingerprint": view.summary_fingerprint,
        "current_fingerprint": view.current_fingerprint,
        "host": view.host,
        "project": view.project_name,
        "project_root": str(view.project_root),
        "sessions": [record_dict(record) for record in view.sessions],
        "updates": [
            {"date": update.date, "detail": update.detail}
            for update in view.updates
        ],
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
        elif "has an invalid orientation answer file" in message:
            code = "invalid_answer"
        else:
            code = "summary_unavailable"
        diagnostics.append({"code": code, "severity": "notice", "message": message})

    projects = []
    for project in project_groups(listing):
        project_sections = section_views(project.views)
        projects.append(
            {
                "name": project.name,
                "root": str(project.root),
                "last_update": project.last_update,
                "last_activity": project.last_activity,
                "tasks": [task_key(listing, view) for view in project.views],
                "sections": {
                    name: [task_key(listing, view) for view in views]
                    for name, views in project_sections.items()
                },
            }
        )

    return {
        "schema_version": PROJECTION_VERSION,
        "scope": listing.scope,
        "tasks": [_view_dict(listing, view) for view in listing.views],
        "sections": {
            name: [task_key(listing, view) for view in views]
            for name, views in groups.items()
        },
        "projects": projects,
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
