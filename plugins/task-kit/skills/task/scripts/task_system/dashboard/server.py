"""The dashboard's local HTTP server.

One detached process per machine, bound to ``127.0.0.1`` on an OS-chosen port,
serving a JSON API and the static UI. The security posture is fixed by
construction, not configuration:

- loopback only; any ``Host`` other than ``127.0.0.1:<port>`` or
  ``localhost:<port>`` is refused (DNS rebinding), and no CORS header is ever
  emitted;
- the per-instance token travels in the URL FRAGMENT (never sent to a server,
  so never logged) and in the ``X-Task-Kit-Token`` header on every API call;
- a mutation additionally needs a same-origin ``Origin``, a JSON content type
  and a body of at most 64 KiB;
- every response is ``Cache-Control: no-store`` and carries a restrictive CSP.

Health is the one unauthenticated endpoint and reveals only the instance
nonce, the code hash, the port and the busy-job count.

The one route a run (not the browser) uses is ``POST /api/v1/helper-calls/<helper>``: it is
authenticated ONLY by the run-scoped token header (``X-Task-Kit-Run-Token``), never by the
dashboard token, the dashboard token authorizes nothing on it and the run token authorizes
nothing else; a request that carries an ``Origin`` header is refused (a page cannot cause
egress); its body cap is 4 MiB (``endpoint_helper.MAX_BODY_BYTES``). See ``endpoint_helper.py``.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import signal
import sys
import threading
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, unquote, urlsplit

from . import cli_check, endpoint_helper, proctree, redact, snapshot
from .codehash import DASHBOARD_PACKAGE, code_hash
from .config import load_config
from .inventory import Inventory, project_id_for, target_root
from .jobs_edge import EdgeError, JobsEdge
from .mutation import TaskMutator
from .runtime import ACTIVE_STATES, DB_FILE, HISTORY_STATES, LIVE_STATES, RuntimeDB, folder_key
from .scheduler import QueueRefused, Scheduler
from .state import (
    LOCK_FILE,
    LOG_FILE,
    SCHEMA,
    LockHeldError,
    ServerLock,
    ServerState,
    remove_state_if_owned,
    runtime_root,
    write_state,
)

LOOPBACK = "127.0.0.1"
TOKEN_HEADER = "X-Task-Kit-Token"
MAX_BODY_BYTES = 64 * 1024
MUTATION_LOCK_FILE = "mutation.lock"
RUNS_IN_SNAPSHOT = 60
EXIT_LOCK_HELD = 75  # EX_TEMPFAIL: another server already runs; use that one.

CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; "
    "object-src 'none'"
)

_STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


def load_static(scripts_dir: Path) -> dict[str, tuple[bytes, str]]:
    """Read the UI assets into memory once.

    A plugin update can delete the directory this server started from while it
    keeps running; serving from memory means that never breaks the page.
    """
    static_dir = Path(scripts_dir).joinpath(*DASHBOARD_PACKAGE, "static")
    assets: dict[str, tuple[bytes, str]] = {}
    for path in sorted(static_dir.iterdir()):
        content_type = _STATIC_TYPES.get(path.suffix)
        if path.is_file() and content_type:
            assets[path.name] = (path.read_bytes(), content_type)
    if "index.html" not in assets:
        raise FileNotFoundError(f"dashboard UI missing: {static_dir / 'index.html'}")
    return assets


class DashboardServer:
    """Binds, records ownership, serves, and cleans up."""

    def __init__(
        self,
        *,
        data_dir: Path,
        scripts_dir: Path,
        inventory: Inventory,
        busy: Callable[[], int] | None = None,
        log=None,
        edge=None,
        config_path: Path | None = None,
        scheduler: Scheduler | None = None,
        cli_observer: "cli_check.CliObserver | bool" = True,
    ) -> None:
        """``cli_observer``: True builds the real per-binary observer (production); False runs without one (no
        Claude binary is observed or waited for); an instance is used as given."""
        self.data_dir = Path(data_dir)
        self.scripts_dir = Path(scripts_dir)
        self.root = runtime_root(self.data_dir)
        self.inventory = inventory
        self._busy = busy
        self._edge = edge if edge is not None else JobsEdge()
        self._config_path = config_path
        self._cli_observer = cli_observer
        self.scheduler = scheduler
        self.helper_broker: endpoint_helper.HelperBroker | None = None
        self.log_stream = log if log is not None else sys.stderr
        self.code_hash = code_hash(self.scripts_dir)
        self.assets = load_static(self.scripts_dir)
        self.token = secrets.token_urlsafe(32)
        self.nonce = secrets.token_hex(16)
        redact.register_secret(self.token)
        self._httpd: ThreadingHTTPServer | None = None
        self._lock = ServerLock(self.root / LOCK_FILE)
        self.state: ServerState | None = None
        self._stopping = threading.Event()

    # -- lifecycle ---------------------------------------------------------

    @property
    def port(self) -> int:
        if self._httpd is None:
            raise RuntimeError("server is not bound")
        return self._httpd.server_address[1]

    def busy_count(self) -> int:
        if self._busy is not None:
            return int(self._busy())
        return self.scheduler.busy_count() if self.scheduler is not None else 0

    def start(self) -> ServerState:
        """Take the lock, bind port 0, and publish ownership. Raises LockHeldError."""
        import psutil

        self.root.mkdir(parents=True, exist_ok=True)
        self._lock.acquire()
        try:
            if self.scheduler is None:
                # The server owns the observer thread that checks each Claude binary once, off every scheduler lock.
                observer = self._cli_observer
                if observer is True:
                    observer = cli_check.CliObserver(
                        root=self.root, edge=self._edge, load_config=lambda: load_config(self._config_path), log=self.log)
                elif observer is False:
                    observer = None
                self.scheduler = Scheduler(
                    root=self.root,
                    db=RuntimeDB(self.root / DB_FILE),
                    mutator=TaskMutator(self.root / MUTATION_LOCK_FILE),
                    edge=self._edge,
                    config_path=self._config_path,
                    log=self.log,
                    observer=observer,
                )
            # A helper call still reserved lost its server: it is interrupted (and still counts against the budget).
            interrupted = self.scheduler.db.interrupt_open_calls()
            if interrupted:
                self.log(f"{interrupted} endpoint helper call(s) were in flight when the previous server stopped; they count as interrupted")
            self.helper_broker = endpoint_helper.HelperBroker(db=self.scheduler.db, edge=self._edge, log=self.log)
            httpd = ThreadingHTTPServer((LOOPBACK, 0), _Handler)
            httpd.daemon_threads = True
            httpd.dashboard = self  # type: ignore[attr-defined]
            self._httpd = httpd
            self.state = ServerState(
                schema=SCHEMA,
                pid=os.getpid(),
                pid_create_time=psutil.Process(os.getpid()).create_time(),
                nonce=self.nonce,
                port=httpd.server_address[1],
                token=self.token,
                code_hash=self.code_hash,
                started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                log_path=str(self.root / LOG_FILE),
            )
            write_state(self.root, self.state)
        except BaseException:
            self._lock.release()
            raise
        self.log(f"serving on {LOOPBACK}:{self.port} (code {self.code_hash[:12]})")
        return self.state

    def serve_forever(self) -> None:
        assert self._httpd is not None, "start() first"
        try:
            if self.scheduler is not None:
                self.scheduler.start()
            self._httpd.serve_forever(poll_interval=0.2)
        finally:
            self.close()

    def request_shutdown(self) -> None:
        """Stop serving; safe to call from a handler thread."""
        if self._httpd is not None and not self._stopping.is_set():
            self._stopping.set()
            threading.Thread(target=self._httpd.shutdown, daemon=True).start()

    def close(self) -> None:
        if self.scheduler is not None:
            self.scheduler.stop()
            self.scheduler.db.close()
            self.scheduler = None
        if self._httpd is not None:
            self._httpd.server_close()
            self._httpd = None
        remove_state_if_owned(self.root, self.nonce)
        self._lock.release()
        redact.clear_secrets()

    def log(self, message: str) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.log_stream.write(f"{stamp} {redact.redact(message)}\n")
        self.log_stream.flush()

    # -- facts the handler serves -----------------------------------------

    def health(self) -> dict:
        return {
            "schema": SCHEMA,
            "instance": self.nonce,
            "code_hash": self.code_hash,
            "port": self.port,
            "busy": self.busy_count(),
        }

    def runtime_view(self) -> dict | None:
        scheduler = self.scheduler
        if scheduler is None:
            return None
        live = scheduler.db.runs(LIVE_STATES)
        recent = scheduler.db.runs(HISTORY_STATES, limit=RUNS_IN_SNAPSHOT, newest_first=True)
        phases = {r.id: p for r in live if (p := scheduler.ledger_phase(r)) is not None}
        active = {folder_key(r.folder) for r in scheduler.db.runs(ACTIVE_STATES)}
        return {"capabilities": scheduler.capabilities(), "runs": sorted(live + recent, key=lambda r: -r.seq), "phases": phases,
                "held": scheduler.db.held_folders(), "run_active": active, "queue_view": scheduler.queue_view()}

    def server_info(self) -> dict:
        return {"instance": self.nonce, "code_hash": self.code_hash[:12]}

    def allowed_hosts(self) -> set[str]:
        return {f"{LOOPBACK}:{self.port}", f"localhost:{self.port}"}

    def allowed_origins(self) -> set[str]:
        return {f"http://{LOOPBACK}:{self.port}", f"http://localhost:{self.port}"}


class _Handler(BaseHTTPRequestHandler):
    server_version = "task-kit-dashboard"
    sys_version = ""

    def version_string(self) -> str:
        return self.server_version

    @property
    def app(self) -> DashboardServer:
        return self.server.dashboard  # type: ignore[attr-defined]

    # -- logging -----------------------------------------------------------

    def log_message(self, format: str, *args) -> None:  # noqa: A002 -- stdlib name
        # Method, path and status only: never headers, never the query string.
        path = urlsplit(self.path).path
        self.app.log(f"{self.command} {path} -> {args[1] if len(args) > 1 else '?'}")

    def log_error(self, format: str, *args) -> None:  # noqa: A002
        self.app.log(f"error {format % args}")

    # -- verbs -------------------------------------------------------------

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def do_OPTIONS(self) -> None:
        # No CORS: a preflight gets the same refusal as any unsupported verb.
        self._dispatch("OPTIONS")

    def do_HEAD(self) -> None:
        self._dispatch("HEAD")

    # -- responses ---------------------------------------------------------

    def _headers(self, content_type: str, length: int) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self._headers(content_type, len(body))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        self._send(status, body, "application/json")

    def _error(self, status: int, code: str, message: str) -> None:
        self._json(status, {"error": {"code": code, "message": message}})

    # -- security gates ----------------------------------------------------

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        return host in self.app.allowed_hosts()

    def _token_ok(self) -> bool:
        supplied = self.headers.get(TOKEN_HEADER) or ""
        return hmac.compare_digest(supplied.encode("utf-8"), self.app.token.encode("utf-8"))

    def _read_json_body(self, *, no_origin: bool = False, limit: int = MAX_BODY_BYTES) -> dict | None:
        """Validate a mutation's origin, type and size; return the JSON body.

        Sends the error response itself and returns None when any gate fails. ``no_origin`` is the helper
        route's gate: the caller is a run's process, never a page, so a request that carries ANY ``Origin``
        is refused instead of one that lacks a same-origin one. ``limit`` is the body cap.
        """
        origin = self.headers.get("Origin")
        if no_origin:
            if origin is not None:
                self._error(403, "bad_origin", "A helper call comes from a run, never from a page: a request with an Origin is refused.")
                return None
        elif origin is None or origin.lower() not in self.app.allowed_origins():
            self._error(403, "bad_origin", "A mutation needs a same-origin Origin header.")
            return None
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            self._error(415, "bad_content_type", "A mutation body must be application/json.")
            return None
        length_header = self.headers.get("Content-Length")
        if length_header is None or not length_header.isdigit():
            self._error(411, "length_required", "A mutation needs a Content-Length.")
            return None
        length = int(length_header)
        if length > limit:
            self._error(413, "body_too_large", f"A mutation body is limited to {limit} bytes.")
            return None
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, ValueError):
            self._error(400, "bad_json", "The body is not valid JSON.")
            return None
        if not isinstance(body, dict):
            self._error(400, "bad_json", "The body must be a JSON object.")
            return None
        return body

    # -- routing -----------------------------------------------------------

    def _dispatch(self, method: str) -> None:
        try:
            if not self._host_ok():
                self._error(403, "forbidden_host", "Only 127.0.0.1 and localhost are served.")
                return
            parts = urlsplit(self.path)
            segments = [unquote(s) for s in parts.path.split("/")[1:]]
            query = parse_qs(parts.query)
            if method in ("GET", "HEAD") and not (segments and segments[0] == "api"):
                self._static(segments)
                return
            if segments[:2] != ["api", "v1"]:
                self._error(404, "not_found", "No such endpoint.")
                return
            route = segments[2:]
            if route == ["health"]:
                if method not in ("GET", "HEAD"):
                    self._error(405, "method_not_allowed", "Use GET.")
                    return
                self._json(200, self.app.health())
                return
            if route[:1] == [endpoint_helper.ROUTE]:
                # Before the dashboard-token check on purpose: this route has its own, narrower credential.
                self._helper_call(method, route)
                return
            if not self._token_ok():
                self._error(401, "unauthorized", "Missing or wrong token; reopen the dashboard with `task-kit`.")
                return
            self._api(method, route, query)
        except ConnectionError as exc:
            # The browser closed the connection mid-response: nothing to answer.
            self.app.log(f"client went away: {type(exc).__name__}")
        except Exception as exc:  # noqa: BLE001 -- a handler must answer, then log loudly
            self.app.log("unhandled error:\n" + traceback.format_exc())
            self._error(500, "internal", f"{type(exc).__name__}: {exc}")

    def _helper_call(self, method: str, route: list[str]) -> None:
        """POST /helper-calls/<helper>: a run asks an endpoint helper (token header only; see the module docstring)."""
        if len(route) != 2 or not route[1]:
            self._error(404, "not_found", "No such endpoint.")
            return
        if method != "POST":
            self._error(405, "method_not_allowed", "Use POST.")
            return
        token = self.headers.get(endpoint_helper.RUN_TOKEN_HEADER) or ""
        if not token:
            self._error(401, "unauthorized", f"A helper call needs the run token in the {endpoint_helper.RUN_TOKEN_HEADER} header.")
            return
        body = self._read_json_body(no_origin=True, limit=endpoint_helper.MAX_BODY_BYTES)
        if body is None:
            return
        broker = self.app.helper_broker
        if broker is None:
            self._error(503, "unavailable", "The dashboard is not ready for helper calls.")
            return
        status, payload = broker.call(token, route[1], body)
        self._json(status, payload)

    def _static(self, segments: list[str]) -> None:
        if segments in ([""], ["index.html"]):
            name = "index.html"
        elif len(segments) == 2 and segments[0] == "static":
            name = segments[1]
        else:
            self._error(404, "not_found", "No such page.")
            return
        asset = self.app.assets.get(name)
        if asset is None:
            self._error(404, "not_found", "No such asset.")
            return
        self._send(200, asset[0], asset[1])

    # -- actions that change shared state --------------------------------------

    def _refused(self, exc: QueueRefused) -> None:
        payload: dict = {"code": exc.code, "message": str(exc)}
        if exc.reasons:
            payload["reasons"] = exc.reasons
        if exc.blocked:
            payload["blocked"] = True
        if exc.current_revision:
            payload["current_revision"] = exc.current_revision
        payload.update(exc.extra)
        if exc.blocked or exc.code == "partial-write":
            self.app.inventory.invalidate()  # the folder changed, so the next read must see it
        self._json(exc.status, {"error": payload})

    def _expected_revision(self, body: dict) -> str | None:
        value = body.get("expected_revision")
        if not isinstance(value, str) or not value:
            self._error(422, "missing_expected_revision", "Every change names the expected_revision it was based on.")
            return None
        return value

    def _item_action(self, method: str, route: list[str]) -> None:
        if method != "POST":
            self._error(405, "method_not_allowed", "Use POST.")
            return
        body = self._read_json_body()
        if body is None:
            return
        expected = self._expected_revision(body)
        if expected is None:
            return
        checkout, task_id, item_id, action = route[1], route[2], route[3], route[4]
        task = self.app.inventory.snapshot().find_task(checkout, task_id)
        if task is None:
            self._error(404, "unknown_task", "No such task in any configured project.")
            return
        scheduler = self.app.scheduler
        try:
            if action == "queue":
                options = body.get("options", {})
                if not isinstance(options, dict):
                    self._error(422, "bad-options", "options must be an object.")
                    return
                run = scheduler.queue(project_root=task.project_root, task_ref=task.id, item_id=item_id,
                                      body=options, expected_revision=expected)
                self.app.log(f"queued run {run.id} for {task.id}/{item_id}")
                self._json(202, {"run": snapshot.run_json(run, task.project_name)})
            else:
                outcome = scheduler.recheck(project_root=task.project_root, task_ref=task.id, expected_revision=expected)
                self.app.inventory.snapshot(force=True)
                self._json(200, outcome)
        except QueueRefused as exc:
            self._refused(exc)

    def _runs(self, method: str, route: list[str], query: dict) -> None:
        scheduler = self.app.scheduler
        run = scheduler.db.get(route[1])
        if run is None:
            self._error(404, "unknown_run", "No such run.")
            return
        if len(route) == 2:
            if method not in ("GET", "HEAD"):
                self._error(405, "method_not_allowed", "Use GET.")
                return
            grant = scheduler.db.helper_grant(run.id)
            detail = {"run": snapshot.run_json(run, ""), "events": scheduler.db.events(run.id),
                      "helpers_offered": grant["offers"] if grant else [],
                      "helper_calls": scheduler.db.helper_calls(run.id)}
            try:
                detail["ledger_events"] = scheduler.ledger_events(run)
            except EdgeError as exc:  # said, not hidden: the dashboard's own events above are still complete
                detail["ledger_events"] = []
                detail["ledger_events_error"] = str(exc)
            self._json(200, detail)
            return
        if route[2] == "log":
            if method not in ("GET", "HEAD"):
                self._error(405, "method_not_allowed", "Use GET.")
                return
            after = query.get("after", ["0"])[0]
            if not after.isdigit():
                self._error(422, "bad_cursor", "after must be a non-negative integer.")
                return
            self._json(200, scheduler.log_tail(run, int(after)))
            return
        if route[2] == "cancel":
            if method != "POST":
                self._error(405, "method_not_allowed", "Use POST.")
                return
            # Process control on an immutable run id: the body is not read for a task revision (see
            # Scheduler.cancel); it must still be a JSON object, like every other mutation.
            if self._read_json_body() is None:
                return
            try:
                cancelled = scheduler.cancel(run.id)
            except QueueRefused as exc:
                self._refused(exc)
                return
            self.app.log(f"cancel of {run.id}: {cancelled.state}")
            self._json(202, {"run": snapshot.run_json(cancelled, "")})
            return
        if route[2] == "retry":
            if method != "POST":
                self._error(405, "method_not_allowed", "Use POST.")
                return
            body = self._read_json_body()
            if body is None:
                return
            expected = self._expected_revision(body)
            if expected is None:
                return
            try:
                fresh = scheduler.retry(run.id, expected_revision=expected)
            except QueueRefused as exc:
                self._refused(exc)
                return
            self.app.log(f"retry of {run.id} queued as {fresh.id}")
            self._json(202, {"run": snapshot.run_json(fresh, "")})
            return
        self._error(404, "not_found", "No such endpoint.")

    # -- task authoring ---------------------------------------------------------

    def _checked_body(self, allowed: set[str], *, revision: bool = True) -> dict | None:
        """The mutation body (gates applied) with unknown keys refused; None after the error was sent."""
        body = self._read_json_body()
        if body is None:
            return None
        unknown = sorted(set(body) - allowed)
        if unknown:
            self._error(422, "unknown_field", f"unknown field(s) {unknown}; allowed: {sorted(allowed)}")
            return None
        if revision and self._expected_revision(body) is None:
            return None
        return body

    def _task_row(self, task) -> dict:
        scheduler = self.app.scheduler
        key = folder_key(task.folder)
        held = key in scheduler.db.held_folders()
        active = any(folder_key(r.folder) == key for r in scheduler.db.runs(ACTIVE_STATES))
        helpers = scheduler.helper_names_by_checkout({task.checkout_id: task.project_root}).get(task.checkout_id)
        return snapshot.task_json(task, held=held, run_active=active, helpers=helpers)

    def _fresh_task(self, checkout: str, task_id: str):
        self.app.inventory.invalidate()
        return self.app.inventory.snapshot().find_task(checkout, task_id)

    def _find_task(self, checkout: str, task_id: str):
        task = self.app.inventory.snapshot().find_task(checkout, task_id)
        if task is None:
            self._error(404, "unknown_task", "No such task in any configured project.")
        return task

    def _task_collection(self, method: str, route: list[str]) -> None:
        """POST /tasks/<checkout>/<task>/items (add an item) and /queue (queue all available)."""
        if method != "POST":
            self._error(405, "method_not_allowed", "Use POST.")
            return
        checkout, task_id, action = route[1], route[2], route[3]
        allowed = ({"expected_revision", "title", "priority", "details", "queue", "options"} if action == "items"
                   else {"expected_revision", "options"})
        body = self._checked_body(allowed)
        if body is None:
            return
        task = self._find_task(checkout, task_id)
        if task is None:
            return
        options = body.get("options", {})
        if not isinstance(options, dict):
            self._error(422, "bad-options", "options must be an object.")
            return
        scheduler = self.app.scheduler
        try:
            if action == "queue":
                runs = scheduler.queue_all(project_root=task.project_root, task_ref=task.id,
                                           expected_revision=body["expected_revision"], body=options)
                self.app.log(f"queued {len(runs)} run(s) for {task.id}")
                self._json(202, {"runs": [snapshot.run_json(r, task.project_name) for r in runs]})
                return
            queue = body.get("queue", False)
            if not isinstance(queue, bool):
                self._error(422, "bad_queue", "queue must be true or false.")
                return
            outcome = scheduler.add_item(project_root=task.project_root, task_ref=task.id,
                                         expected_revision=body["expected_revision"], title=body.get("title"),
                                         priority=body.get("priority"), details=body.get("details"), queue=queue,
                                         options=options)
        except QueueRefused as exc:
            self._refused(exc)
            return
        self.app.log(f"added item {outcome['item']['id']} to {task.id}")
        fresh = self._fresh_task(checkout, task_id)
        payload = {"item": outcome["item"], "revision": outcome["revision"], "task": self._task_row(fresh) if fresh else None}
        status = 201
        if "run" in outcome:
            payload["run"] = snapshot.run_json(outcome["run"], task.project_name)
            status = 202
        if "queue_error" in outcome:
            payload["queue_error"] = outcome["queue_error"]
        self._json(status, payload)

    def _item_state(self, method: str, route: list[str]) -> None:
        """PATCH /items/<checkout>/<task>/<item>: Defer / Undefer."""
        body = self._checked_body({"expected_revision", "state"})
        if body is None:
            return
        checkout, task_id, item_id = route[1], route[2], route[3]
        task = self._find_task(checkout, task_id)
        if task is None:
            return
        try:
            outcome = self.app.scheduler.defer_item(project_root=task.project_root, task_ref=task.id, item_id=item_id,
                                                    expected_revision=body["expected_revision"], state=body.get("state"))
        except QueueRefused as exc:
            self._refused(exc)
            return
        self.app.log(f"item {item_id} of {task.id} is now {outcome['state']}")
        fresh = self._fresh_task(checkout, task_id)
        item = next((i for i in fresh.core.items if i.id == item_id), None) if fresh else None
        self._json(200, {"item": snapshot._item_json(item) if item else {"id": item_id, "state": outcome["state"]},
                         "revision": outcome["revision"]})

    def _run_options(self, method: str, route: list[str]) -> None:
        """PUT /items/<checkout>/<task>/<item>/run-options: the options of the item's queued entry."""
        body = self._checked_body({"options"}, revision=False)
        if body is None:
            return
        options = body.get("options")
        if not isinstance(options, dict):
            self._error(422, "bad-options", "options must be an object.")
            return
        task = self._find_task(route[1], route[2])
        if task is None:
            return
        try:
            run = self.app.scheduler.set_run_options(project_root=task.project_root, task_ref=task.id, item_id=route[3],
                                                     options=options)
        except QueueRefused as exc:
            self._refused(exc)
            return
        self._json(200, {"run": snapshot.run_json(run, task.project_name)})

    def _remove_run(self, method: str, route: list[str]) -> None:
        """DELETE /queue/<run-id>: take a queued entry out of the queue."""
        if method != "DELETE":
            self._error(405, "method_not_allowed", "Use DELETE.")
            return
        if self._read_json_body() is None:
            return
        try:
            run = self.app.scheduler.remove(route[1])
        except QueueRefused as exc:
            self._refused(exc)
            return
        self.app.log(f"removed {run.id} from the queue")
        self._json(200, {"run": snapshot.run_json(run, "")})

    def _new_task(self, method: str, route: list[str]) -> None:
        """POST /projects/<project-id>/tasks: create a dev/tasks task."""
        if method != "POST":
            self._error(405, "method_not_allowed", "Use POST.")
            return
        body = self._checked_body({"title", "priority", "first_item"}, revision=False)
        if body is None:
            return
        project = next((p for p in self.app.inventory.snapshot().projects if p.id == route[1]), None)
        if project is None:
            self._error(404, "unknown_project", "No such project.")
            return
        root = target_root(project)
        try:
            outcome = self.app.scheduler.new_task(project_root=root, title=body.get("title"), priority=body.get("priority"),
                                                  first_item=body.get("first_item"))
        except QueueRefused as exc:
            self._refused(exc)
            return
        self.app.log(f"created task {outcome['path']}")
        fresh = self._fresh_task(project_id_for(Path(root)), outcome["task_ref"])
        payload = {"task": self._task_row(fresh) if fresh else None, "revision": outcome["revision"], "path": outcome["path"]}
        if "followup_error" in outcome:
            payload["followup_error"] = outcome["followup_error"]
        self._json(201, payload)

    def _holds(self, method: str) -> None:
        """Register an interactive hold: the harness is about to work this task folder."""
        if method != "POST":
            self._error(405, "method_not_allowed", "Use POST.")
            return
        body = self._read_json_body()
        if body is None:
            return
        folder, session = body.get("folder"), body.get("session")
        pid, ctime = body.get("pid"), body.get("create_time")
        if not (isinstance(folder, str) and folder and isinstance(session, str) and session
                and isinstance(pid, int) and not isinstance(pid, bool) and isinstance(ctime, (int, float))):
            self._error(422, "bad_hold", "A hold needs folder, session, pid and create_time.")
            return
        scheduler = self.app.scheduler
        active = scheduler.register_hold(folder, session, proctree.ProcIdent(pid, float(ctime)))
        self.app.log(f"hold on {Path(folder).name} by session {session[:8]}")
        self._json(200, {"ok": True, "active_run": snapshot.run_json(active, "") if active else None})

    def _api(self, method: str, route: list[str], query: dict) -> None:
        if route == ["dashboard"]:
            if method not in ("GET", "HEAD"):
                self._error(405, "method_not_allowed", "Use GET.")
                return
            force = query.get("refresh", [""])[0] == "1"
            inventory = self.app.inventory.snapshot(force=force)
            runtime = self.app.runtime_view()
            if runtime is not None:
                roots = {t.checkout_id: t.project_root for p in inventory.projects for t in p.tasks}
                runtime["helpers_by_checkout"] = self.app.scheduler.helper_names_by_checkout(roots)
            payload = snapshot.dashboard_payload(
                inventory,
                project=query.get("project", [""])[0] or None,
                query=query.get("q", [""])[0],
                needs_only=query.get("needs", [""])[0] in ("1", "true"),
                server=self.app.server_info(),
                runtime=runtime,
            )
            self._json(200, payload)
            return
        if route[:1] == ["items"] and len(route) == 5 and route[4] in ("queue", "recheck"):
            self._item_action(method, route)
            return
        if route[:1] == ["tasks"] and len(route) == 4 and route[3] in ("items", "queue"):
            self._task_collection(method, route)
            return
        if route[:1] == ["items"] and len(route) == 4 and method == "PATCH":
            self._item_state(method, route)
            return
        if route[:1] == ["items"] and len(route) == 5 and route[4] == "run-options":
            if method != "PUT":
                self._error(405, "method_not_allowed", "Use PUT.")
                return
            self._run_options(method, route)
            return
        if route[:1] == ["queue"] and len(route) == 2:
            self._remove_run(method, route)
            return
        if route[:1] == ["projects"] and len(route) == 3 and route[2] == "tasks":
            self._new_task(method, route)
            return
        if route[:1] == ["runs"] and len(route) in (2, 3):
            self._runs(method, route, query)
            return
        if route == ["holds"]:
            self._holds(method)
            return
        if route[:1] == ["items"] and len(route) in (4, 5):
            if method not in ("GET", "HEAD"):
                self._error(405, "method_not_allowed", "Use GET.")
                return
            checkout, task_id, item_id = route[1], route[2], route[3]
            task = self.app.inventory.snapshot().find_task(checkout, task_id)
            if task is None:
                self._error(404, "unknown_task", "No such task in any configured project.")
                return
            detail = snapshot.item_detail(task, item_id)
            if detail is None:
                self._error(404, "unknown_item", "No such item in that task.")
                return
            if len(route) == 5:
                if route[4] != "handoff":
                    self._error(404, "not_found", "No such endpoint.")
                    return
                self._json(200, detail["handoff"])
                return
            self._json(200, detail)
            return
        if route == ["shutdown"]:
            if method != "POST":
                self._error(405, "method_not_allowed", "Use POST.")
                return
            body = self._read_json_body()
            if body is None:
                return
            busy = self.app.busy_count()
            if busy and not body.get("force"):
                self._error(409, "busy", f"{busy} job(s) are running; stop is refused without force.")
                return
            self._json(200, {"ok": True})
            self.app.request_shutdown()
            return
        self._error(404, "not_found", "No such endpoint.")


def run(data_dir: Path, scripts_dir: Path, *, log=None) -> int:
    """Run a dashboard server in the foreground until it is asked to stop."""
    server = DashboardServer(
        data_dir=data_dir,
        scripts_dir=scripts_dir,
        inventory=Inventory(),
        log=log,
    )
    try:
        server.start()
    except LockHeldError as exc:
        server.log(str(exc))
        print(f"task-kit dashboard: {exc}", file=sys.stderr)
        return EXIT_LOCK_HELD

    # Read every task folder once now, so the first page load does not wait for it.
    threading.Thread(target=server.inventory.snapshot, name="inventory-warmup", daemon=True).start()

    def _stop(_signum, _frame) -> None:
        server.request_shutdown()

    for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
        number = getattr(signal, name, None)
        if number is not None:
            signal.signal(number, _stop)
    server.serve_forever()
    server.log("stopped")
    return 0
