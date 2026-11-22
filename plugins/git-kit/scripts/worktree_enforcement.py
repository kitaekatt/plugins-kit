"""PreToolUse creation gate and proof for lifecycle-owned Git builds."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

import worktrees as wt

if TYPE_CHECKING:
    from worktree_lifecycle import Lifecycle

_BUILD_OWNERS: dict[Path, tuple[Lifecycle, dict[str, Any]]] = {}


def enabled(repo: Path) -> bool:
    """Only a tracked repository manifest opts into creation enforcement."""
    try:
        main = wt.project_root(repo)
        tracked = wt.git(main, "ls-files", "--", ".claude/bootstrap.json")
    except subprocess.CalledProcessError as exc:
        if exc.returncode == 128 and ("not a git repository" in exc.stderr or "cannot change to" in exc.stderr):
            return False
        raise
    manifest = main / ".claude/bootstrap.json"
    if not tracked or not manifest.exists():
        return False
    data = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{manifest}: expected a JSON object")
    section = data.get("git_kit", {})
    if not isinstance(section, dict):
        raise ValueError(f"{manifest}: git_kit must be an object")
    config = section.get("worktrees", {})
    if config is None:
        return False
    if not isinstance(config, dict) or not isinstance(config.get("enabled", False), bool):
        raise ValueError(f"{manifest}: git_kit.worktrees.enabled must be a boolean")
    return config.get("enabled", False)


def _path(value: str, cwd: Path, environment: dict[str, str]) -> Path:
    value = re.sub(r"\$(?:\{([A-Za-z_][A-Za-z_0-9]*)\}|([A-Za-z_][A-Za-z_0-9]*))",
                   lambda m: environment.get(m[1] or m[2], m[0]), value)
    if value.startswith("~/"):
        value = str(Path(environment.get("HOME", str(Path.home()))) / value[2:])
    path = Path(value)
    return (path if path.is_absolute() else cwd / path).resolve()


def _name(value: str) -> str:
    return Path(value).name.removesuffix(".exe")


def _command(tokens: list[str], cwd: Path, environment: dict[str, str], stdin: str | None = None) -> tuple[list[Path], Path, str | None]:
    """Unwrap common shell launchers without executing any command text."""
    tokens = tokens.copy()
    local = environment.copy()
    while tokens and re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*=.*", tokens[0], re.S):
        key, value = tokens.pop(0).split("=", 1)
        local[key] = value
    if not tokens:
        environment.update(local)
        return [], cwd, None
    while tokens and tokens[0] in {"then", "do", "if", "elif", "!"}:
        tokens.pop(0)
    if not tokens:
        return [], cwd, None
    name = _name(tokens[0])
    if name in {"cd", "pushd"} and len(tokens) > 1:
        index = 1
        while index < len(tokens) and tokens[index] in {"-L", "-P", "--"}:
            index += 1
        return [], _path(tokens[index], cwd, local) if index < len(tokens) else cwd, None
    if name in {"bash", "sh", "zsh", "dash", "ksh"}:
        index = 1
        noexec = False
        from_stdin = False
        while index < len(tokens):
            token = tokens[index]
            if token == "-":
                from_stdin = True
                break
            if token == "--":
                index += 1
                break
            if token in {"-o", "+o", "-O", "+O", "--rcfile", "--init-file"}:
                if token in {"-o", "+o"} and index + 1 < len(tokens) and tokens[index + 1] == "noexec":
                    noexec = token == "-o"
                index += 2
                continue
            if token == "--noexec":
                noexec = True
            elif token.startswith(("-", "+")) and not token.startswith("--"):
                if "n" in token[1:]:
                    noexec = token.startswith("-")
                from_stdin = from_stdin or (token.startswith("-") and "s" in token[1:])
                if token.startswith("-") and "c" in token[1:]:
                    script = tokens[index + 1] if index + 1 < len(tokens) else ""
                    return ([] if noexec else creation_repositories(script, cwd, local)), cwd, None
            elif not token.startswith("-"):
                break
            index += 1
        executes_stdin = from_stdin or index == len(tokens)
        return (creation_repositories(stdin, cwd, local) if stdin is not None and executes_stdin and not noexec else []), cwd, None
    if name == "eval":
        return creation_repositories(" ".join(tokens[1:]), cwd, local), cwd, None
    if name in {"env", "command", "builtin", "exec", "sudo", "time", "timeout", "gtimeout", "nice", "nohup", "setsid", "stdbuf", "xargs"}:
        if name == "command" and any(token in {"-v", "-V"} for token in tokens[1:] if token.startswith("-")):
            return [], cwd, None
        index = 1
        argument_options = {"-u", "--unset", "-g", "--group", "-n", "--adjustment", "-k", "--kill-after", "-s", "--signal", "-a", "-f", "-o", "-I", "-P"}
        while index < len(tokens):
            token = tokens[index]
            if token == "--":
                index += 1
                break
            if name == "env" and token in {"-C", "--chdir"} and index + 1 < len(tokens):
                cwd = _path(tokens[index + 1], cwd, local)
                index += 2
            elif name == "env" and token.startswith("--chdir="):
                cwd = _path(token.split("=", 1)[1], cwd, local)
                index += 1
            elif name == "env" and token in {"-S", "--split-string"} and index + 1 < len(tokens):
                return creation_repositories(tokens[index + 1], cwd, local), cwd, None
            elif re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*=.*", token, re.S):
                key, value = token.split("=", 1)
                local[key] = value
                index += 1
            elif token in argument_options:
                index += 2
            elif token.startswith("-"):
                index += 1
            elif name in {"timeout", "gtimeout"} and re.fullmatch(r"[0-9.]+[smhd]?", token):
                index += 1
            else:
                break
        found, _, output = _command(tokens[index:], cwd, local, stdin)
        return found, cwd, output
    if name == "cat" and all(token in {"-", "-u", "--"} for token in tokens[1:]):
        return [], cwd, stdin
    if name != "git":
        return [], cwd, None
    index = 1
    target = cwd
    git_dir: Path | None = None
    while index < len(tokens):
        token = tokens[index]
        if token == "-C" and index + 1 < len(tokens):
            target = _path(tokens[index + 1], target, local)
            index += 2
        elif token.startswith("-C") and len(token) > 2:
            target = _path(token[2:], target, local)
            index += 1
        elif token == "--work-tree" and index + 1 < len(tokens):
            target = _path(tokens[index + 1], target, local)
            index += 2
        elif token.startswith("--work-tree="):
            target = _path(token.split("=", 1)[1], target, local)
            index += 1
        elif token == "--git-dir" and index + 1 < len(tokens):
            git_dir = _path(tokens[index + 1], target, local)
            index += 2
        elif token.startswith("--git-dir="):
            git_dir = _path(token.split("=", 1)[1], target, local)
            index += 1
        elif token in {"-c", "--config-env", "--namespace", "--exec-path"}:
            index += 2
        elif token.startswith("-"):
            index += 1
        else:
            break
    if tokens[index:index + 2] != ["worktree", "add"]:
        return [], cwd, None
    if git_dir is not None:
        registration = wt.git(target, "--git-dir", str(git_dir), "worktree", "list", "--porcelain", "-z")
        first = registration.split("\0", 1)[0]
        if not first.startswith("worktree "):
            raise ValueError(f"Cannot resolve repository from Git directory: {git_dir}")
        target = Path(first.removeprefix("worktree "))
    return [target], cwd, None


def _heredocs(command: str) -> tuple[str, dict[str, tuple[str, bool]]]:
    """Keep heredoc bodies out of shell tokenization; the recipient owns interpretation."""
    lines = command.splitlines(keepends=True)
    result: list[str] = []
    bodies: dict[str, tuple[str, bool]] = {}
    line_index = 0
    quote: str | None = None
    arithmetic_depth = 0
    pattern = re.compile(r"""<<(-?)[ \t]*((?:'[^'\r\n]*'|"[^"\r\n]*"|\\[^\r\n]|[^\s;&|()<>'"\\])+)""")
    while line_index < len(lines):
        line = lines[line_index]
        line_index += 1
        header: list[str] = []
        pending: list[tuple[str, str, bool, bool]] = []
        index = 0
        copied = 0
        while index < len(line):
            char = line[index]
            if arithmetic_depth:
                if line.startswith("((", index):
                    arithmetic_depth += 1
                    index += 2
                elif line.startswith("))", index):
                    arithmetic_depth -= 1
                    index += 2
                else:
                    index += 1
                continue
            if char == "\\" and quote != "'":
                index += 2
                continue
            if char in {"'", '"'}:
                if quote is None:
                    quote = char
                elif quote == char:
                    quote = None
            if quote is None and char == "#" and (index == 0 or line[index - 1] in " \t\r\n;&|()<>"):
                break
            if quote is None and line.startswith("<<<", index):
                index += 3
                continue
            if quote is None and (line.startswith("$((", index) or line.startswith("((", index)):
                arithmetic_depth = 1
                index += 3 if line.startswith("$((", index) else 2
                continue
            match = pattern.match(line, index) if quote is None else None
            if match:
                marker = f"\0git-kit-heredoc-{len(bodies) + len(pending)}\0"
                raw = match[2]
                delimiter = shlex.split(raw)[0]
                pending.append((marker, delimiter, bool(match[1]), raw == delimiter))
                header.extend([line[copied:index], f" {marker} "])
                index = copied = match.end()
            else:
                index += 1
        header.append(line[copied:])
        result.append("".join(header))
        for marker, delimiter, tabs, expand in pending:
            body: list[str] = []
            while line_index < len(lines):
                current = lines[line_index]
                line_index += 1
                current = current.lstrip("\t") if tabs else current
                if current.rstrip("\r\n") == delimiter:
                    break
                body.append(current)
            else:
                raise ValueError(f"Unterminated shell heredoc: {delimiter}")
            bodies[marker] = ("".join(body), expand)
    return "".join(result), bodies


