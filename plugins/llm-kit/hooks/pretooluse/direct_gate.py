#!/usr/bin/env python3
"""PreToolUse hook (matcher Bash): refuse a relay client call that no live grant covers.

THIS IS A GUARDRAIL, NOT A SECURITY BOUNDARY (relay alternative A, owner ruling
2026-11-03). It reads the Bash command string and recognises the plain forms of
``llm-kit relay client <verb> ...`` (also inside ``bash -c '...'`` and ``$(...)``
up to a few levels). A model that wants to get around it can: run a script that
makes the call, abbreviate an option, build the command in a variable, call the
relay over HTTP with the token it can read, or mint its own grant. The gate
stops accidental model-initiated calls and gives the owner a visible refusal.
The relay enforces the same grant independently at its own ingress.

Behaviour
---------
* A Bash command that does not invoke ``llm-kit relay client`` is ignored
  (no output, exit 0). The hook never emits ``allow``, so the normal
  permission flow is untouched.
* Otherwise the call's session (``--session``, which must equal the hook
  payload's session_id), client verb and target are extracted and sent to
  the relay's ``grant_check`` as origin ``direct:<machine>/claude/<session_id>``.
  grant_check never consumes a use. Not covered -> ``permissionDecision: deny``
  with a reason telling the owner to type ``/direct <verb> <target>`` themselves.
* Relay not running: the call is allowed through with additionalContext (the
  client call will itself fail with "relay not running"); the gate refuses
  nothing it cannot judge.
* A command that mentions ``relay client`` but cannot be parsed, or uses an
  option the gate does not know (argparse prefix abbreviations included), is
  denied: the gate does not guess.
* Bad hook input (not JSON, no session_id, a Bash call without a command)
  exits 2, a blocking error with the message on stderr.
"""
from __future__ import annotations

import json
import re
import shlex
import socket
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import direct_relay as R  # noqa: E402

_MAX_DEPTH = 3
_PUNCT = set(";&|()<>")
_TOOL_RE = re.compile(r"^llm[-_]kit(?:\.exe|\.cli|\.__main__)?$")
_MENTION_RE = re.compile(r"relay\s+client")

#: options that take a value, per `llm-kit relay client` (cli/relay.py).
_VALUE_OPTIONS = frozenset({
    "--env-json", "--config", "--state-dir", "--session", "--grant", "--machine", "--harness", "--state",
    "--since", "--limit", "--prompt", "--messages", "--conversation", "--max-tokens", "--temperature",
    "--kind", "--body", "--id", "--expires-in", "--task", "--hop", "--message-id", "--address", "--wait",
    "--decision", "--input", "--reason",
})
_FLAG_OPTIONS = frozenset({"-h", "--help"})


class GateParseError(ValueError):
    """The command mentions the relay client but the gate cannot read it."""


class ClientCall:
    def __init__(self, client_verb: str, session: Optional[str], grant: Optional[str], target: Optional[str]) -> None:
        self.client_verb = client_verb
        self.wire_verb = R.CLIENT_VERBS[client_verb]
        self.session = session
        self.grant = grant
        self.target = target


def _tokenize(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    return list(lexer)


def _segments(tokens: Sequence[str]) -> list[list[str]]:
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token and set(token) <= _PUNCT:
            segments.append([])
        else:
            segments[-1].append(token)
    return [s for s in segments if s]


def find_client_args(command: str, depth: int = 0) -> list[list[str]]:
    """The argument list after ``relay client`` for each invocation in ``command``.

    Raises GateParseError when the command mentions ``relay client`` but cannot be tokenized.
    """
    if not _MENTION_RE.search(command):
        return []
    if depth > _MAX_DEPTH:
        raise GateParseError("the command nests the relay client call too deeply to read")
    try:
        tokens = _tokenize(command)
    except ValueError as exc:
        raise GateParseError(f"the command cannot be parsed ({exc})") from exc
    found: list[list[str]] = []
    for token in tokens:
        if re.search(r"\s", token) and _MENTION_RE.search(token):
            found.extend(find_client_args(token, depth + 1))
    for segment in _segments(tokens):
        for i, token in enumerate(segment[:-1]):
            if token == "relay" and segment[i + 1] == "client":
                if any(_TOOL_RE.match(Path(t).name) for t in segment[:i]):
                    found.append(segment[i + 2:])
                break
    return found


def parse_client_args(args: Sequence[str], machine: Optional[str]) -> Optional[ClientCall]:
    """The call's verb, session, grant and target; None when there is no verb to judge (help, usage error)."""
    verb: Optional[str] = None
    session: Optional[str] = None
    grant: Optional[str] = None
    options: dict[str, str] = {}
    positionals: list[str] = []
    i = 0
    while i < len(args):
        token = args[i]
        if token.startswith("--") and "=" in token:
            name, _, value = token.partition("=")
            if name not in _VALUE_OPTIONS:
                raise GateParseError(f"option {name} is not one the gate knows; write the full option name")
            options[name] = value
        elif token in _VALUE_OPTIONS:
            if i + 1 >= len(args):
                raise GateParseError(f"option {token} has no value")
            options[token] = args[i + 1]
            i += 1
        elif token in _FLAG_OPTIONS:
            return None
        elif token.startswith("-") and len(token) > 1:
            raise GateParseError(f"option {token} is not one the gate knows; write the full option name")
        elif verb is None:
            verb = token
        else:
            positionals.append(token)
        i += 1
    if verb is None or verb not in R.CLIENT_VERBS:
        return None
    session = options.get("--session")
    grant = options.get("--grant")
    target: Optional[str] = None
    if verb in ("read", "send", "answer"):
        target = positionals[0] if positionals else None
    elif verb == "ask":
        if positionals:
            target = positionals[0]
            if "/" not in target and machine is not None:
                target = f"{machine}/llm/{target}"
    elif verb in ("sessions", "questions"):
        target = options.get("--machine")
    elif verb == "replies":
        target = options.get("--message-id") or options.get("--address")
    return ClientCall(verb, session, grant, target)


def _deny(reason: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason,
    }}


