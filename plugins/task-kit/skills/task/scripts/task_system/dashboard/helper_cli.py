"""The client a run executes to ask an endpoint helper (standard library only).

Usage: ``<python> helper_cli.py [--system TEXT] <helper>`` with the excerpt on stdin; the answer is
printed. ``<python> helper_cli.py --check`` prints a marker and exits 0 without touching a server, so a
caller can tell that the client itself runs.

Where it looks is in the environment the dashboard gave the run: ``TASK_KIT_RUN_TOKEN`` (the run-scoped
token) and ``TASK_KIT_DASHBOARD_ROOT`` (the directory holding the dashboard's ``server.json``, read for the
loopback port on every call so a restarted server is found). It sends the token in the
``X-Task-Kit-Run-Token`` header and nothing else; it imports no dashboard module and no llm-kit code, and
the server makes the endpoint call.

Exit status: 0 answered; 1 the call failed or the server cannot be reached; 2 refused (one stderr line:
unknown or revoked token, a helper not offered, an empty or oversize request, a spent budget); 3 no usable
endpoint (every model was unusable or the attempt limit was reached).
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
from pathlib import Path

CHECK_MARKER = "task-kit-helper-cli ok"
TOKEN_ENV = "TASK_KIT_RUN_TOKEN"
ROOT_ENV = "TASK_KIT_DASHBOARD_ROOT"
RUN_TOKEN_HEADER = "X-Task-Kit-Run-Token"
SERVER_FILE = "server.json"
REQUEST_TIMEOUT_S = 1800.0

EXIT_ANSWERED, EXIT_FAILED, EXIT_REFUSED, EXIT_NO_ENDPOINT = 0, 1, 2, 3


def _fail(message: str, code: int) -> int:
    print(f"task-kit helper: {message}", file=sys.stderr)
    return code


def _port() -> tuple[int | None, str]:
    root = os.environ.get(ROOT_ENV)
    if not root:
        return None, f"{ROOT_ENV} is not set; this command works only inside a dashboard run"
    path = Path(root) / SERVER_FILE
    try:
        port = json.loads(path.read_text(encoding="utf-8"))["port"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return None, f"cannot read the dashboard's {SERVER_FILE} ({type(exc).__name__}); is the dashboard running?"
    if isinstance(port, bool) or not isinstance(port, int):
        return None, f"the dashboard's {SERVER_FILE} names no port"
    return port, ""


def main(argv: list[str], stdin_text: str | None = None) -> int:
    parser = argparse.ArgumentParser(prog="helper_cli.py", add_help=True, description="Ask a configured endpoint helper.")
    parser.add_argument("helper", nargs="?", help="the helper to ask")
    parser.add_argument("--system", default=None, help="system text for the model (counted against the helper's input limit)")
    parser.add_argument("--check", action="store_true", help="print a marker and exit; reaches no server")
    args = parser.parse_args(argv)
    if args.check:
        print(CHECK_MARKER)
        return EXIT_ANSWERED
    if not args.helper:
        return _fail("name the helper to ask", EXIT_REFUSED)
    token = os.environ.get(TOKEN_ENV)
    if not token:
        return _fail(f"{TOKEN_ENV} is not set; this command works only inside a dashboard run that was offered a helper", EXIT_REFUSED)
    port, why = _port()
    if port is None:
        return _fail(why, EXIT_FAILED)
    text = stdin_text if stdin_text is not None else sys.stdin.buffer.read().decode("utf-8", errors="replace")
    body = json.dumps({"input": text, "system": args.system}).encode("utf-8")
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=REQUEST_TIMEOUT_S)
    try:
        connection.request("POST", f"/api/v1/helper-calls/{args.helper}", body=body, headers={
            "Content-Type": "application/json", RUN_TOKEN_HEADER: token})
        response = connection.getresponse()
        raw = response.read()
    except (OSError, http.client.HTTPException) as exc:
        return _fail(f"the dashboard did not answer ({type(exc).__name__}: {exc})", EXIT_FAILED)
    finally:
        connection.close()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return _fail(f"the dashboard answered {response.status} with something that is not JSON", EXIT_FAILED)
    if response.status == 200 and payload.get("status") == "completed":
        sys.stdout.write(payload.get("text") or "")
        sys.stdout.flush()
        return EXIT_ANSWERED
    error = payload.get("error")
    if isinstance(error, dict):
        line = f"{error.get('code', 'refused')}: {error.get('message', '')}"
        return _fail(line, EXIT_REFUSED if 400 <= response.status < 500 else EXIT_FAILED)
    status = payload.get("status")
    detail = (payload.get("detail") or status or "failed").strip().splitlines()[0]
    return _fail(f"{status or 'failed'}: {detail}", EXIT_NO_ENDPOINT if status in ("unroutable", "attempt-limit") else EXIT_FAILED)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main(sys.argv[1:]))
