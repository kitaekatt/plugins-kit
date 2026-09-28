"""llama-swap model swapper: HTTP client, lifecycle, and process safety rules.

A llama-swap swapper fronts several local model servers on one port and
starts the requested server as its own child process. This module is the
library the ``swapper`` CLI verbs and the launcher guard are thin facades
over. It owns four things that change together:

- the swapper's HTTP surface (``GET /running``, ``POST /api/models/unload``) and the
  schema of its ``/running`` payload;
- the rules for which local PID may be signalled on a model's behalf;
- the TERM -> bounded wait -> KILL lifecycle;
- stray detection and the launch guard, which reuse the same process graph.

Safety rules for :func:`terminate_model` (each has a test):

1. The swapper URL and the model's ``/running`` proxy URL are loopback. A
   non-loopback target raises :class:`NonLocalTargetError`; terminate and
   unload are operator actions on the owning host.
2. Exactly one same-user process whose exact name is ``llama-swap`` listens
   on the swapper's port.
3. The PID to signal is a DIRECT child of that swapper (parent PID equal)
   and listens on the model's proxy port. Nothing is ever matched by name
   pattern or command-line pattern and then signalled.
4. The PID's ``create_time`` is re-checked immediately before each signal,
   so a reused PID is never signalled.
5. SIGTERM first; SIGKILL only if the process outlives the grace period.
6. Access denied is :class:`InspectionIndeterminate` (CLI exit 5), never
   "not found".

The OS-facing half lives in :mod:`llm_scripting_kit._swapper_process`,
imported lazily so HTTP-only use needs neither that module nor ``psutil``.
"""

from __future__ import annotations

import ipaddress
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional, Tuple
from urllib.parse import urlsplit

if TYPE_CHECKING:  # pragma: no cover
    from ._swapper_process import Inspector, ProcessRecord


# ---------------------------------------------------------------------------
# Errors. ``exit_code`` is the CLI contract, so the facade maps without logic.
# ---------------------------------------------------------------------------

EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_INDETERMINATE = 5


class SwapperError(Exception):
    """Base error. Operation failure unless a subclass says otherwise."""

    exit_code = EXIT_FAILURE


class SwapperUsageError(SwapperError):
    """Bad arguments or configuration; nothing was attempted."""

    exit_code = EXIT_USAGE


class NonLocalTargetError(SwapperUsageError):
    """A process-affecting action was aimed at a non-loopback swapper."""


class SwapperHTTPError(SwapperError):
    """The swapper was unreachable or answered with an HTTP error."""


class SwapperProtocolError(SwapperError):
    """The swapper answered, but not with the documented payload shape."""


class ModelNotRunningError(SwapperError):
    """The requested model is not in the swapper's ``/running`` set."""


class SafetyRefusal(SwapperError):
    """A safety rule failed, so no signal was sent."""


class ProcessChangedError(SafetyRefusal):
    """The verified process vanished or changed identity before a signal."""


class LaunchRefused(SafetyRefusal):
    """A manual launch was refused while a same-user swapper is active.

    A distinct exit code (3), not EXIT_FAILURE's 1: an uncaught Python
    exception also exits 1, so a caller that must fail OPEN on anything but
    an explicit refusal (model-server.sh) cannot treat exit 1 as meaning
    "refused" -- see swapper-cli-u3.
    """

    exit_code = 3


class TerminationFailed(SwapperError):
    """The process survived SIGTERM, the grace period, and SIGKILL."""


class UnloadIncomplete(SwapperError):
    """The unload request returned, but ``/running`` did not become empty."""


class ProcessNotFound(SwapperError):
    """A PID does not exist, or was reused (different create_time)."""


class InspectionIndeterminate(SwapperError):
    """The process table could not be read well enough to decide."""

    exit_code = EXIT_INDETERMINATE


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunningModel:
    """One entry of the swapper's ``/running`` payload."""

    model: str
    state: str
    proxy: str
    cmd: str


@dataclass(frozen=True)
class ManagedModelProcess:
    """A model server PID verified as the swapper's direct child."""

    model: RunningModel
    swapper_pid: int
    pid: int
    create_time: float


@dataclass(frozen=True)
class TerminationResult:
    """``signal`` is the last signal sent; ``escalated`` means SIGKILL was needed."""

    process: ManagedModelProcess
    signal: str
    escalated: bool


