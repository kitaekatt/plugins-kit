#!/usr/bin/env python3
"""PreToolUse hook (matcher Bash): let an enabled session's relay client calls through, refuse the rest.

Owner ruling 2026-11-11, verbatim: "so i only want to type /direct once" and
"I don't think sending a message should show a permission prompt. I do think
messages that are sent should be visible in harness." So once the owner has
typed /direct in this session (userpromptsubmit/direct_enable.py), every plain
relay client call of this plugin's launcher is ALLOWED (no permission prompt)
and a send or an answer shows its exact text and target to the owner. A session
where the owner never typed /direct, or ran /clear since, is refused.

THIS IS A GUARDRAIL, NOT A SECURITY BOUNDARY (relay alternative A, owner ruling
2026-11-03). It reads the Bash command string. A model that wants to get around
it can write the enablement record, run a script that makes the call, or call
the relay over HTTP with the token it can read and mint its own grant. The gate
stops accidental model-initiated calls and gives the owner a visible refusal.
The relay still checks a grant at its own ingress: the gate mints one for each
call it allows, so a relay client call that did not pass the gate is refused
there.

Behaviour
---------
* A Bash command that does not invoke ``llm-kit relay client`` is ignored (no
  output, exit 0): the normal permission flow is untouched. Invoking means a
  command word (after separators, substitutions and ``bash -c`` strings are
  read as the shell reads them) that runs the launcher followed by ``relay
  client``; the same words in a quoted string, commit message, heredoc body,
  comment, or echo/grep argument are text and do not engage the gate.
* A command that invokes it is allowed only when ALL of these hold; otherwise
  it is denied with the reason:

  - it is one plain command: this plugin's own launcher (``bin/llm-kit``, the
    path the skill writes) as the first word, then ``relay client <verb>``,
    and nothing else -- no pipe, list, redirection, substitution, variable,
    glob, wrapper (``bash -c``) or second command. Words are bare characters
    from ``[A-Za-z0-9_./:=@%+,-]`` or single-quoted text, and ``\\'`` outside
    quotes is a literal quote (the skill's ``'\\''`` idiom). The gate allows
    only what it can read exactly as the shell will run it;
  - every option is one the client defines, written in full (argparse prefix
    abbreviations are refused), and none of ``--grant``, ``--config``,
    ``--state-dir``, ``--env-json`` (the gate grants each call itself, against
    the default relay) or ``--body -`` (the owner must see the text);
  - this machine's relay.yaml turns the relay on (top-level ``enabled:
    true``, direct_relay.relay_opt_in); the relay is off by default and the
    denial names the file and the key;
  - ``--session`` is this session's id (a session acts only for itself);
  - this session is enabled (the owner typed /direct in it);
  - the relay mints the call's grant: ``grant_mint`` for exactly this verb and
    target, one use, 120 s, with the typed /direct line as its audit
    ``command``. A relay refusal (an unknown or ambiguous selector, for
    example) denies with the relay's code.

* An allowed call returns ``permissionDecision: allow`` and a
  ``permissionDecisionReason`` naming the call. A ``send`` or ``answer`` also
  returns a ``systemMessage`` with the exact target and text, which Claude Code
  shows to the owner in the session.
* Relay not running: an enabled session's plain call is allowed with
  additionalContext; the client call then fails by itself.
* ``--help`` or a call with no verb is left to the normal permission flow.
* Bad hook input (not JSON, no session_id, a Bash call without a command)
  exits 2, a blocking error with the message on stderr.
"""
from __future__ import annotations

import json
import os
import re
import socket
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import direct_relay as R  # noqa: E402

_MAX_DEPTH = 3
_TOOL_RE = re.compile(r"^llm[-_]kit(?:\.exe|\.cli|\.__main__)?$")
_MENTION_RE = re.compile(r"relay\s+client")
_BARE_RE = re.compile(r"[A-Za-z0-9_./:=@%+,-]")

GRANT_TTL_S = 120
GRANT_USES = 1

