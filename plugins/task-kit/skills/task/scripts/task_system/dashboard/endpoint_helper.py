"""The endpoint helper broker: a run asks the dashboard server to call a text-only model for it.

A helper is a configured model endpoint (``dashboard.endpoint_helpers`` in ``~/.claude/task.local.yaml``)
that a run may ask a question, never a worker: it has no tools, no file access and no say in the result.
All authority is server-side, because a worker can write every file in its run directory:

- WHICH helpers a run may call, their limits and the repositories that consent to them are decided by the
  server from the configuration when the run is dispatched, and stored in the runtime database
  (``helper_grants``) -- never read back from a run directory;
- the worker holds one run-scoped token (``TASK_KIT_RUN_TOKEN``), issued at dispatch and revoked in the same
  transaction that ends the run; the database keeps only its SHA-256;
- each call is authorized and recorded in one transaction (``RuntimeDB.reserve_helper_call``): token,
  revocation, live run, offer, size and the per-helper budget, then the reservation row;
- the endpoint call itself is made here, in the server, through the llm-kit edge.

This is enforced by the server, and it is exactly that strong: a process that calls the model library
directly, or reads a key out of the user's data directory, can still reach an endpoint without a helper
(the guard table in the architecture names this).
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from pathlib import Path
from typing import Callable

from .config import DashboardConfig
from .jobs_edge import EdgeError
from .runtime import CALL_COMPLETED, CALL_FAILED, Refusal, RuntimeDB

TOKEN_ENV = "TASK_KIT_RUN_TOKEN"
ROOT_ENV = "TASK_KIT_DASHBOARD_ROOT"
RUN_TOKEN_HEADER = "X-Task-Kit-Run-Token"
ROUTE = "helper-calls"
#: The request body cap of the helper route only (the rest of the API keeps its 64 KiB).
MAX_BODY_BYTES = 4 * 1024 * 1024
HELPERS_DIR = "helpers"
CLI_FILE = "helper_cli.py"

#: The system text sent when the caller supplies none. It is the server's own text and is not counted against
#: ``max_input_chars``; a caller-supplied system text replaces it and IS counted.
DEFAULT_SYSTEM = (
    "You are a text-only helper. You have no tools and no file access. Answer the request using only the text "
    "you are given, and say so when that text does not contain the answer."
)

REQUEST_KEYS = {"input", "system"}


def offers_for(config: DashboardConfig, repo_key: str) -> list[dict]:
    """The helpers ``config`` offers to a repository (by consent key), as the immutable offers a grant stores."""
    return [
        {"name": h.name, "models": list(h.models), "purpose": h.purpose, "max_calls": h.max_calls,
         "timeout_s": h.timeout_s, "max_input_chars": h.max_input_chars}
        for h in config.endpoint_helpers if h.offered_to(repo_key)
    ]


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def cli_path() -> Path:
    """The helper client a run executes (stdlib only, beside this module)."""
    return Path(__file__).with_name(CLI_FILE).resolve()


def prompt_section(offers: list[dict], *, python: str, cli: Path | None = None) -> str:
    """The worker prompt's section naming the offered helpers and the one command that asks them ('' for no offer)."""
    if not offers:
        return ""
    command = f'"{python}" "{cli if cli is not None else cli_path()}"'
    lines = [
        "Endpoint helpers (text-only models: no tools, no file access), configured for this repository in "
        "~/.claude/task.local.yaml (dashboard.endpoint_helpers):",
    ]
    for offer in offers:
        purpose = offer["purpose"] or "no purpose stated"
        lines.append(f"- {offer['name']}: {purpose} (at most {offer['max_calls']} calls in this run, "
                     f"input up to {offer['max_input_chars']} characters)")
    lines += [
        "To ask one, run this with the excerpt on the lines before EOF, naming one helper above for <name>; the answer is printed:",
        f"Run: {command} <name> <<'EOF'",
        "<the excerpt>",
        "EOF",
        "Send only the excerpt the question needs and never a credential. Check an answer before you rely on it; "
        "a helper never edits files and never owns the result.",
    ]
    return "\n".join(lines)