# ---------------------------------------------------------------------------
# URLs and locality
# ---------------------------------------------------------------------------

SWAPPER_NAME = "llama-swap"

#: Exact process names of the model servers this module recognises.
SERVER_NAMES = frozenset({"ninfer-serve", "llama-server"})

#: argv token that identifies an MLX server. Its process name is the Python
#: interpreter, so it is recognised by argv only, and only for reporting.
MLX_SERVER_MODULE = "mlx_lm.server"

#: llama-swap endpoint that unloads every resident model (POST, no body).
UNLOAD_PATH = "/api/models/unload"

_WILDCARD_IPS = frozenset({"0.0.0.0", "::", ""})


def _host_is_loopback(host: Optional[str]) -> bool:
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def is_loopback_url(url: str) -> bool:
    """True when ``url``'s host is ``localhost`` or a loopback IP literal."""
    return _host_is_loopback(urlsplit(url).hostname)


def _require_loopback(url: str, action: str) -> None:
    if not is_loopback_url(url):
        raise NonLocalTargetError(
            f"refusing to {action} through non-loopback swapper {url}; "
            "run it on the host that owns the swapper"
        )


def _port_of(url: str) -> int:
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError as exc:
        raise SwapperUsageError(f"invalid port in URL {url!r}") from exc
    if port is not None:
        return port
    if parts.scheme == "https":
        return 443
    if parts.scheme == "http":
        return 80
    raise SwapperUsageError(f"URL {url!r} has no port and no http(s) scheme")


def normalize_swapper_url(url: str) -> str:
    """Validate an http(s) URL and strip a trailing ``/`` and ``/v1``."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise SwapperUsageError(f"swapper URL must be http(s)://host[:port], got {url!r}")
    base = url.strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return base


def resolve_swapper_url(
    *, endpoint: Optional[str], url: Optional[str], registry=None
) -> str:
    """The swapper base URL from exactly one of a registry entry or a URL.

    A registry entry's ``base_url`` is the OpenAI-compatible base, so the
    trailing ``/v1`` is removed to reach the swapper's own API root.
    """
    if (endpoint is None) == (url is None):
        raise SwapperUsageError("give exactly one of an endpoint name or a URL")
    if url is not None:
        return normalize_swapper_url(url)
    from .model_endpoints import (  # noqa: PLC0415 -- keep import light
        HARNESS_KIND,
        EndpointRegistryError,
        harness_entry_message,
        resolve_registry_entry,
    )

    try:
        entry = resolve_registry_entry(endpoint, registry=registry)
    except EndpointRegistryError as exc:
        raise SwapperUsageError(str(exc)) from exc
    if entry.kind == HARNESS_KIND or not entry.base_url:
        raise SwapperUsageError(harness_entry_message(entry.id, entry.harness))
    return normalize_swapper_url(entry.base_url)


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------

#: (method, url, timeout_s) -> response body. Raises SwapperHTTPError.
Transport = Callable[[str, str, float], bytes]


def _urllib_transport(method: str, url: str, timeout_s: float) -> bytes:
    data = b"" if method == "POST" else None
    req = urllib.request.Request(url, data=data, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise SwapperHTTPError(f"{method} {url} returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise SwapperHTTPError(f"{method} {url} failed: {exc}") from exc


def parse_running(body: bytes) -> Tuple[RunningModel, ...]:
    """Validate and parse a ``/running`` payload."""
    try:
        doc = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SwapperProtocolError(f"/running did not return JSON: {exc}") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("running"), list):
        raise SwapperProtocolError("/running payload must be an object with a 'running' list")
    out = []
    for i, item in enumerate(doc["running"]):
        if not isinstance(item, dict):
            raise SwapperProtocolError(f"/running entry {i} is not an object")
        fields = {}
        for key in ("model", "state", "proxy"):
            value = item.get(key)
            if not isinstance(value, str) or not value:
                raise SwapperProtocolError(
                    f"/running entry {i} lacks a non-empty string '{key}'"
                )
            fields[key] = value
        cmd = item.get("cmd", "")
        if not isinstance(cmd, str):
            raise SwapperProtocolError(f"/running entry {i} has a non-string 'cmd'")
        out.append(RunningModel(cmd=cmd, **fields))
    return tuple(out)


class SwapperClient:
    """HTTP access to one llama-swap swapper."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 5.0,
        transport: Optional[Transport] = None,
    ) -> None:
        self.base_url = normalize_swapper_url(base_url)
        self.timeout_s = timeout_s
        self._transport = transport or _urllib_transport

    @property
    def port(self) -> int:
        return _port_of(self.base_url)

    @property
    def is_local(self) -> bool:
        return is_loopback_url(self.base_url)

    def running(self) -> Tuple[RunningModel, ...]:
        """The resident models. Read-only; allowed for a remote swapper."""
        body = self._transport("GET", f"{self.base_url}/running", self.timeout_s)
        return parse_running(body)

    def unload_all(
        self, *, settle_s: float = 10.0, poll_s: float = 0.25
    ) -> Tuple[RunningModel, ...]:
        """Unload every resident model and verify ``/running`` becomes empty.

        Returns the models that were resident before the unload. Refuses a
        non-loopback swapper. In-flight requests are not drained.
        """
        _require_loopback(self.base_url, "unload models")
        before = self.running()
        self._transport("POST", f"{self.base_url}{UNLOAD_PATH}", self.timeout_s)
        deadline = time.monotonic() + max(0.0, settle_s)
        while True:
            remaining = self.running()
            if not remaining:
                return before
            if time.monotonic() >= deadline:
                names = ", ".join(m.model for m in remaining)
                raise UnloadIncomplete(f"/running still lists {names} after POST {UNLOAD_PATH}")
            time.sleep(poll_s)


