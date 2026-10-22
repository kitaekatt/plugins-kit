"""Task facts as JSON documents for another program (databench's tasks module).

The contract is ``skills/task/references/task-export-protocol.md`` (normative)
and its schema ``task-export.schema.json``. This module builds the payloads;
``export_cli`` owns the command line, the envelope, stdout and exit codes.

The export carries task FACTS: what each task folder, its validation, its
items and its working-session registrations say. Views a manager builds from
them (sections, needs, hand-off commands, item UI states, counts, filters)
are the consumer's. The export reads through the task system's domain
modules (``discovery``, ``validate``, ``task_items``, ``sessions``) and
imports nothing under ``task_system.dashboard``, so ``task-kit export`` runs
without dashboard code.

Nothing here writes. The git commands validation runs take no optional locks
(``GIT_OPTIONAL_LOCKS=0``, set by ``export_cli``), so not even git's index
refresh writes.

Grouping. A task folder reached through several checkouts (``dev/tasks``
linked into more than one) is assigned once, to the checkout the task
system's own deduplication prefers (the shortest directory name), as
``task list --scope all`` and the dashboard do. A repository is one origin:
every checkout whose ``origin`` normalizes to the same remote
(``repo_identity.repo_key_for``) is one repository.
"""

from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import machine as machine_mod
from . import plan_sections, repo_identity, sessions, sessions_config, vcs
from .discovery import (
    NO_PROJECT_DIRECTORIES_MESSAGE,
    TASK_CONFIG_PATH,
    DiscoveryError,
    ProjectRoot,
    configured_project_directories,
    discover_project_roots,
    read_task_block,
    read_task_updates,
)
from .resolve import ARCHIVED_DIRNAME, KNOWN_ROOTS, LOCATION_TMP
from .task_items import read_task_items, sort_items
from .types import HAND_OFF
from .validate import validate_ref

PROTOCOLS = (1,)
MAX_UPDATES = 20
PLAN_SECTION_LIMIT = 8000
LOAD_WORKERS = 8
# The task roots the export reads: the task system's own, without tmp/.
TASK_ROOTS = tuple(root for root in KNOWN_ROOTS if root != LOCATION_TMP)
# The files a task's content revision covers.
REVISION_FILES = ("task.yaml", "plan.md", "log.md")
SUMMARY_SOURCE_TASK_YAML = "task.yaml"


class ExportError(Exception):
    """A handled failure: ``code`` is one of the protocol's error codes."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# -- identities ---------------------------------------------------------------


def _canonical(path: Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.realpath(path)))


def checkout_id(root: Path) -> str:
    """A checkout's id: the first 16 hex digits of SHA-256 over its canonical real path."""
    return hashlib.sha256(_canonical(root).encode("utf-8")).hexdigest()[:16]


def repository_id(key: str) -> str:
    """A repository's id: the first 16 hex digits of SHA-256 over its normalized origin key."""
    return hashlib.sha256(("repo:" + key).encode("utf-8")).hexdigest()[:16]


def _preference(project: ProjectRoot) -> tuple[int, str, str, str]:
    """The task system's tie-break for a folder several checkouts reach: shortest name wins."""
    return (len(project.name), project.name.casefold(), project.name, str(project.root))


def _error(code: str, message: str, root: Path | str | None = None) -> dict:
    return {"code": code, "message": message, "root": None if root is None else str(root)}


# -- discovery ------------------------------------------------------------------


def resolve_parents(parents: tuple[Path, ...] | None, config_path: Path | None = None) -> tuple[Path, ...]:
    """The parent directories to discover projects under: the given ones, else task-kit's configured ones."""
    try:
        if parents:
            resolved = []
            for parent in parents:
                candidate = Path(parent).expanduser()
                if not candidate.is_absolute() or not candidate.is_dir():
                    raise DiscoveryError(f"--parent is not an existing absolute directory: {candidate}")
                resolved.append(candidate.resolve())
            return tuple(dict.fromkeys(resolved))
        found = configured_project_directories(config_path if config_path is not None else TASK_CONFIG_PATH)
    except DiscoveryError as exc:
        raise ExportError("discovery_failed", str(exc)) from exc
    if not found:
        raise ExportError("discovery_failed", NO_PROJECT_DIRECTORIES_MESSAGE)
    return found


@dataclass(frozen=True)
class _Owner:
    project: ProjectRoot
    location: str
    stub: str

    @property
    def path(self) -> str:
        return f"{self.location}/{self.stub}"

    @property
    def folder(self) -> Path:
        return self.project.root / self.location / self.stub


def _list_task_stubs(base: Path) -> list[str]:
    stubs = []
    with os.scandir(base) as entries:
        for entry in entries:
            if entry.name == ARCHIVED_DIRNAME or not entry.is_dir():
                continue
            if os.path.isfile(os.path.join(entry.path, "task.yaml")):
                stubs.append(entry.name)
    return sorted(stubs)


