"""Shared stdlib-only helpers for the /direct grant hooks.

Used by userpromptsubmit/direct_grant.py (mints a grant for a typed /direct
command) and pretooluse/direct_gate.py (refuses a Bash call to the relay client
that no live grant covers).

THIS IS A GUARDRAIL, NOT A SECURITY BOUNDARY (relay alternative A, owner
ruling 2026-11-03). A model that can run Bash as the same user can read the
relay token file, forge hook input, or call the relay over HTTP and mint a
grant for itself. The hooks stop accidental model-initiated relay calls and
give the owner a visible refusal; they do not stop a model that is trying to
get around them. Nothing here may be described to a user as protection against
a same-user process.

The hooks run under the bootstrap standalone interpreter, which carries no
llm_kit package, so the few facts they need from llm_kit are reimplemented
here, reading the same files the same way:

* machine label: ``llm_kit.relay.config.resolve_machine`` -- the host name must
  match a key of ``~/.claude/env.json`` ``machines`` (case-insensitive, whole
  name); the label is that machine's ``tailscale_hostname`` lowercased; an
  unknown or disabled machine is an error, with no fallback label.
* state directory: ``$XDG_STATE_HOME/llm-kit/relay`` (absolute values only),
  else ``~/.local/state/llm-kit/relay``; the relay token is ``relay.token``
  there and is refused when its mode is loose or its owner is foreign.
* listener: 127.0.0.1:4710 unless ``$XDG_CONFIG_HOME/llm-kit/relay.yaml``
  (else ``~/.config/llm-kit/relay.yaml``) carries a block-style ``relay:``
  mapping with ``host`` and ``port``; any other shape of that block is an
  error here rather than a guess. The host must be a literal loopback address.
* wire format: ``relay-protocol/1``, ``POST /v1/<verb>`` with ``X-Relay-Token``
  and optionally ``X-Relay-Grant``.

Errors are raised, never swallowed (plugin standard: fail loudly).
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import stat
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

PROTOCOL = "relay-protocol/1"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4710
HTTP_TIMEOUT_S = 5.0

_RESERVED_LABELS = ("claude", "codex", "llm", "latest", "task", "cwd", "q")
_RESERVED_ALT = "|".join(_RESERVED_LABELS)
MACHINE_LABEL = rf"(?!(?:{_RESERVED_ALT})(?![a-z0-9-]))[a-z0-9](?:[a-z0-9-]{{0,61}}[a-z0-9])?"
SESSION_ID = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
MACHINE_RE = re.compile(MACHINE_LABEL)
SESSION_RE = re.compile(SESSION_ID)

#: client verb (``/direct`` and ``llm-kit relay client``) -> wire verb, protocol section 8.
CLIENT_VERBS: Mapping[str, str] = {
    "machines": "list_machines",
    "sessions": "list_sessions",
    "read": "read_session",
    "ask": "ask_llm",
    "send": "send_message",
    "replies": "get_replies",
    "questions": "list_questions",
    "answer": "answer_question",
}


class HookError(Exception):
    """A configuration or input problem the hook cannot recover from. Always loud."""


class RelayUnavailable(HookError):
    """The relay cannot be reached (not running, or no token because none ever ran)."""


class RelayRefused(Exception):
    """The relay answered with an error envelope."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def valid_session_id(session: object) -> bool:
    return isinstance(session, str) and session != "latest" and SESSION_RE.fullmatch(session) is not None


# -- machine label ---------------------------------------------------------------------

def resolve_machine_label(env_json: Optional[Path] = None, *, hostname: Optional[Callable[[], str]] = None) -> str:
    """This machine's relay label (see the module docstring for the rules)."""
    path = Path(env_json) if env_json is not None else Path.home() / ".claude" / "env.json"
    host = (hostname or socket.gethostname)()
    if not isinstance(host, str) or not host.strip():
        raise HookError("the host name is empty; cannot resolve the machine label")
    host = host.strip()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise HookError(f"machine registry {path} does not exist") from None
    except (OSError, ValueError) as exc:
        raise HookError(f"cannot read machine registry {path}: {exc}") from exc
    machines = raw.get("machines") if isinstance(raw, dict) else None
    if not isinstance(machines, dict):
        raise HookError(f"machine registry {path} has no 'machines' mapping")
    matches = [key for key in machines if isinstance(key, str) and key.lower() == host.lower()]
    if len(matches) != 1:
        raise HookError(f"host {host!r} matches {len(matches)} machines in {path}; it must match exactly one")
    entry = machines[matches[0]]
    if not isinstance(entry, dict) or entry.get("disabled") is True:
        raise HookError(f"machine {matches[0]!r} in {path} is not an enabled machine")
    tailscale = entry.get("tailscale_hostname")
    if not isinstance(tailscale, str) or not tailscale.strip():
        raise HookError(f"machine {matches[0]!r} in {path} has no tailscale_hostname to use as its label")
    label = tailscale.strip().lower()
    if MACHINE_RE.fullmatch(label) is None:
        raise HookError(f"machine {matches[0]!r} has tailscale_hostname {tailscale!r}, which is not a valid machine label")
    return label


# -- state, token, listener --------------------------------------------------------------