# ---------------------------------------------------------------------------
# Process rules
# ---------------------------------------------------------------------------

_MAX_ANCESTRY = 64


def _default_inspector() -> "Inspector":
    from ._swapper_process import PsutilInspector  # noqa: PLC0415 -- lazy psutil

    return PsutilInspector()


def _norm(name: Optional[str]) -> Optional[str]:
    from ._swapper_process import normalize_name  # noqa: PLC0415

    return normalize_name(name)


def _listens_on(addrs, port: int) -> bool:
    """A loopback or wildcard listener on ``port`` (what a loopback URL reaches)."""
    return any(p == port and (ip in _WILDCARD_IPS or _host_is_loopback(ip)) for ip, p in addrs)


def _find_swapper(
    insp: "Inspector", procs, owner: str, port: int
) -> "ProcessRecord":
    matches = []
    for rec in procs:
        if _norm(rec.name) != SWAPPER_NAME:
            continue
        if rec.owner is None:
            raise InspectionIndeterminate(f"cannot read the owner of {SWAPPER_NAME} PID {rec.pid}")
        if rec.owner != owner:
            continue
        try:
            addrs = insp.listen_addrs(rec.pid)
        except ProcessNotFound:
            continue
        if _listens_on(addrs, port):
            matches.append(rec)
    if not matches:
        raise SafetyRefusal(
            f"no same-user process named exactly '{SWAPPER_NAME}' listens on port {port}"
        )
    if len(matches) > 1:
        pids = ", ".join(str(r.pid) for r in matches)
        raise SafetyRefusal(f"several {SWAPPER_NAME} processes listen on port {port}: {pids}")
    return matches[0]


def _verify_identity(insp: "Inspector", pid: int, create_time: float, parent: int) -> None:
    """Re-read ``pid`` and confirm it is still the verified swapper child."""
    rec = insp.process(pid)  # ProcessNotFound / InspectionIndeterminate propagate
    if rec.create_time != create_time:
        raise ProcessNotFound(f"PID {pid} was reused (create_time changed)")
    if rec.ppid != parent:
        raise ProcessChangedError(
            f"PID {pid} is no longer a direct child of {SWAPPER_NAME} PID {parent}"
        )


