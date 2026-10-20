"""A cached, read-only inventory of every configured project's task folders.

Discovery and validation are the task system's own: this module enumerates the
``dev/tasks/`` folders ``task.py list --scope all`` does for every project under
the configured parent directories (``tmp/`` tasks are not shown: the dashboard
does not support them), classifies each through ``validate_ref``, and reads its
items through ``read_task_items``. It adds only what a long-lived reader needs:

- A task folder is read once per CONTENT. Each folder carries a fingerprint of
  its ``task.yaml`` and every ``*.md`` (relative path, size, ``mtime_ns``); the
  folder is re-read and re-validated only when the fingerprint changes, on an
  explicit ``force`` refresh, or when the owning project changes. Validation
  shells out to ``git``, so a poll that found nothing changed must not spawn.
- A physical folder reached through several checkouts (``dev/tasks`` linked into
  more than one) is assigned once, to the checkout the task system's own
  deduplication prefers (the shortest directory name). That checkout is the
  task's working root: the directory runs execute in.
- A PROJECT is a git repository, not a checkout: every checkout whose ``origin``
  remote names the same repository is one project (see ``repo_identity``). The
  checkout stays on each task (``checkout_id``, ``project_root``) because queue
  and run actions execute in one concrete checkout.
- The snapshot ``revision`` is a digest of everything shown, so it changes
  exactly when the picture changes.

Nothing here writes. Refreshing never touches a task folder.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..discovery import (
    NO_PROJECT_DIRECTORIES_MESSAGE,
    TASK_CONFIG_PATH,
    DiscoveryError,
    ProjectRoot,
    TaskUpdate,
    configured_project_directories,
    discover_project_roots,
    read_task_block,
    read_task_updates,
)
from ..resolve import ARCHIVED_DIRNAME, KNOWN_ROOTS, LOCATION_TMP
from ..task_items import read_task_items, sort_items
from ..types import HAND_OFF
from ..validate import validate_ref
from . import mutation, repo_identity, vcs

# Item state (plan.md vocabulary) -> the state the UI draws.
ITEM_UI_STATE = {
    "available": "available",
    "in-flight": "in-flight",
    "blocked-user": "blocked",
    "deferred": "deferred",
}
UNKNOWN_ITEM_STATE = "unknown"

# The task roots the dashboard reads: the task system's own, without tmp/.
DASHBOARD_ROOTS = tuple(root for root in KNOWN_ROOTS if root != LOCATION_TMP)

# Classifications whose blocked or invalid condition needs a person.
NEEDS_CLASSIFICATIONS = ("active", "blocked", "invalid")
MAX_UPDATES_KEPT = 8
LOAD_WORKERS = 8


@dataclass(frozen=True)
class ItemView:
    id: str
    title: str
    state: str
    ui_state: str
    priority: str | None
    note: str | None


@dataclass(frozen=True)
class TaskCore:
    """What a task folder says, independent of which project reached it."""

    classification: str
    title: str | None
    priority: str | None
    description: str | None
    summary: str | None
    blocked_by: tuple[str, ...]
    updates: tuple[TaskUpdate, ...]
    items: tuple[ItemView, ...]
    has_items_block: bool
    item_errors: tuple[str, ...]
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    notes: tuple[str, ...]
    revision: str = ""

    @property
    def last_activity(self) -> str | None:
        return max((u.timestamp for u in self.updates), default=None)


@dataclass(frozen=True)
class TaskEntry:
    project_id: str  # the repository (see repo_identity.repo_id_for)
    project_name: str  # the repository name, e.g. plugins-kit
    project_root: str  # the working root: the checkout this task was read from
    checkout_id: str  # project_id_for(project_root); runs and item routes key on it
    location: str  # dev/tasks
    stub: str
    id: str  # project-relative canonical path, e.g. dev/tasks/<stub>
    folder: str
    core: TaskCore


@dataclass(frozen=True)
class ProjectEntry:
    id: str
    name: str
    roots: tuple[str, ...]  # every checkout of the repository that holds tasks
    tasks: tuple[TaskEntry, ...]


@dataclass(frozen=True)
class InventorySnapshot:
    revision: str
    generation: int
    generated_at: str
    projects: tuple[ProjectEntry, ...]
    errors: tuple[dict, ...] = ()

    def find_task(self, checkout_id: str, task_id: str) -> TaskEntry | None:
        """The task with this id read from this checkout (``TaskEntry.checkout_id``)."""
        for project in self.projects:
            for task in project.tasks:
                if task.checkout_id == checkout_id and task.id == task_id:
                    return task
        return None


def project_id_for(root: Path) -> str:
    """Checkout identity: SHA-256 of the canonical checkout root.

    Runs, the run queue's per-checkout lease and the item routes key on this.
    The dashboard's PROJECT (a repository) is ``repo_identity.repo_id_for``.
    """
    canonical = os.path.normcase(os.path.normpath(os.path.realpath(root)))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def target_root(project: "ProjectEntry") -> str:
    """The checkout a NEW task of this project is created in: the task system's own tie-break (shortest directory name)."""
    return min(project.roots, key=lambda r: (len(Path(r).name), Path(r).name.casefold(), Path(r).name, r))