#: options that take a value, per `llm-kit relay client` (cli/relay.py).
_VALUE_OPTIONS = frozenset({
    "--env-json", "--config", "--state-dir", "--session", "--grant", "--machine", "--harness", "--state",
    "--since", "--limit", "--prompt", "--messages", "--conversation", "--max-tokens", "--temperature",
    "--kind", "--body", "--id", "--expires-in", "--task", "--hop", "--message-id", "--address", "--wait",
    "--decision", "--input", "--reason",
})
_FLAG_OPTIONS = frozenset({"-h", "--help"})
#: options the gate refuses on an allowed call, with the reason.
_REFUSED_OPTIONS: Mapping[str, str] = {
    "--grant": "omit --grant; the gate grants each call itself",
    "--config": "the gate grants calls against the default relay only; omit --config",
    "--state-dir": "the gate grants calls against the default relay only; omit --state-dir",
    "--env-json": "the gate grants calls against the default relay only; omit --env-json",
}


class GateParseError(ValueError):
    """The command mentions the relay client but the gate cannot read it."""


class ClientCall:
    def __init__(self, client_verb: str, session: Optional[str], target: Optional[str],
                 options: Mapping[str, str]) -> None:
        self.client_verb = client_verb
        self.wire_verb = R.CLIENT_VERBS[client_verb]
        self.session = session
        self.target = target
        self.options = dict(options)


# -- finding relay client calls (any form) ---------------------------------------------------

class _Word:
    """One shell word: its literal text and whether a shell would expand it (variable, glob, substitution)."""

    __slots__ = ("text", "dynamic")

    def __init__(self, text: str, dynamic: bool) -> None:
        self.text = text
        self.dynamic = dynamic


_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\+?=")
_KEYWORDS = frozenset({"{", "}", "!", "if", "then", "else", "elif", "do", "while", "until", "time", "coproc",
                       "function"})
_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh", "eval"})
#: command words that run another command given as their arguments.
_WRAPPERS = _SHELLS | frozenset({
    "env", "sudo", "doas", "command", "builtin", "exec", "nohup", "nice", "ionice", "timeout", "xargs", "stdbuf",
    "setsid", "caffeinate", "watch", "uv", "uvx", "pipx", "npx", "script",
})
_PYTHON_RE = re.compile(r"^python[0-9.]*(?:\.exe)?$")


def _mentions(text: str) -> bool:
    """Cheap pre-filter: could ``text`` spell ``relay client`` once quotes and escapes are removed?"""
    return _MENTION_RE.search(re.sub(r"['\"\\]", "", text)) is not None