def creation_repositories(command: str, cwd: Path, environment: dict[str, str] | None = None) -> list[Path]:
    """Find raw creation targets through compound commands and nested shells."""
    command, heredocs = _heredocs(command)
    # Shell line continuations disappear before tokenization, except in single
    # quotes where a nested shell/eval will interpret them in its own pass.
    normalized: list[str] = []
    index = 0
    quote: str | None = None
    while index < len(command):
        char = command[index]
        if char == "\\" and quote != "'" and index + 1 < len(command):
            if command[index + 1] != "\n":
                normalized.extend(command[index:index + 2])
            index += 2
            continue
        if char == "#" and quote is None and (index == 0 or command[index - 1] in " \t\r\n;&|()<>"):
            while index < len(command) and command[index] != "\n":
                index += 1
            continue
        if char in {"'", '"'}:
            if quote is None:
                quote = char
            elif quote == char:
                quote = None
        normalized.append(char)
        index += 1
    lexer = shlex.shlex("".join(normalized), posix=True, punctuation_chars=";&|()<>\n")
    lexer.whitespace_split = True
    lexer.whitespace = " \t\r"
    lexer.commenters = ""
    repositories: list[Path] = []
    tokens: list[str] = []
    stack: list[Path] = []
    local = dict(os.environ if environment is None else environment)
    stdin: str | None = None
    for token in [*lexer, ";"]:
        if token in heredocs:
            stdin, expand = heredocs[token]
            if expand:
                # Unquoted heredocs expand command substitutions even when
                # their recipient treats the remaining body as literal data.
                for match in re.finditer(r"(?<!\\)(?:\$\((.*?)\)|`([^`]*)`)", stdin, re.S):
                    repositories.extend(creation_repositories(match[1] or match[2] or "", cwd, local))
        elif token and all(char in ";&|()<>\n" for char in token):
            found, cwd, output = _command(tokens, cwd, local, stdin)
            repositories.extend(found)
            tokens = []
            stdin = output if token.strip("\n") in {"|", "|&"} else None
            for char in token:
                if char == "(":
                    stack.append(cwd)
                elif char == ")" and stack:
                    cwd = stack.pop()
        else:
            tokens.append(token)
    return repositories


