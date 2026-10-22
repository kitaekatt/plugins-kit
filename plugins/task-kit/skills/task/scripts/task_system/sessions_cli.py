"""The CLI side of working-session records: what ``work`` registers, what ``sessions`` lists / releases / publishes,
and how ``list`` / ``show`` / ``status`` display a task's registrations.

Design and rulings: ``docs/planning/task-kit-dashboard/working-sessions-plan.md``. This module is glue over the
library (``sessions`` the store, ``session_publish`` the git publisher, ``harness`` / ``machine`` identity); it
holds no record logic of its own and never imports the dashboard package.

Conventions.

- ``work`` reports on stderr, every line prefixed ``session: `` -- stdout and the exit code of ``work`` are exactly
  what they were before registration existed. A registration that cannot be made is a line, never an exit code:
  the validation gate passed, so the work may proceed.
- The read paths (:class:`SessionReader`, :func:`list_command`) never write: no record, no index, no lock, no
  commit, no network.
- Every message that says a record is not published says what that means (the library's own wording).
"""

from __future__ import annotations

import datetime
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from . import datadir, discovery, harness, oslock, resolve, session_publish, sessions
from . import machine as machine_module
from .sessions_config import SessionsConfig, SessionsConfigError, load_sessions_config

PREFIX = "session: "
STATE_ORDER = (sessions.STATE_RUNNING, sessions.STATE_NOT_RUNNING, sessions.STATE_UNVERIFIED)
_DROPPED_WORDS = {sessions.TASK_CLOSED: "a closed", sessions.TASK_ARCHIVED: "an archived", sessions.TASK_ABSENT: "an absent"}


# -- small formatting helpers ------------------------------------------------------------------------------


def _now(now: datetime.datetime | None) -> datetime.datetime:
    return now if now is not None else datetime.datetime.now(datetime.timezone.utc)


def format_age(timestamp: str, now: datetime.datetime | None = None) -> str:
    """``<1m`` / ``5m`` / ``3h`` / ``2d`` for a ``YYYY-MM-DDTHH:MM:SSZ`` timestamp; ``?`` when it is unreadable."""
    try:
        then = datetime.datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return "?"
    seconds = int((_now(now) - then).total_seconds())
    if seconds < 60:
        return "<1m"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _ago(age: str) -> str:
    return f"{age} ago"


def short_id(session_id: str) -> str:
    return session_id[:8]


def _task_state_suffix(record: sessions.Record) -> str:
    word = _DROPPED_WORDS.get(record.task_state or "")
    return f" on {word} task" if word else ""


def describe(record: sessions.Record, now: datetime.datetime | None = None) -> str:
    """``m5pro running (Afterplay2, 2h)`` / ``m5pro not running (registered 2d ago)`` / ``5090w unverified (...)``."""
    age = format_age(record.registered_at, now)
    if record.state == sessions.STATE_RUNNING:
        detail = f"{record.checkout}, {age}"
    else:
        detail = f"registered {_ago(age)}"
    return f"{record.host} {record.state} ({detail}){_task_state_suffix(record)}"


def describe_long(record: sessions.Record) -> str:
    """The ``show`` / ``status`` form: ``m5pro running (Afterplay2, session abcd1234, since 2026-10-21T18:04:11Z)``."""
    return (f"{record.host} {record.state} ({record.checkout}, session {short_id(record.session_id)}, "
            f"since {record.registered_at}){_task_state_suffix(record)}")


def compact(record: sessions.Record, now: datetime.datetime | None = None) -> str:
    """The ``list`` form: ``m5pro running 2h`` (host, state, age since registration)."""
    return f"{record.host} {record.state} {format_age(record.registered_at, now)}"


def working_suffix(records: tuple[sessions.Record, ...], now: datetime.datetime | None = None) -> str:
    """`` [working: m5pro running 2h; 5090w unverified 3h]`` for a ``list`` task line; empty without records."""
    if not records:
        return ""
    return " [working: " + "; ".join(compact(r, now) for r in records) + "]"


def record_dict(record: sessions.Record) -> dict:
    """A record as structured output (``list --format json|yaml``, ``sessions --format json``)."""
    return {
        "session_id": record.session_id,
        "host": record.host,
        "state": record.state,
        "harness": record.harness,
        "checkout": record.checkout,
        "registered_at": record.registered_at,
        "recorded_at": record.recorded_at,
        "transcript_path": record.transcript_path,
        "task_state": record.task_state,
        "this_machine": record.owned,
    }


