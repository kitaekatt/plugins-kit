"""Working-session records: which Claude Code harness session is registered as working a task.

A registration lasts from ``/task work`` until ``/task hand-off``. Harness exit, crash and resume do not
end it; nothing removes a record because its process died. Design and rulings:
``docs/planning/task-kit-dashboard/working-sessions-plan.md``.

Storage. Each machine writes ONE file per physical task root, ``<task root>/.sessions/<host id>.yaml``
(task.yaml is untouched), so the record travels with the tasks' own version control and no two machines
ever write the same file. A machine owns every file carrying its fingerprint (a renamed host still owns
its old file), refuses to write a file named with its host id that carries another fingerprint, and
reads such a file as another machine's. Publication (commit, fetch, push) is ``session_publish``'s; this
module never runs git and never touches the network.

The data directory holds NO session state. ``<data dir>/sessions/locks/`` keeps information-free OS lock
files and nothing else: a registration, a session id, a root or a stub is never written there (distributed
truth, principle 1: git is the single source of truth). Every operation finds its work by scanning the task
roots of the project it is given (:func:`task_roots`) for this machine's own file, matching the session id
(refresh) or the process stamp (clear re-key, register). A session that registered tasks in another project
than the one scanned is not seen from here. Writers take the per-session locks (by id), then the per-root
lock, and never nest otherwise. Each root file is replaced in one atomic write and every operation is
idempotent and rediscovers its work from the files, so an interruption leaves each root either wholly old or
wholly new, and the same operation, re-run, completes the rest.

States of a record, as seen by a reader: ``running`` / ``not running`` (this machine's file, decided by
the exact process stamp) and ``unverified`` (another machine's file).

Callers (the CLI, the SessionStart hook, the dashboard) use this module's functions; none calls another
caller's verb. Preflight of a durable root's publishability is ``session_publish.preflight``'s.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

import yaml

from . import datadir, harness, oslock, procstamp, resolve
from .machine import Machine
from .procstamp import Stamp

SCHEMA_VERSION = 1
SESSIONS_DIRNAME = ".sessions"
LOCAL_DIRNAME = "sessions"
LOCK_TIMEOUT_S = 30.0

STATE_RUNNING = "running"
STATE_NOT_RUNNING = "not running"
STATE_UNVERIFIED = "unverified"

TASK_OPEN = "open"
TASK_CLOSED = "closed"
TASK_ARCHIVED = "archived"
TASK_ABSENT = "absent"
TASK_UNKNOWN = "unknown"
_DROPPED_TASK_STATES = (TASK_CLOSED, TASK_ARCHIVED, TASK_ABSENT)

ACTION_REGISTERED = "registered"
ACTION_REFRESHED = "refreshed"

_SESSION_ID_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_FINGERPRINT = re.compile(r"^[0-9a-f]{16}$")
_OS_INSTANCES = ("macos", "linux", "linux-wsl", "windows")
_FILE_KEYS = ("_schema_version", "host", "machine", "os_instance", "sessions", "dismissed")
_DISMISSAL_KEYS = ("host", "session_id", "registered_at", "dismissed_at")
_RECORD_KEYS = ("session_id", "harness", "task", "checkout", "transcript_path", "registered_at", "process")
_REQUIRED_RECORD_KEYS = tuple(key for key in _RECORD_KEYS if key != "transcript_path")


class SessionsError(RuntimeError):
    """A session operation cannot proceed; the message says why and what to do."""


class HostCollisionError(SessionsError):
    """The file named with this machine's host id carries another machine's fingerprint."""


class InvalidOwnFileError(SessionsError):
    """The file this machine would write does not parse or validate; it is not overwritten."""


class _FormatError(ValueError):
    """A session file that does not follow the format."""


# -- records -----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Record:
    """One registration. The first group is persisted; the second is filled in when a record is read."""

    session_id: str
    task: str
    checkout: str
    registered_at: str
    stamp: Stamp
    recorded_at: str
    harness: str = harness.HARNESS
    transcript_path: str | None = None
    host: str = ""  # the file's host id
    owned: bool = False  # the file carries this machine's fingerprint
    state: str = ""  # running / not running / unverified
    task_state: str | None = None  # open / closed / archived / absent / unknown
    root: Path | None = None  # the physical task root

    def persisted(self) -> dict:
        """The record as file fields, keys in the documented order."""
        data: dict = {"session_id": self.session_id, "harness": self.harness, "task": self.task,
                      "checkout": self.checkout}
        if self.transcript_path is not None:
            data["transcript_path"] = self.transcript_path
        data["registered_at"] = self.registered_at
        process = self.stamp.to_dict()
        process["recorded_at"] = self.recorded_at
        data["process"] = process
        return data

    @property
    def dropped_on_next_write(self) -> bool:
        """True when the task is closed, archived or absent: the owner's next write drops the record."""
        return self.task_state in _DROPPED_TASK_STATES


@dataclass(frozen=True)
class Dismissal:
    """A human's statement that another machine's registration is not to be shown.

    Written to the DISMISSER's own file (``<root>/.sessions/<by>.yaml``, key ``dismissed``), so it travels with
    git like a registration and no machine ever edits another machine's file. It names the target by
    ``(host, session_id, registered_at)``: a later registration (another session id or registered_at) is a new
    record and shows again. Honored by every reader, and by the owner at its next write, whatever its process
    looks like: the human dismissing is a contribution of information.
    """

    host: str  # the machine whose file holds the target record
    session_id: str
    registered_at: str
    dismissed_at: str
    by: str = ""  # the host id of the file this entry is in (filled in when read)

    @property
    def target(self) -> tuple[str, str, str]:
        return (self.host, self.session_id, self.registered_at)

    def persisted(self) -> dict:
        return {"host": self.host, "session_id": self.session_id, "registered_at": self.registered_at,
                "dismissed_at": self.dismissed_at}


