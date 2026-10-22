"""The ``task-kit`` command: start, reuse, restart, stop and report the dashboard.

Typing ``task-kit`` launches the dashboard. If a server is already running and
runs the code that is on disk, it is reused; if the code changed, the server is
restarted (unless jobs are running); if none is running, one is started
detached, and the browser is opened on it.

Ownership is never guessed. There is no port scan and no ``lsof``/``ps``: the
launcher trusts only the ``server.json`` record, accepts a process as the
recorded server only when its PID, its creation time (via ``psutil``) AND the
instance nonce its health endpoint reports all match, and treats anything else
that answers on the recorded port as foreign. A record it cannot parse stops
the launcher; it never licenses starting a second server or stopping a stranger.

Exit codes: 0 ok; 1 error; 2 usage (``characterize --worker codex`` included); 3 ``status``: not
running; 4 the server is up but the browser could not be opened; 5 refused because jobs are running;
6 refused inside a dashboard run.

``characterize`` runs real Claude sessions that check the limits native sub-agents rely on and writes the
record that turns them on (``subagents.py``). It exits 0 whatever the verdict (the record says what passed),
1 when it cannot run (no Claude Code, llm-kit missing or too old).

Inside a dashboard run every command is refused (exit 6), ``status`` and ``serve``
included. Two tripwires, checked before anything else is read or written: the
``TASK_KIT_AGENT_DEPTH`` marker the run's command inherits, and the process
ancestry (``proctree.dashboard_runner_ancestor``): a live runner of the dashboard
this launcher targets, verified by its own ``runner.json``. An isolated dashboard
(``--data-dir``) is a different root and is not refused. Neither is confinement.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .. import procspawn
from ..datadir import DataDirError, resolve_data_dir
from . import proctree, subagents
from .codehash import code_hash
from .config import DashboardConfigError, load_config
from .jobs_edge import EdgeError, JobsEdge
from .server import EXIT_LOCK_HELD, TOKEN_HEADER
from .server import run as run_server
from .state import (
    LOG_FILE,
    DashboardStateError,
    ServerState,
    ensure_private_root,
    read_state,
    runtime_root,
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_NOT_RUNNING = 3
EXIT_BROWSER = 4
EXIT_BUSY = 5
EXIT_NESTED = 6
AGENT_DEPTH_ENV = "TASK_KIT_AGENT_DEPTH"
NESTED_MESSAGE = (
    "task-kit: refused inside a dashboard run: a run may not start, stop or restart its dashboard, "
    "start another run, or characterize the CLI"
)

READY_TIMEOUT_S = 30.0
READY_POLL_S = 0.2
STOP_TIMEOUT_S = 10.0
PROBE_TIMEOUT_S = 2.0
BROWSER_TIMEOUT_S = 10.0
BROWSER_LOG_FILE = "browser.log"
CREATE_TIME_TOLERANCE_S = 2.0
NO_OPEN_ENV = "TASK_KIT_NO_OPEN"

# Decisions the launcher can reach about the recorded server.
START = "start"  # nothing recorded, or the record is stale
REUSE = "reuse"
RESTART_CODE = "restart-code"


class LauncherError(RuntimeError):
    """A condition the launcher reports (exit 1) rather than works around."""


class BusyError(LauncherError):
    """Refused because jobs are running (exit 5)."""


class BrowserOpenError(RuntimeError):
    """The server is up but the browser could not be opened."""

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(f"could not open the browser ({kind}): {detail}")
        self.kind = kind  # spawn | exit | timeout
        self.detail = detail


class ProbeFailed(Exception):
    """The health endpoint did not give a usable answer."""

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(f"{kind}: {detail}")
        self.kind = kind  # refused | timeout | invalid


# -- process facts ---------------------------------------------------------


def process_create_time(pid: int) -> float | None:
    """The creation time of ``pid``, or None when no such process exists."""
    import psutil

    try:
        return psutil.Process(pid).create_time()
    except psutil.NoSuchProcess:
        return None


def identity_matches(state: ServerState, create_time: float | None) -> bool:
    return create_time is not None and abs(create_time - state.pid_create_time) <= CREATE_TIME_TOLERANCE_S


def probe_health(port: int, timeout: float = PROBE_TIMEOUT_S) -> dict:
    """``GET /api/v1/health`` on the loopback port, or raise :class:`ProbeFailed`."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request("GET", "/api/v1/health")
        response = connection.getresponse()
        raw = response.read()
        if response.status != 200:
            raise ProbeFailed("invalid", f"HTTP {response.status}")
        data = json.loads(raw.decode("utf-8"))
    except ConnectionRefusedError as exc:
        raise ProbeFailed("refused", str(exc)) from exc
    except TimeoutError as exc:
        raise ProbeFailed("timeout", str(exc)) from exc
    except (OSError, http.client.HTTPException) as exc:
        raise ProbeFailed("refused", f"{type(exc).__name__}: {exc}") from exc
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProbeFailed("invalid", f"not JSON ({exc})") from exc
    finally:
        connection.close()
    if not isinstance(data, dict) or not all(
        k in data for k in ("instance", "code_hash", "port", "busy")
    ):
        raise ProbeFailed("invalid", "not a task-kit health document")
    return data


