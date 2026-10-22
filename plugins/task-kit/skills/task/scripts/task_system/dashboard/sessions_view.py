"""The dashboard's side of working-session registrations (``task_system.sessions``).

A registration says "a Claude Code session is working this task". The dashboard SHOWS every registration it can
read (the library already hides the ones a human dismissed) and lets exactly one kind of it act:

- **holds** the task (this dashboard starts no run on it and queue-starts none, the same effect as an interactive
  hold): a record in a file this machine owns whose session is VERIFIED running (process id plus the exact creation
  stamp);
- **display only**, never blocking any action (Queue, Retry, authoring, Cancel, ...): this machine's records whose
  process is not running, and every other machine's records, which this machine cannot verify.

Nothing here releases a registration because its process is gone. The background pass asks the library for the cleanups
every write of this machine's file performs anyway (``sessions.reconcile``): records on closed, archived or absent tasks,
records some machine dismissed, and this machine's dismissal entries whose target is gone. The library owns every read,
state and write; this module calls its public functions only, and keeps no machine-local registration or dismissal state.

:class:`SessionsMaintainer` also publishes this machine's pending session files (``session_publish``), one publish per
repository per interval in a background thread, so a record committed while offline reaches the other machines
without anyone running ``task sessions publish``.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .. import discovery, machine, session_publish, sessions, sessions_config

DEFAULT_PUBLISH_INTERVAL_S = 60.0
FIRST_PASS_DELAY_S = 5.0
SHORT_ID = 8


class SessionsUnavailable(RuntimeError):
    """The registrations cannot be read on this machine (unreadable machine id or sessions configuration)."""


@dataclass(frozen=True)
class SessionView:
    """One registration as the page shows it."""

    session_id: str
    host: str
    state: str  # running / not running / unverified
    mine: bool  # the file carries this machine's fingerprint
    holds: bool  # mine, and the session is verified running
    checkout: str
    registered_at: str
    recorded_at: str
    task_state: str | None

    @property
    def short_id(self) -> str:
        return self.session_id[:SHORT_ID]

    def to_json(self) -> dict:
        return {"session": self.short_id, "session_id": self.session_id, "host": self.host, "state": self.state, "mine": self.mine,
                "holds": self.holds, "dismissable": self.dismissable, "checkout": self.checkout, "registered_at": self.registered_at,
                "recorded_at": self.recorded_at, "task_state": self.task_state}

    @property
    def dismiss_key(self) -> tuple[str, str, str]:
        """What a dismissal is keyed on: a new registration (another session or ``registered_at``) is a new key."""
        return (self.host, self.session_id, self.registered_at)

    @property
    def dismissable(self) -> bool:
        """Only another machine's unverified registration can be dismissed (it blocks nothing; this hides it here)."""
        return not self.mine and self.state == sessions.STATE_UNVERIFIED

    def revision_key(self) -> tuple:
        """What a change of the picture is made of: no age, so a quiet minute does not change the revision."""
        return (self.session_id, self.host, self.state, self.mine, self.recorded_at, self.task_state)


def view_of(record: sessions.Record) -> SessionView:
    return SessionView(
        session_id=record.session_id, host=record.host, state=record.state, mine=record.owned,
        holds=record.owned and record.state == sessions.STATE_RUNNING, checkout=record.checkout,
        registered_at=record.registered_at, recorded_at=record.recorded_at, task_state=record.task_state)


@dataclass(frozen=True)
class SessionRoot:
    """A physical task root the inventory read ``.sessions`` from."""

    root: str  # the physical (realpath) task root
    project_root: str  # a checkout whose ``dev/tasks`` reaches it


class SessionsReader:
    """Reads registrations for the inventory and the scheduler; resolves this machine once."""

    def __init__(self, *, config_path: Path | None = None, machine_value: machine.Machine | None = None) -> None:
        self._config_path = Path(config_path) if config_path is not None else discovery.TASK_CONFIG_PATH
        self._machine = machine_value
        self._fixed = machine_value is not None
        self._lock = threading.Lock()

    def invalidate(self) -> None:
        """Forget the resolved machine (a forced refresh re-reads ``sessions.host``); a machine given at construction stays."""
        with self._lock:
            if not self._fixed:
                self._machine = None

    def config(self) -> sessions_config.SessionsConfig:
        try:
            return sessions_config.load_sessions_config(self._config_path)
        except sessions_config.SessionsConfigError as exc:
            raise SessionsUnavailable(str(exc)) from exc

    def machine(self) -> machine.Machine:
        with self._lock:
            if self._machine is not None:
                return self._machine
        config = self.config()
        try:
            found = machine.current(override=config.host)
        except machine.MachineError as exc:
            raise SessionsUnavailable(f"working-session registrations cannot be matched to this machine: {exc}") from exc
        with self._lock:
            self._machine = found
        return found

    def record_enabled(self) -> bool:
        """``sessions.record``; an unreadable configuration reads as enabled (nothing is published or pruned on a guess either way)."""
        try:
            return self.config().record
        except SessionsUnavailable:
            return False

    def read(self, root: Path) -> sessions.RootView:
        return sessions.read_root(Path(root), self.machine(), check_tasks=True)

    def registrations(self, folder: str | Path) -> list[SessionView]:
        """Every registration of the task folder in any state (what Re-check reports); ``[]`` when none or unreadable."""
        real = Path(os.path.realpath(str(folder)))
        try:
            mine = self.machine()
        except SessionsUnavailable:
            return []
        if not (real.parent / sessions.SESSIONS_DIRNAME).is_dir():
            return []
        view = sessions.read_root(real.parent, mine, check_tasks=False)
        return [view_of(r) for r in view.records if r.task == real.name]

    def holders(self, folder: str | Path) -> list[sessions.Record]:
        """This machine's verified-running registrations of the task folder; ``[]`` when none or when they cannot be read.

        Unverifiable state never holds: an unreadable machine identity, another machine's record and a record whose
        process is not running all yield nothing here.
        """
        real = Path(os.path.realpath(str(folder)))
        try:
            mine = self.machine()
        except SessionsUnavailable:
            return []
        if not (real.parent / sessions.SESSIONS_DIRNAME).is_dir():
            return []
        view = sessions.read_root(real.parent, mine, check_tasks=False)
        return [r for r in view.records if r.task == real.name and r.owned and r.state == sessions.STATE_RUNNING]