class _Scanner:
    """Splits a shell command line into simple commands (lists of words), the way a shell reads it.

    Quoted text, comments, redirection targets and heredoc bodies are never command words. Command
    substitutions ($( ), backticks, <( ), >( )) found in unquoted text, double quotes or an unquoted-delimiter
    heredoc body are scanned as commands of their own. Raises GateParseError for text it cannot read.
    """

    def __init__(self, text: str, depth: int) -> None:
        if depth > _MAX_DEPTH:
            raise GateParseError("the command nests substitutions or wrappers too deeply to read")
        self.s = text.replace("\\\n", "")
        self.depth = depth
        self.commands: list[list[_Word]] = []
        self.pending_heredocs: list[tuple[str, bool, bool]] = []  # (delimiter, quoted, strip tabs)

    def scan(self) -> list[list[_Word]]:
        end = self._run(0, stop_paren=False)
        assert end == len(self.s)
        return self.commands

    # -- nested text -------------------------------------------------------------------------
    def _nested(self, text: str) -> None:
        self.commands.extend(_Scanner(text, self.depth + 1).scan())

    def _paren_substitution(self, i: int) -> int:
        """``i`` is just past ``$(``; scan to the matching ``)``; returns the index past it."""
        sub = _Scanner.__new__(_Scanner)
        sub.s, sub.depth, sub.commands, sub.pending_heredocs = self.s, self.depth + 1, self.commands, []
        if sub.depth > _MAX_DEPTH:
            raise GateParseError("the command nests substitutions or wrappers too deeply to read")
        return sub._run(i, stop_paren=True)

    def _backtick(self, i: int) -> int:
        """``i`` is just past an opening backtick; returns the index past the closing one."""
        j, buf = i, []
        while j < len(self.s):
            c = self.s[j]
            if c == "\\" and j + 1 < len(self.s):
                nxt = self.s[j + 1]
                buf.append(nxt if nxt in "`\\$" else c + nxt)
                j += 2
                continue
            if c == "`":
                self._nested("".join(buf))
                return j + 1
            buf.append(c)
            j += 1
        raise GateParseError("the command has an unterminated backtick")

    def _double_quoted(self, i: int, closing: Optional[str] = '"') -> tuple[str, bool, int]:
        """``i`` is just past an opening double quote. Returns (text, dynamic, index past the closing quote).

        ``closing=None`` scans to the end of the text (a heredoc body)."""
        out: list[str] = []
        dynamic = False
        n = len(self.s)
        while i < n:
            c = self.s[i]
            if closing is not None and c == closing:
                return "".join(out), dynamic, i + 1
            if c == "\\" and i + 1 < n:
                nxt = self.s[i + 1]
                out.append(nxt if nxt in '"\\$`' else c + nxt)
                i += 2
            elif c == "$" and self.s.startswith("(", i + 1):
                dynamic = True
                i = self._paren_substitution(i + 2)
            elif c == "$":
                dynamic = dynamic or re.match(r"[A-Za-z0-9_{@*#?!$-]", self.s[i + 1:i + 2]) is not None
                out.append(c)
                i += 1
            elif c == "`":
                dynamic = True
                i = self._backtick(i + 1)
            else:
                out.append(c)
                i += 1
        if closing is None:
            return "".join(out), dynamic, i
        raise GateParseError("the command has an unterminated double quote")

    # -- heredocs ----------------------------------------------------------------------------
    def _read_heredoc_bodies(self, i: int) -> int:
        """``i`` is just past a newline; consume the body of each pending heredoc."""
        n = len(self.s)
        for delimiter, quoted, strip in self.pending_heredocs:
            body: list[str] = []
            while i < n:
                eol = self.s.find("\n", i)
                line = self.s[i:] if eol < 0 else self.s[i:eol]
                i = n if eol < 0 else eol + 1
                if (line.lstrip("\t") if strip else line) == delimiter:
                    break
                body.append(line)
            if not quoted:
                self._expand_heredoc_body("\n".join(body))
        self.pending_heredocs = []
        return i

    def _expand_heredoc_body(self, body: str) -> None:
        """An unquoted-delimiter body is double-quote-like text: only its substitutions run."""
        probe = _Scanner.__new__(_Scanner)
        probe.s, probe.depth, probe.commands, probe.pending_heredocs = body, self.depth + 1, self.commands, []
        probe._double_quoted(0, None)

    def _heredoc_delimiter(self, i: int) -> tuple[str, bool, int]:
        n = len(self.s)
        while i < n and self.s[i] in " \t":
            i += 1
        raw_start = i
        while i < n and self.s[i] not in " \t\n;&|()<>":
            if self.s[i] in "'\"":
                close = self.s.find(self.s[i], i + 1)
                if close < 0:
                    raise GateParseError("the command has an unterminated quote in a heredoc delimiter")
                i = close
            i += 1
        raw = self.s[raw_start:i]
        if not raw:
            raise GateParseError("the command has a heredoc with no delimiter")
        quoted = any(ch in raw for ch in "'\"\\")
        return re.sub(r"['\"\\]", "", raw), quoted, i

    # -- the main loop -----------------------------------------------------------------------
    def _run(self, i: int, *, stop_paren: bool) -> int:
        s, n = self.s, len(self.s)
        words: list[_Word] = []
        parts: list[str] = []
        dynamic = False
        in_word = False
        skip_next = False  # the next word is a redirection target
        depth = 0

        def end_word() -> None:
            nonlocal parts, dynamic, in_word, skip_next
            if in_word:
                if skip_next:
                    skip_next = False
                else:
                    words.append(_Word("".join(parts), dynamic))
            parts, dynamic, in_word = [], False, False

        def end_command() -> None:
            nonlocal words, skip_next
            end_word()
            if words:
                self.commands.append(words)
            words, skip_next = [], False

        while i < n:
            c = s[i]
            if c in " \t":
                end_word()
                i += 1
            elif c == "\n":
                end_command()
                i += 1
                if self.pending_heredocs:
                    i = self._read_heredoc_bodies(i)
            elif c == "#" and not in_word:
                eol = s.find("\n", i)
                i = n if eol < 0 else eol
            elif c in ";&|":
                end_command()
                i += 1
            elif c == "(":
                end_command()
                depth += 1
                i += 1
            elif c == ")":
                end_command()
                if depth > 0:
                    depth -= 1
                    i += 1
                elif stop_paren:
                    return i + 1
                else:
                    raise GateParseError("the command has an unbalanced parenthesis")
            elif c in "<>":
                if s.startswith("(", i + 1):
                    in_word, dynamic = True, True
                    i = self._paren_substitution(i + 2)
                    continue
                if in_word and "".join(parts).isdigit() and not dynamic:
                    parts, in_word = [], False  # a file descriptor prefix such as 2>
                end_word()
                if s.startswith("<<", i) and not s.startswith("<<<", i):
                    i += 2
                    strip = s.startswith("-", i)
                    i += 1 if strip else 0
                    delimiter, quoted, i = self._heredoc_delimiter(i)
                    self.pending_heredocs.append((delimiter, quoted, strip))
                else:
                    i += 3 if s.startswith("<<<", i) else (2 if s.startswith(">>", i) or s.startswith(">&", i)
                                                          or s.startswith("<&", i) or s.startswith(">|", i) else 1)
                    skip_next = True
            elif c == "'":
                close = s.find("'", i + 1)
                if close < 0:
                    raise GateParseError("the command has an unterminated single quote")
                parts.append(s[i + 1:close])
                in_word = True
                i = close + 1
            elif c == '"':
                text, dyn, i = self._double_quoted(i + 1)
                parts.append(text)
                dynamic, in_word = dynamic or dyn, True
            elif c == "`":
                dynamic, in_word = True, True
                i = self._backtick(i + 1)
            elif c == "$" and s.startswith("(", i + 1):
                dynamic, in_word = True, True
                i = self._paren_substitution(i + 2)
            elif c == "$" and s.startswith("'", i + 1):
                j = i + 2
                while j < n and s[j] != "'":
                    j += 2 if s[j] == "\\" else 1
                if j >= n:
                    raise GateParseError("the command has an unterminated $'...' string")
                parts.append(s[i + 2:j])
                dynamic, in_word = True, True  # escapes are decoded by the shell: not the literal text
                i = j + 1
            elif c == "$":
                parts.append(c)
                dynamic, in_word = True, True
                i += 1
            elif c == "\\":
                if i + 1 >= n:
                    raise GateParseError("the command ends in a backslash")
                parts.append(s[i + 1])
                in_word = True
                i += 2
            else:
                if c in "*?[~" and (c != "~" or not in_word):
                    dynamic = True
                parts.append(c)
                in_word = True
                i += 1
        if stop_paren:
            raise GateParseError("the command has an unterminated $( substitution")
        end_command()
        return i