@dataclass(frozen=True)
class RootView:
    """Every record of one physical task root (all files) not hidden by a dismissal, plus a note per unreadable file."""

    root: Path
    records: tuple[Record, ...]
    notes: tuple[str, ...]
    dismissals: tuple[Dismissal, ...] = ()


@dataclass(frozen=True)
class TakeoverDecision:
    allowed: bool
    message: str


@dataclass(frozen=True)
class KeptRecord:
    record: Record
    message: str


@dataclass(frozen=True)
class RegisterResult:
    record: Record  # the caller's registration
    action: str  # ACTION_REGISTERED | ACTION_REFRESHED
    root: Path
    taken_over: tuple[Record, ...]  # records replaced under the takeover policy (none while it refuses)
    kept_not_running: tuple[KeptRecord, ...]  # other sessions' not-running records, kept, with the release hint
    others: tuple[Record, ...]  # other records on the task: this machine's running ones and other machines' (unverified)
    also_holds: tuple[str, ...]  # other tasks this session holds registrations for
    dropped: tuple[Record, ...]  # this machine's records on closed, archived or absent tasks, dropped
    changed_files: tuple[Path, ...]  # files written (to publish); empty when nothing changed
    notes: tuple[str, ...]


@dataclass(frozen=True)
class ReleaseResult:
    root: Path
    removed: tuple[Record, ...]
    dropped: tuple[Record, ...]
    changed_files: tuple[Path, ...]
    notes: tuple[str, ...]


@dataclass(frozen=True)
class RefreshResult:
    refreshed: tuple[Record, ...]
    dropped: tuple[Record, ...]
    changed_files: tuple[Path, ...]
    notes: tuple[str, ...]


@dataclass(frozen=True)
class DismissResult:
    root: Path
    dismissal: Dismissal
    target: Record  # the dismissed record, as it was
    changed_files: tuple[Path, ...]  # empty when the same dismissal was already recorded
    notes: tuple[str, ...]


@dataclass(frozen=True)
class ReconcileResult:
    """What a write of this machine's file in one root removed on its own: closed-task records, dismissed records,
    and dismissal entries whose target is gone."""

    root: Path
    dropped: tuple[Record, ...]  # this machine's records on closed, archived or absent tasks
    dismissed: tuple[Record, ...]  # this machine's records some machine dismissed
    tombstones: tuple[Dismissal, ...]  # this machine's dismissal entries whose target record no longer exists
    changed_files: tuple[Path, ...]
    notes: tuple[str, ...]


@dataclass(frozen=True)
class RekeyResult:
    previous_ids: tuple[str, ...]
    rekeyed: tuple[Record, ...]
    dropped: tuple[Record, ...]
    changed_files: tuple[Path, ...]
    notes: tuple[str, ...]


# -- policy ------------------------------------------------------------------------------------------------


def not_running_takeover_policy(record: Record, ref: str) -> TakeoverDecision:
    """May ``/task work`` replace ANOTHER session's registration whose process is not running?

    This is the one place that decides it (OPEN user question Q2 in the plan). It currently REFUSES: a
    registration lasts until hand-off, nothing ends one because its process died, so the record is kept and
    only an explicit, named release removes it. To relax the policy, return ``TakeoverDecision(True, ...)``
    here; the callers already replace the record and report it as taken over.
    """
    return TakeoverDecision(
        False,
        f"session {record.session_id[:8]} registered {record.task} on {record.host} and its process is not "
        "running; its registration is kept (a registration ends only at hand-off). To remove it, release it "
        f"explicitly: task sessions release {ref} --all-local")


# -- small helpers -----------------------------------------------------------------------------------------


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_home_relative(path: str | os.PathLike, home: Path | None = None) -> str | None:
    """``~/...`` for a path under the home directory; None for a path outside it (it is not recorded)."""
    home_dir = Path(os.path.expanduser("~")) if home is None else Path(home)
    found = Path(path)
    for base in (home_dir, home_dir.resolve()):
        for candidate in (found, found.resolve()):
            try:
                relative = candidate.relative_to(base)
            except ValueError:
                continue
            return "~/" + relative.as_posix()
    return None


def _check_session_id(session_id: str) -> str:
    if not isinstance(session_id, str) or not _SESSION_ID_SAFE.match(session_id):
        raise SessionsError(f"the session id {session_id!r} cannot be recorded")
    return session_id


def _sessions_dir(data_dir: Path | None) -> Path:
    base = datadir.resolve_data_dir() if data_dir is None else Path(data_dir)
    return base / LOCAL_DIRNAME


def _session_lock(local: Path, session_id: str) -> oslock.FileLock:
    return oslock.FileLock(local / "locks" / f"session-{_check_session_id(session_id)}.lock", LOCK_TIMEOUT_S)


def _root_key(root: Path) -> str:
    return os.path.normcase(os.path.realpath(root))


def _root_lock(local: Path, root: Path) -> oslock.FileLock:
    digest = hashlib.sha256(_root_key(root).encode("utf-8")).hexdigest()[:16]
    return oslock.FileLock(local / "locks" / f"root-{digest}.lock", LOCK_TIMEOUT_S)


