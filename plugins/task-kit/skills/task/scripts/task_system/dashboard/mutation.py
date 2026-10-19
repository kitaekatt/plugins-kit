"""Expected-revision, atomic mutation of a task folder.

The dashboard is the only writer that uses this module; harness writers (the
task.py verbs and the agent editing files) do not take its lock, so every
mutation here defends itself against a concurrent harness edit:

- A task folder's REVISION is a SHA-256 over ``task.yaml``, ``plan.md`` and
  ``log.md`` (the files the dashboard may change).
- :meth:`TaskMutator.mutate` takes a cross-process lock, re-reads the three
  files, compares the revision with the caller's ``expected_revision`` (a
  caller with no expectation passes ``None`` and gets read-modify-write under
  the lock), builds the new texts, and replaces each file with a sibling write
  + fsync + ``os.replace``. Immediately before each replace the target is read
  again and compared with the bytes the edit was built from; a difference is
  :class:`StaleRevision`, never an overwrite, and a failing replace is
  :class:`MutationError` naming the file. Neither is retried.
- The three files cannot be replaced as one unit. They are replaced in the
  order plan.md, task.yaml, log.md; a failure part-way names the file and
  leaves the earlier replacements in place (they are individually valid).

The pure text helpers below edit one file's text and never touch the disk.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import yaml

from ..discovery import log_timestamp

TASK_YAML = "task.yaml"
PLAN = "plan.md"
LOG = "log.md"
FILES = (PLAN, TASK_YAML, LOG)  # replacement order
LOCK_TIMEOUT_S = 10.0
LOCK_POLL_S = 0.05

_YAML_FENCE = re.compile(r"^(```[ \t]*ya?ml[^\n]*\n)(.*?)^(```[ \t]*)$", re.MULTILINE | re.DOTALL)


class MutationError(RuntimeError):
    """A mutation did not happen; ``code`` is stable and ``path`` names the file."""

    def __init__(self, code: str, message: str, path: Path | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.path = path


class StaleRevision(MutationError):
    """The task folder changed since the revision the caller based its edit on."""

    def __init__(self, message: str, path: Path | None = None, current: str | None = None) -> None:
        super().__init__("stale-revision", message, path)
        self.current = current


# -- revision ---------------------------------------------------------------


def _digest(data: bytes | None) -> str:
    return hashlib.sha256(b"\0missing" if data is None else b"\1" + data).hexdigest()


def read_files(folder: Path) -> dict[str, bytes | None]:
    """The raw bytes of each mutable file (``None`` when the file is absent)."""
    contents: dict[str, bytes | None] = {}
    for name in FILES:
        try:
            contents[name] = (Path(folder) / name).read_bytes()
        except FileNotFoundError:
            contents[name] = None
        except OSError as exc:
            raise MutationError("unreadable", f"cannot read {Path(folder) / name}: {exc}", Path(folder) / name) from exc
    return contents


def revision_of(contents: dict[str, bytes | None]) -> str:
    digest = hashlib.sha256()
    for name in FILES:
        digest.update(name.encode("ascii") + b"=" + _digest(contents[name]).encode("ascii") + b"\n")
    return digest.hexdigest()[:24]


def folder_revision(folder: Path) -> str:
    return revision_of(read_files(folder))


# -- lock -------------------------------------------------------------------


class ProcessLock:
    """A blocking-with-timeout OS file lock; separate handles exclude each other."""

    def __init__(self, path: Path, timeout_s: float = LOCK_TIMEOUT_S) -> None:
        self.path = Path(path)
        self.timeout_s = timeout_s
        self._handle = None

    def __enter__(self) -> "ProcessLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout_s
        while True:
            handle = open(self.path, "a+b")
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._handle = handle
                return self
            except OSError:
                handle.close()
                if time.monotonic() >= deadline:
                    raise MutationError(
                        "lock-timeout",
                        f"another dashboard writer held {self.path} for {self.timeout_s:g} seconds",
                        self.path,
                    ) from None
                time.sleep(LOCK_POLL_S)

    def __exit__(self, *_exc) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


# -- the mutator ------------------------------------------------------------


@dataclass(frozen=True)
class TaskTexts:
    """The current text of each mutable file ('' when a file is absent)."""

    task_yaml: str
    plan: str
    log: str


Edit = Callable[[TaskTexts], dict[str, str]]


def _text(raw: bytes | None, path: Path) -> str:
    if raw is None:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MutationError("invalid-source", f"{path} is not valid UTF-8: {exc}", path) from exc


class TaskMutator:
    """Applies an edit to one task folder under a lock and an expected revision."""

    def __init__(self, lock_path: Path, *, lock_timeout_s: float = LOCK_TIMEOUT_S) -> None:
        self._lock_path = Path(lock_path)
        self._lock_timeout_s = lock_timeout_s

    @property
    def lock_path(self) -> Path:
        return self._lock_path

    def mutate(self, folder: Path, edit: Edit, *, expected_revision: str | None = None) -> str:
        """Run ``edit`` and write what it returns; the new revision.

        ``edit`` maps file names (plan.md, task.yaml, log.md) to their new text
        and may return an empty mapping (nothing to write).
        """
        folder = Path(folder)
        with ProcessLock(self._lock_path, self._lock_timeout_s):
            before = read_files(folder)
            current = revision_of(before)
            if expected_revision is not None and current != expected_revision:
                raise StaleRevision(
                    f"{folder} changed since revision {expected_revision} (now {current}); "
                    "nothing was written",
                    folder,
                    current,
                )
            # An edit sees LF text whatever the file holds; a file that used CRLF is written back with CRLF.
            crlf = {name: raw is not None and b"\r\n" in raw for name, raw in before.items()}
            texts = TaskTexts(
                task_yaml=_text(before[TASK_YAML], folder / TASK_YAML).replace("\r\n", "\n"),
                plan=_text(before[PLAN], folder / PLAN).replace("\r\n", "\n"),
                log=_text(before[LOG], folder / LOG).replace("\r\n", "\n"),
            )
            changes = edit(texts)
            unknown = set(changes) - set(FILES)
            if unknown:
                raise MutationError("invalid-edit", f"an edit may only change {FILES}, not {sorted(unknown)}")
            for name in FILES:
                if name in changes:
                    text = changes[name].replace("\r\n", "\n")
                    if crlf[name]:
                        text = text.replace("\n", "\r\n")
                    self._replace(folder / name, text, before[name])
            return folder_revision(folder)

    @staticmethod
    def _replace(path: Path, text: str, built_from: bytes | None) -> None:
        temp = path.with_name(f"{path.name}.{os.getpid()}.dash.tmp")
        try:
            with open(temp, "wb") as handle:
                handle.write(text.encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            try:
                now = path.read_bytes()
            except FileNotFoundError:
                now = None
            if _digest(now) != _digest(built_from):
                raise StaleRevision(f"{path} changed while the edit was prepared; it was not overwritten", path)
            os.replace(temp, path)
        except MutationError:
            raise
        except OSError as exc:
            raise MutationError("replace-failed", f"cannot write {path}: {exc}", path) from exc
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass


# -- pure text helpers -------------------------------------------------------


def _items_block(plan: str) -> tuple[re.Match, dict] | None:
    for match in _YAML_FENCE.finditer(plan):
        try:
            data = yaml.safe_load(match.group(2))
        except yaml.YAMLError:
            continue
        if isinstance(data, dict) and isinstance(data.get("task_items"), dict):
            return match, data
    return None


def raw_items(plan: str) -> list[dict] | None:
    """The raw item mappings of plan.md's ``task_items`` block, or None when absent."""
    found = _items_block(plan)
    if found is None:
        return None
    items = found[1]["task_items"].get("items")
    return [dict(i) for i in items if isinstance(i, dict)] if isinstance(items, list) else []