def _order(records) -> tuple[sessions.Record, ...]:
    """This machine's running records first, then its not-running ones, then other machines'; stable by registration."""
    rank = {state: index for index, state in enumerate(STATE_ORDER)}
    return tuple(sorted(records, key=lambda r: (rank.get(r.state, len(rank)), r.registered_at, r.session_id)))


# -- reading (never writes) --------------------------------------------------------------------------------


def location_of(task_id: str) -> tuple[str, str] | None:
    """``(location, stub)`` for ``dev/tasks/<stub>`` / ``tmp/<stub>``; None for anything else (nested, remote, odd)."""
    for location in resolve.KNOWN_ROOTS:
        head = location + "/"
        if task_id.startswith(head):
            stub = task_id[len(head):]
            if stub and "/" not in stub:
                return location, stub
    return None


class SessionReader:
    """A task's registrations, reading each physical task root's ``.sessions`` directory at most once.

    The machine (an OS machine-id read) is determined only when some ``.sessions`` directory exists, so a project
    that has never registered a session pays nothing. A machine or configuration that cannot be determined is ONE
    note in :attr:`notes`, after which no registrations are shown (ownership cannot be judged without it).
    """

    def __init__(self, *, config_path: Path | None = None, machine: machine_module.Machine | None = None) -> None:
        self.notes: list[str] = []
        self._config_path = config_path
        self._machine = machine
        self._machine_failed = False
        self._views: dict[str, sessions.RootView | None] = {}

    def machine(self) -> machine_module.Machine | None:
        if self._machine is None and not self._machine_failed:
            try:
                config = load_sessions_config(self._config_path)
                self._machine = machine_module.current(override=config.host)
            except (SessionsConfigError, machine_module.MachineError) as exc:
                self._machine_failed = True
                self.notes.append(f"working sessions are not shown: {exc}")
        return self._machine

    def view(self, task_root: Path) -> sessions.RootView | None:
        """The records of one physical task root, or None when it has no ``.sessions`` directory (or no machine)."""
        key = os.path.normcase(os.path.realpath(task_root))
        if key not in self._views:
            root = Path(os.path.realpath(task_root))
            found = None
            if (root / sessions.SESSIONS_DIRNAME).is_dir():
                machine = self.machine()
                if machine is not None:
                    found = sessions.read_root(root, machine, check_tasks=False)
                    self.notes.extend(found.notes)
            self._views[key] = found
        return self._views[key]

    def records_for(self, project_root: Path, location: str, stub: str) -> tuple[sessions.Record, ...]:
        view = self.view(Path(project_root) / location)
        if view is None:
            return ()
        return _order(r for r in view.records if r.task == stub)


# -- work: register ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Env:
    """What a registration needs from the process: injectable for tests."""

    environ: Mapping[str, str]
    config_path: Path | None = None
    data_dir: Path | None = None


def _line(text: str) -> str:
    return PREFIX + text


def _not_registered(reason: str) -> list[str]:
    return [_line(f"not registered: {reason}")]


def _publish_fragment(root: Path, machine, data_dir: Path, location: str, record: bool,
                      verdict: session_publish.Preflight) -> str:
    """Publish this machine's session files after a write; returns the outcome fragment of the `session:` line."""
    if verdict.kind == session_publish.KIND_LOCAL:
        return "local only: tmp is machine-local"
    try:
        result = session_publish.publish(verdict.repo_root, machine, data_dir=data_dir, record=record)
    except (OSError, RuntimeError) as exc:
        return f"not published: {type(exc).__name__}: {exc}; {session_publish.CANNOT_SEE}"
    return result.message