@contextlib.contextmanager
def _locked(local: Path, session_ids: Iterable[str], roots: Iterable[Path] = ()):
    """The session locks by id, then the root locks by path; a timeout becomes a :class:`SessionsError`."""
    ordered_ids = sorted(set(session_ids))
    ordered_roots = sorted({_root_key(root): root for root in roots}.items())
    with contextlib.ExitStack() as stack:
        try:
            for session_id in ordered_ids:
                stack.enter_context(_session_lock(local, session_id))
            for _key, root in ordered_roots:
                stack.enter_context(_root_lock(local, root))
        except oslock.LockTimeout as exc:
            raise SessionsError(f"another task-kit process holds a session lock: {exc}") from exc
        yield


def _checkpoint(phase: str) -> None:
    """A named point between two phases of a multi-step write. A no-op; the interruption tests replace it."""


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _physical_root(project_root: Path, location: str) -> Path:
    root = Path(project_root) / location
    if not root.is_dir():
        raise SessionsError(f"the task root {root} does not exist")
    return Path(os.path.realpath(root))


def task_roots(project_root: Path) -> tuple[Path, ...]:
    """The physical task roots of a project that exist (``dev/tasks``, ``tmp``), deduplicated."""
    found: dict[str, Path] = {}
    for location in resolve.KNOWN_ROOTS:
        candidate = Path(project_root) / location
        if candidate.is_dir():
            found.setdefault(_root_key(candidate), Path(os.path.realpath(candidate)))
    return tuple(found.values())