def set_item(plan: str, item_id: str, *, state: str, note: str | None) -> str:
    """plan.md text with one item's ``state`` and ``note`` replaced.

    The block is re-serialized (YAML comments inside it do not survive, the
    same contract as the task.yaml edits in state_ops); the rest of the file is
    byte-identical. Raises :class:`MutationError` when the block or item is
    missing.
    """
    found = _items_block(plan)
    if found is None:
        raise MutationError("invalid-source", "plan.md has no task_items block to edit")
    match, data = found
    items = data["task_items"].get("items")
    hits = [i for i in items if isinstance(i, dict) and i.get("id") == item_id] if isinstance(items, list) else []
    if len(hits) != 1:
        raise MutationError("invalid-source", f"plan.md has {len(hits)} items with id {item_id!r}; expected exactly one")
    hits[0]["state"] = state
    if note is None:
        hits[0].pop("note", None)
    else:
        hits[0]["note"] = note
    body = yaml.safe_dump(data, sort_keys=False, default_flow_style=False)
    return plan[: match.start(2)] + body + plan[match.end(2):]


def set_task_status(task_yaml: str, status: str) -> str:
    """task.yaml text with ``task.status`` replaced (state_ops' read-modify-write contract)."""
    try:
        data = yaml.safe_load(task_yaml)
    except yaml.YAMLError as exc:
        raise MutationError("invalid-source", f"task.yaml is unparseable: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("task"), dict):
        raise MutationError("invalid-source", "task.yaml has no 'task' mapping")
    data["task"]["status"] = status
    return yaml.safe_dump(data, sort_keys=False, default_flow_style=False)


def stored_status(task_yaml: str) -> str | None:
    try:
        data = yaml.safe_load(task_yaml)
    except yaml.YAMLError:
        return None
    if isinstance(data, dict) and isinstance(data.get("task"), dict):
        value = data["task"].get("status")
        return value if isinstance(value, str) else None
    return None


def append_log(log: str, detail: str) -> str:
    """log.md text with one dated entry appended (the task system's entry shape)."""
    detail = " ".join(detail.split())
    prefix = log if (not log or log.endswith("\n")) else log + "\n"
    return f"{prefix}- {log_timestamp()}: {detail}\n"