def decision(payload: dict[str, Any]) -> dict[str, Any]:
    tool = payload.get("tool_name")
    arguments = payload.get("tool_input", {})
    if not isinstance(arguments, dict):
        raise ValueError("PreToolUse tool_input must be an object")
    cwd = Path(payload.get("cwd") or Path.cwd())
    if tool == "Agent" and arguments.get("isolation") == "worktree":
        candidates = [cwd]
    elif tool == "Bash" and isinstance(arguments.get("command"), str):
        try:
            candidates = creation_repositories(arguments["command"], cwd)
        except (ValueError, subprocess.SubprocessError):
            # Parsing never opts a disabled or non-repository directory in.
            if not enabled(cwd):
                return {}
            raise
    else:
        return {}
    for repo in candidates:
        if enabled(repo):
            message = ("This repository enables git-kit's worktree lifecycle in tracked .claude/bootstrap.json. "
                       "Use worktrees.py take --agent <agent> --session <session> instead of raw creation; "
                       "use worktrees.py configure --propose to review settings. "
                       "Only lifecycle maintenance creates managed spares.")
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                           "permissionDecisionReason": message}}
    return {}


@contextmanager
def maintenance_lock(lifecycle: Lifecycle) -> Iterator[bool]:
    """Bind build authority to this process's actual maintenance lock context."""
    from worktree_pool import _file_lock
    from worktree_safety import process_identity

    with _file_lock(lifecycle.state / "maintenance.lock", blocking=False) as held:
        if not held:
            yield False
            return
        key = lifecycle.state.resolve()
        _BUILD_OWNERS[key] = (lifecycle, process_identity(os.getpid()))
        try:
            yield True
        finally:
            del _BUILD_OWNERS[key]


