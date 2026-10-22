"""Argument facts for the Claude Code CLI, and a Commander-style lexer over them.

`claude` parses its command line with Commander. The launcher has to rebuild
that command line for a relaunch: keep every flag the user passed, drop the
ones that choose a session (the relaunch chooses the session itself), and drop
the prompt. Doing that without misreading a value as a flag, or a flag as a
value, takes the same lexing rules Commander uses, so they are written out here
and pinned to the live `claude --help` by tests/relaunch-kit/test_claude_args.py.

Lexing rules (Commander's `parseOptions`, as observed in claude 2.1.308):

* `--` ends option parsing. Everything after it is an operand.
* A token starting with `-` and longer than one character looks like an option
  (`maybe_option`). A lone `-` is an operand.
* An exact known name (`--model`, `-p`) is an option. By arity:
    none      takes nothing;
    required  `<v>`: takes the next token, whatever it looks like;
    optional  `[v]`: takes the next token only if it does not look like an option;
    variadic  `<v...>`: takes the next token, then every following token that
              does not look like an option.
* `-xVALUE` (a known short flag followed by more text): for a flag that takes a
  value (required, optional or variadic) the rest of the token is the value.
  For a flag that takes none, the rest is re-read as `-` plus the remainder
  (`-cr` is `-c`, then `-r`).
* `--name=value` is accepted only for a flag that takes a value. For a flag
  that takes none it is an unknown option.
* Any other token that looks like an option is an unknown option: claude would
  refuse it, and so does the launcher (`ArgsError`).
* The first operand decides whether this is a subcommand (`claude mcp ...`).
  Everything after a subcommand name belongs to that subcommand and is not
  lexed here.
"""

from __future__ import annotations

import collections
import re
from dataclasses import dataclass
from typing import Deque, List, Optional, Tuple

NONE = "none"
REQUIRED = "required"
OPTIONAL = "optional"
VARIADIC = "variadic"

# role: a session selector is removed on a relaunch; a mode flag means this
# invocation never returns to an interactive loop, so the launcher runs claude
# once and exits with its status.
SELECTOR = "selector"
MODE = "mode"


@dataclass(frozen=True)
class Flag:
    names: Tuple[str, ...]
    arity: str
    role: str = ""


# Every option in `claude --help` (2.1.308). tests/relaunch-kit/test_claude_args.py
# compares this table, alias by alias and arity by arity, with the live help.
FLAGS: Tuple[Flag, ...] = (
    Flag(("--add-dir",), VARIADIC),
    Flag(("--agent",), REQUIRED),
    Flag(("--agents",), REQUIRED),
    Flag(("--allow-dangerously-skip-permissions",), NONE),
    Flag(("--allowedTools", "--allowed-tools"), VARIADIC),
    Flag(("--append-system-prompt",), REQUIRED),
    Flag(("--autocompact",), REQUIRED),
    Flag(("--ax-screen-reader",), NONE),
    Flag(("--bg", "--background"), NONE, MODE),
    Flag(("--bare",), NONE),
    Flag(("--betas",), VARIADIC),
    Flag(("--brief",), NONE),
    Flag(("--chrome",), NONE),
    Flag(("--cloud",), OPTIONAL, MODE),
    Flag(("-c", "--continue"), NONE, SELECTOR),
    Flag(("--dangerously-skip-permissions",), NONE),
    Flag(("-d", "--debug"), OPTIONAL),
    Flag(("--debug-file",), REQUIRED),
    Flag(("--desktop",), NONE, MODE),
    Flag(("--disable-slash-commands",), NONE),
    Flag(("--disallowedTools", "--disallowed-tools"), VARIADIC),
    Flag(("--effort",), REQUIRED),
    Flag(("--environment",), REQUIRED, MODE),
    Flag(("--exclude-dynamic-system-prompt-sections",), NONE),
    Flag(("--fallback-model",), REQUIRED),
    Flag(("--file",), VARIADIC),
    Flag(("--fork-session",), NONE, SELECTOR),
    Flag(("--forward-subagent-text",), NONE),
    Flag(("--from-pr",), OPTIONAL, SELECTOR),
    Flag(("-h", "--help"), NONE, MODE),
    Flag(("--ide",), NONE),
    Flag(("--include-hook-events",), NONE),
    Flag(("--include-partial-messages",), NONE),
    Flag(("--input-format",), REQUIRED),
    Flag(("--json-schema",), REQUIRED),
    Flag(("--max-budget-usd",), REQUIRED),
    Flag(("--mcp-config",), VARIADIC),
    Flag(("--model",), REQUIRED),
    Flag(("-n", "--name"), REQUIRED),
    Flag(("--no-chrome",), NONE),
    Flag(("--no-session-persistence",), NONE),
    Flag(("--output-format",), REQUIRED),
    Flag(("--permission-mode",), REQUIRED),
    Flag(("--permission-prompts",), REQUIRED),
    Flag(("--plugin-dir",), REQUIRED),
    Flag(("--plugin-url",), REQUIRED),
    Flag(("-p", "--print"), NONE, MODE),
    Flag(("--prompt-suggestions",), OPTIONAL),
    Flag(("--remote-control",), OPTIONAL),
    Flag(("--remote-control-session-name-prefix",), REQUIRED),
    Flag(("--replay-user-messages",), NONE),
    Flag(("--restricted",), NONE),
    Flag(("-r", "--resume"), OPTIONAL, SELECTOR),
    Flag(("--safe-mode",), NONE),
    Flag(("--session-id",), REQUIRED, SELECTOR),
    Flag(("--setting-sources",), REQUIRED),
    Flag(("--settings",), REQUIRED),
    Flag(("--strict-mcp-config",), NONE),
    Flag(("--system-prompt",), REQUIRED),
    Flag(("--system-prompt-snapshot",), REQUIRED),
    Flag(("--teleport",), OPTIONAL, SELECTOR),
    Flag(("--tmux",), NONE),
    Flag(("--tools",), VARIADIC),
    Flag(("--verbose",), NONE),
    Flag(("-v", "--version"), NONE, MODE),
    Flag(("-w", "--worktree"), OPTIONAL),
)