def post_shutdown(state: ServerState, force: bool, timeout: float = PROBE_TIMEOUT_S) -> None:
    """Ask the recorded server (authenticated) to shut down gracefully."""
    body = json.dumps({"force": bool(force)}).encode("utf-8")
    connection = http.client.HTTPConnection("127.0.0.1", state.port, timeout=timeout)
    try:
        connection.request(
            "POST",
            "/api/v1/shutdown",
            body=body,
            headers={
                "Content-Type": "application/json",
                "Origin": f"http://127.0.0.1:{state.port}",
                TOKEN_HEADER: state.token,
            },
        )
        response = connection.getresponse()
        detail = response.read().decode("utf-8", "replace")
    finally:
        connection.close()
    if response.status == 409:
        raise BusyError(f"jobs are running; the server refused to stop ({detail.strip()})")
    if response.status != 200:
        raise LauncherError(f"the server refused the shutdown request: HTTP {response.status} {detail.strip()}")


def spawn_server(entrypoint: Path, data_dir: Path, log_path: Path) -> subprocess.Popen:
    """Start ``serve`` detached: no stdin, stdout and stderr appended to the log.

    The platform's detach flags come from :mod:`procspawn`, the one place that reads the platform.
    """
    ensure_private_root(log_path.parent)
    command = [sys.executable, str(entrypoint), "serve", "--data-dir", str(data_dir)]
    kwargs: dict = {"stdin": subprocess.DEVNULL, "close_fds": True, "cwd": str(log_path.parent),
                    **procspawn.detached(windows=procspawn.WINDOWS)}
    with open(log_path, "ab") as log:
        return subprocess.Popen(command, stdout=log, stderr=log, **kwargs)


def _log_tail(path: Path, offset: int) -> str:
    """The last non-empty line the opener wrote since ``offset`` (at most 300 characters)."""
    with open(path, "rb") as handle:
        handle.seek(offset)
        text = handle.read().decode("utf-8", "replace")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1][-300:] if lines else ""


