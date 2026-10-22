"""Expected-revision, atomic-per-file mutation of a task folder.

The dashboard is the only writer that uses this module; harness writers (the
task.py verbs and the agent editing files) do not take its lock, so every
mutation here defends itself against a concurrent harness edit:

- A task folder's REVISION is a SHA-256 over ``task.yaml``, ``plan.md`` and
  ``log.md`` (the files the dashboard may change).
- :meth:`TaskMutator.mutate` works in two phases under a cross-process lock.
  Phase 1 re-reads the three files, compares the revision with the caller's
  ``expected_revision`` (a caller with no expectation passes ``None`` and gets
  read-modify-write under the lock), builds the new texts, and writes and
  fsyncs EVERY sibling temp file. Phase 2 runs inside the caller's ``gate``
  (a context manager; none by default): ``precommit()`` first, then, file by
  file, a fresh read compared with the bytes the edit was built from and
  ``os.replace``. Neither a lock wait, an edit nor a temp write happens in
  phase 2, so a gate can be held for milliseconds. A difference found by the
  compare is :class:`StaleRevision`, never an overwrite, and a failing replace
  is :class:`MutationError` naming the file. Neither is retried.
- The guarantee is narrower than "harness changes are never overwritten": a
  harness writer takes no lock, so a write that lands between a file's final
  compare and its ``os.replace`` (one read, one digest compare, one rename), or
  an append through a handle opened before the replace, is lost and not
  detected. The final read + compare is the last thing before the replace.
- The three files cannot be replaced as one unit. They are replaced in the
  order plan.md, task.yaml, log.md; a failure part-way names the file and
  leaves the earlier replacements in place (they are individually valid).
  Every :class:`MutationError` carries ``written``, the files already replaced
  (empty when the folder is unchanged).

The pure text helpers below edit one file's text and never touch the disk. The
task_items block is found with the READER's pattern (``YAML_BLOCK_RE``), so the
writer and the reader can never disagree about which fence is the block, and it
is dumped without line wrapping (``dump_items_block``); task.yaml helpers keep
the exact dump call of the task system's own ``update`` verb.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, ContextManager

import yaml
from skills_kit_lib.document_walker import YAML_BLOCK_RE

from .. import oslock
from ..discovery import log_timestamp

TASK_YAML = "task.yaml"
PLAN = "plan.md"
LOG = "log.md"
FILES = (PLAN, TASK_YAML, LOG)  # replacement order
LOCK_TIMEOUT_S = 10.0
LOCK_POLL_S = 0.05

# No-wrap width for the task_items block: a long title stays on one line, as a person writes it.
ITEMS_BLOCK_WIDTH = 1_000_000


class MutationError(RuntimeError):
    """A mutation did not happen; ``code`` is stable and ``path`` names the file.

    ``written`` is the tuple of file names already replaced when this was raised
    (empty when the folder is unchanged) and ``intended`` the files the edit meant to
    replace; :meth:`TaskMutator.mutate` sets both on every error it raises.
    """

    def __init__(self, code: str, message: str, path: Path | None = None, written: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.code = code
        self.path = path
        self.written = tuple(written)
        self.intended: tuple[str, ...] = ()


class StaleRevision(MutationError):
    """The task folder changed since the revision the caller based its edit on."""

    def __init__(self, message: str, path: Path | None = None, current: str | None = None,
                 written: tuple[str, ...] = ()) -> None:
        super().__init__("stale-revision", message, path, written)
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
    """A blocking-with-timeout OS file lock; separate handles exclude each other (see :mod:`task_system.oslock`)."""

    def __init__(self, path: Path, timeout_s: float = LOCK_TIMEOUT_S) -> None:
        self.path = Path(path)
        self.timeout_s = timeout_s
        self._lock = oslock.FileLock(self.path, timeout_s, LOCK_POLL_S)

    def __enter__(self) -> "ProcessLock":
        try:
            self._lock.__enter__()
        except oslock.LockTimeout:
            raise MutationError(
                "lock-timeout",
                f"another dashboard writer held {self.path} for {self.timeout_s:g} seconds",
                self.path,
            ) from None
        return self

    def __exit__(self, *_exc) -> None:
        self._lock.__exit__(*_exc)


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

    def mutate(self, folder: Path, edit: Edit, *, expected_revision: str | None = None,
               gate: ContextManager | None = None, precommit: Callable[[], None] | None = None) -> str:
        """Run ``edit`` and write what it returns; the new revision.

        ``edit`` maps file names (plan.md, task.yaml, log.md) to their new text
        and may return an empty mapping (nothing to write). ``gate`` is entered
        around phase 2 only (``precommit`` and the per-file compare + replace),
        after the process lock; ``precommit`` may raise to refuse the write
        before any file is replaced. Phase 2 runs even when ``edit`` changed
        nothing, so a ``precommit`` side effect is not skipped.
        """
        folder = Path(folder)
        written: list[str] = []
        temps: list[Path] = []
        prepared: list[tuple[Path, Path, str]] = []
        try:
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
                # Phase 1: every temp file is written and fsynced before anything is replaced.
                for name in FILES:
                    if name in changes:
                        text = changes[name].replace("\r\n", "\n")
                        if crlf[name]:
                            text = text.replace("\n", "\r\n")
                        temp = self._write_temp(folder / name, text)
                        temps.append(temp)
                        prepared.append((folder / name, temp, _digest(before[name])))
                # Phase 2: inside the gate, the refusals, then compare + replace file by file.
                with (gate if gate is not None else contextlib.nullcontext()):
                    if precommit is not None:
                        precommit()
                    for path, temp, built_from in prepared:
                        self._replace(path, temp, built_from)
                        written.append(path.name)
                return folder_revision(folder)
        except MutationError as exc:
            exc.written = tuple(written)
            exc.intended = tuple(path.name for path, _temp, _digest_ in prepared)
            raise
        finally:
            for temp in temps:
                try:
                    temp.unlink()
                except OSError:
                    pass

    @staticmethod
    def _write_temp(path: Path, text: str) -> Path:
        temp = path.with_name(f"{path.name}.{os.getpid()}.dash.tmp")
        try:
            with open(temp, "wb") as handle:
                handle.write(text.encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            try:
                temp.unlink()
            except OSError:
                pass
            raise MutationError("replace-failed", f"cannot write {path}: {exc}", path) from exc
        return temp

    @staticmethod
    def _replace(path: Path, temp: Path, built_from: str) -> None:
        """Compare the target with the bytes the edit was built from, then replace it.

        The read and the compare are the last statements before ``os.replace``
        (the residual race is that one read, one compare and one rename wide).
        """
        try:
            try:
                now = path.read_bytes()
            except FileNotFoundError:
                now = None
            if _digest(now) != built_from:
                raise StaleRevision(f"{path} changed while the edit was prepared; it was not overwritten", path)
            os.replace(temp, path)
        except MutationError:
            raise
        except OSError as exc:
            raise MutationError("replace-failed", f"cannot write {path}: {exc}", path) from exc


# -- pure text helpers -------------------------------------------------------


def _items_block(plan: str) -> tuple[re.Match, dict] | None:
    """The fenced block holding ``task_items`` (the reader's recognition: group 1 is its text)."""
    for match in YAML_BLOCK_RE.finditer(plan):
        try:
            data = yaml.safe_load(match.group(1))
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


def dump_items_block(data: dict) -> str:
    """A task_items block body: block style, never wrapped (a title stays on one line)."""
    return yaml.safe_dump(data, sort_keys=False, default_flow_style=False, width=ITEMS_BLOCK_WIDTH, allow_unicode=True)


def _require_block(plan: str) -> tuple[re.Match, dict, list]:
    found = _items_block(plan)
    if found is None:
        raise MutationError("invalid-source", "plan.md has no task_items block to edit")
    match, data = found
    items = data["task_items"].get("items")
    if not isinstance(items, list):
        raise MutationError("invalid-source", "plan.md's task_items block has no items list")
    return match, data, items


def _splice(plan: str, match: re.Match, data: dict) -> str:
    return plan[: match.start(1)] + dump_items_block(data) + plan[match.end(1):]


def _one_item(items: list, item_id: str) -> dict:
    hits = [i for i in items if isinstance(i, dict) and i.get("id") == item_id]
    if len(hits) != 1:
        raise MutationError("invalid-source", f"plan.md has {len(hits)} items with id {item_id!r}; expected exactly one")
    return hits[0]


def set_item(plan: str, item_id: str, *, state: str, note: str | None) -> str:
    """plan.md text with one item's ``state`` and ``note`` replaced.

    The block is re-serialized (YAML comments inside it do not survive, the
    same contract as the task.yaml edits in state_ops); the rest of the file is
    byte-identical. Raises :class:`MutationError` when the block or item is
    missing.
    """
    match, data = _items_block(plan) or (None, None)
    if match is None:
        raise MutationError("invalid-source", "plan.md has no task_items block to edit")
    items = data["task_items"].get("items")
    hit = _one_item(items if isinstance(items, list) else [], item_id)
    hit["state"] = state
    if note is None:
        hit.pop("note", None)
    else:
        hit["note"] = note
    return _splice(plan, match, data)


def set_item_state(plan: str, item_id: str, state: str) -> str:
    """plan.md text with one item's ``state`` replaced and its ``note`` left as it is."""
    match, data, items = _require_block(plan)
    _one_item(items, item_id)["state"] = state
    return _splice(plan, match, data)


def append_item(plan: str, item: dict) -> str:
    """plan.md text with ``item`` appended to the task_items block (the rest is byte-identical)."""
    match, data, items = _require_block(plan)
    if any(isinstance(i, dict) and i.get("id") == item.get("id") for i in items):
        raise MutationError("invalid-source", f"plan.md already has an item with id {item.get('id')!r}")
    items.append(dict(item))
    return _splice(plan, match, data)


def _task_mapping(task_yaml: str) -> dict:
    try:
        data = yaml.safe_load(task_yaml)
    except yaml.YAMLError as exc:
        raise MutationError("invalid-source", f"task.yaml is unparseable: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("task"), dict):
        raise MutationError("invalid-source", "task.yaml has no 'task' mapping")
    return data


def _dump_task_yaml(data: dict) -> str:
    # Exactly the task system's own call (state_ops._write_task_yaml): task.yaml bytes must not differ.
    return yaml.safe_dump(data, sort_keys=False, default_flow_style=False)


def set_task_fields(task_yaml: str, **fields: object) -> str:
    """task.yaml text with ``task.<field>`` replaced (what ``task.py update`` would write)."""
    data = _task_mapping(task_yaml)
    data["task"].update(fields)
    return _dump_task_yaml(data)


def set_task_status(task_yaml: str, status: str) -> str:
    """task.yaml text with ``task.status`` replaced (state_ops' read-modify-write contract)."""
    return set_task_fields(task_yaml, status=status)


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
