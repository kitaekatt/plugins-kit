#!/usr/bin/env python3
"""UserPromptSubmit hook: a /direct the owner typed enables /direct for the rest of the session.

Owner ruling 2026-11-11, verbatim: "so i only want to type /direct once" and
"sure /clear can turn it off". So one typed ``/direct`` (with or without a
verb) enables every /direct verb in this Claude Code session -- machines,
sessions, read, ask, send, replies, questions, answer -- with no expiry and no
use count, until the session ends or the owner runs ``/clear`` (the SessionEnd
hook, sessionend/direct_revoke.py, deletes the record). A session where the
owner never typed /direct stays refused by the gate (pretooluse/direct_gate.py).

THIS IS A GUARDRAIL, NOT A SECURITY BOUNDARY (relay alternative A, owner ruling
2026-11-03). The hook decides "the owner typed this" from the prompt text alone.
A model with Bash as the same user can write the enablement record itself,
forge this hook's input, or call the relay over HTTP with the token it can
read. The gate stops accidental model-initiated relay calls and gives the owner
a visible refusal; it does not stop a model that is trying to get around it.

Behaviour
---------
* The prompt (leading whitespace stripped) must start with ``/direct`` or
  ``/llm-kit:direct`` followed by whitespace or the end. A channel-injected
  message arrives wrapped in ``<channel ...>...</channel>``, so it can never
  start with ``/direct`` and never enables (Claude Code 2.1.317, measured in
  K0). Anything else does nothing at all, before any file access.
* The typed line is not parsed here: the skill runs the verb it names. The
  first line (whitespace collapsed, at most 1024 characters) is kept in the
  record, and the gate sends it as the audit ``command`` of each grant it mints.
* The record is written whether or not the relay is running; the relay is not
  asked. A failure to write it is reported (additionalContext and a
  systemMessage) and never blocks the prompt.
* Bad hook input (not JSON, no usable session_id) exits 1 with a message on
  stderr (a visible, non-blocking hook error).
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import direct_relay as R  # noqa: E402

_PREFIX_RE = re.compile(r"/(?:llm-kit:)?direct(?=\s|$)")
MAX_COMMAND_CHARS = 1024


def typed_direct(prompt: str) -> Optional[str]:
    """The typed /direct line (first line, whitespace collapsed), or None when the prompt is not one."""
    text = prompt.lstrip()
    if text.startswith("<channel ") or _PREFIX_RE.match(text) is None:
        return None
    return " ".join(text.split("\n", 1)[0].split())[:MAX_COMMAND_CHARS]


def _context(text: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": text}}


def run(payload: Mapping[str, Any], env: Mapping[str, str], *, now: Callable[[], float] = time.time) -> Optional[dict[str, Any]]:
    """The hook's JSON output for one payload, or None for "do nothing"."""
    prompt = payload.get("prompt")
    if not isinstance(prompt, str):
        return None
    command = typed_direct(prompt)
    if command is None:
        return None
    session = payload["session_id"]
    try:
        R.enable(env, session, command, now=now())
    except R.HookError as exc:
        out = _context(f"/direct: this session could not be enabled ({exc}). Relay client calls stay refused.")
        out["systemMessage"] = out["hookSpecificOutput"]["additionalContext"]
        return out
    return _context(
        f"/direct: the owner typed /direct, so this session ({session}) is enabled for every /direct verb "
        f"({', '.join(R.CLIENT_VERBS)}) until the session ends or the owner runs /clear. Each call is one plain "
        f"command, `{R.launcher()} relay client <verb> --session {session} ...`; the gate "
        "(llm-kit hooks/pretooluse/direct_gate.py) lets it through with no permission prompt and shows the owner "
        "every message it sends. Run only what the owner asks for. "
        "The enablement is a guardrail against accidental calls, not a security boundary."
    )


def main(argv: Optional[Sequence[str]] = None, stdin: Any = None, stdout: Any = None, env: Optional[Mapping[str, str]] = None) -> int:
    import os

    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    env = os.environ if env is None else env
    try:
        payload = R.read_payload(stdin)
    except R.HookError as exc:
        print(f"direct_enable.py: {exc}", file=sys.stderr)
        return 1
    output = run(payload, env)
    if output is not None:
        stdout.write(json.dumps(output, ensure_ascii=True))
        stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