def _relay_client_index(words: Sequence[_Word], start: int = 0) -> int:
    """The index of the ``relay`` word of the first literal ``relay client`` pair at or after ``start``, or -1."""
    for i in range(start, len(words) - 1):
        if words[i].text == "relay" and words[i + 1].text == "client" and not (words[i].dynamic or words[i + 1].dynamic):
            return i
    return -1


def _is_tool(word: _Word) -> bool:
    return not word.dynamic and _TOOL_RE.match(Path(word.text).name) is not None


def _invocations(words: Sequence[_Word], depth: int) -> list[list[str]]:
    """The arguments after ``relay client`` when this simple command runs the client, in command position."""
    k = 0
    while k < len(words) and (_ASSIGN_RE.match(words[k].text) or words[k].text in _KEYWORDS):
        k += 1
    words = words[k:]
    if not words:
        return []
    head, rest = words[0], words[1:]
    pair = _relay_client_index(words)
    if pair < 0:
        # No literal ``relay client`` pair here. A shell or eval may still carry one inside a string argument.
        base = Path(head.text).name
        if not head.dynamic and base in _SHELLS:
            return _from_strings(rest, depth)
        return []
    if head.dynamic:
        raise GateParseError("a command word is built by the shell (variable, glob or substitution) before "
                             "`relay client`")
    base = Path(head.text).name
    if _TOOL_RE.match(base):
        return [[w.text for w in words[pair + 2:]]]
    if base in _WRAPPERS or _PYTHON_RE.match(base):
        # Wrapper or interpreter: the tool word may follow, after options.
        if any(_is_tool(w) for w in words[:pair]):
            return [[w.text for w in words[pair + 2:]]]
        if any(w.dynamic for w in words[:pair]):
            raise GateParseError("a word before `relay client` is built by the shell, so the command word is unknown")
        if base in _SHELLS:
            return _from_strings(rest, depth)
        return []
    return []