def _absolute_env(env: Mapping[str, str], name: str) -> Optional[Path]:
    value = (env.get(name) or "").strip()
    return Path(value) if value and os.path.isabs(value) else None


def state_dir(env: Mapping[str, str]) -> Path:
    home = _absolute_env(env, "XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return home / "llm-kit" / "relay"


def config_file(env: Mapping[str, str]) -> Path:
    home = _absolute_env(env, "XDG_CONFIG_HOME") or Path.home() / ".config"
    return home / "llm-kit" / "relay.yaml"


def read_token(env: Mapping[str, str]) -> str:
    path = state_dir(env) / "relay.token"
    try:
        info = path.stat()
    except FileNotFoundError:
        raise RelayUnavailable(f"relay token {path} does not exist: no relay has run on this state directory") from None
    except OSError as exc:
        raise HookError(f"cannot stat relay token {path}: {exc}") from exc
    if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise HookError(f"relay token {path} has a loose mode {stat.S_IMODE(info.st_mode):o}; refusing to read it")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise HookError(f"relay token {path} is owned by another user; refusing to read it")
    try:
        value = path.read_text(encoding="ascii").strip()
    except (OSError, ValueError) as exc:
        raise HookError(f"cannot read relay token {path}: {exc}") from exc
    if not value:
        raise HookError(f"relay token {path} is empty")
    return value


def _scalar(text: str) -> str:
    text = text.split("#", 1)[0].strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1]
    return text


def listener(env: Mapping[str, str]) -> tuple[str, int]:
    """Host and port of the local relay: the defaults, or relay.yaml's block-style ``relay:`` mapping."""
    path = config_file(env)
    if not path.is_file():
        return DEFAULT_HOST, DEFAULT_PORT
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError) as exc:
        raise HookError(f"cannot read relay config {path}: {exc}") from exc
    host, port = DEFAULT_HOST, DEFAULT_PORT
    in_block = False
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[0].isspace():
            key, _, rest = line.partition(":")
            in_block = key.strip() == "relay"
            if in_block and _scalar(rest):
                raise HookError(
                    f"relay config {path}: the 'relay:' block must be written in block style "
                    "(host and port on their own indented lines) for the /direct hooks to read it"
                )
            continue
        if in_block:
            key, _, rest = line.strip().partition(":")
            if key == "host":
                host = _scalar(rest)
            elif key == "port":
                try:
                    port = int(_scalar(rest))
                except ValueError:
                    raise HookError(f"relay config {path}: relay port {rest.strip()!r} is not an integer") from None
    try:
        loopback = ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        raise HookError(f"relay config {path}: relay host {host!r} is not a literal loopback address")
    if not 1 <= port <= 65535:
        raise HookError(f"relay config {path}: relay port {port} is out of range")
    return host, port


def relay_url(env: Mapping[str, str]) -> str:
    host, port = listener(env)
    host = host.strip("[]")
    return f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"


# -- the call ------------------------------------------------------------------------------

def call(env: Mapping[str, str], verb: str, params: Mapping[str, Any], *, origin: str,
         grant: Optional[str] = None, timeout_s: float = HTTP_TIMEOUT_S) -> dict[str, Any]:
    """One relay verb. Returns the result; raises RelayRefused (error envelope), RelayUnavailable (cannot connect)."""
    token = read_token(env)
    url = f"{relay_url(env)}/v1/{verb}"
    request_id = "req-hook-" + os.urandom(8).hex()
    envelope = {"protocol": PROTOCOL, "id": request_id, "verb": verb, "origin": origin, "params": dict(params)}
    body = json.dumps(envelope, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    headers = {"Content-Type": "application/json", "X-Relay-Token": token}
    if grant is not None:
        headers["X-Relay-Grant"] = grant
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never route the token through a proxy
    try:
        with opener.open(request, timeout=timeout_s) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
    except (urllib.error.URLError, OSError) as exc:
        raise RelayUnavailable(f"the relay at {url} did not answer: {type(exc).__name__}: {exc}") from exc
    try:
        reply = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise HookError(f"the relay at {url} answered with a body that is not JSON") from exc
    if not isinstance(reply, dict) or reply.get("protocol") != PROTOCOL:
        raise HookError(f"the relay at {url} answered without a {PROTOCOL} envelope")
    if reply.get("ok") is True:
        result = reply.get("result")
        if not isinstance(result, dict):
            raise HookError(f"the relay at {url} sent an ok response without a result object")
        return result
    error = reply.get("error")
    if not isinstance(error, dict) or not isinstance(error.get("code"), str):
        raise HookError(f"the relay at {url} sent a malformed error object")
    raise RelayRefused(error["code"], str(error.get("message", "")))


def read_payload(stream: Any) -> dict[str, Any]:
    """The hook's stdin JSON object; HookError when it is not one or carries no usable session_id."""
    try:
        payload = json.loads(stream.read())
    except ValueError as exc:
        raise HookError(f"hook input is not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise HookError("hook input is not a JSON object")
    if not valid_session_id(payload.get("session_id")):
        raise HookError(f"hook input has no usable session_id (got {payload.get('session_id')!r})")
    return payload