def require_managed_build(lifecycle: Lifecycle, record: dict[str, Any]) -> None:
    """Require the running OS lock owner and its exact persisted build reservation."""
    from worktree_config import read_object
    from worktree_pool import _file_lock
    from worktree_safety import process_identity

    identity = process_identity(os.getpid())
    owner = _BUILD_OWNERS.get(lifecycle.state.resolve())
    if owner is None or owner[0] is not lifecycle or owner[1] != identity:
        raise ValueError("Managed build requires ownership of the active maintenance lock context")
    worker = read_object(lifecycle.state / "maintenance.json")
    persisted = lifecycle.get(record["id"])
    if (worker.get("outcome") != "running" or any(worker.get(key) != value for key, value in identity.items())
            or record.get("worker") != identity or persisted != record
            or record.get("state") != "building" or record.get("holder")
            or record.get("operation", {}).get("name") != "build"
            or record["operation"].get("phase") != "git worktree add"):
        raise ValueError("Managed build requires matching maintenance worker and persisted build ownership")
    expected = lifecycle.namespace / ".staging" / record["id"]
    if Path(record["path"]) != expected:
        raise ValueError("Managed build destination disagrees with reserved ownership")
    lifecycle.validate_path(expected)
    with _file_lock(lifecycle.state / "maintenance.lock", blocking=False) as available:
        if available:
            raise ValueError("Managed build requires the held maintenance lock; worker metadata alone grants no ownership")


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("PreToolUse input must be a JSON object")
        result = decision(payload)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        detail = exc.stderr if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        result = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                  "permissionDecisionReason": f"git-kit lifecycle enforcement failed: {detail}; inspect tracked .claude/bootstrap.json and run worktrees.py configure --propose"}}
    if result:
        print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