def open_browser(
    url: str,
    *,
    platform: str | None = None,
    startfile: Callable[[str], None] | None = None,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    popen: Callable[..., subprocess.Popen] = subprocess.Popen,
    log_path: Path | None = None,
    timeout: float = BROWSER_TIMEOUT_S,
) -> str:
    """Ask the platform to open ``url``, or raise :class:`BrowserOpenError`.

    Returns a sentence naming what was confirmed. It never claims a window
    opened: the opener's exit status is all the launcher can see.

    - Windows: ``os.startfile``. A spawn ``OSError`` is kind ``spawn``.
    - macOS: ``open`` is a run-to-completion acceptor, judged by its exit
      status. A spawn error, a nonzero exit and the deadline are three kinds.
    - Linux: ``xdg-open`` may BE the browser (it can run one in the
      foreground), so it is started in its own session with its output
      appended to ``log_path`` (a file, never DEVNULL) and waited on for at
      most ``timeout`` seconds. Exit 0 is accepted, a nonzero exit is kind
      ``exit`` carrying the log's last line, and still running at the deadline
      is accepted and left running.
    """
    platform = sys.platform if platform is None else platform
    if platform == "win32":
        opener = startfile if startfile is not None else getattr(os, "startfile")
        try:
            opener(url)
        except OSError as exc:
            raise BrowserOpenError("spawn", str(exc)) from exc
        return "asked Windows to open the URL"
    if platform == "darwin":
        command = ["open", url]
        try:
            completed = run(command, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError as exc:
            raise BrowserOpenError("spawn", f"open not found: {exc}") from exc
        except OSError as exc:
            raise BrowserOpenError("spawn", str(exc)) from exc
        except subprocess.TimeoutExpired as exc:
            raise BrowserOpenError("timeout", f"open did not return within {timeout:g}s") from exc
        if completed.returncode != 0:
            raise BrowserOpenError("exit", f"open exited {completed.returncode}: {(completed.stderr or '').strip()}")
        return "asked open to open the URL"
    if log_path is None:
        raise ValueError("opening a browser on this platform needs log_path for the opener's output")
    command = ["xdg-open", url]
    log_path = Path(log_path)
    offset = log_path.stat().st_size if log_path.exists() else 0
    try:
        with open(log_path, "ab") as log:
            process = popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                            close_fds=True, start_new_session=True)
    except FileNotFoundError as exc:
        raise BrowserOpenError("spawn", f"xdg-open not found: {exc}") from exc
    except OSError as exc:
        raise BrowserOpenError("spawn", str(exc)) from exc
    try:
        code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return (f"asked xdg-open to open the URL (still running after {timeout:g}s; it may be the browser itself; "
                f"its output goes to {log_path})")
    if code != 0:
        raise BrowserOpenError("exit", f"xdg-open exited {code}: {_log_tail(log_path, offset)} (see {log_path})")
    return "asked xdg-open to open the URL"


# -- the launcher ----------------------------------------------------------


@dataclass(frozen=True)
class Observation:
    """What the launcher can establish about the recorded server."""

    state: ServerState | None
    process_matches: bool
    health: dict | None
    probe_error: ProbeFailed | None