def register_for_work(project_root: Path, canonical: str, *, env: Mapping[str, str] | None = None,
                      config_path: Path | None = None, data_dir: Path | None = None,
                      now: datetime.datetime | None = None) -> list[str]:
    """Register the calling Claude Code session as working ``canonical``, publish, and return the stderr lines.

    Called by ``task.py work`` AFTER its validation gate passed. Returns no lines outside a Claude Code subprocess.
    Every refusal is a ``session: not registered: <reason>`` line with nothing written.
    """
    environ = os.environ if env is None else env
    if harness.session_id(environ) is None:
        return []
    if harness.in_dashboard_run(environ):
        return _not_registered("this is a dashboard run (the dashboard shows its runs; a run must not commit)")
    try:
        config = load_sessions_config(config_path)
    except SessionsConfigError as exc:
        return _not_registered(str(exc))
    if not config.record:
        return _not_registered("sessions.record is false")
    try:
        identity = harness.resolve_identity(environ)
        machine = machine_module.current(override=config.host)
        resolved = resolve.resolve_ref(canonical, Path(project_root))
        physical = Path(os.path.realpath(Path(project_root) / resolved.location))
        verdict = session_publish.preflight(physical, machine=machine, location=resolved.location, record=config.record)
        if not verdict.ok:
            return _not_registered(verdict.reason)
        base = datadir.resolve_data_dir() if data_dir is None else Path(data_dir)
        result = sessions.register(identity, machine, Path(project_root), canonical, data_dir=base, env=environ)
    except (harness.IdentityError, machine_module.MachineError, resolve.RefResolutionError, datadir.DataDirError,
            sessions.SessionsError, oslock.LockTimeout, OSError) as exc:
        return _not_registered(str(exc))
    fragment = _publish_fragment(physical, machine, base, resolved.location, config.record, verdict)
    record = result.record
    lines = [_line(f"{result.action} {machine.host} on {canonical} (session {short_id(record.session_id)}); {fragment}")]
    for taken in result.taken_over:
        lines.append(_line(f"took over {canonical} from session {short_id(taken.session_id)} (not running)"))
    for kept in result.kept_not_running:
        lines.append(_line(f"kept: {kept.message}"))
    for other in _order(result.others):
        lines.append(_line(
            f"also working this task: {compact(other, now)} (session {short_id(other.session_id)})"))
    if result.also_holds:
        lines.append(_line("this session also holds " + ", ".join(result.also_holds)))
    for dropped in result.dropped:
        lines.append(_line(
            f"dropped the record of session {short_id(dropped.session_id)} on {dropped.task} "
            f"({dropped.task_state} task)"))
    for note in result.notes:
        lines.append(_line(f"note: {note}"))
    return lines


# -- sessions release / publish / list ---------------------------------------------------------------------


def _roots_with_location(project_root: Path) -> list[tuple[str, Path]]:
    """``(location, physical root)`` for each task root of a project, deduplicated by physical root."""
    found: dict[str, tuple[str, Path]] = {}
    for location in resolve.KNOWN_ROOTS:
        candidate = Path(project_root) / location
        if candidate.is_dir():
            real = Path(os.path.realpath(candidate))
            found.setdefault(os.path.normcase(str(real)), (location, real))
    return list(found.values())


def _location_of_root(project_root: Path, root: Path) -> str:
    key = os.path.normcase(os.path.realpath(root))
    for location, real in _roots_with_location(project_root):
        if os.path.normcase(str(real)) == key:
            return location
    return resolve.LOCATION_DEV_TASKS