def _from_strings(words: Sequence[_Word], depth: int) -> list[list[str]]:
    """Invocations inside string arguments of a shell or eval (``bash -c '...'``)."""
    found: list[list[str]] = []
    for word in words:
        if _mentions(word.text):
            found.extend(_find(word.text, depth + 1))
    return found


def _find(command: str, depth: int) -> list[list[str]]:
    found: list[list[str]] = []
    for words in _Scanner(command, depth).scan():
        found.extend(_invocations(words, depth))
    return found


def find_client_args(command: str, depth: int = 0) -> list[list[str]]:
    """The argument list after ``relay client`` for each invocation in ``command``.

    An invocation is a simple command, in command position, whose command word is this tool
    (``llm-kit``, ``llm_kit``) or a wrapper, interpreter or shell that runs it, followed by the words ``relay
    client``. The same words inside a quoted string, a commit message, a heredoc body, a comment, an echo or grep
    argument or a redirection target are text, not an invocation. Command substitutions and ``bash -c`` strings
    are read as commands.

    Raises GateParseError when the command mentions ``relay client`` and cannot be read, or when a real
    invocation's command word is built by the shell: an ambiguous invocation is refused, never ignored.
    """
    if not _mentions(command):
        return []
    return _find(command, depth)


# -- the one form the gate allows ------------------------------------------------------------

def plain_words(command: str) -> Optional[list[str]]:
    """The command's words when it is plain (see the module docstring), else None.

    Each word is bare characters from ``[A-Za-z0-9_./:=@%+,-]``, single-quoted
    text, or ``\\<char>`` (a literal character), concatenated. A word may not
    start with ``=`` (zsh expands ``=name``). Words are separated by spaces or
    tabs. Anything else -- double quotes, ``$``, a backtick, a glob, a newline
    outside single quotes, an operator -- makes the command not plain.
    """
    words: list[str] = []
    i, n = 0, len(command)
    while i < n:
        if command[i] in " \t":
            i += 1
            continue
        parts: list[str] = []
        start = i
        while i < n and command[i] not in " \t":
            char = command[i]
            if char == "'":
                end = command.find("'", i + 1)
                if end < 0:
                    return None
                parts.append(command[i + 1:end])
                i = end + 1
            elif char == "\\":
                if i + 1 >= n or command[i + 1] == "\n":
                    return None
                parts.append(command[i + 1])
                i += 2
            elif _BARE_RE.fullmatch(char):
                if char == "=" and i == start:
                    return None
                parts.append(char)
                i += 1
            else:
                return None
        words.append("".join(parts))
    return words


def plain_client_args(command: str, launcher: Path) -> Optional[list[str]]:
    """The arguments after ``relay client`` when ``command`` is one plain call of ``launcher``, else None."""
    words = plain_words(command)
    if not words or len(words) < 3 or words[1:3] != ["relay", "client"]:
        return None
    if not os.path.isabs(words[0]) or os.path.realpath(words[0]) != os.path.realpath(str(launcher)):
        return None
    return words[3:]


def parse_client_args(args: Sequence[str], machine: Optional[str]) -> Optional[ClientCall]:
    """The call's verb, session, target and options; None when there is no verb to judge (help, usage error)."""
    verb: Optional[str] = None
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
    return ClientCall(verb, options.get("--session"), target, options)


# -- the decision --------------------------------------------------------------------------

def _deny(reason: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason,
    }}


def _allow(reason: str, *, shown: Optional[str] = None, context: Optional[str] = None) -> dict[str, Any]:
    specific: dict[str, Any] = {
        "hookEventName": "PreToolUse", "permissionDecision": "allow", "permissionDecisionReason": reason,
    }
    if context is not None:
        specific["additionalContext"] = context
    out: dict[str, Any] = {"hookSpecificOutput": specific}
    if shown is not None:
        out["systemMessage"] = shown
    return out


def shown_to_owner(call: ClientCall) -> Optional[str]:
    """The line the owner sees for a call that sends something: the exact target and text."""
    if call.client_verb == "send":
        kind = call.options.get("--kind", "?")
        return f"/direct send to {call.target} ({kind}):\n{call.options.get('--body', '')}"
    if call.client_verb == "answer":
        decision = call.options.get("--decision", "?")
        detail = call.options.get("--input") if decision == "answer" else call.options.get("--reason")
        return f"/direct answer {call.target}: {decision}" + (f"\n{detail}" if detail else "")
    return None