class Launcher:
    """start / reuse / restart / stop / status, with every effect injectable."""

    def __init__(
        self,
        *,
        data_dir: Path,
        scripts_dir: Path,
        entrypoint: Path,
        out=None,
        err=None,
        spawn: Callable[[Path, Path, Path], subprocess.Popen] = spawn_server,
        probe: Callable[[int], dict] = probe_health,
        create_time: Callable[[int], float | None] = process_create_time,
        shutdown: Callable[[ServerState, bool], None] = post_shutdown,
        browser: Callable[[str], str | None] | None = None,
        kill: Callable[[ServerState], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        ready_timeout: float = READY_TIMEOUT_S,
        stop_timeout: float = STOP_TIMEOUT_S,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.scripts_dir = Path(scripts_dir)
        self.entrypoint = Path(entrypoint)
        self.root = runtime_root(self.data_dir)
        self.out = out if out is not None else sys.stdout
        self.err = err if err is not None else sys.stderr
        self._spawn = spawn
        self._probe = probe
        self._create_time = create_time
        self._shutdown = shutdown
        self._browser = browser if browser is not None else (
            lambda url: open_browser(url, log_path=self.root / BROWSER_LOG_FILE))
        self._kill = kill if kill is not None else self._kill_verified
        self._sleep = sleep
        self._monotonic = monotonic
        self.ready_timeout = ready_timeout
        self.stop_timeout = stop_timeout

    # -- observation -------------------------------------------------------

    def _say(self, message: str) -> None:
        print(message, file=self.out)

    def _warn(self, message: str) -> None:
        print(message, file=self.err)

    def observe(self) -> Observation:
        try:
            state = read_state(self.root)
        except DashboardStateError as exc:
            raise LauncherError(str(exc)) from exc
        if state is None:
            return Observation(None, False, None, None)
        matches = identity_matches(state, self._create_time(state.pid))
        try:
            health = self._probe(state.port)
            error = None
        except ProbeFailed as exc:
            health, error = None, exc
        return Observation(state, matches, health, error)

    def decide(self, obs: Observation, current_hash: str) -> str:
        """START, REUSE or RESTART_CODE; raise for conditions that need a person."""
        state = obs.state
        if state is None:
            return START
        log = state.log_path
        if obs.process_matches:
            if obs.health is None:
                raise LauncherError(
                    f"the recorded dashboard server (pid {state.pid}) is running but not answering on "
                    f"127.0.0.1:{state.port} ({obs.probe_error}). It was left alone. "
                    f"Run `task-kit stop --force` to stop it (identity-checked), then `task-kit`. Log: {log}"
                )
            if obs.health["instance"] != state.nonce:
                raise LauncherError(
                    f"127.0.0.1:{state.port} answers as a different dashboard instance than the one "
                    f"recorded for pid {state.pid}; refusing to touch either. Log: {log}"
                )
            return REUSE if obs.health["code_hash"] == current_hash else RESTART_CODE
        if obs.health is not None:
            raise LauncherError(
                f"something answers on 127.0.0.1:{state.port}, but the recorded server process "
                f"(pid {state.pid}) is not running with the recorded identity. It is not ours to "
                "stop; refusing to start a second dashboard over it. Delete "
                f"{self.root / 'server.json'} only after confirming that port is not a task-kit dashboard."
            )
        return START

    # -- commands ----------------------------------------------------------

    def launch(self, *, open_url: bool) -> int:
        current = code_hash(self.scripts_dir)
        obs = self.observe()
        decision = self.decide(obs, current)
        state = obs.state
        if decision == RESTART_CODE:
            busy = int(obs.health["busy"])  # type: ignore[index]
            if busy:
                self._warn(
                    f"task-kit: the dashboard code changed but {busy} job(s) are running, so the "
                    "running server was kept. Run `task-kit restart` once they finish."
                )
                decision = REUSE
            else:
                self._say("task-kit: dashboard code changed; restarting the server")
                self._stop_server(state, force=False)  # type: ignore[arg-type]
                decision = START
        if decision == START:
            state = self._start()
            self._say(f"task-kit: dashboard started (pid {state.pid}, port {state.port})")
        else:
            self._say(f"task-kit: dashboard already running (pid {state.pid}, port {state.port})")  # type: ignore[union-attr]
        url = state.url()  # type: ignore[union-attr]
        self._say(f"task-kit: {url}")
        if open_url:
            try:
                confirmed = self._browser(url)
            except BrowserOpenError as exc:
                self._warn(f"task-kit: {exc}. The server is running; open the URL above yourself.")
                return EXIT_BROWSER
            if confirmed:
                self._say(f"task-kit: {confirmed}")
        return EXIT_OK

    def status(self) -> int:
        obs = self.observe()
        state = obs.state
        if state is None or (not obs.process_matches and obs.health is None):
            self._say("task-kit: dashboard not running")
            if state is not None:
                self._say(f"  (stale record {self.root / 'server.json'}: pid {state.pid} is gone)")
            return EXIT_NOT_RUNNING
        current = code_hash(self.scripts_dir)
        decision = self.decide(obs, current)  # raises for unresponsive / foreign
        self._say("task-kit: dashboard running")
        self._say(f"  url:  {state.url()}")
        self._say(f"  pid:  {state.pid}   started: {state.started_at}")
        self._say(
            "  code: "
            + ("current" if decision == REUSE else "OUT OF DATE (run `task-kit restart`)")
        )
        self._say(f"  jobs: {obs.health['busy']} running")  # type: ignore[index]
        self._say(f"  log:  {state.log_path}")
        return EXIT_OK

    def stop(self, *, force: bool) -> int:
        obs = self.observe()
        state = obs.state
        if state is None or (not obs.process_matches and obs.health is None):
            self._say("task-kit: dashboard not running")
            return EXIT_OK
        if obs.process_matches and obs.health is None:
            if not force:
                self.decide(obs, "")  # raises the unresponsive explanation
            self._kill(state)
            self._say(f"task-kit: dashboard stopped (pid {state.pid}, forced)")
            return EXIT_OK
        self.decide(obs, "")  # raises for a foreign answer
        busy = int(obs.health["busy"])  # type: ignore[index]
        if busy and not force:
            raise BusyError(
                f"{busy} job(s) are running; stop is refused. Wait for them, or use "
                "`task-kit stop --force`."
            )
        self._stop_server(state, force=force)
        self._say(f"task-kit: dashboard stopped (pid {state.pid})")
        return EXIT_OK

    def restart(self, *, force: bool, open_url: bool) -> int:
        self.stop(force=force)
        return self.launch(open_url=open_url)

    # -- mechanics ---------------------------------------------------------

    def _wait_gone(self, state: ServerState) -> bool:
        deadline = self._monotonic() + self.stop_timeout
        while True:
            if not identity_matches(state, self._create_time(state.pid)):
                return True
            if self._monotonic() >= deadline:
                return False
            self._sleep(0.1)

    def _stop_server(self, state: ServerState, *, force: bool) -> None:
        self._shutdown(state, force)
        if self._wait_gone(state):
            return
        if force:
            self._kill(state)
            return
        raise LauncherError(
            f"the dashboard server (pid {state.pid}) did not exit within {self.stop_timeout:g}s of "
            "the shutdown request. Run `task-kit stop --force` to stop it."
        )

    def _kill_verified(self, state: ServerState) -> None:
        """Terminate the recorded server and its children after re-verifying identity."""
        import psutil

        try:
            process = psutil.Process(state.pid)
        except psutil.NoSuchProcess:
            return
        if not identity_matches(state, process.create_time()):
            raise LauncherError(
                f"pid {state.pid} no longer has the recorded creation time; refusing to kill it"
            )
        victims = process.children(recursive=True) + [process]
        for victim in victims:
            try:
                victim.terminate()
            except psutil.NoSuchProcess:
                pass
        _gone, alive = psutil.wait_procs(victims, timeout=5)
        for victim in alive:
            try:
                victim.kill()
            except psutil.NoSuchProcess:
                pass
        psutil.wait_procs(alive, timeout=5)

    def _start(self) -> ServerState:
        log_path = self.root / LOG_FILE
        process = self._spawn(self.entrypoint, self.data_dir, log_path)
        deadline = self._monotonic() + self.ready_timeout
        while True:
            returncode = process.poll()
            if returncode is not None and returncode != EXIT_LOCK_HELD:
                raise LauncherError(
                    f"the dashboard server exited with code {returncode} while starting. Log: {log_path}"
                )
            # EXIT_LOCK_HELD: another launcher's server won the lock; keep waiting for that one.
            try:
                state = read_state(self.root)
            except DashboardStateError as exc:
                raise LauncherError(str(exc)) from exc
            if state is not None:
                try:
                    health = self._probe(state.port)
                except ProbeFailed:
                    health = None
                if health is not None and health["instance"] == state.nonce and identity_matches(
                    state, self._create_time(state.pid)
                ):
                    return state
            if self._monotonic() >= deadline:
                raise LauncherError(
                    f"the dashboard server did not become ready within {self.ready_timeout:g}s. "
                    f"Log: {log_path}"
                )
            self._sleep(READY_POLL_S)


# -- command line ----------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="task-kit",
        description="Launch the task dashboard: one local server over every configured project's tasks.",
        epilog=(
            "With no command, task-kit starts the dashboard (or reuses the running one, restarting "
            "it if its code changed) and opens it in your browser. Exit codes: 0 ok, 1 error, "
            "2 usage, 3 status: not running, 4 browser could not be opened, 5 jobs are running, "
            "6 refused inside a dashboard run."
        ),
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=["status", "stop", "restart", "serve", "characterize"],
        help="status: report the server; stop: stop it; restart: stop and start it; "
        "serve: run the server in the foreground (used by the launcher); "
        "characterize: check the installed Claude Code's sub-agent limits with a few short real sessions "
        "and record whether runs may use native sub-agents",
    )
    parser.add_argument("--worker", choices=["claude", "codex"], default=None,
                        help="characterize: the worker to check (only claude has native sub-agents here)")
    parser.add_argument("--entry", help="characterize: the model entry the sessions use (default: dashboard.claude_entry)")
    parser.add_argument("--no-open", action="store_true", help=f"do not open the browser (also {NO_OPEN_ENV}=1)")
    parser.add_argument("--force", action="store_true", help="stop/restart even if jobs are running or the server is unresponsive")
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="use another task-kit data directory (an isolated second dashboard); "
        "default: the data directory of the venv this runs in",
    )
    return parser