def _owners(parents: tuple[Path, ...], errors: list[dict]) -> tuple[dict[str, _Owner], dict[str, tuple[ProjectRoot, Path]]]:
    """Every task folder once, keyed by its physical path, and every physical task root once.

    Projects are visited most-preferred first, so the first checkout to reach
    a folder owns it.
    """
    try:
        roots = discover_project_roots(parents)
    except DiscoveryError as exc:
        raise ExportError("discovery_failed", str(exc)) from exc
    owners: dict[str, _Owner] = {}
    bases: dict[str, tuple[ProjectRoot, Path]] = {}
    stubs_by_base: dict[str, list[str]] = {}
    for project in sorted(roots, key=_preference):
        for location in TASK_ROOTS:
            base = project.root / location
            if not base.is_dir():
                continue
            physical_base = _canonical(base)
            if physical_base not in stubs_by_base:
                bases[physical_base] = (project, base)
                try:
                    stubs_by_base[physical_base] = _list_task_stubs(base)
                except OSError as exc:
                    errors.append(_error("unreadable", f"cannot list {base}: {exc}", project.root))
                    stubs_by_base[physical_base] = []
            for stub in stubs_by_base[physical_base]:
                owners.setdefault(_canonical(base / stub), _Owner(project, location, stub))
    return owners, bases


def _repository(root: Path, errors: list[dict]) -> tuple[str, str]:
    """(origin key, repository name) of a checkout; git failing falls back to the path, reported."""
    try:
        return repo_identity.repo_key_for(root), repo_identity.repo_name_for(root)
    except vcs.GitError as exc:
        errors.append(_error(
            "repo_identity",
            f"cannot read the git remote of {root}; it is its own repository, named after its directory: {exc}",
            root,
        ))
        return repo_identity.path_key(root), Path(root).name


# -- one task -------------------------------------------------------------------


def content_revision(folder: Path) -> str:
    """The task's content revision: SHA-256 over the bytes of task.yaml, plan.md and log.md (absent ones marked)."""
    digest = hashlib.sha256()
    for name in REVISION_FILES:
        try:
            data: bytes | None = (folder / name).read_bytes()
        except FileNotFoundError:
            data = None
        part = hashlib.sha256(b"\0missing" if data is None else b"\1" + data).hexdigest()
        digest.update(f"{name}={part}\n".encode("ascii"))
    return digest.hexdigest()[:16]


def _text(block: dict, key: str) -> str | None:
    value = block.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _strings(value: object) -> list[str]:
    return [entry for entry in value if isinstance(entry, str)] if isinstance(value, list) else []


def item_record(item) -> dict:
    return {"id": item.id, "title": item.title, "state": item.state, "priority": item.priority, "note": item.note}


def task_facts(owner: _Owner) -> dict:
    """What one task folder says, through the task system's own readers (no checkout or sessions yet)."""
    folder = owner.folder
    result = validate_ref(owner.path, owner.project.root)
    block = read_task_block(folder) or {}
    updates = read_task_updates(folder)
    items = read_task_items(folder, HAND_OFF)
    summary = _text(block, "summary")
    return {
        "path": owner.path,
        "stub": owner.stub,
        "location": owner.location,
        "revision": content_revision(folder),
        "title": _text(block, "title"),
        "priority": _text(block, "priority"),
        "description": _text(block, "description"),
        "summary": summary,
        "summary_source": SUMMARY_SOURCE_TASK_YAML if summary is not None else None,
        "classification": result.classification,
        "blocked_by": _strings(block.get("blocked_by")),
        "last_activity": max((update.timestamp for update in updates), default=None),
        "updates": [
            {"date": update.date, "time": update.time, "detail": update.detail} for update in updates[:MAX_UPDATES]
        ],
        "updates_total": len(updates),
        "has_items_block": items.block_found,
        "items": [item_record(item) for item in sort_items(items.items)],
        "findings": {
            "errors": list(result.errors),
            "warnings": list(result.warnings),
            "item_errors": list(items.errors),
            "notes": list(result.notes),
        },
    }


# -- working sessions -------------------------------------------------------------


def current_machine(config_path: Path | None = None) -> machine_mod.Machine:
    """This machine, as working-session records name it; raises ``sessions.SessionsError``-like errors as RuntimeError."""
    config = sessions_config.load_sessions_config(config_path if config_path is not None else TASK_CONFIG_PATH)
    return machine_mod.current(override=config.host)


def session_record(record: sessions.Record) -> dict:
    return {
        "host": record.host,
        "session_id": record.session_id,
        "registered_at": record.registered_at,
        "state": record.state,
        "local": record.owned,
        "recorded_at": record.recorded_at,
        "task_state": record.task_state,
    }