def _error(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


def release_command(project_root: Path, ref: str, *, all_local: bool, env: Mapping[str, str] | None = None,
                    config_path: Path | None = None, data_dir: Path | None = None) -> int:
    """``task.py sessions release <ref> [--all-local]``: remove this machine's registration(s), then publish."""
    environ = os.environ if env is None else env
    try:
        config = load_sessions_config(config_path)
        machine = machine_module.current(override=config.host)
        identity = None
        if not all_local:
            try:
                identity = harness.resolve_identity(environ)
            except harness.IdentityError as exc:
                return _error(f"cannot tell which registration is the caller's ({exc}); run this inside the Claude "
                              f"Code session that worked the task, or pass --all-local to release every "
                              f"registration this machine holds for it")
        base = datadir.resolve_data_dir() if data_dir is None else Path(data_dir)
        result = sessions.release(identity, machine, Path(project_root), ref, all_local=all_local, data_dir=base)
    except (SessionsConfigError, machine_module.MachineError, datadir.DataDirError, sessions.SessionsError,
            oslock.LockTimeout, OSError) as exc:
        return _error(str(exc))
    location = _location_of_root(Path(project_root), result.root)
    label = ref
    try:
        label = resolve.resolve_ref(ref, Path(project_root)).canonical
    except resolve.RefResolutionError:
        pass
    lines: list[str] = []
    if result.changed_files:
        verdict = session_publish.preflight(result.root, machine=machine, location=location, record=config.record)
        if verdict.ok:
            fragment = _publish_fragment(result.root, machine, base, location, config.record, verdict)
        else:
            fragment = f"released on this machine; not published: {verdict.reason}"
    else:
        fragment = "nothing changed"
    if result.removed:
        count = len(result.removed)
        ids = ", ".join(short_id(r.session_id) for r in result.removed)
        lines.append(_line(f"released {machine.host} from {label} ({count} registration{'s' if count != 1 else ''}: "
                           f"session {ids}); {fragment}"))
    else:
        lines.append(_line(f"nothing to release for {label} on {machine.host}; {fragment}"))
    for dropped in result.dropped:
        lines.append(_line(f"dropped the record of session {short_id(dropped.session_id)} on {dropped.task} "
                           f"({dropped.task_state} task)"))
    for note in result.notes:
        lines.append(_line(f"note: {note}"))
    for line in lines:
        print(line)
    return 0


def publish_command(project_root: Path, *, config_path: Path | None = None, data_dir: Path | None = None) -> int:
    """``task.py sessions publish``: drain this machine's pending session files for the project's durable roots."""
    try:
        config = load_sessions_config(config_path)
        machine = machine_module.current(override=config.host)
        base = datadir.resolve_data_dir() if data_dir is None else Path(data_dir)
    except (SessionsConfigError, machine_module.MachineError, datadir.DataDirError) as exc:
        return _error(str(exc))
    repos: dict[str, Path] = {}
    refused: list[str] = []
    for location, root in _roots_with_location(project_root):
        verdict = session_publish.preflight(root, machine=machine, location=location, record=config.record)
        if verdict.kind == session_publish.KIND_LOCAL:
            continue
        if not verdict.ok:
            if session_publish.session_file(root, machine).exists():
                refused.append(f"{location}: not published: {verdict.reason}")
            continue
        repos.setdefault(os.path.normcase(str(verdict.repo_root)), verdict.repo_root)
    status = 0
    for line in refused:
        print(_line(line))
        status = 1
    for repo in repos.values():
        try:
            result = session_publish.publish(repo, machine, data_dir=base, record=config.record)
        except (OSError, RuntimeError) as exc:
            print(_line(f"{repo}: not published: {type(exc).__name__}: {exc}"))
            status = 1
            continue
        print(_line(f"{repo}: {result.message}"))
        if result.status not in (session_publish.PUBLISHED, session_publish.NOTHING_PENDING,
                                 session_publish.DISABLED):
            status = 1
    if not repos and not refused:
        print(_line("nothing to publish: no durable task root of this project carries session records"))
    return status


def _project_roots(project_root: Path, scope: str) -> list[tuple[str, Path]]:
    """``(project name, project root)`` for ``--scope project`` (this project) or ``all`` (configured directories)."""
    if scope == "project":
        return [(Path(project_root).name, Path(project_root))]
    parents = discovery.configured_project_directories()
    if not parents:
        raise discovery.DiscoveryError(discovery.NO_PROJECT_DIRECTORIES_MESSAGE)
    return [(entry.name, entry.root) for entry in discovery.discover_project_roots(parents)]


def list_command(project_root: Path, *, scope: str, output_format: str, config_path: Path | None = None,
                 now: datetime.datetime | None = None) -> int:
    """``task.py sessions [list]``: every registration of the scope's task roots with its state, plus this
    machine's pending session files. Read-only and offline."""
    try:
        config = load_sessions_config(config_path)
        machine = machine_module.current(override=config.host)
        projects = _project_roots(project_root, scope)
    except (SessionsConfigError, machine_module.MachineError, discovery.DiscoveryError) as exc:
        return _error(str(exc))
    notes: list[str] = []
    seen: set[str] = set()
    rows: list[dict] = []
    pending: list[dict] = []
    for name, root in projects:
        for location, physical in _roots_with_location(root):
            key = os.path.normcase(str(physical))
            if key in seen:
                continue
            seen.add(key)
            view = sessions.read_root(physical, machine, check_tasks=True)
            notes.extend(f"{name}/{location}: {note}" for note in view.notes)
            for record in _order(view.records):
                rows.append({"project": name, "location": location, "record": record})
            verdict = session_publish.preflight(physical, machine=machine, location=location, record=config.record)
            if verdict.kind == session_publish.KIND_DURABLE and verdict.repo_root is not None:
                files = session_publish.pending(verdict.repo_root, machine)
                if files:
                    pending.append({"project": name, "repo": str(verdict.repo_root), "files": list(files)})
    for note in notes:
        print(f"note: {note}", file=sys.stderr)
    if output_format == "json":
        data = {
            "schema_version": "1",
            "scope": scope,
            "host": machine.host,
            "sessions": [{"project": row["project"], "task": f"{row['location']}/{row['record'].task}",
                          **record_dict(row["record"])} for row in rows],
            "pending": pending,
            "notes": notes,
        }
        sys.stdout.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        return 0
    print(f"Working sessions (this machine: {machine.host}):")
    if not rows:
        print("  none")
    for row in rows:
        record = row["record"]
        prefix = f"{row['project']}/" if scope == "all" else ""
        print(f"  {prefix}{row['location']}/{record.task}  {describe(record, now)}  "
              f"session {short_id(record.session_id)}")
    if pending:
        print("Pending publication (this machine's session files not yet on the upstream):")
        for item in pending:
            print(f"  {item['repo']}: {', '.join(item['files'])}")
    else:
        print("Pending publication: none")
    return 0