def _plain_form(launcher: Path) -> str:
    return f"`{launcher} relay client <verb> --session <this session id> ...`"


def run(
    payload: Mapping[str, Any],
    env: Mapping[str, str],
    *,
    hostname: Optional[Callable[[], str]] = None,
    env_json: Optional[Path] = None,
    launcher: Optional[Path] = None,
) -> Optional[dict[str, Any]]:
    """The hook's JSON output for one payload, or None for "no opinion"."""
    if payload.get("tool_name") != "Bash":
        return None
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str):
        raise R.HookError("a Bash tool call carries no command string")
    session = payload["session_id"]
    own = launcher if launcher is not None else R.launcher()
    try:
        found = find_client_args(command)
    except GateParseError as exc:
        return _deny(f"This command calls the llm-kit relay client, but the /direct gate cannot read it ({exc}). "
                     f"Use one plain command per call: {_plain_form(own)}.")
    if not found:
        return None
    args = plain_client_args(command, own)
    if args is None:
        return _deny("Refused: the /direct gate allows only one plain relay client call of this plugin's launcher "
                     f"per Bash call, {_plain_form(own)}, with every value single-quoted and nothing else "
                     "(no pipes, variables, substitutions, wrappers or other commands).")
    try:
        machine = R.resolve_machine_label(env_json, hostname=hostname)
    except R.HookError as exc:
        return _deny(f"The /direct gate cannot tell which machine this is ({exc}), so the relay client call is refused.")
    try:
        call = parse_client_args(args, machine)
    except GateParseError as exc:
        return _deny(f"The /direct gate cannot read this relay client call ({exc}).")
    if call is None:
        return None
    try:
        opted_in = R.relay_opt_in(env)
    except R.HookError as exc:
        return _deny(f"Refused: the /direct gate cannot read the relay settings ({exc}).")
    if not opted_in:
        return _deny(
            f"Refused: /direct is off on this machine because the llm-kit relay is not enabled, so "
            f"`llm-kit relay client {call.client_verb}` may not run. Only the owner turns it on: "
            f"{R.opt_in_hint(env)}. Do not change that file yourself and do not retry."
        )
    if call.session != session:
        return _deny(
            f"Refused: --session must be this session's id ({session}); got {call.session!r}. A session acts only "
            "for itself; the owner types /direct in the session that should act."
        )
    for name, why in _REFUSED_OPTIONS.items():
        if name in call.options:
            return _deny(f"Refused: {why}.")
    if call.client_verb == "send" and call.options.get("--body") == "-":
        return _deny("Refused: write the message text in --body (single-quoted) so the owner sees what is sent.")
    try:
        record = R.enablement(env, session)
    except R.HookError as exc:
        return _deny(f"Refused: the /direct gate cannot read this session's enablement ({exc}).")
    if record is None:
        return _deny(
            f"Refused: /direct is not enabled in this session, so `llm-kit relay client {call.client_verb}` may not "
            "run. Only the owner enables it, by typing /direct in this session. Do not retry until they have."
        )
    origin = f"direct:{machine}/claude/{session}"
    params = {
        "session": f"{machine}/claude/{session}", "verb": call.wire_verb,
        "targets": [call.target] if call.target is not None else [],
        "expires_in_s": GRANT_TTL_S, "uses": GRANT_USES, "origin_proof": {"type": "none"},
        "command": record["command"],
    }
    what = f"/direct {call.client_verb}" + (f" {call.target}" if call.target else "")
    shown = shown_to_owner(call)
    try:
        R.call(env, "grant_mint", params, origin=origin)
    except R.RelayUnavailable as exc:
        return _allow(f"{what} (relay not running)", shown=shown,
                      context=f"/direct gate: the relay is not running ({exc}); the client call will fail by itself.")
    except R.RelayRefused as exc:
        return _deny(f"Refused: the relay would not grant {what} ({exc}).")
    except R.HookError as exc:
        return _deny(f"Refused: the /direct gate cannot reach the relay safely ({exc}).")
    return _allow(f"{what}: this session is enabled for /direct", shown=shown)


def main(argv: Optional[Sequence[str]] = None, stdin: Any = None, stdout: Any = None, env: Optional[Mapping[str, str]] = None) -> int:
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