def hold_message(holders: Iterable[sessions.Record]) -> str:
    """Why a registration holds a task, naming the session(s) and how it ends."""
    held = list(holders)
    first = held[0]
    more = f" (and {len(held) - 1} more)" if len(held) > 1 else ""
    return (f"Claude Code session {first.session_id[:SHORT_ID]}{more} is working this task on {first.host} "
            f"(registered {first.registered_at}); the dashboard yields until it hands off")


class SessionsMaintainer:
    """Background: reconcile this machine's session files, then publish what is pending.

    One pass per ``interval_s``. Reconciling drops this machine's records on closed, archived or absent tasks and
    those a human dismissed, and its dismissal entries whose target is gone; it never drops a record because its
    process is not running. A pass publishes at most once per repository (``session_publish.publish`` is bounded; the
    pass reports each outcome through ``log``).
    """

    def __init__(self, reader: SessionsReader, roots: Callable[[], Iterable[SessionRoot]], *,
                 interval_s: float = DEFAULT_PUBLISH_INTERVAL_S, first_delay_s: float = FIRST_PASS_DELAY_S,
                 data_dir: Path | None = None, log: Callable[[str], None] = lambda message: None) -> None:
        self._reader = reader
        self._roots = roots
        self._interval = interval_s
        self._first_delay = first_delay_s
        self._data_dir = data_dir
        self._log = log
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pass_lock = threading.Lock()
        self.passes = 0

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="sessions-maintainer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _run(self) -> None:
        delay = self._first_delay
        while not self._stop.wait(delay):
            delay = self._interval
            try:
                self.once()
            except Exception as exc:  # noqa: BLE001 -- a background pass must say what failed, and the next pass runs again
                self._log(f"sessions: background pass failed: {type(exc).__name__}: {exc}")

    def once(self) -> dict:
        """One pass, synchronously: ``{"reconciled": [...], "published": [...], "skipped": reason|None}``."""
        with self._pass_lock:
            self.passes += 1
            outcome: dict = {"reconciled": [], "published": [], "skipped": None}
            if not self._reader.record_enabled():
                outcome["skipped"] = "sessions.record is false or the sessions configuration is unreadable"
                return outcome
            try:
                mine = self._reader.machine()
            except SessionsUnavailable as exc:
                outcome["skipped"] = str(exc)
                return outcome
            published: set[str] = set()
            for entry in self._roots():
                if not Path(entry.root).is_dir():
                    continue
                try:
                    result = sessions.reconcile(mine, Path(entry.root), data_dir=self._data_dir)
                except sessions.SessionsError as exc:
                    self._log(f"sessions: cannot reconcile {entry.root}: {exc}")
                    continue
                outcome["reconciled"].extend(
                    f"{entry.root}:{r.task}" for r in (*result.dropped, *result.dismissed))
                outcome["reconciled"].extend(f"{entry.root}:dismissal of {d.session_id[:8]} on {d.host}"
                                             for d in result.tombstones)
                gate = session_publish.preflight(Path(entry.root), machine=mine)
                if not gate.ok or gate.repo_root is None:  # a root that cannot carry records (or a tmp root) publishes nothing
                    continue
                if not session_publish.pending(Path(entry.root), mine):
                    continue
                top = str(gate.repo_root)
                if top in published:  # one publish per repository per pass
                    continue
                published.add(top)
                result = session_publish.publish(Path(entry.root), mine, data_dir=self._data_dir)
                outcome["published"].append({"root": entry.root, "status": result.status, "message": result.message})
                self._log(f"sessions: publish {entry.root}: {result.status}: {result.message}")
            return outcome


def dismiss_and_publish(reader: SessionsReader, project_root: Path, host: str, session_id: str, registered_at: str, *,
                        data_dir: Path | None = None) -> tuple[sessions.DismissResult, session_publish.PublishResult]:
    """Dismiss another machine's registration in git (this machine's own file), then publish it.

    The human dismissing is a contribution of information: the entry records who (this host) and when, readers hide
    the record at once, and its owner drops it at its next write. Publication is bounded and reports its outcome
    (a failure leaves the dismissal committed and pending, like a registration).
    """
    mine = reader.machine()
    result = sessions.dismiss(mine, project_root, host, session_id, registered_at, data_dir=data_dir)
    published = session_publish.publish(result.root, mine, data_dir=data_dir, record=reader.record_enabled())
    return result, published
