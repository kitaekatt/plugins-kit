"""Durable local ownership of one job command, independent of its manager.

This opt-in wrapper does not change the jobs ledger or its public verbs.
Use one shared ``lease_path`` for commands that write the same ledger. Only
supervised callers participate in that lease; an ordinary jobs invocation
still has the documented one-process-per-ledger precondition.

Requires the ``local`` extra. Runtime files contain caller-provided argv and
environment overrides and must live in a private, machine-local directory.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import json
import errno
import math
import os
from pathlib import Path
import subprocess
import sys
import time
from types import MappingProxyType
from typing import Any
import uuid

from llm_kit.storage.atomic_json import write as _atomic_write
from . import _supervisor_process as processes
from ._supervisor_process import ProcessIdentity


SCHEMA = 1
TERMINAL_STATES = frozenset({"finished", "cancelled", "timeout", "refused"})
POLL_S = 0.02
_READ_DEADLINE: ContextVar[float | None] = ContextVar("supervisor_read_deadline", default=None)


class SupervisorError(RuntimeError):
    """Ownership could not be established; ``handle`` permits later inspection."""

    def __init__(self, message: str, *, handle: ProcessHandle | None = None) -> None:
        super().__init__(message)
        self.handle = handle


class SupervisorUnsupported(SupervisorError):
    """The required local process-control capability is unavailable."""


def _positive(value: float, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{label} must be finite and positive")
    return float(value)


def _absolute(value: Path, label: str) -> Path:
    value = Path(value)
    if not value.is_absolute():
        raise ValueError(f"{label} must be absolute")
    return value.resolve()


def _argv(value: tuple[str, ...], label: str, *, empty: bool = False) -> tuple[str, ...]:
    if isinstance(value, str):
        raise ValueError(f"{label} must be an argv sequence, not a shell command")
    value = tuple(value)
    if (not value and not empty) or any(not isinstance(arg, str) or not arg or "\0" in arg for arg in value):
        raise ValueError(f"{label} must contain nonempty strings")
    return value


@dataclass(frozen=True)
class JobProcessSpec:
    run_dir: Path
    argv: tuple[str, ...]
    cwd: Path
    timeout_s: float
    env: Mapping[str, str] = field(default_factory=dict)
    cancel_argv: tuple[str, ...] = ()
    lease_path: Path | None = None
    grace_s: float = 2.0
    cancel_timeout_s: float = 30.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_dir", _absolute(self.run_dir, "run_dir"))
        object.__setattr__(self, "cwd", _absolute(self.cwd, "cwd"))
        object.__setattr__(self, "argv", _argv(self.argv, "argv"))
        object.__setattr__(self, "cancel_argv", _argv(self.cancel_argv, "cancel_argv", empty=True))
        lease = self.lease_path if self.lease_path is not None else self.run_dir / "lease.json"
        object.__setattr__(self, "lease_path", _absolute(lease, "lease_path"))
        for label in ("timeout_s", "grace_s", "cancel_timeout_s"):
            object.__setattr__(self, label, _positive(getattr(self, label), label))
        if not isinstance(self.env, Mapping) or any(
            not isinstance(k, str) or not k or "=" in k or "\0" in k or not isinstance(v, str) or "\0" in v
            for k, v in self.env.items()
        ):
            raise ValueError("env must map valid environment names to strings")
        object.__setattr__(self, "env", MappingProxyType(dict(self.env)))

    def to_dict(self) -> dict[str, Any]:
        return {"run_dir": str(self.run_dir), "argv": list(self.argv), "cwd": str(self.cwd),
                "timeout_s": self.timeout_s, "env": dict(self.env), "cancel_argv": list(self.cancel_argv),
                "lease_path": str(self.lease_path), "grace_s": self.grace_s,
                "cancel_timeout_s": self.cancel_timeout_s}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> JobProcessSpec:
        return cls(**dict(value))


@dataclass(frozen=True)
class ProcessHandle:
    run_dir: Path
    nonce: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_dir", _absolute(self.run_dir, "run_dir"))
        if not isinstance(self.nonce, str) or len(self.nonce) != 32 or any(c not in "0123456789abcdef" for c in self.nonce):
            raise ValueError("nonce must be 32 hexadecimal characters")

    def to_dict(self) -> dict[str, Any]:
        return {"schema": SCHEMA, "run_dir": str(self.run_dir), "nonce": self.nonce}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ProcessHandle:
        if set(value) != {"schema", "run_dir", "nonce"} or value["schema"] != SCHEMA:
            raise ValueError("Unsupported supervised handle schema")
        return cls(Path(value["run_dir"]), value["nonce"])


@dataclass(frozen=True)
class ProcessStatus:
    handle: ProcessHandle
    state: str
    alive: bool
    returncode: int | None = None
    detail: str = ""
    survivors: tuple[ProcessIdentity, ...] = ()
    result: Mapping[str, Any] | None = None

    @property
    def stdout_path(self) -> Path:
        return self.handle.run_dir / "stdout.log"

    @property
    def stderr_path(self) -> Path:
        return self.handle.run_dir / "stderr.log"

    def to_dict(self) -> dict[str, Any]:
        return {"schema": SCHEMA, "handle": self.handle.to_dict(), "state": self.state,
                "alive": self.alive, "returncode": self.returncode, "detail": self.detail,
                "survivors": [p.to_dict() for p in self.survivors],
                "stdout_path": str(self.stdout_path), "stderr_path": str(self.stderr_path),
                "result": dict(self.result) if self.result is not None else None}


def _supported() -> None:
    if os.name not in {"posix", "nt"}:
        raise SupervisorUnsupported(f"Supervised jobs do not support os.name={os.name!r}")
    try:
        processes.psutil_module()
    except processes.ProcessError as exc:
        raise SupervisorUnsupported(str(exc)) from exc


@contextmanager
def _state_reads_until(deadline: float) -> Iterator[None]:
    outer = _READ_DEADLINE.get()
    token = _READ_DEADLINE.set(min(deadline, outer) if outer is not None else deadline)
    try:
        yield
    finally:
        _READ_DEADLINE.reset(token)


def _read(path: Path, *, handle: ProcessHandle | None = None) -> dict[str, Any] | None:
    # Native Windows can briefly deny an open during atomic publication. Keep
    # that uncertainty bounded by both one second and the caller's wait budget.
    retry_until = time.monotonic() + 1.0
    outer = _READ_DEADLINE.get()
    if outer is not None:
        retry_until = min(retry_until, outer)
    denied = False
    while True:
        try:
            text = path.read_text(encoding="utf-8")
            break
        except FileNotFoundError as exc:
            if denied:
                raise SupervisorError(f"Supervised state disappeared after a read denial: {path}", handle=handle) from exc
            return None
        except PermissionError as exc:
            remaining = retry_until - time.monotonic()
            if os.name == "nt" and exc.errno == errno.EACCES and remaining > 0:
                denied = True
                time.sleep(min(POLL_S, remaining))
                continue
            raise SupervisorError(f"Cannot read supervised state {path}: {exc}", handle=handle) from exc
        except (OSError, ValueError) as exc:
            raise SupervisorError(f"Cannot read supervised state {path}: {exc}", handle=handle) from exc
    try:
        document = json.loads(text)
    except ValueError as exc:
        raise SupervisorError(f"Cannot read supervised state {path}: {exc}", handle=handle) from exc
    if not isinstance(document, dict) or document.get("schema") != SCHEMA:
        raise SupervisorError(f"Unsupported or malformed supervised state: {path}", handle=handle)
    return document


def _write(path: Path, value: Mapping[str, Any]) -> None:
    _atomic_write(path, {"schema": SCHEMA, **value})
    if os.name == "posix":
        path.chmod(0o600)


@contextmanager
def _lock(path: Path, *, blocking: bool = True) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            # Windows byte-range locks also deny reads. Check metadata before
            # locking instead of reading a byte another owner can hold.
            if os.fstat(stream.fileno()).st_size == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            # msvcrt's blocking lock has a fixed ten-retry limit. Use the
            # nonblocking primitive and an explicit wait for the blocking case.
            while True:
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise
                    if not blocking:
                        raise BlockingIOError(f"Supervisor lock held: {path}") from None
                    time.sleep(POLL_S)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _load(handle: ProcessHandle | Path) -> tuple[ProcessHandle, JobProcessSpec]:
    directory = handle.run_dir if isinstance(handle, ProcessHandle) else _absolute(handle, "run_dir")
    intent = _read(directory / "intent.json", handle=handle if isinstance(handle, ProcessHandle) else None)
    if intent is None:
        raise SupervisorError(f"No supervised dispatch intent: {directory}")
    found = ProcessHandle(directory, intent["nonce"])
    if isinstance(handle, ProcessHandle) and handle != found:
        raise SupervisorError("Supervised run nonce does not match", handle=handle)
    spec = JobProcessSpec.from_dict(intent["spec"])
    if spec.run_dir != directory:
        raise SupervisorError("Supervised run directory does not match intent", handle=found)
    return found, spec


def _record(handle: ProcessHandle, filename: str) -> dict[str, Any] | None:
    value = _read(handle.run_dir / filename, handle=handle)
    if value is not None and value.get("nonce") != handle.nonce:
        raise SupervisorError(f"Supervised {filename} nonce does not match", handle=handle)
    return value


def _tree(handle: ProcessHandle) -> tuple[ProcessIdentity | None, list[ProcessIdentity]]:
    child = _record(handle, "child.json")
    tree = _record(handle, "tree.json")
    recorded = [ProcessIdentity.from_dict(p) for p in tree["members"]] if tree is not None else []
    finalizer = _record(handle, "finalizer.json")
    if finalizer is not None:
        identity = ProcessIdentity.from_dict(finalizer["process"])
        recorded.extend([identity, *processes.session_members(identity)])
    return ProcessIdentity.from_dict(child["process"]) if child is not None else None, recorded


def _release(handle: ProcessHandle, spec: JobProcessSpec) -> None:
    assert spec.lease_path is not None
    with _lock(_lease_lock_path(spec)):
        lease = _read(spec.lease_path, handle=handle)
        if lease is None or lease.get("nonce") != handle.nonce:
            raise SupervisorError("Cannot release a different supervised lease", handle=handle)
        _write(spec.lease_path, {**lease, "released": True})


def _lease_lock_path(spec: JobProcessSpec) -> Path:
    assert spec.lease_path is not None
    if spec.lease_path.parent == spec.run_dir:
        return spec.run_dir.parent / ("." + spec.run_dir.name + ".lease.lock")
    return spec.lease_path.with_suffix(spec.lease_path.suffix + ".lock")


def _verify_lease(handle: ProcessHandle, spec: JobProcessSpec) -> None:
    assert spec.lease_path is not None
    lease = _read(spec.lease_path, handle=handle)
    if lease is None or lease.get("nonce") != handle.nonce or lease.get("released") is not False:
        raise SupervisorError("Supervised lease is no longer owned", handle=handle)


def _spawn_options(*, detached: bool) -> dict[str, Any]:
    if os.name == "posix":
        return {"start_new_session": True} if detached else {}
    flags = subprocess.CREATE_NO_WINDOW
    if detached:
        flags |= subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    return {"creationflags": flags}


def start(spec: JobProcessSpec, *, startup_timeout_s: float = 10.0) -> ProcessHandle:
    """Persist authorization, claim its resource and await the dispatch handshake.

    An existing directory is never reused. A startup timeout requests cancellation
    and raises with ``error.handle``; inspection/adoption must settle it before retry.
    """
    _supported()
    _positive(startup_timeout_s, "startup_timeout_s")
    if not spec.cwd.is_dir():
        raise ValueError(f"cwd is not a directory: {spec.cwd}")
    handle = ProcessHandle(spec.run_dir, uuid.uuid4().hex)
    assert spec.lease_path is not None
    with _lock(_lease_lock_path(spec)):
        previous = _read(spec.lease_path)
        if previous is not None and previous.get("released") is not True:
            prior = ProcessHandle.from_dict(previous["handle"])
            raise SupervisorError("Supervised resource is still owned; adopt its prior handle", handle=prior)
        spec.run_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
        _write(spec.run_dir / "intent.json", {"nonce": handle.nonce, "spec": spec.to_dict()})
        _write(spec.lease_path, {"nonce": handle.nonce, "handle": handle.to_dict(), "released": False})
    worker = Path(__file__).with_name("_supervisor_worker.py")
    try:
        with (spec.run_dir / "supervisor.log").open("ab") as log:
            subprocess.Popen([sys.executable, str(worker), "run", str(spec.run_dir), handle.nonce],
                             cwd=spec.cwd, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                             **_spawn_options(detached=True))
    except OSError as exc:
        _write(spec.run_dir / "result.json", {"nonce": handle.nonce, "state": "refused", "returncode": None,
                                               "detail": f"Supervisor launch failed: {exc}", "survivors": []})
        _release(handle, spec)
        raise SupervisorError(str(exc), handle=handle) from exc
    deadline = time.monotonic() + startup_timeout_s
    with _state_reads_until(deadline):
        while time.monotonic() < deadline:
            if _record(handle, "go.json") is not None or _record(handle, "result.json") is not None:
                return handle
            time.sleep(POLL_S)
    _write(spec.run_dir / "cancel.json", {"nonce": handle.nonce, "reason": "startup handshake timed out"})
    raise SupervisorError("Supervisor dispatch handshake timed out; cancellation requested", handle=handle)


def inspect(handle: ProcessHandle | Path) -> ProcessStatus:
    """Read durable state without adopting, signalling or editing a ledger."""
    _supported()
    handle, _spec = _load(handle)
    result = _record(handle, "result.json")
    runner = _record(handle, "runner.json")
    owner = processes.live(ProcessIdentity.from_dict(runner["process"])) if runner is not None else None
    child, recorded = _tree(handle)
    members = processes.reachable(child, recorded)
    if result is not None:
        state = result["state"]
        if state not in TERMINAL_STATES | {"indeterminate"}:
            raise SupervisorError(f"Invalid supervised result state: {state}", handle=handle)
        if members:
            state = "indeterminate"
        return ProcessStatus(handle, state, owner is not None or bool(members), result.get("returncode"),
                             result.get("detail", ""), tuple(members), result)
    if owner is not None:
        state = "cancelling" if _record(handle, "cancel.json") is not None else "running"
        return ProcessStatus(handle, state, True)
    return ProcessStatus(handle, "indeterminate", bool(members), detail="Supervisor absent without a final result; adopt to inspect",
                         survivors=tuple(members))


def _finish_ledger(handle: ProcessHandle, spec: JobProcessSpec, reason: str) -> dict[str, Any]:
    if not spec.cancel_argv:
        return {"skipped": "no-cancel-command"}
    env = {**os.environ, **spec.env}
    owner = processes.identity(os.getpid())
    if owner is None:
        raise SupervisorError("Cannot establish ledger finalizer owner", handle=handle)
    _write(handle.run_dir / "finalizer-owner.json", {"nonce": handle.nonce, "process": owner.to_dict()})
    worker = Path(__file__).with_name("_supervisor_worker.py")
    try:
        child = subprocess.Popen([sys.executable, str(worker), "finalize", str(handle.run_dir), handle.nonce],
                                 cwd=spec.cwd, env=env, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True,
                                 encoding="utf-8", errors="replace", **_spawn_options(detached=True))
    except OSError as exc:
        return {"returncode": None, "error": str(exc)}
    deadline = time.monotonic() + min(10.0, spec.cancel_timeout_s)
    ident = None
    with _state_reads_until(deadline):
        while time.monotonic() < deadline:
            record = _record(handle, "finalizer.json")
            launched = processes.identity(child.pid)
            if record is not None and launched is not None:
                candidate = ProcessIdentity.from_dict(record["process"])
                if candidate == launched or candidate in processes.descendants(launched):
                    ident = candidate
                    break
            if child.poll() is not None:
                break
            time.sleep(POLL_S)
    if ident is None:
        launched = processes.identity(child.pid)
        survivors = processes.stop(launched, [], spec.grace_s)
        return {"returncode": None, "error": "Ledger finalizer did not acknowledge dispatch",
                "survivors": [p.to_dict() for p in survivors]}
    _write(handle.run_dir / "finalizer-go.json", {"nonce": handle.nonce, "process": ident.to_dict()})
    try:
        stdout, stderr = child.communicate(timeout=spec.cancel_timeout_s)
    except subprocess.TimeoutExpired:
        survivors = processes.stop(ident, [], spec.grace_s)
        if survivors:
            return {"returncode": None, "error": "Ledger finalizer timed out with surviving processes",
                    "survivors": [p.to_dict() for p in survivors]}
        stdout, stderr = child.communicate(timeout=spec.grace_s)
        return {"returncode": None, "error": "Ledger finalizer timed out", "stdout": stdout, "stderr": stderr}
    survivors = processes.stop(ident, [], spec.grace_s)
    if survivors:
        return {"returncode": None, "error": "Ledger finalizer left surviving processes",
                "survivors": [p.to_dict() for p in survivors]}
    return {"returncode": child.returncode, "stdout": stdout, "stderr": stderr}


def _settle(handle: ProcessHandle, spec: JobProcessSpec, *, state: str, reason: str,
            returncode: int | None = None) -> dict[str, Any]:
    child, recorded = _tree(handle)
    survivors = processes.stop(child, recorded, spec.grace_s)
    result: dict[str, Any] = {"nonce": handle.nonce, "state": state, "returncode": returncode,
                              "detail": reason, "survivors": [p.to_dict() for p in survivors]}
    if survivors:
        result["state"] = "indeterminate"
        result["detail"] = "Owned descendants survived shutdown; ledger untouched"
    elif state != "finished" and child is not None and _record(handle, "go.json") is not None:
        outcome = _finish_ledger(handle, spec, reason)
        result["cancel"] = outcome
        if "returncode" in outcome and outcome["returncode"] != 0:
            result["state"] = "indeterminate"
            result["detail"] = "Job process stopped but ledger cancellation failed"
    else:
        result["cancel"] = {"skipped": "completed" if state == "finished" else "no-dispatch"}
    _write(handle.run_dir / "result.json", result)
    if result["state"] != "indeterminate":
        _release(handle, spec)
    return result


def adopt(handle: ProcessHandle | Path) -> ProcessStatus:
    """Keep a live owner; recover a vanished owner's tree without resuming work."""
    status = inspect(handle)
    handle, spec = _load(status.handle)
    previous_owner = _record(handle, "runner.json")
    try:
        with _lock(handle.run_dir / "owner.lock", blocking=False):
            runner = _record(handle, "runner.json")
            if runner is not None and processes.live(ProcessIdentity.from_dict(runner["process"])) is not None:
                return inspect(handle)
            result = _record(handle, "result.json")
            if result is not None and result["state"] in TERMINAL_STATES and not status.survivors:
                # A supervisor may die after its receipt but before lease release.
                assert spec.lease_path is not None
                lease = _read(spec.lease_path, handle=handle)
                if lease is not None and lease.get("nonce") == handle.nonce and not lease.get("released"):
                    _release(handle, spec)
                return inspect(handle)
            _verify_lease(handle, spec)
            _write(handle.run_dir / "cancel.json", {"nonce": handle.nonce, "reason": "Supervisor lost; recovering owned processes"})
    except BlockingIOError:
        return inspect(handle)
    # Recovery is itself detached and supervised. In particular a Windows
    # ledger-finalizer tree must belong to a kill-on-close job even if the
    # adopting manager subsequently dies. Racing adopters contend on owner.lock.
    worker = Path(__file__).with_name("_supervisor_worker.py")
    try:
        with (handle.run_dir / "supervisor.log").open("ab") as log:
            subprocess.Popen([sys.executable, str(worker), "recover", str(handle.run_dir), handle.nonce],
                             cwd=spec.cwd, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                             **_spawn_options(detached=True))
    except OSError as exc:
        raise SupervisorError(f"Cannot launch recovery supervisor: {exc}", handle=handle) from exc
    deadline = time.monotonic() + 10.0
    with _state_reads_until(deadline):
        while time.monotonic() < deadline:
            status = inspect(handle)
            if status.state in TERMINAL_STATES and not status.alive:
                return status
            # A failed finalizer is also a settled recovery outcome, with its lease
            # retained. The supervisor's exit is the proof no finalizer still runs.
            runner = _record(handle, "runner.json")
            if status.result is not None and runner is not None and runner != previous_owner and not status.alive:
                return status
            time.sleep(POLL_S)
        return inspect(handle)


def cancel(handle: ProcessHandle | Path, reason: str, *, wait_s: float = 10.0) -> ProcessStatus:
    """Request tree shutdown, then ledger cancellation; never signal a bare PID."""
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("Cancellation needs a nonempty reason")
    _positive(wait_s, "wait_s")
    handle, spec = _load(handle)
    status = inspect(handle)
    if status.state in TERMINAL_STATES and not status.alive:
        return status
    _verify_lease(handle, spec)
    _write(handle.run_dir / "cancel.json", {"nonce": handle.nonce, "reason": reason.strip()})
    deadline = time.monotonic() + wait_s
    with _state_reads_until(deadline):
        while time.monotonic() < deadline:
            status = inspect(handle)
            if status.state in TERMINAL_STATES and not status.alive:
                return status
            if _record(handle, "runner.json") is None or not status.alive:
                return adopt(handle)
            if status.state == "indeterminate":
                return adopt(handle)
            time.sleep(POLL_S)
    return status


__all__ = ["JobProcessSpec", "ProcessHandle", "ProcessIdentity", "ProcessStatus", "SupervisorError",
           "SupervisorUnsupported", "start", "inspect", "adopt", "cancel"]