def _preference(project: ProjectRoot) -> tuple[int, str, str, str]:
    """The task system's own tie-break for a folder reached by several
    projects (``discovery._project_preference``): shortest name wins."""
    return (len(project.name), project.name.casefold(), project.name, str(project.root))


def _physical(folder: Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.realpath(folder)))


def _fingerprint(folder: str) -> tuple:
    """(relative path, size, mtime_ns) of task.yaml and every *.md in the folder."""
    found = []
    for dirpath, _dirnames, filenames in os.walk(folder):
        for name in filenames:
            top_yaml = name == "task.yaml" and dirpath == folder
            if not (top_yaml or name.lower().endswith(".md")):
                continue
            full = os.path.join(dirpath, name)
            info = os.stat(full)
            rel = os.path.relpath(full, folder).replace(os.sep, "/")
            found.append((rel, info.st_size, info.st_mtime_ns))
    found.sort()
    return tuple(found)


def _list_task_stubs(base: Path) -> list[str]:
    """Stubs of the task folders directly under ``base`` (canonical depth)."""
    stubs = []
    with os.scandir(base) as entries:
        for entry in entries:
            if entry.name == ARCHIVED_DIRNAME or not entry.is_dir():
                continue
            if os.path.isfile(os.path.join(entry.path, "task.yaml")):
                stubs.append(entry.name)
    return sorted(stubs)


def _folder_revision(folder: Path) -> str:
    """The mutation revision of a task folder; an unreadable file is an OSError here
    so the refresh reports it through its ``unreadable`` error channel."""
    try:
        return mutation.folder_revision(folder)
    except mutation.MutationError as exc:
        raise OSError(str(exc)) from exc


def _string_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(v for v in value if isinstance(v, str))


def load_task_core(canonical: str, project_root: Path) -> TaskCore:
    """Read one task folder through the task system's own functions."""
    result = validate_ref(canonical, project_root)
    folder = project_root / canonical
    block = read_task_block(folder) or {}
    updates = read_task_updates(folder)[:MAX_UPDATES_KEPT]
    items_result = read_task_items(folder, HAND_OFF)
    items = []
    for record in sort_items(items_result.items):
        items.append(
            ItemView(
                id=record.id,
                title=record.title,
                state=record.state,
                ui_state=ITEM_UI_STATE.get(record.state, UNKNOWN_ITEM_STATE),
                priority=record.priority,
                note=record.note,
            )
        )

    def text(key: str) -> str | None:
        value = block.get(key)
        return value.strip() if isinstance(value, str) and value.strip() else None

    return TaskCore(
        classification=result.classification,
        title=text("title"),
        priority=text("priority"),
        description=text("description"),
        summary=text("summary"),
        blocked_by=_string_list(block.get("blocked_by")),
        updates=tuple(updates),
        items=tuple(items),
        has_items_block=items_result.block_found,
        item_errors=tuple(items_result.errors),
        errors=tuple(result.errors),
        warnings=tuple(result.warnings),
        notes=tuple(result.notes),
        revision=_folder_revision(folder),
    )


@dataclass
class _CacheEntry:
    fingerprint: tuple
    project_root: str
    core: TaskCore