# Subcommand names and aliases from the `Commands:` section of `claude --help`,
# plus Commander's implicit `help`.
SUBCOMMANDS = frozenset({
    "agents", "attach", "auth", "auto-mode", "doctor", "gateway", "help",
    "import", "install", "kill", "logs", "mcp", "plugin", "plugins", "purge",
    "respawn", "rm", "setup-token", "stop", "ultrareview", "update", "upgrade",
})

_BY_NAME = {name: flag for flag in FLAGS for name in flag.names}
_LONG_WITH_VALUE = re.compile(r"^--[^=]+=")


class ArgsError(Exception):
    """The command line cannot be carried across a relaunch (claude would refuse it too)."""


@dataclass(frozen=True)
class Parsed:
    # Why this invocation never loops (for example "-p" or "subcommand mcp"), else None.
    pass_through: Optional[str]
    # The user's tokens minus session selectors, operands and `--`, in their spelling.
    carried: Tuple[str, ...]
    # The positional arguments (the prompt), for the record.
    operands: Tuple[str, ...]


def maybe_option(token: str) -> bool:
    """Commander's `maybeOption`: longer than one character and starts with a dash."""
    return len(token) > 1 and token[0] == "-"


class _Item:
    __slots__ = ("flag", "tokens")

    def __init__(self, flag: Flag, tokens: List[str]) -> None:
        self.flag = flag
        self.tokens = tokens


def parse(argv: List[str]) -> Parsed:
    queue: Deque[str] = collections.deque(argv)
    items: List[_Item] = []
    operands: List[str] = []
    active: Optional[_Item] = None  # the variadic option still collecting values

    def pass_through(reason: str) -> Parsed:
        return Parsed(reason, (), tuple(operands))

    def note_operand(token: str) -> Optional[Parsed]:
        if not operands and token in SUBCOMMANDS:
            operands.append(token)
            return pass_through("subcommand " + token)
        operands.append(token)
        return None

    while queue:
        token = queue.popleft()
        if token == "--":
            rest = list(queue)
            queue.clear()
            for operand in rest:
                done = note_operand(operand)
                if done is not None:
                    return done
            break
        if active is not None and not maybe_option(token):
            active.tokens.append(token)
            continue
        active = None
        if maybe_option(token):
            flag = _BY_NAME.get(token)
            if flag is not None:
                tokens = [token]
                if flag.arity in (REQUIRED, VARIADIC):
                    if not queue:
                        raise ArgsError("option '%s' argument missing" % token)
                    tokens.append(queue.popleft())
                elif flag.arity == OPTIONAL:
                    if queue and not maybe_option(queue[0]):
                        tokens.append(queue.popleft())
                item = _Item(flag, tokens)
                items.append(item)
                if flag.role == MODE:
                    return pass_through(token)
                if flag.arity == VARIADIC:
                    active = item
                continue
        if len(token) > 2 and token[0] == "-" and token[1] != "-":
            flag = _BY_NAME.get("-" + token[1])
            if flag is not None:
                if flag.arity == NONE:
                    items.append(_Item(flag, ["-" + token[1]]))
                    if flag.role == MODE:
                        return pass_through("-" + token[1])
                    queue.appendleft("-" + token[2:])
                else:
                    # The rest of the token is this flag's value.
                    items.append(_Item(flag, [token]))
                continue
        if _LONG_WITH_VALUE.match(token):
            name = token.split("=", 1)[0]
            flag = _BY_NAME.get(name)
            if flag is not None and flag.arity != NONE:
                items.append(_Item(flag, [token]))
                if flag.role == MODE:
                    return pass_through(name)
                continue
        if maybe_option(token):
            name = token.split("=", 1)[0] if _LONG_WITH_VALUE.match(token) else token
            raise ArgsError(
                "unknown option '%s'. Claude Code does not accept it, or relaunch-kit's flag "
                "table (lib/claude_args.py) is older than this claude. Run claude directly, or "
                "update relaunch-kit." % name
            )
        done = note_operand(token)
        if done is not None:
            return done

    carried = tuple(tok for item in items if item.flag.role != SELECTOR for tok in item.tokens)
    return Parsed(None, carried, tuple(operands))


def with_request(carried: Tuple[str, ...], *, resume_id: Optional[str], prompt: Optional[str]) -> List[str]:
    """The relaunch command line: the carried flags, then the request.

    `--resume` and the `--` that precedes a prompt are both option-like, so they
    end any variadic option at the end of `carried` instead of being swallowed by
    it. The `--` also lets a prompt start with a dash.
    """
    out = list(carried)
    if resume_id is not None:
        out += ["--resume", resume_id]
    if prompt is not None:
        out += ["--", prompt]
    return out
