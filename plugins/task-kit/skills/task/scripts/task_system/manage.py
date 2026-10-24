"""Negotiated task-domain operations for a manager, independent of its service.

The CLI contract is references/task-manage-protocol.md. Mutations serialize by
physical task repository, compare a four-file content revision, validate the
candidate, and retain the task system's per-file atomic replacement behavior.
A linked dev/tasks root is supported. Links below that root are refused.
Harness editors do not take this lock: mutation.TaskMutator documents the
remaining final-compare/rename race and reports partial writes explicitly.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
from pathlib import Path
import re
from typing import Any, Callable

import yaml
from skills_kit_lib import schema_engine

from . import authoring, export, mutation, plan_sections, schemas, sessions, session_publish, vcs
from .datadir import resolve_data_dir
from .discovery import ProjectRoot, read_task_block
from .init import _scaffold_contents, init_task
from .machine import Machine
from .task_items import ITEM_ID_RE
from .types import HAND_OFF

PROTOCOLS = (1,)
INPUT_LIMIT = 1_048_576
ACTIONS = ("task-create", "task-update", "item-add", "item-update", "item-complete", "session-dismiss", "accept-defaults")
FIELDS = {
    "task-create": {"title", "priority", "description", "first_item"},
    "task-update": {"title", "priority", "description", "status"},
    "item-add": {"title", "priority", "details", "acceptance_argv", "note"},
    "item-update": {"item", "title", "priority", "note", "state", "acceptance_argv"},
    "item-complete": {"item", "summary"},
    "session-dismiss": {"host", "session_id", "registered_at"},
    "accept-defaults": {"defaults"},
}


class ManageError(export.ExportError):
    """A machine-readable refusal. Written files are explicit after partial changes."""

    def __init__(self, code: str, message: str, *, written: tuple[str, ...] = ()) -> None:
        super().__init__(code, message)
        self.written = written


def protocols() -> dict[str, Any]:
    return {"protocols": list(PROTOCOLS), "actions": list(ACTIONS), "input_limit": INPUT_LIMIT,
            "revision_files": list(mutation.FILES)}


def _project(project: Path) -> Path:
    if not project.is_absolute() or not project.is_dir():
        raise ManageError("usage", "--project must name an existing absolute directory")
    root = project.resolve()
    # The dev directory cannot redirect the task root. dev/tasks itself is the sanctioned link.
    if (root / "dev").is_symlink():
        raise ManageError("refused", "the project's dev directory must not be a link")
    return root


def _folder(project: Path, task: str, *, create: bool = False) -> Path:
    if not isinstance(task, str) or not re.fullmatch(r"dev/tasks/[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?", task):
        raise ManageError("usage", "task must be a canonical dev/tasks/<stub> path")
    if task.rsplit("/", 1)[1] in ("archived-tasks", ".", ".."):
        raise ManageError("usage", "reserved task folder")
    base = project / "dev/tasks"
    if not base.is_dir():
        raise ManageError("refused", "the project has no dev/tasks root")
    folder = base / task.rsplit("/", 1)[1]
    if folder.is_symlink() or folder.resolve().parent != base.resolve():
        raise ManageError("refused", "task folders must be direct, unlinked children of dev/tasks")
    if not create and not folder.is_dir():
        raise ManageError("unknown_task", f"no task {task!r}")
    if folder.exists():
        # Readers traverse Markdown and answer records too. Refuse links anywhere in this task.
        if any(path.is_symlink() for path in folder.rglob("*")):
            raise ManageError("refused", "task files and subdirectories must not be links")
    return folder


def _lock(folder: Path, data_dir: Path) -> Path:
    base = folder.parent.resolve()
    if vcs.is_repository(base):
        base = Path(vcs._git(base, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    key = hashlib.sha256(str(base).encode("utf-8")).hexdigest()
    return data_dir / "manage" / f"{key}.lock"


def _accept(value: object) -> list[list[str]]:
    if not isinstance(value, list) or not value or not all(
        isinstance(argv, list) and argv and all(isinstance(arg, str) and arg and "\0" not in arg for arg in argv)
        for argv in value
    ):
        raise ManageError("refused", "acceptance_argv must be a non-empty list of non-empty string argv lists")
    return value


def _registrations(project: Path, folder: Path, mine: Machine) -> list[dict[str, Any]]:
    view = sessions.read_root(folder.parent.resolve(), mine, check_tasks=True)
    if view.notes:
        raise ManageError("refused", "working sessions cannot be verified: " + "; ".join(view.notes))
    return [export.session_record(record) for record in view.records if record.task == folder.name]


def _hold(project: Path, folder: Path, mine: Machine) -> None:
    if any(record["local"] and record["state"] == sessions.STATE_RUNNING
           for record in _registrations(project, folder, mine)):
        raise ManageError("held", "a verified local session works this task; resolve it in the harness")


def input_payload(project: Path, task: str, item: str | None = None, *,
                  resolve_machine: Callable[[], Machine] = export.current_machine) -> dict[str, Any]:
    """Full task input, never truncated. Without an item, inspect post-worker state."""
    project = _project(project)
    folder = _folder(project, task)
    before = mutation.read_files(folder)
    if sum(len(raw or b"") for raw in before.values()) > INPUT_LIMIT:
        raise ManageError("too_large", f"task input exceeds {INPUT_LIMIT} bytes; shorten the task before queueing")
    owner = export._Owner(ProjectRoot(project.name, project), "dev/tasks", folder.name)
    facts = export.task_facts(owner)
    facts.pop("_answer_notes", None)
    raw_items = mutation.raw_items((before[mutation.PLAN] or b"").decode("utf-8"))
    if facts["findings"]["errors"] or facts["findings"]["item_errors"] or raw_items is None:
        raise ManageError("refused", "the task has invalid source; fix it in the harness")
    selected = None
    instructions = None
    acceptance: list[list[str]] = []
    if item is not None:
        selected = next((entry for entry in raw_items if entry["id"] == item), None)
        if selected is None:
            raise ManageError("unknown_item", f"task has no item {item!r}")
        section = plan_sections.find_item_section((before[mutation.PLAN] or b"").decode("utf-8"), item)
        if section.status != plan_sections.FOUND:
            raise ManageError("refused", f"item instructions are {section.status}; fix the item section in the harness")
        instructions = section.body
        if "accept" in selected:
            acceptance = _accept(selected["accept"])
    mine = resolve_machine()
    registrations = _registrations(project, folder, mine)
    git = asdict(vcs.facts(project))
    if mutation.read_files(folder) != before:
        raise ManageError("conflict", "task changed while its input was read; read it again")
    facts.update(folder=str(folder.resolve()), revision=mutation.revision_of(before),
                 status=yaml.safe_load(before[mutation.TASK_YAML])["task"]["status"], items=raw_items,
                 plan=(before[mutation.PLAN] or b"").decode("utf-8"),
                 log=(before[mutation.LOG] or b"").decode("utf-8"),
                 context=(before[mutation.CONTEXT] or b"").decode("utf-8"), sessions=registrations)
    return {"project_root": str(project), "checkout": export.checkout_id(project),
            "repository": export.repository_id(export.repo_identity.repo_key_for(project)),
            "task": facts, "item": selected, "instructions": instructions,
            "acceptance_argv": acceptance, "git": git}


def _fields(action: str, value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - FIELDS[action]:
        raise ManageError("usage", f"fields must contain only {sorted(FIELDS[action])}")
    result = dict(value)
    for key in ("title", "summary"):
        if key in result:
            result[key] = authoring.check_title(result[key])
    if "priority" in result:
        result["priority"] = authoring.check_priority(result["priority"])
    for key in ("title", "summary", "description", "note", "item", "host", "session_id", "registered_at"):
        if key in result and (not isinstance(result[key], str) or "\0" in result[key]):
            raise ManageError("usage", f"{key} must be text without NUL")
    if "acceptance_argv" in result:
        result["acceptance_argv"] = _accept(result["acceptance_argv"])
    if "state" in result and result["state"] not in HAND_OFF.item_state_vocabulary:
        raise ManageError("usage", "unknown item state")
    if "status" in result and result["status"] not in ("active", "blocked", "deferred", "closed"):
        raise ManageError("usage", "unknown task status")
    return result


def _validate_candidate(task_yaml: str, plan: str) -> None:
    data = yaml.safe_load(task_yaml)
    failures, _ = schema_engine.validate(data, schemas.TASK_SCHEMA)
    block = data.get("task", {}) if isinstance(data, dict) else {}
    if block.get("status") not in HAND_OFF.state_vocabulary:
        failures.append(("task.status", "unknown status"))
    items = mutation.raw_items(plan)
    if items is None:
        raise ManageError("refused", "candidate plan has no task_items block")
    errors, _ = schema_engine.validate({"task_items": {"items": items}}, schemas.TASK_ITEMS_SCHEMA)
    failures.extend(errors)
    ids = [entry.get("id") for entry in items]
    if len(set(ids)) != len(ids):
        raise ManageError("refused", "candidate has duplicate item ids")
    for entry in items:
        if not isinstance(entry.get("id"), str) or not ITEM_ID_RE.fullmatch(entry["id"]):
            raise ManageError("refused", "candidate has invalid item id")
        if entry.get("state") not in HAND_OFF.item_state_vocabulary:
            raise ManageError("refused", "candidate has invalid item state")
        authoring.check_priority(entry.get("priority"))
        if "accept" in entry:
            _accept(entry["accept"])
    if failures:
        raise ManageError("refused", "candidate is invalid: " + "; ".join(f"{path}: {text}" for path, text in failures))


def _complete(plan: str, item: str, summary: str) -> str:
    match, data, entries = mutation._require_block(plan)
    entries[:] = [entry for entry in entries if entry["id"] != item]
    text = mutation._splice(plan, match, data)
    line = f"- {item}: {summary}\n"
    heading = "## Accomplished\n"
    if heading not in text:
        first, separator, rest = text.partition("\n")
        return first + separator + "\n" + heading + "\n" + line + "\n" + rest
    return text.replace(heading, heading + "\n" + line, 1)


def _edit(action: str, fields: dict[str, Any], texts: mutation.TaskTexts) -> dict[str, str]:
    task_yaml, plan = texts.task_yaml, texts.plan
    if action == "task-update":
        task_yaml = mutation.set_task_fields(task_yaml, **fields)
    elif action == "item-add":
        title = authoring.check_title(fields.get("title"))
        plan, added = authoring.add_item_to_plan(plan, title=title, priority=fields.get("priority"),
                                                details=authoring.check_details(fields.get("details")))
        changes = {key: value for key, value in fields.items() if key in ("note", "acceptance_argv")}
        if changes:
            match, data, entries = mutation._require_block(plan)
            entry = mutation._one_item(entries, added["id"])
            for key, value in changes.items():
                entry["accept" if key == "acceptance_argv" else key] = value
            plan = mutation._splice(plan, match, data)
    elif action == "item-update":
        match, data, entries = mutation._require_block(plan)
        entry = mutation._one_item(entries, fields.get("item", ""))
        for key, value in fields.items():
            if key == "item":
                continue
            name = "accept" if key == "acceptance_argv" else key
            if value is None:
                entry.pop(name, None)
            else:
                entry[name] = value
        plan = mutation._splice(plan, match, data)
    elif action == "item-complete":
        item = fields.get("item")
        if not isinstance(item, str) or not ITEM_ID_RE.fullmatch(item):
            raise ManageError("usage", "item-complete requires an item id")
        summary = authoring.check_title(fields.get("summary"))
        plan = _complete(plan, item, summary)
    elif action == "accept-defaults":
        # These are exact grounded decision records, checked against the current answer by the caller.
        for decision in fields["defaults"]:
            match, data, entries = mutation._require_block(plan)
            found = [entry for entry in entries if entry["id"] == decision["item"]]
            if found:
                found[0].update(state="available", note="Accepted default: " + decision["default"])
                plan = mutation._splice(plan, match, data)
    _validate_candidate(task_yaml, plan)
    changes = {mutation.LOG: mutation.append_log(texts.log, f"manager: {action}: {fields!r}")}
    if task_yaml != texts.task_yaml:
        changes[mutation.TASK_YAML] = task_yaml
    if plan != texts.plan:
        changes[mutation.PLAN] = plan
    return changes


def _check_defaults(folder: Path, fields: dict[str, Any]) -> None:
    from datetime import datetime
    from . import answer_store, questions

    choices = fields.get("defaults")
    if not isinstance(choices, list) or not choices or not all(isinstance(choice, dict) for choice in choices):
        raise ManageError("usage", "accept-defaults requires a non-empty defaults list")
    catalog = questions.load_catalog(questions.default_shipped_dir(), questions.default_fleet_dir())
    block = read_task_block(folder) or {}
    logs = answer_store.RepoLogs()
    top = logs.top_of(folder)
    found = answer_store.task_answers(catalog, folder, block, block.get("status", "active"),
                                     repo_top=top, log=logs.log_for(top), now=datetime.now())
    state = found.states.get("decisions")
    record = state.as_dict() if state is not None else {}
    current = (record.get("answer") or {}).get("decisions", [])
    if record.get("state") != "current" or any(choice not in current or not choice.get("default") for choice in choices):
        raise ManageError("conflict", "defaults must match the current grounded decisions answer")


def mutate_payload(project: Path, request: object, *, data_dir: Path | None = None,
                   resolve_machine: Callable[[], Machine] = export.current_machine) -> dict[str, Any]:
    """Apply one semantic mutation. The manager validates receipt and acceptance before item-complete."""
    if not isinstance(request, dict) or set(request) != {"task", "expected_revision", "action", "fields"}:
        raise ManageError("usage", "request must contain exactly task, expected_revision, action and fields")
    action = request["action"]
    if not isinstance(action, str) or action not in ACTIONS:
        raise ManageError("usage", "unknown manage action")
    project = _project(project)
    task = request["task"]
    folder = _folder(project, task, create=action == "task-create")
    physical_folder = folder.resolve()
    fields = _fields(action, request["fields"])
    expected = request["expected_revision"]
    if action == "task-create":
        if expected is not None:
            raise ManageError("usage", "task-create requires expected_revision null (expected absence)")
    elif not isinstance(expected, str) or not expected:
        raise ManageError("usage", "a mutation requires expected_revision")
    local = data_dir if data_dir is not None else resolve_data_dir()
    lock = _lock(folder, local)
    if action == "task-create":
        title = authoring.check_title(fields.get("title"))
        first = fields.get("first_item")
        if first is not None:
            if not isinstance(first, dict):
                raise ManageError("usage", "first_item must be an item-add fields object")
            _fields("item-add", first)
        initial = {key: value for key, value in fields.items() if key != "first_item"}
        initial["title"] = title
        contents = _scaffold_contents(HAND_OFF, title, task)
        texts = mutation.TaskTexts(contents["task.yaml"], contents["plan.md"], contents["log.md"], contents["CLAUDE.md"])
        changes = _edit("task-update", initial, texts)
        if first is not None:
            latest = mutation.TaskTexts(changes.get("task.yaml", texts.task_yaml), texts.plan,
                                        changes.get("log.md", texts.log), texts.context)
            changes.update(_edit("item-add", _fields("item-add", first), latest))
        contents.update(changes)
        with mutation.ProcessLock(lock):
            if _folder(project, task, create=True).resolve() != physical_folder:
                raise ManageError("conflict", "the task root changed before creation")
            if folder.exists():
                raise ManageError("conflict", "task already exists; expected absence")
            folder = init_task(folder.name, project, dest="dev/tasks")
            # This newly created folder is private to this operation until its repository lock releases.
            written = tuple(str(folder / name) for name in contents)
            try:
                for name, text in contents.items():
                    path = physical_folder / name
                    temp = mutation.TaskMutator._write_temp(path, text)
                    try:
                        mutation.TaskMutator._replace(path, temp, mutation._digest(path.read_bytes()))
                    finally:
                        temp.unlink(missing_ok=True)
            except (OSError, mutation.MutationError) as exc:
                raise ManageError("internal", f"task created but follow-up failed: {exc}", written=written) from exc
            revision = mutation.folder_revision(folder)
        return {"task": task, "revision": revision, "action": action}

    mine = resolve_machine()
    folder = physical_folder
    if action == "session-dismiss":
        if set(fields) != FIELDS[action]:
            raise ManageError("usage", "session-dismiss requires host, session_id and registered_at")
        with mutation.ProcessLock(lock):
            if _folder(project, task).resolve() != folder:
                raise ManageError("conflict", "the task root changed before dismissal")
            if mutation.folder_revision(folder) != expected:
                raise ManageError("conflict", "task changed since expected_revision")
            target = next((record for record in _registrations(project, folder, mine)
                           if all(record[key] == fields[key] for key in FIELDS[action])), None)
            if target is None:
                raise ManageError("conflict", "the exact session registration no longer belongs to this task")
            result = sessions.dismiss(mine, project, fields["host"], fields["session_id"], fields["registered_at"], data_dir=local)
        published = session_publish.publish(result.root, mine, data_dir=local)
        if not published.published:
            raise ManageError("publication_failed", published.message, written=tuple(str(path) for path in result.changed_files))
        publication = asdict(published)
        publication["repo_root"] = str(published.repo_root) if published.repo_root is not None else None
        return {"task": task, "revision": mutation.folder_revision(folder), "action": action,
                "publication": publication}

    def edit(texts: mutation.TaskTexts) -> dict[str, str]:
        if _folder(project, task).resolve() != folder:
            raise ManageError("conflict", "the task root changed")
        _hold(project, folder, mine)
        if action == "accept-defaults":
            _check_defaults(folder, fields)
        return _edit(action, fields, texts)

    def precommit() -> None:
        if _folder(project, task).resolve() != folder:
            raise ManageError("conflict", "the task root changed while the edit was prepared")
        _hold(project, folder, mine)
        if mutation.folder_revision(folder) != expected:
            raise ManageError("conflict", "task changed while the edit was prepared")

    # Session registration uses this same root lock. A new hold cannot cross the final check and replacements.
    session_gate = sessions._locked(local, (), (folder.parent.resolve(),))
    revision = mutation.TaskMutator(lock).mutate(folder, edit, expected_revision=expected,
                                                precommit=precommit, gate=session_gate)
    return {"task": task, "revision": revision, "action": action, "item": fields.get("item")}