def _task_state(root: Path, stub: str) -> str:
    """open / closed / archived / absent / unknown for ``<root>/<stub>``; unknown is never dropped."""
    folder = root / stub
    if not folder.is_dir():
        return TASK_ABSENT
    try:
        data = yaml.safe_load((folder / "task.yaml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return TASK_UNKNOWN
    block = data.get("task") if isinstance(data, dict) else None
    status = block.get("status") if isinstance(block, dict) else None
    if status == TASK_CLOSED:
        return TASK_CLOSED
    if status == TASK_ARCHIVED:
        return TASK_ARCHIVED
    return TASK_OPEN if isinstance(status, str) else TASK_UNKNOWN


# -- the file format ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _FileData:
    host: str
    machine: str
    os_instance: str
    records: tuple[Record, ...]
    dismissals: tuple[Dismissal, ...] = ()


def _parse_dismissal(raw: object, index: int, by: str) -> Dismissal:
    where = f"dismissed[{index}]"
    if not isinstance(raw, dict):
        raise _FormatError(f"{where} is not a mapping")
    unknown = sorted(set(raw) - set(_DISMISSAL_KEYS))
    if unknown:
        raise _FormatError(f"{where} has unknown keys {unknown}")
    missing = [key for key in _DISMISSAL_KEYS if key not in raw]
    if missing:
        raise _FormatError(f"{where} lacks {missing}")
    session_id = _require_str(raw, "session_id", where)
    if not _SESSION_ID_SAFE.match(session_id):
        raise _FormatError(f"{where}: session_id {session_id!r} is not a valid session id")
    for key in ("registered_at", "dismissed_at"):
        if not _TIMESTAMP.match(_require_str(raw, key, where)):
            raise _FormatError(f"{where}: {key} must be a UTC timestamp like 2026-10-21T18:04:11Z")
    return Dismissal(_require_str(raw, "host", where), session_id, raw["registered_at"], raw["dismissed_at"], by)


def _require_str(data: Mapping, key: str, where: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _FormatError(f"{where}: {key} must be a non-empty string")
    return value


def _parse_record(raw: object, index: int) -> Record:
    where = f"sessions[{index}]"
    if not isinstance(raw, dict):
        raise _FormatError(f"{where} is not a mapping")
    unknown = sorted(set(raw) - set(_RECORD_KEYS))
    if unknown:
        raise _FormatError(f"{where} has unknown keys {unknown}")
    missing = [key for key in _REQUIRED_RECORD_KEYS if key not in raw]
    if missing:
        raise _FormatError(f"{where} lacks {missing}")
    session_id = _require_str(raw, "session_id", where)
    if not _SESSION_ID_SAFE.match(session_id):
        raise _FormatError(f"{where}: session_id {session_id!r} is not a valid session id")
    transcript = raw.get("transcript_path")
    if transcript is not None and (not isinstance(transcript, str) or not transcript.startswith("~/")):
        raise _FormatError(f"{where}: transcript_path must be home-relative (~/...)")
    registered = _require_str(raw, "registered_at", where)
    if not _TIMESTAMP.match(registered):
        raise _FormatError(f"{where}: registered_at must be a UTC timestamp like 2026-10-21T18:04:11Z")
    process = raw["process"]
    if not isinstance(process, dict):
        raise _FormatError(f"{where}: process must be a mapping")
    process = dict(process)
    recorded = process.pop("recorded_at", None)
    if not isinstance(recorded, str) or not _TIMESTAMP.match(recorded):
        raise _FormatError(f"{where}: process.recorded_at must be a UTC timestamp")
    try:
        stamp = Stamp.from_dict(process)
    except procstamp.StampError as exc:
        raise _FormatError(f"{where}: process: {exc}") from exc
    return Record(session_id=session_id, task=_require_str(raw, "task", where),
                  checkout=_require_str(raw, "checkout", where), registered_at=registered, stamp=stamp,
                  recorded_at=recorded, harness=_require_str(raw, "harness", where), transcript_path=transcript)


def _parse_file(text: str, stem: str) -> _FileData:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise _FormatError(f"not valid YAML: {str(exc).splitlines()[0] if str(exc) else 'parse error'}") from exc
    if not isinstance(data, dict):
        raise _FormatError("the top level is not a mapping")
    unknown = sorted(set(data) - set(_FILE_KEYS))
    if unknown:
        raise _FormatError(f"unknown keys {unknown}")
    if data.get("_schema_version") != SCHEMA_VERSION or isinstance(data.get("_schema_version"), bool):
        raise _FormatError(f"_schema_version is {data.get('_schema_version')!r}, expected {SCHEMA_VERSION}")
    host = _require_str(data, "host", "file")
    if host != stem:
        raise _FormatError(f"host {host!r} does not equal the file name {stem!r}")
    machine_fp = _require_str(data, "machine", "file")
    if not _FINGERPRINT.match(machine_fp):
        raise _FormatError(f"machine {machine_fp!r} is not a 16-digit hex fingerprint")
    os_instance = _require_str(data, "os_instance", "file")
    if os_instance not in _OS_INSTANCES:
        raise _FormatError(f"os_instance {os_instance!r} is not one of {list(_OS_INSTANCES)}")
    raw_records = data.get("sessions")
    if not isinstance(raw_records, list):
        raise _FormatError("sessions must be a list")
    records = tuple(_parse_record(raw, i) for i, raw in enumerate(raw_records))
    raw_dismissed = data.get("dismissed", [])
    if not isinstance(raw_dismissed, list):
        raise _FormatError("dismissed must be a list")
    dismissals = tuple(_parse_dismissal(raw, i, host) for i, raw in enumerate(raw_dismissed))
    seen: set[tuple[str, str]] = set()
    for record in records:
        key = (record.task, record.session_id)
        if key in seen:
            raise _FormatError(f"task {record.task} lists session {record.session_id[:8]} twice")
        seen.add(key)
    return _FileData(host, machine_fp, os_instance, records, dismissals)


def _sorted(records: Iterable[Record]) -> list[Record]:
    return sorted(records, key=lambda record: (record.task, record.session_id))


def _serialize(host: str, fingerprint: str, os_instance: str, records: Iterable[Record],
               dismissals: Iterable[Dismissal] = ()) -> str:
    document = {"_schema_version": SCHEMA_VERSION, "host": host, "machine": fingerprint, "os_instance": os_instance,
                "sessions": [record.persisted() for record in _sorted(records)]}
    ordered = sorted(dismissals, key=lambda d: d.target)
    if ordered:  # absent when empty, so a file without dismissals is byte-identical to one written before the key existed
        document["dismissed"] = [d.persisted() for d in ordered]
    return yaml.safe_dump(document, sort_keys=False, default_flow_style=False)


@dataclass(frozen=True)
class _Scanned:
    path: Path
    data: _FileData | None
    error: str | None

    @property
    def stem(self) -> str:
        return self.path.stem


def _scan_root(root: Path) -> list[_Scanned]:
    directory = Path(root) / SESSIONS_DIRNAME
    if not directory.is_dir():
        return []
    scanned = []
    for path in sorted(directory.glob("*.yaml")):
        try:
            data, error = _parse_file(path.read_text(encoding="utf-8"), path.stem), None
        except _FormatError as exc:
            data, error = None, str(exc)
        except (OSError, UnicodeDecodeError) as exc:
            data, error = None, f"unreadable: {exc}"
        scanned.append(_Scanned(path, data, error))
    return scanned


def _dismissals_of(scanned: Iterable[_Scanned]) -> tuple[Dismissal, ...]:
    return tuple(d for item in scanned if item.data is not None for d in item.data.dismissals)


def _owns(scanned: _Scanned, machine: Machine) -> bool:
    return scanned.data is not None and scanned.data.machine == machine.fingerprint


def _state_of(record: Record, owned: bool, notes: list[str]) -> str:
    if not owned:
        return STATE_UNVERIFIED
    try:
        return STATE_RUNNING if procstamp.is_running(record.stamp) else STATE_NOT_RUNNING
    except procstamp.StampError as exc:
        notes.append(f"the process of session {record.session_id[:8]} ({record.task}) cannot be checked: {exc}")
        return STATE_UNVERIFIED


def _decorate(scanned: list[_Scanned], root: Path, machine: Machine, check_tasks: bool,
              notes: list[str]) -> list[Record]:
    task_states: dict[str, str] = {}
    out = []
    for item in scanned:
        if item.data is None:
            continue
        owned = _owns(item, machine)
        for record in item.data.records:
            task_state = None
            if check_tasks:
                task_state = task_states.setdefault(record.task, _task_state(root, record.task))
            out.append(dataclasses.replace(record, host=item.data.host, owned=owned, root=root, task_state=task_state,
                                           state=_state_of(record, owned, notes)))
    return out


def read_root(task_root: Path, machine: Machine, *, check_tasks: bool = True) -> RootView:
    """Every record of every ``.sessions`` file under one physical task root, with states; never writes.

    A file that does not parse is a note and its records are ignored. ``check_tasks`` fills ``task_state``
    (a record on a closed, archived or absent task is shown as such until its owner's next write drops it).
    """
    root = Path(task_root)
    scanned = _scan_root(root)
    notes = [f"{item.path.name}: not a valid session file ({item.error}); its records are ignored"
             for item in scanned if item.error is not None]
    records = _decorate(scanned, root, machine, check_tasks, notes)
    dismissals = _dismissals_of(scanned)
    targets = {d.target for d in dismissals}
    records = [r for r in records if (r.host, r.session_id, r.registered_at) not in targets]
    return RootView(root, tuple(_sorted(records)), tuple(notes), dismissals)


def read_project(project_root: Path, machine: Machine, *, check_tasks: bool = True) -> tuple[RootView, ...]:
    """:func:`read_root` over each of the project's physical task roots."""
    return tuple(read_root(root, machine, check_tasks=check_tasks) for root in task_roots(project_root))


# -- the root edit: read-modify-write of this machine's files under one root -------------------------------


class _RootEdit:
    """This machine's files of one root, loaded under the root lock; :meth:`commit` writes what changed."""

    def __init__(self, root: Path, machine: Machine) -> None:
        self.root = Path(root)
        self.machine = machine
        self.notes: list[str] = []
        scanned = _scan_root(self.root)
        self.target = self.root / SESSIONS_DIRNAME / f"{machine.host}.yaml"
        self.files: dict[Path, list[Record]] = {}
        self.dismissals: dict[Path, list[Dismissal]] = {}
        self._headers: dict[Path, _FileData] = {}
        self.foreign: list[Record] = []
        targets = {d.target for d in _dismissals_of(scanned)}
        for item in scanned:
            if item.path == self.target:
                if item.data is None:
                    raise InvalidOwnFileError(
                        f"{item.path} does not parse or validate ({item.error}); it is not overwritten. "
                        "Repair or remove it, then retry")
                if item.data.machine != machine.fingerprint:
                    raise HostCollisionError(
                        f"{item.path} carries another machine's fingerprint ({item.data.machine}); this machine "
                        f"({machine.fingerprint}) will not write a file named {machine.host}.yaml. Two machines "
                        "share the host id; set sessions.host in ~/.claude/task.local.yaml on one of them")
            if item.data is None:
                self.notes.append(f"{item.path.name}: not a valid session file ({item.error}); its records are ignored")
            elif item.data.machine == machine.fingerprint:
                self.files[item.path] = list(item.data.records)
                self.dismissals[item.path] = list(item.data.dismissals)
                self._headers[item.path] = item.data
            else:
                self.foreign.extend(dataclasses.replace(r, host=item.data.host, owned=False, root=self.root,
                                                        state=STATE_UNVERIFIED) for r in item.data.records
                                    if (item.data.host, r.session_id, r.registered_at) not in targets)
        self.target_existed = self.target in self.files
        self.files.setdefault(self.target, [])
        self.dismissals.setdefault(self.target, [])
        self._original = {path: list(records) for path, records in self.files.items()}
        self._original_dismissals = {path: list(entries) for path, entries in self.dismissals.items()}
        self._task_states: dict[str, str] = {}
        # every record of another machine's file, dismissed or not (a dismissed record is still the target of its tombstone)
        self._foreign_keys = {(item.data.host, r.session_id, r.registered_at) for item in scanned
                              if item.data is not None and item.data.machine != machine.fingerprint
                              for r in item.data.records}
        self._all_valid = all(item.data is not None for item in scanned)
        self.dismissed_away: list[tuple[Path, Record]] = []
        self.tombstones_dropped: list[Dismissal] = []
        self._reconcile(targets)

    def _reconcile(self, targets: set) -> None:
        """Drop this machine's records that some machine dismissed, then its dismissal entries whose target is gone.

        The owner honors a dismissal whatever its record's process looks like; a session that is in fact alive
        registers again at its next ``/task work`` (a new ``registered_at``, so a new record). Tombstones do not
        accumulate: an entry goes once its target record is no longer in the target host's file. A tree with an
        unreadable session file keeps every tombstone (the target may live in that file).
        """
        for path in list(self.files):
            host = self._headers[path].host if path in self._headers else path.stem
            for record in list(self.files[path]):
                if (host, record.session_id, record.registered_at) in targets:
                    self.files[path].remove(record)
                    self.dismissed_away.append((path, record))
        if not self._all_valid:
            return
        existing = set(self._foreign_keys)
        for path, records in self.files.items():
            host = self._headers[path].host if path in self._headers else path.stem
            existing.update((host, r.session_id, r.registered_at) for r in records)
        for path in list(self.dismissals):
            for entry in list(self.dismissals[path]):
                if entry.target not in existing:
                    self.dismissals[path].remove(entry)
                    self.tombstones_dropped.append(entry)

    def own_records(self) -> list[tuple[Path, Record]]:
        return [(path, record) for path, records in self.files.items() for record in records]

    def task_state(self, stub: str) -> str:
        if stub not in self._task_states:
            self._task_states[stub] = _task_state(self.root, stub)
        return self._task_states[stub]

    def remove(self, path: Path, record: Record) -> None:
        self.files[path].remove(record)

    def replace(self, path: Path, old: Record, new: Record) -> None:
        records = self.files[path]
        records[records.index(old)] = new

    def add(self, record: Record) -> None:
        self.files[self.target].append(record)

    def sweep(self, protect: Iterable[str] = ()) -> list[tuple[Path, Record]]:
        """Drop this machine's records on closed, archived or absent tasks (never those in ``protect``)."""
        protected = set(protect)
        dropped = []
        for path, record in self.own_records():
            if record.task not in protected and self.task_state(record.task) in _DROPPED_TASK_STATES:
                self.remove(path, record)
                dropped.append((path, record))
        return dropped

    def _text(self, path: Path, records: Iterable[Record], dismissals: Iterable[Dismissal] = ()) -> str:
        header = self._headers.get(path)
        host = header.host if header is not None else path.stem
        return _serialize(host, self.machine.fingerprint, self.machine.os_instance, records, dismissals)

    def commit(self) -> tuple[Path, ...]:
        """Write every file whose record set changed (an unchanged set is never rewritten)."""
        written = []
        for path, records in self.files.items():
            existed = path in self._headers
            entries = self.dismissals.get(path, [])
            if not existed and not records and not entries:
                continue
            if existed and self._text(path, self._original[path], self._original_dismissals.get(path, [])) \
                    == self._text(path, records, entries):
                continue
            if not existed and path != self.target:
                continue
            _atomic_write(path, self._text(path, records, entries))
            written.append(path)
        return tuple(written)


def _decorated(edit: _RootEdit, pairs: Iterable[tuple[Path, Record]], notes: list[str]) -> list[Record]:
    """Own records (with the file each came from) filled in for a result: host, state, task state, root."""
    return [dataclasses.replace(r, owned=True, root=edit.root, host=path.stem, task_state=edit.task_state(r.task),
                                state=_state_of(r, True, notes))
            for path, r in pairs]


# -- finding a session's registrations in the files ----------------------------------------------------------


def _own_session_records(root: Path, machine: Machine, session_id: str) -> list[Record]:
    return [r for item in _scan_root(root) if _owns(item, machine) for r in item.data.records
            if r.session_id == session_id]


def _own_session_holdings(project_root: Path, machine: Machine, session_id: str) -> list[tuple[Path, str]]:
    """The ``(physical root, stub)`` pairs the project's root files hold for ``session_id`` on this machine."""
    return [(root, record.task) for root in task_roots(project_root)
            for record in _own_session_records(root, machine, session_id)]


# -- operations --------------------------------------------------------------------------------------------


def _resolve_task(project_root: Path, ref: str) -> tuple[Path, str]:
    try:
        resolved = resolve.resolve_ref(ref, Path(project_root))
    except resolve.RefResolutionError as exc:
        raise SessionsError(str(exc)) from exc
    return _physical_root(project_root, resolved.location), resolved.stub


def _resolve_task_for_release(project_root: Path, ref: str, machine: Machine) -> tuple[Path, str]:
    """Like :func:`_resolve_task`, but a bare stub whose folder is gone resolves through this machine's own records."""
    try:
        return _resolve_task(project_root, ref)
    except SessionsError:
        if "/" in ref or Path(ref).is_absolute():
            raise
        holders = [root for root in task_roots(project_root)
                   if any(r.task == ref for item in _scan_root(root) if _owns(item, machine) for r in item.data.records)]
        if len(holders) != 1:
            raise
        return holders[0], ref


def register(identity: harness.Identity, machine: Machine, project_root: Path, ref: str, *,
             data_dir: Path | None = None, transcript_path: str | None = None,
             env: Mapping[str, str] | None = None) -> RegisterResult:
    """Register the calling session as working the task ``ref``, after ``work``'s gate passed.

    Same session id, or a record whose process stamp equals the caller's (a ``/clear`` whose hook did not run),
    is refreshed in place. Another session's RUNNING record is kept and reported (both show). Another session's
    NOT-RUNNING record follows :func:`not_running_takeover_policy` (currently: kept, with the release hint).
    Another machine's records are reported and never edited. ``transcript_path`` is the home-relative path a
    hook supplied; None looks it up under the Claude config directory.
    """
    root, stub = _resolve_task(project_root, ref)
    if not (root / stub).is_dir():
        raise SessionsError(f"the task folder {root / stub} does not exist")
    session_id = _check_session_id(identity.session_id)
    local = _sessions_dir(data_dir)
    notes: list[str] = []
    if transcript_path is None:
        lookup = harness.find_transcript(session_id, env)
        transcript_path = lookup.path
        if lookup.note:
            notes.append(lookup.note)
    checkout = os.path.basename(os.path.abspath(project_root))
    now = utc_now()

    prior = [r for item in _scan_root(root) if _owns(item, machine) for r in item.data.records if r.task == stub]
    lineage = {r.session_id for r in prior if r.session_id != session_id
               and procstamp.same_process(r.stamp, identity.stamp)}
    ids = {session_id} | lineage

    taken_over: list[Record] = []
    kept: list[KeptRecord] = []
    others: list[Record] = []
    with _locked(local, ids):
        with _locked(local, (), (root,)):
            edit = _RootEdit(root, machine)
            notes.extend(edit.notes)
            mine = [(p, r) for p, r in edit.own_records() if r.task == stub]
            same_session = [(p, r) for p, r in mine if r.session_id == session_id]
            same_process = [(p, r) for p, r in mine if r.session_id != session_id
                            and procstamp.same_process(r.stamp, identity.stamp)]
            base = (same_session or same_process or [None])[0]
            action = ACTION_REFRESHED if base else ACTION_REGISTERED
            record = Record(
                session_id=session_id, task=stub, checkout=checkout, stamp=identity.stamp, recorded_at=now,
                registered_at=min([r.registered_at for _p, r in same_session + same_process], default=now),
                transcript_path=transcript_path or (base[1].transcript_path if base else None))
            placed = base[0] if base else edit.target
            for path, old in same_session + same_process:
                edit.remove(path, old)
            edit.files[placed].append(record)
            for path, raw in mine:
                if (path, raw) in same_session or (path, raw) in same_process:
                    continue
                other = _decorated(edit, [(path, raw)], notes)[0]
                if other.state != STATE_NOT_RUNNING:  # running, or a process table that could not be read
                    others.append(other)
                    continue
                decision = not_running_takeover_policy(other, ref)
                if decision.allowed:
                    edit.remove(path, raw)
                    taken_over.append(other)
                else:
                    kept.append(KeptRecord(other, decision.message))
            others.extend(dataclasses.replace(r, task_state=edit.task_state(r.task))
                          for r in edit.foreign if r.task == stub)
            dropped = _decorated(edit, edit.sweep(protect=(stub,)), notes)
            _checkpoint("before_write")
            changed = edit.commit()
            _checkpoint("root_written")
        also_holds = tuple(s for r, s in _own_session_holdings(project_root, machine, session_id)
                           if (r, s) != (root, stub))
    final = _decorated(edit, [(placed, record)], notes)[0]
    return RegisterResult(final, action, root, tuple(taken_over), tuple(kept), tuple(others), also_holds,
                          tuple(dropped), changed, tuple(notes))


def release(identity: harness.Identity | None, machine: Machine, project_root: Path, ref: str, *,
            all_local: bool = False, data_dir: Path | None = None) -> ReleaseResult:
    """Remove this machine's registrations of ``ref``: the caller's (its session id or process stamp), or with
    ``all_local`` every one this machine holds for the task (the human's explicit cleanup; no identity needed).
    Everything is read from, and written to, the root's file.
    """
    if identity is None and not all_local:
        raise SessionsError("releasing the caller's registration needs the caller's session identity "
                            "(run it inside the Claude Code session, or pass --all-local)")
    root, stub = _resolve_task_for_release(project_root, ref, machine)
    local = _sessions_dir(data_dir)
    notes: list[str] = []

    def matches(record: Record) -> bool:
        if all_local:
            return True
        return record.session_id == identity.session_id or procstamp.same_process(record.stamp, identity.stamp)

    prior = [r for item in _scan_root(root) if _owns(item, machine) for r in item.data.records
             if r.task == stub and matches(r)]
    ids = {r.session_id for r in prior} | ({identity.session_id} if identity is not None else set())
    with _locked(local, ids):
        with _locked(local, (), (root,)):
            edit = _RootEdit(root, machine)
            notes.extend(edit.notes)
            removed = []
            for path, record in edit.own_records():
                if record.task == stub and matches(record):
                    edit.remove(path, record)
                    removed.append((path, record))
            dropped = edit.sweep()
            _checkpoint("before_write")
            changed = edit.commit()
            decorated = _decorated(edit, removed, notes)
            dropped = _decorated(edit, dropped, notes)
            _checkpoint("root_written")
    return ReleaseResult(root, tuple(decorated), tuple(dropped), changed, tuple(notes))


def refresh(identity: harness.Identity, machine: Machine, transcript_path: str | None = None, *,
            project_root: Path, data_dir: Path | None = None) -> RefreshResult:
    """A resumed session (same id, new process): give every record of this machine carrying the session id the
    new process stamp and transcript path. The records are found in the ``project_root``'s task roots (a
    registration made in another project is not seen from here). Writes nothing when this machine holds no
    such record or nothing differs. ``transcript_path`` is home-relative (see :func:`to_home_relative`); None
    keeps the recorded one.
    """
    session_id = _check_session_id(identity.session_id)
    local = _sessions_dir(data_dir)
    notes: list[str] = []
    refreshed: list[Record] = []
    dropped: list[Record] = []
    changed: list[Path] = []
    roots = [root for root in task_roots(project_root) if _own_session_records(root, machine, session_id)]
    if not roots:
        return RefreshResult((), (), (), ())
    now = utc_now()
    with _locked(local, {session_id}):
        for root in roots:
            with _locked(local, (), (root,)):
                edit = _RootEdit(root, machine)
                notes.extend(edit.notes)
                touched = []
                for path, record in edit.own_records():
                    if record.session_id != session_id:
                        continue
                    new_path = transcript_path if transcript_path is not None else record.transcript_path
                    if procstamp.same_process(record.stamp, identity.stamp) and record.stamp == identity.stamp \
                            and new_path == record.transcript_path:
                        continue
                    new = dataclasses.replace(record, stamp=identity.stamp, recorded_at=now, transcript_path=new_path)
                    edit.replace(path, record, new)
                    touched.append((path, new))
                dropped.extend(_decorated(edit, edit.sweep(), notes))
                _checkpoint("before_write")
                changed.extend(edit.commit())
                refreshed.extend(_decorated(edit, touched, notes))
                _checkpoint("root_written")
    return RefreshResult(tuple(refreshed), tuple(dropped), tuple(changed), tuple(notes))


def rekey_after_clear(identity: harness.Identity, machine: Machine, transcript_path: str | None = None, *,
                      project_root: Path, data_dir: Path | None = None) -> RekeyResult:
    """After ``/clear`` (new session id, same process): move EVERY registration of the calling process to the
    new session id, in one idempotent operation.

    The registrations are this machine's records, in the ``project_root``'s task roots, whose process stamp is
    the caller's and whose session id is not the new one (a registration made in another project is not seen
    from here). Each root's matching records are rewritten under that root's lock, one atomic file write per
    root. A run interrupted between roots leaves the rest still carrying the process stamp, so the same
    operation, re-run, finds and completes them. ``transcript_path`` (the post-clear transcript, home-relative)
    is recorded when given.
    """
    new_id = _check_session_id(identity.session_id)
    local = _sessions_dir(data_dir)
    notes: list[str] = []

    def moves(record: Record) -> bool:
        return record.session_id != new_id and procstamp.same_process(record.stamp, identity.stamp)

    found: list[tuple[Path, set[str]]] = []
    for root in task_roots(project_root):
        ids = {r.session_id for item in _scan_root(root) if _owns(item, machine) for r in item.data.records
               if moves(r)}
        if ids:
            found.append((root, ids))
    previous = sorted({sid for _root, ids in found for sid in ids})
    if not previous:
        return RekeyResult((), (), (), (), tuple(notes))
    now = utc_now()
    rekeyed: list[Record] = []
    dropped: list[Record] = []
    changed: list[Path] = []
    with _locked(local, {*previous, new_id}):
        for number, (root, _ids) in enumerate(found):
            with _locked(local, (), (root,)):
                edit = _RootEdit(root, machine)
                notes.extend(edit.notes)
                touched = []
                for path, record in edit.own_records():
                    if moves(record):
                        new = dataclasses.replace(record, session_id=new_id, stamp=identity.stamp, recorded_at=now,
                                                  transcript_path=transcript_path or record.transcript_path)
                        edit.replace(path, record, new)
                        touched.append((path, new))
                _merge_duplicates(edit)
                dropped.extend(_decorated(edit, edit.sweep(), notes))
                _checkpoint(f"before_write:{number}")
                changed.extend(edit.commit())
                rekeyed.extend(_decorated(edit, [(p, r) for p, r in touched if r in edit.files[p]], notes))
                _checkpoint(f"root_written:{number}")
    return RekeyResult(tuple(previous), tuple(rekeyed), tuple(dropped), tuple(changed), tuple(notes))


def dismiss(machine: Machine, project_root: Path, target_host: str, session_id: str, registered_at: str, *,
            data_dir: Path | None = None) -> DismissResult:
    """Record, in THIS machine's own file, that the human dismissed another machine's registration.

    ``(target_host, session_id, registered_at)`` names a record in some other machine's file under one of the
    project's task roots. The entry (``host``, ``session_id``, ``registered_at``, ``dismissed_at``) goes into
    ``<root>/.sessions/<this host>.yaml`` under the root lock; the caller publishes it like any record. Readers
    hide the record at once; its owner drops it at its next write, even when the process looks alive, and this
    machine drops the entry once the record is gone. Another machine's file is never edited. Dismissing the same
    record twice writes nothing the second time; a record of this machine is released, not dismissed.
    """
    session_id = _check_session_id(session_id)
    if not _TIMESTAMP.match(registered_at or ""):
        raise SessionsError(f"registered_at {registered_at!r} is not a UTC timestamp like 2026-10-21T18:04:11Z")
    local = _sessions_dir(data_dir)
    notes: list[str] = []
    for root in task_roots(project_root):
        found = [(item, r) for item in _scan_root(root) if item.data is not None and item.data.host == target_host
                 for r in item.data.records if r.session_id == session_id and r.registered_at == registered_at]
        if not found:
            continue
        item, record = found[0]
        if _owns(item, machine):
            raise SessionsError(f"session {session_id[:8]} on {target_host} is this machine's own registration; "
                                f"release it (task sessions release {record.task} --all-local), do not dismiss it")
        with _locked(local, (), (root,)):
            edit = _RootEdit(root, machine)
            notes.extend(edit.notes)
            entry = Dismissal(target_host, session_id, registered_at, utc_now(), machine.host)
            existing = [d for d in edit.dismissals[edit.target] if d.target == entry.target]
            if not existing:
                edit.dismissals[edit.target].append(entry)
            changed = edit.commit()
            _checkpoint("root_written")
        shown = dataclasses.replace(record, host=target_host, owned=False, root=root, state=STATE_UNVERIFIED)
        return DismissResult(root, existing[0] if existing else entry, shown, changed, tuple(notes))
    raise SessionsError(f"no registration of session {session_id[:8]} on {target_host} (registered {registered_at}) "
                        f"under {Path(project_root)}'s task roots; it may already be gone")


def reconcile(machine: Machine, task_root: Path, *, data_dir: Path | None = None) -> ReconcileResult:
    """Apply the owner-side cleanups to this machine's file in one root, without any registration change.

    The same cleanups every write performs: drop this machine's records on closed, archived or absent tasks, its
    records some machine dismissed, and its dismissal entries whose target record is gone. Nothing is dropped
    because a process is not running. Writes only when something changed; the caller publishes.
    """
    root = Path(task_root)
    local = _sessions_dir(data_dir)
    with _locked(local, (), (root,)):
        edit = _RootEdit(root, machine)
        dropped = edit.sweep()
        changed = edit.commit()
        _checkpoint("root_written")
    notes = list(edit.notes)
    return ReconcileResult(root, tuple(r for _p, r in dropped), tuple(r for _p, r in edit.dismissed_away),
                           tuple(edit.tombstones_dropped), changed, tuple(notes))


def _merge_duplicates(edit: _RootEdit) -> None:
    """One record per (task, session id): keep the earliest registration, with the freshest process facts."""
    for path, records in edit.files.items():
        groups: dict[tuple[str, str], list[Record]] = {}
        for record in records:
            groups.setdefault((record.task, record.session_id), []).append(record)
        merged = []
        for group in groups.values():
            newest = max(group, key=lambda r: r.recorded_at)
            merged.append(dataclasses.replace(newest, registered_at=min(r.registered_at for r in group)))
        edit.files[path] = merged