def _type_it(call: ClientCall) -> str:
    target = f" {call.target}" if call.target else ""
    return f"`/direct {call.client_verb}{target}`"


def run(
    payload: Mapping[str, Any],
    env: Mapping[str, str],
    *,
    hostname: Optional[Callable[[], str]] = None,
    env_json: Optional[Path] = None,
) -> Optional[dict[str, Any]]:
    """The hook's JSON output for one payload, or None for "no opinion"."""
    if payload.get("tool_name") != "Bash":
        return None
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str):
        raise R.HookError("a Bash tool call carries no command string")
    session = payload["session_id"]
    try:
        arg_lists = find_client_args(command)
    except GateParseError as exc:
        return _deny(f"This command calls the llm-kit relay client, but the grant gate cannot read it ({exc}). "
                     "Use one plain `llm-kit relay client <verb> ...` command per call.")
    if not arg_lists:
        return None
    try:
        machine = R.resolve_machine_label(env_json, hostname=hostname)
    except R.HookError as exc:
        return _deny(f"The grant gate cannot tell which machine this is ({exc}), so the relay client call is refused.")
    notes: list[str] = []
    for args in arg_lists:
        try:
            call = parse_client_args(args, machine)
        except GateParseError as exc:
            return _deny(f"The grant gate cannot read this llm-kit relay client call ({exc}).")
        if call is None:
            continue
        if call.session is not None and call.session != session:
            return _deny(
                f"Refused: --session {call.session} is not this session ({session}). A session acts only for itself; "
                "the owner must type /direct in the session that should act."
            )
        params: dict[str, Any] = {"session": f"{machine}/claude/{session}", "verb": call.wire_verb}
        if call.target is not None:
            params["target"] = call.target
        if call.grant is not None:
            params["grant_id"] = call.grant
        try:
            result = R.call(env, "grant_check", params, origin=f"direct:{machine}/claude/{session}")
        except R.RelayUnavailable as exc:
            notes.append(f"grant gate: the relay is not running, so {call.client_verb} could not be checked ({exc}); "
                         "the client call will fail by itself.")
            continue
        except R.RelayRefused as exc:
            return _deny(f"Refused: the relay could not check a grant for {call.client_verb} ({exc}).")
        except R.HookError as exc:
            return _deny(f"Refused: the grant gate cannot reach the relay safely ({exc}).")
        if result.get("covered") is not True:
            code = result.get("code") or "grant_required"
            return _deny(
                f"Refused ({code}): no live /direct grant covers `llm-kit relay client {call.client_verb}`"
                + (f" on {call.target}" if call.target else "")
                + f" for this session. Only the owner should grant it, by typing {_type_it(call)} in this session. "
                "Do not retry until they have."
            )
    if notes:
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": " ".join(notes)}}
    return None


def main(argv: Optional[Sequence[str]] = None, stdin: Any = None, stdout: Any = None, env: Optional[Mapping[str, str]] = None) -> int:
    import os

    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    env = os.environ if env is None else env
    try:
        payload = R.read_payload(stdin)
        output = run(payload, env, hostname=socket.gethostname)
    except R.HookError as exc:
        print(f"direct_gate.py: {exc}", file=sys.stderr)
        return 2
    if output is not None:
        stdout.write(json.dumps(output, ensure_ascii=True))
        stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