def locate_managed_model(
    client: SwapperClient, model: str, *, inspector: Optional["Inspector"] = None
) -> ManagedModelProcess:
    """Find and verify the PID serving ``model`` for a local swapper.

    Read-only. Raises :class:`ModelNotRunningError`, :class:`SafetyRefusal`,
    :class:`NonLocalTargetError`, or :class:`InspectionIndeterminate`.
    """
    _require_loopback(client.base_url, "inspect processes")
    matches = [m for m in client.running() if m.model == model]
    if not matches:
        raise ModelNotRunningError(f"model {model!r} is not in {client.base_url}/running")
    if len(matches) > 1:
        raise SafetyRefusal(f"/running lists model {model!r} more than once")
    entry = matches[0]
    if not is_loopback_url(entry.proxy):
        raise SafetyRefusal(f"model {model!r} proxies to non-loopback {entry.proxy}")
    proxy_port = _port_of(entry.proxy)

    insp = inspector if inspector is not None else _default_inspector()
    owner = insp.current_owner()
    procs = insp.processes()
    swapper = _find_swapper(insp, procs, owner, client.port)

    listeners = []
    unreadable = []
    for rec in procs:
        if rec.ppid is None:
            unreadable.append(rec.pid)
            continue
        # Parent verification: only a DIRECT child of the swapper qualifies.
        if rec.ppid != swapper.pid:
            continue
        if rec.owner != owner:
            continue
        try:
            addrs = insp.listen_addrs(rec.pid)
        except ProcessNotFound:
            continue
        if _listens_on(addrs, proxy_port):
            listeners.append(rec)

    if not listeners:
        if unreadable:
            raise InspectionIndeterminate(
                f"no readable direct child of {SWAPPER_NAME} PID {swapper.pid} listens on "
                f"port {proxy_port}, and the parent of PID(s) "
                f"{', '.join(map(str, unreadable))} is unreadable"
            )
        raise SafetyRefusal(
            f"no direct child of {SWAPPER_NAME} PID {swapper.pid} listens on "
            f"proxy port {proxy_port} for model {model!r}"
        )
    if len(listeners) > 1:
        pids = ", ".join(str(r.pid) for r in listeners)
        raise SafetyRefusal(f"several swapper children listen on port {proxy_port}: {pids}")

    child = listeners[0]
    try:
        _verify_identity(insp, child.pid, child.create_time, swapper.pid)
    except ProcessNotFound as exc:
        raise ProcessChangedError(f"PID {child.pid} changed during verification: {exc}") from exc
    return ManagedModelProcess(
        model=entry, swapper_pid=swapper.pid, pid=child.pid, create_time=child.create_time
    )


def terminate_model(
    client: SwapperClient,
    model: str,
    *,
    grace_s: float = 10.0,
    kill_wait_s: float = 5.0,
    inspector: Optional["Inspector"] = None,
) -> TerminationResult:
    """SIGTERM the verified server for ``model``; SIGKILL only on timeout."""
    from ._swapper_process import SIGKILL, SIGTERM  # noqa: PLC0415

    if grace_s < 0 or kill_wait_s < 0:
        raise SwapperUsageError("grace and kill-wait seconds must be >= 0")
    _require_loopback(client.base_url, "terminate a model")
    insp = inspector if inspector is not None else _default_inspector()
    proc = locate_managed_model(client, model, inspector=insp)

    try:
        _verify_identity(insp, proc.pid, proc.create_time, proc.swapper_pid)
        insp.signal(proc.pid, proc.create_time, SIGTERM)
    except ProcessNotFound as exc:
        raise ProcessChangedError(f"not signalled: {exc}") from exc
    if insp.wait_gone(proc.pid, proc.create_time, grace_s):
        return TerminationResult(process=proc, signal=SIGTERM, escalated=False)

    try:
        _verify_identity(insp, proc.pid, proc.create_time, proc.swapper_pid)
        insp.signal(proc.pid, proc.create_time, SIGKILL)
    except ProcessNotFound:
        # It exited between the timeout and the re-check: SIGTERM sufficed.
        return TerminationResult(process=proc, signal=SIGTERM, escalated=False)
    if insp.wait_gone(proc.pid, proc.create_time, kill_wait_s):
        return TerminationResult(process=proc, signal=SIGKILL, escalated=True)
    raise TerminationFailed(f"PID {proc.pid} survived SIGTERM and SIGKILL")


# ---------------------------------------------------------------------------
# Strays and the launch guard
# ---------------------------------------------------------------------------


def classify_server(rec: "ProcessRecord") -> Optional[bool]:
    """True for a recognised model server, False if not, None if unreadable."""
    name = _norm(rec.name)
    if name is None:
        return None
    if name in SERVER_NAMES:
        return True
    if rec.cmdline is None:
        return None if name.lower().startswith("python") else False
    for arg in rec.cmdline:
        base = arg.replace("\\", "/").rstrip("/")
        if base == MLX_SERVER_MODULE or base.endswith("/" + MLX_SERVER_MODULE):
            return True
        if base.endswith("mlx_lm/server.py"):
            return True
    return False


