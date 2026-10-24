"""Harness authentication probes (adapters layer; performs I/O; never raises).

Answers one question per harness: is it running on a subscription session pool
(SUBSCRIPTION), on metered billing (METERED), or is that unknown (UNKNOWN, which
callers treat as spend). Reasons name fields and variables, never secret values.

* ``claude_auth``: any diverting variable set and non-empty -> METERED naming the
  variable; otherwise ``claude auth status`` JSON: ``authMethod == "claude.ai"``
  with ``apiProvider == "firstParty"`` -> SUBSCRIPTION, any other value -> METERED;
  launch failure, timeout or parse failure -> UNKNOWN.
* ``codex_auth``: asks Codex for its own resolved configuration with
  ``codex doctor --json`` in the run's working directory and environment. An auth
  environment variable gives METERED (or UNKNOWN for ``CODEX_ACCESS_TOKEN`` alone)
  before doctor runs. SUBSCRIPTION only when doctor shows provider ``openai``,
  ChatGPT auth and no stored API key; another provider or auth mode is METERED;
  anything else (missing doctor, non-zero exit, timeout, unknown schema, missing
  field, a check status other than ok) is UNKNOWN. No file-reading fallback.
  Definitive results (SUBSCRIPTION, METERED) are cached per process, keyed by
  ``(cwd, CODEX_AUTH_ENV values, CODEX_HOME)``; UNKNOWN is never cached, so a timeout
  does not stick.
  If the backend ever adds a ``-c`` key starting with ``model_provider``,
  ``model_providers`` or ``profile``, it must be forwarded to doctor too.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Tuple

from llm_kit.adapters.claude_command import (
    CLAUDE_EXECUTABLE_ENV,
    ClaudeExecutableError,
    resolve_claude_executable,
)
from llm_kit.registry.cost import (
    BILLING_DIVERTING_VARS,
    METERED,
    SUBSCRIPTION,
    UNKNOWN,
    HarnessAuth,
)

CODEX_AUTH_ENV = ("CODEX_API_KEY", "OPENAI_API_KEY", "CODEX_ACCESS_TOKEN")
DOCTOR_SCHEMA = 1

# A value quoted in a reason is a configuration identifier (provider name, auth
# mode, version, claude authMethod): short and identifier-shaped. Anything else
# (a long or odd-looking string, which is what a credential looks like) is
# replaced by a placeholder, so a malformed field cannot carry a secret into a reason.
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9._:+-]{1,32}$")

# (cwd, hashed CODEX_AUTH_ENV values, CODEX_HOME) -> definitive HarnessAuth.
# UNKNOWN is never cached: a timeout or missing binary must not stick for the
# lifetime of the process.
_CODEX_CACHE: Dict[Tuple[str, str, str], HarnessAuth] = {}


def clear_codex_auth_cache() -> None:
    """Drop every cached ``codex_auth`` result (tests, and callers that changed codex state)."""
    _CODEX_CACHE.clear()


def _quote(value: object) -> str:
    """A field value that is safe to put in a reason, or a placeholder."""
    if isinstance(value, str) and _SAFE_VALUE.match(value):
        return value
    return "<unrecognized>"


def _run_json(
    argv: list,
    *,
    what: str,
    env: Mapping[str, str],
    cwd: Optional[Path],
    runner: Callable[..., subprocess.CompletedProcess],
    timeout_s: float,
) -> Tuple[Optional[Any], Optional[str]]:
    """Run ``argv`` and parse its stdout as JSON: ``(document, None)`` or ``(None, failure reason)``."""
    try:
        done = runner(
            argv,
            cwd=None if cwd is None else str(cwd),
            env=dict(env),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, f"{what} timed out after {timeout_s:g}s"
    except Exception as exc:  # contract: never raises; the type names the cause, the message is dropped
        return None, f"{what} could not run ({type(exc).__name__})"
    if done.returncode != 0:
        return None, f"{what} exited {done.returncode}"
    try:
        return json.loads(done.stdout), None
    except (TypeError, ValueError):
        return None, f"{what} output is not JSON"


def claude_auth(
    env: Optional[Mapping[str, str]] = None,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    timeout_s: float = 10,
) -> HarnessAuth:
    """Claude authentication state; ``env`` defaults to ``os.environ``."""
    env = os.environ if env is None else env
    for name in BILLING_DIVERTING_VARS:
        if env.get(name):
            return HarnessAuth(METERED, f"{name} is set")
    try:
        # Unset: the bare name, as always. Set: validated and resolved, so a
        # bad value is named here rather than surfacing as a FileNotFoundError.
        executable = (
            resolve_claude_executable(env) if CLAUDE_EXECUTABLE_ENV in env else "claude"
        )
    except ClaudeExecutableError:
        # Never raises (this probe's contract); the reason names the variable so
        # the misconfiguration is visible, and the dispatch itself raises.
        return HarnessAuth(UNKNOWN, f"{CLAUDE_EXECUTABLE_ENV} is set to an unusable value")
    doc, failure = _run_json(
        [executable, "auth", "status"], what="claude auth status",
        env=env, cwd=None, runner=runner, timeout_s=timeout_s,
    )
    if failure is not None:
        return HarnessAuth(UNKNOWN, failure)
    if not isinstance(doc, dict) or not isinstance(doc.get("authMethod"), str):
        return HarnessAuth(UNKNOWN, "claude auth status has no authMethod field")
    method, provider = doc["authMethod"], doc.get("apiProvider")
    if method == "claude.ai" and provider == "firstParty":
        return HarnessAuth(SUBSCRIPTION, "claude auth status: authMethod claude.ai, apiProvider firstParty")
    return HarnessAuth(
        METERED,
        f"claude auth status: authMethod {_quote(method)}, apiProvider {_quote(provider)}",
    )


def _doctor_check(doc: Mapping[str, Any], name: str) -> Optional[Mapping[str, Any]]:
    """``doc["checks"][name]`` when it is a mapping with status ``ok`` and mapping details, else None."""
    checks = doc.get("checks")
    check = checks.get(name) if isinstance(checks, dict) else None
    if not isinstance(check, dict) or check.get("status") != "ok":
        return None
    return check if isinstance(check.get("details"), dict) else None


def _codex_from_doctor(doc: Any) -> HarnessAuth:
    """Interpret one ``codex doctor --json`` report (see the module docstring for the rule)."""
    if not isinstance(doc, dict):
        return HarnessAuth(UNKNOWN, "codex doctor report is not an object")
    schema = doc.get("schemaVersion")
    if isinstance(schema, bool) or schema != DOCTOR_SCHEMA:
        return HarnessAuth(UNKNOWN, f"codex doctor schemaVersion is not {DOCTOR_SCHEMA}")
    cl, ac = _doctor_check(doc, "config.load"), _doctor_check(doc, "auth.credentials")
    if cl is None or ac is None:
        return HarnessAuth(UNKNOWN, "codex doctor config.load or auth.credentials is missing or not ok")
    provider = cl["details"].get("model provider")
    mode = ac["details"].get("stored auth mode")
    stored_key = ac["details"].get("stored API key")
    if not all(isinstance(v, str) for v in (provider, mode, stored_key)):
        return HarnessAuth(UNKNOWN, "codex doctor lacks model provider, stored auth mode or stored API key")
    if provider != "openai":
        return HarnessAuth(METERED, f"codex doctor: effective provider {_quote(provider)}")
    if mode != "chatgpt" or stored_key != "false":
        return HarnessAuth(
            METERED,
            f"codex doctor: stored auth mode {_quote(mode)}, stored API key {_quote(stored_key)}",
        )
    return HarnessAuth(
        SUBSCRIPTION,
        f"doctor: provider openai, chatgpt auth (codex {_quote(doc.get('codexVersion'))})",
    )


def codex_auth(
    env: Optional[Mapping[str, str]] = None,
    *,
    cwd: Path,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    timeout_s: float = 30.0,
) -> HarnessAuth:
    """Codex authentication state via ``codex doctor --json`` run in ``cwd`` with ``env``."""
    env = os.environ if env is None else env
    for name in CODEX_AUTH_ENV[:2]:
        if env.get(name):
            return HarnessAuth(METERED, f"{name} is set")
    if env.get(CODEX_AUTH_ENV[2]):
        return HarnessAuth(UNKNOWN, f"{CODEX_AUTH_ENV[2]} is set: not proven subscription")
    secrets = "\0".join(env.get(name) or "" for name in CODEX_AUTH_ENV)
    key = (
        str(cwd),
        hashlib.sha256(secrets.encode("utf-8", "surrogateescape")).hexdigest(),
        env.get("CODEX_HOME") or "",
    )
    cached = _CODEX_CACHE.get(key)
    if cached is not None:
        return cached
    doc, failure = _run_json(
        ["codex", "doctor", "--json"], what="codex doctor",
        env=env, cwd=cwd, runner=runner, timeout_s=timeout_s,
    )
    auth = HarnessAuth(UNKNOWN, failure) if failure is not None else _codex_from_doctor(doc)
    if auth.state != UNKNOWN:
        _CODEX_CACHE[key] = auth
    return auth


def probe_harness_auth(
    harnesses: Iterable[str],
    env: Optional[Mapping[str, str]] = None,
    *,
    cwd: Path,
) -> Dict[str, HarnessAuth]:
    """Probe only the claude/codex harnesses named (case-insensitive); keyed by lower-case name."""
    wanted = {h.lower() for h in harnesses if isinstance(h, str)}
    result: Dict[str, HarnessAuth] = {}
    if "claude" in wanted:
        result["claude"] = claude_auth(env)
    if "codex" in wanted:
        result["codex"] = codex_auth(env, cwd=cwd)
    return result