class Inventory:
    """Read-only, fingerprint-cached view over every configured project."""

    def __init__(
        self,
        *,
        config_path: Path | None = None,
        parents: tuple[Path, ...] | None = None,
        min_interval: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config_path = config_path
        self._parents = parents
        self._min_interval = min_interval
        self._clock = clock
        self._lock = threading.Lock()
        self._cache: dict[str, _CacheEntry] = {}
        # checkout root -> (repository name, error to report or None); read once,
        # re-read on a forced refresh (a remote can change without a file change).
        self._repos: dict[str, tuple[str, dict | None]] = {}
        self._snapshot: InventorySnapshot | None = None
        self._refreshed_at: float | None = None
        self._generation = 0
        #: Number of times a folder was (re)read; tests and the UI use it to
        #: show that an unchanged poll costs nothing.
        self.loads = 0

    def snapshot(self, *, force: bool = False) -> InventorySnapshot:
        """The current inventory, refreshed at most once per ``min_interval``.

        ``force`` re-reads and re-validates every folder (the explicit action
        that also picks up git state, which a file fingerprint cannot see).
        """
        with self._lock:
            now = self._clock()
            fresh = (
                self._snapshot is not None
                and self._refreshed_at is not None
                and now - self._refreshed_at < self._min_interval
            )
            if fresh and not force:
                return self._snapshot  # type: ignore[return-value]
            self._snapshot = self._refresh(force)
            self._refreshed_at = self._clock()
            return self._snapshot

    def invalidate(self) -> None:
        """Drop the freshness window so the next ``snapshot()`` re-scans (a write just changed a folder).

        The per-folder fingerprint cache stays: only the changed folder is re-read and re-validated.
        """
        with self._lock:
            self._refreshed_at = None

    # -- refresh -----------------------------------------------------------

    def _project_roots(self, errors: list[dict]) -> tuple[ProjectRoot, ...]:
        try:
            if self._parents is not None:
                parents = self._parents
            else:
                parents = configured_project_directories(
                    self._config_path if self._config_path is not None else TASK_CONFIG_PATH
                )
            if not parents:
                raise DiscoveryError(NO_PROJECT_DIRECTORIES_MESSAGE)
            return discover_project_roots(parents)
        except DiscoveryError as exc:
            errors.append({"code": "discovery", "message": str(exc)})
            return ()

    def _refresh(self, force: bool) -> InventorySnapshot:
        errors: list[dict] = []
        roots = self._project_roots(errors)

        # Walk each PHYSICAL task root once, however many projects link to it.
        stubs_by_base: dict[str, list[str]] = {}
        owners: dict[str, tuple[ProjectRoot, str, str]] = {}
        for project in sorted(roots, key=_preference):
            for location in DASHBOARD_ROOTS:
                base = project.root / location
                if not base.is_dir():
                    continue
                physical_base = _physical(base)
                if physical_base not in stubs_by_base:
                    try:
                        stubs_by_base[physical_base] = _list_task_stubs(base)
                    except OSError as exc:
                        errors.append(
                            {"code": "unreadable", "message": f"cannot list {base}: {exc}"}
                        )
                        stubs_by_base[physical_base] = []
                for stub in stubs_by_base[physical_base]:
                    folder = base / stub
                    key = _physical(folder)
                    # Projects are visited most-preferred first: first owner wins.
                    owners.setdefault(key, (project, location, stub))

        by_project: dict[str, list[TaskEntry]] = {}
        live_keys = set()
        cores: dict[str, TaskCore] = {}
        pending = []  # (key, project, location, stub, fingerprint) needing a read
        for key, (project, location, stub) in owners.items():
            live_keys.add(key)
            folder = project.root / location / stub
            try:
                fingerprint = _fingerprint(str(folder))
            except OSError as exc:
                errors.append(
                    {"code": "unreadable", "message": f"cannot read task folder {folder}: {exc}"}
                )
                self._cache.pop(key, None)
                live_keys.discard(key)
                continue
            cached = self._cache.get(key)
            if (
                force
                or cached is None
                or cached.fingerprint != fingerprint
                or cached.project_root != str(project.root)
            ):
                pending.append((key, project, location, stub, fingerprint))
            else:
                cores[key] = cached.core

        # Validation shells out to git; read the changed folders in parallel.
        if pending:
            with ThreadPoolExecutor(max_workers=LOAD_WORKERS) as pool:
                futures = [
                    (item, pool.submit(load_task_core, f"{item[2]}/{item[3]}", item[1].root))
                    for item in pending
                ]
                for (key, project, location, stub, fingerprint), future in futures:
                    try:
                        core = future.result()
                    except OSError as exc:
                        folder = project.root / location / stub
                        errors.append(
                            {"code": "unreadable", "message": f"cannot read task folder {folder}: {exc}"}
                        )
                        self._cache.pop(key, None)
                        live_keys.discard(key)
                        continue
                    self.loads += 1
                    self._cache[key] = _CacheEntry(fingerprint, str(project.root), core)
                    cores[key] = core

        if force:
            self._repos.clear()
        repo_names: dict[str, str] = {}
        for key in cores:
            repo_names[str(owners[key][0].root)] = ""
        for root in repo_names:
            repo_names[root] = self._repo_name(root, errors)

        project_names: dict[str, str] = {}
        project_roots: dict[str, set[str]] = {}
        for key, core in cores.items():
            project, location, stub = owners[key]
            root = str(project.root)
            name = repo_names[root]
            pid = repo_identity.repo_id_for(name)
            project_names.setdefault(pid, name)
            project_roots.setdefault(pid, set()).add(root)
            by_project.setdefault(pid, []).append(
                TaskEntry(
                    project_id=pid,
                    project_name=project_names[pid],
                    project_root=root,
                    checkout_id=project_id_for(project.root),
                    location=location,
                    stub=stub,
                    id=f"{location}/{stub}",
                    folder=str(project.root / location / stub),
                    core=core,
                )
            )
        for stale in set(self._cache) - live_keys:
            del self._cache[stale]

        projects = []
        for pid, tasks in by_project.items():
            tasks.sort(key=lambda t: (t.id, t.project_root))
            projects.append(
                ProjectEntry(
                    id=pid,
                    name=project_names[pid],
                    roots=tuple(sorted(project_roots[pid])),
                    tasks=tuple(tasks),
                )
            )
        projects.sort(key=lambda p: (p.name.casefold(), p.name, p.roots))
        self._generation += 1
        return InventorySnapshot(
            revision=_revision(projects, errors),
            generation=self._generation,
            generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            projects=tuple(projects),
            errors=tuple(errors),
        )


    def _repo_name(self, root: str, errors: list[dict]) -> str:
        """The repository name for a checkout, read once; a failure falls back to
        the directory name and is reported on every snapshot until it clears."""
        cached = self._repos.get(root)
        if cached is None:
            try:
                cached = (repo_identity.repo_name_for(Path(root)), None)
            except vcs.GitError as exc:
                cached = (
                    Path(root).name,
                    {
                        "code": "repo-identity",
                        "message": f"cannot read the git remote of {root}; "
                        f"using the directory name: {exc}",
                    },
                )
            self._repos[root] = cached
        if cached[1] is not None:
            errors.append(cached[1])
        return cached[0]


def _revision(projects: list[ProjectEntry], errors: list[dict]) -> str:
    digest = hashlib.sha256()
    for project in projects:
        digest.update(f"P|{project.id}|{project.name}|{'|'.join(project.roots)}\n".encode("utf-8"))
        for task in project.tasks:
            core = task.core
            payload = {
                "id": task.id,
                "root": task.project_root,
                "folder": task.folder,
                "classification": core.classification,
                "title": core.title,
                "priority": core.priority,
                "summary": core.summary,
                "blocked_by": core.blocked_by,
                "updates": [(u.date, u.time, u.detail) for u in core.updates],
                "items": [
                    (i.id, i.title, i.state, i.priority, i.note) for i in core.items
                ],
                "item_errors": core.item_errors,
                "errors": core.errors,
                "warnings": core.warnings,
                "rev": core.revision,
            }
            digest.update(json.dumps(payload, sort_keys=True).encode("utf-8"))
    digest.update(json.dumps(errors, sort_keys=True).encode("utf-8"))
    return digest.hexdigest()[:16]
