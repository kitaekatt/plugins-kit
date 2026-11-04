#!/usr/bin/env python3
"""UserPromptSubmit hook: mint a relay grant for a /direct command the user typed.

THIS IS A GUARDRAIL, NOT A SECURITY BOUNDARY (relay alternative A, owner ruling
2026-11-03). The hook decides "the user typed this" from the prompt text alone.
A model with Bash as the same user can read the relay token, forge this hook's
input, or call the relay over HTTP, and mint a grant for itself. The grant gate
stops accidental model-initiated calls to the relay client and gives the owner a
visible refusal; it does not stop a model that is trying to get around it.

Behaviour
---------
* The prompt (leading whitespace stripped) must start with ``/direct`` or
  ``/llm-kit:direct`` followed by whitespace. A channel-injected message
  arrives wrapped in ``<channel ...>...</channel>``, so it can never start with
  ``/direct`` and never mints (Claude Code 2.1.317, measured in K0).
  Anything else does nothing at all.
* ``/direct <verb> <targets...>`` is parsed AS DATA: whitespace split, no shell,
  no quoting, no interpolation. ``<verb>`` is a client verb (machines, sessions,
  read, ask, send, replies, questions, answer) and maps to its relay wire verb.
  An unknown verb or a missing required target blocks the prompt with the
  reason, because the user typed it and a typo should be visible at once.
  ``/direct`` alone and ``/direct help`` mint nothing.
* A grant is minted with ``grant_mint`` as origin
  ``direct:<machine>/claude/<session_id>`` (the session id comes from the hook
  payload), ``origin_proof {"type": "none"}``, a 300 s expiry, and 1 use for a
  write or an LLM call (send, answer, ask) or 8 uses for a read (a read grant
  covers the rest of the invocation: paged reads, polling replies).
* Relay not running, or no token (no relay ever ran): the prompt goes through
  with additionalContext and a systemMessage saying the relay is not running.
  A relay refusal (for example an ambiguous selector) is reported the same
  way. This hook never blocks a prompt because of the relay.
* Bad hook input (not JSON, no usable session_id) exits 1 with a message on
  stderr (a visible, non-blocking hook error).

The machine label and relay token are found the way the relay finds them; see
direct_relay.py (stdlib reimplementation of llm_kit.relay.config).
"""
from __future__ import annotations

import json
import re
import socket
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import direct_relay as R  # noqa: E402

EXPIRES_IN_S = 300
READ_USES = 8
ONE_USE = 1
MAX_TARGETS = 16

_PREFIX_RE = re.compile(r"/(?:llm-kit:)?direct(?=\s|$)")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

#: client verb -> (uses, target shape).
#: "one": exactly the first token is the target and the rest is free text;
#: "many": every leading token before the first option-like token is a target;
#: "none": the verb takes no target.
_SHAPES: Mapping[str, tuple[int, str]] = {
    "machines": (READ_USES, "none"),
    "sessions": (READ_USES, "many"),
    "read": (READ_USES, "many"),
    "replies": (READ_USES, "many"),
    "questions": (READ_USES, "many"),
    "ask": (ONE_USE, "one"),
    "send": (ONE_USE, "one"),
    "answer": (ONE_USE, "one"),
}
#: verbs whose target is optional.
_OPTIONAL_TARGET = frozenset({"machines", "sessions", "questions", "replies"})
#: option names that may carry the target of a "many" verb.
_TARGET_OPTIONS = {"--machine": "sessions questions", "--address": "replies", "--message-id": "replies"}


class DirectParseError(ValueError):
    """The typed /direct line is malformed."""


class DirectCommand:
    def __init__(self, client_verb: str, targets: list[str], uses: int, command: str) -> None:
        self.client_verb = client_verb
        self.wire_verb = R.CLIENT_VERBS[client_verb]
        self.targets = targets
        self.uses = uses
        self.command = command