def write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` readable by its owner only (the mode is set at creation, never loosened first)."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def write_private_json(path: Path, payload) -> None:
    """``runfiles.write_json`` for a file that carries a token: a private temporary file, then a rename."""
    temp = path.with_name(path.name + ".tmp")
    write_private(temp, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(temp, path)


def grant_env(token: str, root: Path) -> dict[str, str]:
    """The environment variables a run with a grant carries: its token, and where its dashboard keeps ``server.json``."""
    return {TOKEN_ENV: token, ROOT_ENV: str(root)}


def request_problem(body: dict) -> tuple[tuple[str, str] | None, str, str | None]:
    """``(problem, input, system)`` of a request body; ``problem`` is ``(code, message)`` or None when it is usable."""
    unknown = sorted(set(body) - REQUEST_KEYS)
    text, system = body.get("input"), body.get("system")
    if unknown:
        return ("bad-request", f"unknown field(s) {unknown}; allowed: {sorted(REQUEST_KEYS)}"), "", None
    if not isinstance(text, str):
        return ("bad-request", "input must be a string"), "", None
    if system is not None and not isinstance(system, str):
        return ("bad-request", "system must be a string or null"), text, None
    if not text.strip():
        return ("empty-input", "the helper input is empty"), text, system
    return None, text, system


class HelperBroker:
    """Authorizes, records and performs helper calls. One per server; every method is safe from request threads."""

    def __init__(self, *, db: RuntimeDB, edge, log: Callable[[str], None] = lambda message: None) -> None:
        self.db = db
        self.edge = edge
        self._log = log

    def call(self, token: str, helper: str, body: dict) -> tuple[int, dict]:
        """One helper call: ``(http status, payload)``.

        Every refusal after the token was recognized is a ``refused`` row. The size limit counts the input and
        the system text together: every caller-supplied character that reaches the model.
        """
        problem, text, system = request_problem(body)
        chars = len(text) + len(system or "")
        outcome = self.db.reserve_helper_call(hash_token(token), helper, chars, problem=problem)
        if isinstance(outcome, Refusal):
            error: dict = {"code": outcome.code, "message": outcome.message}
            if outcome.offered or outcome.code == "not-offered":
                error["offered"] = list(outcome.offered)
            self._log(f"helper call to {helper} refused: {outcome.code}")
            return outcome.status, {"error": error}
        run = self.db.get(outcome.run_id)
        if run is None:  # a grant without its run cannot happen (the grant references an existing run)
            self.db.end_helper_call(outcome.call_id, state=CALL_FAILED, status="no-run", detail="the grant's run is gone")
            return 500, {"error": {"code": "no-run", "message": "the run of this token is gone"}}
        offer = outcome.offer
        try:
            self._keep(run.run_dir, outcome.call_id, text, system)
        except OSError as exc:
            detail = f"the raw input could not be kept in the run directory: {exc}"
            self.db.end_helper_call(outcome.call_id, state=CALL_FAILED, status="not-kept", detail=detail)
            return 500, {"error": {"code": "not-kept", "message": detail}}
        try:
            result = self.edge.complete_transport(
                list(offer["models"]), system=system if system is not None else DEFAULT_SYSTEM, prompt=text,
                timeout_s=float(offer["timeout_s"]), project_root=run.project_root)
        except EdgeError as exc:
            self.db.end_helper_call(outcome.call_id, state=CALL_FAILED, status="edge-error", detail=str(exc))
            self._log(f"helper call {outcome.call_id} to {helper} failed: {exc}")
            return 502, {"error": {"code": "edge-error", "message": str(exc)}}
        except Exception as exc:  # noqa: BLE001 -- recorded, then the handler answers 500 and logs the traceback
            self.db.end_helper_call(outcome.call_id, state=CALL_FAILED, status="error", detail=f"{type(exc).__name__}: {exc}")
            raise
        answered = result.status == "completed"
        try:
            self._keep_output(run.run_dir, outcome.call_id, result.text)
        except OSError as exc:  # the endpoint was paid and the call counts; it ends failed, never answered without its record
            detail = f"the raw output could not be kept in the run directory: {exc}"
            self.db.end_helper_call(outcome.call_id, state=CALL_FAILED, entry=result.entry, status="not-kept", detail=detail)
            return 500, {"error": {"code": "not-kept", "message": detail}}
        self.db.end_helper_call(
            outcome.call_id, state=CALL_COMPLETED if answered else CALL_FAILED, entry=result.entry, status=result.status,
            output_chars=len(result.text), usage=result.usage or None, reported_cost_usd=result.reported_cost_usd,
            detail=result.detail or None)
        self._log(f"helper call {outcome.call_id} to {helper} for run {run.id}: {result.status}")
        payload = {
            "status": result.status, "entry": result.entry, "text": result.text, "attempts": list(result.attempts),
            "usage": result.usage, "reported_cost_usd": result.reported_cost_usd, "detail": result.detail,
        }
        if result.floor is not None:
            payload["floor"] = result.floor
        return (200 if answered else 502), payload

    # -- the raw text, kept privately and never served ---------------------------------------------

    def _keep(self, run_dir: str | None, call_id: int, text: str, system: str | None) -> None:
        if not run_dir:
            raise OSError("the run has no run directory")
        folder = Path(run_dir) / HELPERS_DIR
        folder.mkdir(mode=0o700, exist_ok=True)
        write_private(folder / f"{call_id}.in.txt", text)
        if system is not None:
            write_private(folder / f"{call_id}.system.txt", system)

    def _keep_output(self, run_dir: str | None, call_id: int, text: str) -> None:
        write_private(Path(run_dir) / HELPERS_DIR / f"{call_id}.out.txt", text)  # type: ignore[arg-type]