def _sessions(
    bases: dict[str, tuple[ProjectRoot, Path]],
    resolve_machine: Callable[[], machine_mod.Machine],
    errors: list[dict],
) -> dict[tuple[str, str], list[dict]]:
    """Every registration under every physical task root, keyed by (physical root, stub)."""
    if not bases:
        return {}
    try:
        mine = resolve_machine()
    except (sessions_config.SessionsConfigError, machine_mod.MachineError) as exc:
        errors.append(_error("sessions", f"working-session registrations cannot be read on this machine: {exc}"))
        return {}
    found: dict[tuple[str, str], list[dict]] = {}
    for physical_base in sorted(bases):
        project, base = bases[physical_base]
        real = Path(os.path.realpath(base))
        view = sessions.read_root(real, mine, check_tasks=True)
        for note in view.notes:
            errors.append(_error("sessions", f"{real}: {note}", project.root))
        for record in view.records:
            found.setdefault((physical_base, record.task), []).append(session_record(record))
    for records in found.values():
        records.sort(key=lambda r: (r["registered_at"], r["host"], r["session_id"]))
    return found


# -- payloads ---------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode("utf-8")).hexdigest()[:16]


def tasks_payload(
    parents: tuple[Path, ...] | None = None,
    *,
    config_path: Path | None = None,
    resolve_machine: Callable[[], machine_mod.Machine] | None = None,
    now: Callable[[], datetime] = _utc_now,
) -> dict:
    """The payload of ``export tasks`` (protocol 1)."""
    errors: list[dict] = []
    owners, bases = _owners(resolve_parents(parents, config_path), errors)
    facts: dict[str, dict] = {}
    if owners:
        with ThreadPoolExecutor(max_workers=LOAD_WORKERS) as pool:
            futures = {key: pool.submit(task_facts, owner) for key, owner in owners.items()}
            for key, future in futures.items():
                try:
                    facts[key] = future.result()
                except OSError as exc:
                    owner = owners[key]
                    errors.append(_error("unreadable", f"cannot read task folder {owner.folder}: {exc}", owner.project.root))
    machine_reader = resolve_machine if resolve_machine is not None else (lambda: current_machine(config_path))
    registrations = _sessions(bases, machine_reader, errors)

    checkouts: dict[str, dict] = {}
    repositories: dict[str, dict] = {}
    tasks: list[tuple[tuple, dict]] = []
    for key, record in facts.items():
        owner = owners[key]
        root = owner.project.root
        cid = checkout_id(root)
        if cid not in checkouts:
            origin, name = _repository(root, errors)
            rid = repository_id(origin)
            checkouts[cid] = {"id": cid, "root": str(root), "repository": rid}
            entry = repositories.setdefault(rid, {"id": rid, "name": name, "checkouts": []})
            entry["checkouts"].append(cid)
        rid = checkouts[cid]["repository"]
        record = {
            "checkout": cid,
            **record,
            "sessions": registrations.get((_canonical(root / owner.location), owner.stub), []),
        }
        name = repositories[rid]["name"]
        tasks.append(((name.casefold(), name, record["path"], str(root)), record))
    for entry in repositories.values():
        entry["checkouts"].sort(key=lambda cid: checkouts[cid]["root"])
    payload = {
        "repositories": sorted(repositories.values(), key=lambda r: (r["name"].casefold(), r["name"], r["id"])),
        "checkouts": sorted(checkouts.values(), key=lambda c: c["root"]),
        "tasks": [record for _key, record in sorted(tasks, key=lambda pair: pair[0])],
        "errors": errors,
    }
    return {
        "generated_at": now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "revision": _digest(payload),
        **payload,
    }


def item_payload(
    checkout: str,
    task: str,
    item: str,
    parents: tuple[Path, ...] | None = None,
    *,
    config_path: Path | None = None,
) -> dict:
    """The payload of ``export item``: one item of one task the ``tasks`` export lists, with its plan.md section."""
    owners, _bases = _owners(resolve_parents(parents, config_path), [])
    in_checkout = [owner for owner in owners.values() if checkout_id(owner.project.root) == checkout]
    if not in_checkout:
        raise ExportError("unknown_checkout", f"no checkout with id {checkout!r} holds a task")
    owner = next((candidate for candidate in in_checkout if candidate.path == task), None)
    if owner is None:
        raise ExportError("unknown_task", f"checkout {checkout} holds no task {task!r}")
    items = read_task_items(owner.folder, HAND_OFF)
    found = next((record for record in items.items if record.id == item), None)
    if found is None:
        raise ExportError("unknown_item", f"task {task} has no item {item!r}")
    try:
        plan_text = (owner.folder / "plan.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        plan_text = ""
    section = plan_sections.find_item_section(plan_text, item)
    body = section.body if section.status == plan_sections.FOUND else None
    return {
        "checkout": checkout,
        "path": owner.path,
        "item": item_record(found),
        "plan_section": None if body is None else body[:PLAN_SECTION_LIMIT],
        "plan_section_truncated": body is not None and len(body) > PLAN_SECTION_LIMIT,
    }


def protocols_payload() -> dict:
    return {"protocols": list(PROTOCOLS)}