def _has_swapper_ancestor(insp: "Inspector", rec: "ProcessRecord", by_pid) -> bool:
    """Walk parent PIDs from ``rec`` (inclusive) looking for llama-swap."""
    seen = set()
    current: Optional["ProcessRecord"] = rec
    for _ in range(_MAX_ANCESTRY):
        if current is None or current.pid in seen:
            return False
        seen.add(current.pid)
        name = _norm(current.name)
        if name is None:
            raise InspectionIndeterminate(f"cannot read the name of PID {current.pid}")
        if name == SWAPPER_NAME:
            return True
        ppid = current.ppid
        if ppid is None:
            raise InspectionIndeterminate(f"cannot read the parent of PID {current.pid}")
        if ppid <= 0 or ppid == current.pid:
            return False
        current = by_pid.get(ppid)
        if current is None:
            try:
                current = insp.process(ppid)
            except ProcessNotFound:
                return False
    return False


def find_strays(
    *, inspector: Optional["Inspector"] = None, uid=None
) -> Tuple["ProcessRecord", ...]:
    """Recognised model servers of one user with no llama-swap ancestor.

    ``uid`` defaults to the calling user. Detection only; nothing is signalled.
    """
    insp = inspector if inspector is not None else _default_inspector()
    owner = str(uid) if uid is not None else insp.current_owner()
    procs = insp.processes()
    by_pid = {p.pid: p for p in procs}
    strays = []
    for rec in procs:
        if rec.owner is not None and rec.owner != owner:
            continue
        kind = classify_server(rec)
        if kind is False:
            continue
        if kind is None or rec.owner is None:
            raise InspectionIndeterminate(f"cannot classify PID {rec.pid}: details unreadable")
        if not _has_swapper_ancestor(insp, rec, by_pid):
            strays.append(rec)
    return tuple(strays)


def check_launch_allowed(
    *, caller_pid: Optional[int] = None, inspector: Optional["Inspector"] = None
) -> None:
    """Allow a launch under llama-swap; refuse a manual one beside a swapper.

    ``caller_pid`` (default: this process) and its ancestors are searched for
    llama-swap. If none is found and a same-user llama-swap is running, the
    launch would bypass it, so :class:`LaunchRefused` is raised.
    """
    insp = inspector if inspector is not None else _default_inspector()
    pid = caller_pid if caller_pid is not None else os.getpid()
    try:
        caller = insp.process(pid)
    except ProcessNotFound as exc:
        raise SwapperUsageError(f"caller PID {pid} does not exist") from exc
    procs = insp.processes()
    by_pid = {p.pid: p for p in procs}
    if _has_swapper_ancestor(insp, caller, by_pid):
        return
    owner = insp.current_owner()
    active = []
    for rec in procs:
        name = _norm(rec.name)
        if name is None:
            if rec.owner in (owner, None):
                raise InspectionIndeterminate(f"cannot read the name of PID {rec.pid}")
            continue
        if name != SWAPPER_NAME:
            continue
        if rec.owner is None:
            raise InspectionIndeterminate(f"cannot read the owner of {SWAPPER_NAME} PID {rec.pid}")
        if rec.owner == owner:
            active.append(rec.pid)
    if active:
        raise LaunchRefused(
            f"{SWAPPER_NAME} is running for this user (PID "
            f"{', '.join(map(str, active))}); launch the model through it, "
            "or stop it first"
        )


__all__ = [
    "EXIT_FAILURE",
    "EXIT_USAGE",
    "EXIT_INDETERMINATE",
    "SwapperError",
    "SwapperUsageError",
    "NonLocalTargetError",
    "SwapperHTTPError",
    "SwapperProtocolError",
    "ModelNotRunningError",
    "SafetyRefusal",
    "ProcessChangedError",
    "LaunchRefused",
    "TerminationFailed",
    "UnloadIncomplete",
    "ProcessNotFound",
    "InspectionIndeterminate",
    "RunningModel",
    "ManagedModelProcess",
    "TerminationResult",
    "SWAPPER_NAME",
    "SERVER_NAMES",
    "MLX_SERVER_MODULE",
    "UNLOAD_PATH",
    "SwapperClient",
    "is_loopback_url",
    "normalize_swapper_url",
    "resolve_swapper_url",
    "parse_running",
    "locate_managed_model",
    "terminate_model",
    "classify_server",
    "find_strays",
    "check_launch_allowed",
]