def parse_direct(prompt: str, machine: Optional[str] = None) -> Optional[DirectCommand]:
    """The typed command, or None when this prompt is not one (or is /direct with nothing to mint).

    ``machine`` is used only to complete a bare ``ask`` endpoint name to
    ``<machine>/llm/<name>`` as ``llm-kit relay client ask`` does.
    Raises DirectParseError for a malformed command.
    """
    text = prompt.lstrip()
    if text.startswith("<channel "):
        return None
    match = _PREFIX_RE.match(text)
    if match is None:
        return None
    tokens = text[match.end():].split()
    if not tokens or tokens[0] == "help":
        return None
    verb = tokens[0]
    if verb not in R.CLIENT_VERBS:
        raise DirectParseError(
            f"unknown /direct verb {verb!r}; expected one of: {', '.join(R.CLIENT_VERBS)}"
        )
    uses, shape = _SHAPES[verb]
    rest = tokens[1:]
    targets: list[str] = []
    if shape == "one":
        if not rest or rest[0].startswith("-"):
            raise DirectParseError(f"/direct {verb} needs a target: /direct {verb} <target> ...")
        targets = [rest[0]]
    elif shape == "many":
        i = 0
        while i < len(rest) and not rest[i].startswith("-"):
            targets.append(rest[i])
            i += 1
        while i < len(rest):
            if rest[i] in _TARGET_OPTIONS and verb in _TARGET_OPTIONS[rest[i]].split():
                if i + 1 >= len(rest):
                    raise DirectParseError(f"/direct {verb}: {rest[i]} needs a value")
                targets.append(rest[i + 1])
                i += 1
            i += 1
        if not targets and verb not in _OPTIONAL_TARGET:
            raise DirectParseError(f"/direct {verb} needs a target: /direct {verb} <target> ...")
    if len(targets) > MAX_TARGETS:
        raise DirectParseError(f"/direct {verb}: at most {MAX_TARGETS} targets")
    for target in targets:
        if len(target) > 1100 or _CONTROL_RE.search(target):
            raise DirectParseError(f"/direct {verb}: target {target[:40]!r} is not valid")
    if verb == "ask" and machine is not None and "/" not in targets[0]:
        targets = [f"{machine}/llm/{targets[0]}"]
    command = " ".join(text.split("\n", 1)[0].split())[:1024]
    return DirectCommand(verb, targets, uses, command)


def _context(text: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": text}}


def _notice(text: str) -> dict[str, Any]:
    out = _context(text)
    out["systemMessage"] = text
    return out


def run(
    payload: Mapping[str, Any],
    env: Mapping[str, str],
    *,
    hostname: Optional[Callable[[], str]] = None,
    env_json: Optional[Path] = None,
) -> Optional[dict[str, Any]]:
    """The hook's JSON output for one payload, or None for "do nothing"."""
    prompt = payload.get("prompt")
    if not isinstance(prompt, str):
        return None
    session = payload["session_id"]
    # Cheap exit before any file or network access: only a typed /direct line goes further.
    if not _PREFIX_RE.match(prompt.lstrip()):
        return None
    try:
        machine = R.resolve_machine_label(env_json, hostname=hostname)
    except R.HookError as exc:
        return _notice(f"/direct: cannot mint a grant, the machine label is unresolved ({exc}). No grant was minted.")
    try:
        command = parse_direct(prompt, machine)
    except DirectParseError as exc:
        return {"decision": "block", "reason": f"/direct: {exc}"}
    if command is None:
        return None
    origin = f"direct:{machine}/claude/{session}"
    params = {
        "session": f"{machine}/claude/{session}",
        "verb": command.wire_verb,
        "targets": command.targets,
        "expires_in_s": EXPIRES_IN_S,
        "uses": command.uses,
        "origin_proof": {"type": "none"},
        "command": command.command,
    }
    try:
        result = R.call(env, "grant_mint", params, origin=origin)
    except R.RelayUnavailable as exc:
        return _notice(
            "/direct: the llm-kit relay is not running, so no grant was minted and the command cannot run "
            f"(start it with `llm-kit relay serve`). Detail: {exc}"
        )
    except R.RelayRefused as exc:
        return _notice(f"/direct: the relay refused to mint a grant ({exc}). No grant was minted.")
    except R.HookError as exc:
        return _notice(f"/direct: cannot reach the relay safely ({exc}). No grant was minted.")
    grant = result.get("grant") if isinstance(result.get("grant"), dict) else {}
    shown = ", ".join(grant.get("targets", command.targets)) or "no target"
    return _context(
        f"/direct: the prompt starts with /direct and a relay grant was minted: verb {command.wire_verb}, "
        f"targets {shown}, {EXPIRES_IN_S} s, {command.uses} use(s). The grant is a guardrail against "
        "accidental calls, not a security boundary. Calls to `llm-kit relay client "
        f"{command.client_verb} --session {session}` for these targets now pass the grant gate."
    )


def main(argv: Optional[Sequence[str]] = None, stdin: Any = None, stdout: Any = None, env: Optional[Mapping[str, str]] = None) -> int:
    import os

    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    env = os.environ if env is None else env
    try:
        payload = R.read_payload(stdin)
    except R.HookError as exc:
        print(f"direct_grant.py: {exc}", file=sys.stderr)
        return 1
    output = run(payload, env, hostname=socket.gethostname)
    if output is not None:
        stdout.write(json.dumps(output, ensure_ascii=True))
        stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