def run_characterize(args: argparse.Namespace, data_dir: Path, scripts_dir: Path) -> int:
    """``task-kit characterize``: the sessions, the judgement, the record. Exit 0 whatever the verdict; 1 when it cannot run."""
    if args.worker not in (None, "claude"):
        print("task-kit: Codex workers are refused, so there is nothing to characterize for them", file=sys.stderr)
        return EXIT_USAGE
    try:
        subagents.characterize(
            root=runtime_root(data_dir), edge=JobsEdge(), config=load_config(), python=sys.executable,
            helper_cli=Path(__file__).with_name("helper_cli.py"), scripts_dir=scripts_dir, entry=args.entry)
    except (DashboardConfigError, subagents.CharacterizeError, EdgeError) as exc:
        print(f"task-kit: {exc}", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_OK


def main(argv: list[str] | None, *, scripts_dir: Path, entrypoint: Path) -> int:
    args = build_parser().parse_args(argv)
    if args.command != "characterize" and (args.worker is not None or args.entry is not None):
        build_parser().error("--worker and --entry apply only to the characterize command")
    if AGENT_DEPTH_ENV in os.environ:  # tripwire 1: the marker every run's command inherits
        print(NESTED_MESSAGE, file=sys.stderr)
        return EXIT_NESTED
    try:
        data_dir = args.data_dir if args.data_dir is not None else resolve_data_dir()
    except DataDirError as exc:
        print(f"task-kit: {exc}", file=sys.stderr)
        return EXIT_ERROR
    if proctree.dashboard_runner_ancestor(runtime_root(data_dir)) is not None:  # tripwire 2: the ancestry
        print(NESTED_MESSAGE, file=sys.stderr)
        return EXIT_NESTED
    if args.command == "serve":
        return run_server(data_dir, scripts_dir)
    if args.command == "characterize":
        return run_characterize(args, data_dir, scripts_dir)
    launcher = Launcher(data_dir=data_dir, scripts_dir=scripts_dir, entrypoint=entrypoint)
    open_url = not (args.no_open or os.environ.get(NO_OPEN_ENV) == "1")
    try:
        if args.command == "status":
            return launcher.status()
        if args.command == "stop":
            return launcher.stop(force=args.force)
        if args.command == "restart":
            return launcher.restart(force=args.force, open_url=open_url)
        return launcher.launch(open_url=open_url)
    except BusyError as exc:
        print(f"task-kit: {exc}", file=sys.stderr)
        return EXIT_BUSY
    except LauncherError as exc:
        print(f"task-kit: {exc}", file=sys.stderr)
        return EXIT_ERROR
