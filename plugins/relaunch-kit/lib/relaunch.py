"""clauder: run claude, and run it again when the session asks to be restarted.

The launcher exports a per-launch token (RELAUNCH_KIT_TOKEN) and a rendezvous
directory (RELAUNCH_KIT_DIR) to claude, runs it, and after it exits looks for
<dir>/<token>.json. No file: exit with claude's status. A restart request: run
claude again in the same terminal. Token file schema v1 is the interface to the
plugin's hooks module (plugins/relaunch-kit/hooks/register.ts).

Stdlib only; written for Python 3.8 and later, because a launcher shim may fall
back to whatever python is on PATH.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable, Dict, Iterator, List, Mapping, Optional, Sequence

import claude_args

TOKEN_ENV = "RELAUNCH_KIT_TOKEN"
DIR_ENV = "RELAUNCH_KIT_DIR"
TARGET_ENV = "RELAUNCH_KIT_CLAUDE"
SHIM_ENV = "RELAUNCH_KIT_SHIM"  # set to "cmd" by clauder.cmd

SCHEMA = 1
ACTIONS = ("resume", "prompt", "cancelled")
REASONS = ("restart", "hand-off", "check")
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
WRITTEN_AT_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?Z$"
)
FIELDS = frozenset(
    ("schema", "token", "action", "session_id", "cwd", "prompt", "reason", "written_at", "writer")
)
PROMPT_MAX = 4096

MAX_ACTIONABLE_AGE_S = 120  # an actionable file older than this at claude's exit is not acted on
FAST_RUN_S = 20  # a run shorter than this that asks for a relaunch is "fast"
FAST_LIMIT = 4  # the Nth consecutive fast relaunch request stops the loop
SWEEP_S = 24 * 3600  # token files left by closed terminals
REJECTED_KEEP_S = 7 * 24 * 3600


class LaunchError(Exception):
    """The launcher cannot start claude as asked (exit status 2)."""


class UnsafeArgument(LaunchError):
    """An argument cannot be passed to a cmd.exe target without being altered."""


class RequestRejected(Exception):
    """A token file failed validation; the message says why."""


# --- locating claude ---------------------------------------------------------


def own_bin_dir() -> str:
    """The bin directory beside this lib directory (plugin dir or data dir copy)."""
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")


def _norm_dir(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


def find_on_path(
    name: str,
    path_value: str,
    *,
    windows: bool,
    pathext: str,
    skip_dirs: Sequence[str],
) -> Optional[str]:
    """First PATH match for `name`, skipping `skip_dirs`.

    POSIX: an executable regular file called `name`. Windows: in each directory,
    `name` plus each PATHEXT extension in PATHEXT order. An empty PATH entry is
    skipped (it would mean the current directory).
    """
    skip = set(_norm_dir(d) for d in skip_dirs)
    exts = [e if e.startswith(".") else "." + e for e in pathext.split(";") if e] if windows else [""]
    for directory in path_value.split(";" if windows else ":"):
        if not directory or _norm_dir(directory) in skip:
            continue
        for ext in exts:
            candidate = os.path.join(directory, name + ext)
            if not os.path.isfile(candidate):
                continue
            if not windows and not os.access(candidate, os.X_OK):
                continue
            return candidate
    return None


def resolve_target(env: Mapping[str, str], *, windows: bool) -> str:
    """The ONE claude every launch of this run uses."""
    pinned = env.get(TARGET_ENV)
    if pinned:
        if not os.path.isabs(pinned) or not os.path.isfile(pinned):
            raise LaunchError(
                "%s must be the absolute path of an existing file, got %r" % (TARGET_ENV, pinned)
            )
        return pinned
    found = find_on_path(
        "claude",
        env.get("PATH", ""),
        windows=windows,
        pathext=env.get("PATHEXT", ".COM;.EXE;.BAT;.CMD"),
        skip_dirs=[own_bin_dir()],
    )
    if found is None:
        raise LaunchError(
            "claude was not found on PATH (clauder's own directory is skipped). "
            "Install Claude Code, or set %s to its absolute path." % TARGET_ENV
        )
    return found


def classify_target(target: str, *, windows: bool) -> str:
    """'native' (run directly) or 'batch' (a cmd.exe script)."""
    if not windows:
        return "native"
    ext = os.path.splitext(target)[1].lower()
    if ext in (".exe", ".com"):
        return "native"
    if ext in (".cmd", ".bat"):
        return "batch"
    raise LaunchError(
        "cannot run %s: only .exe, .com, .cmd and .bat claude installs are supported on Windows" % target
    )


# --- building the child command line (Windows) -------------------------------

_BATCH_UNSAFE = {'"': 'a double quote', "%": "a percent sign", "\r": "a carriage return",
                 "\n": "a line feed", "\x00": "a NUL"}


def native_command_line(target: str, args: Sequence[str]) -> str:
    """The MSVCRT / CommandLineToArgvW command line a native Windows program parses."""
    return subprocess.list2cmdline([target] + list(args))


def quote_for_cmd(arg: str, *, what: str = "argument") -> str:
    """Wrap `arg` in double quotes for a `cmd /c` line, or refuse it.

    Inside double quotes cmd treats & | < > ( ) ^ literally, and /v:off makes ! literal.
    A double quote, a percent sign, CR, LF or NUL cannot be carried through cmd.exe
    unaltered, so they are refused here instead of being passed mangled. Backslashes
    that end the argument are doubled so the program's own parser does not read
    them as escaping the closing quote.
    """
    for char, label in _BATCH_UNSAFE.items():
        if char in arg:
            shown = arg if len(arg) <= 80 else arg[:77] + "..."
            raise UnsafeArgument(
                "%s %r cannot be passed through cmd.exe unaltered: it contains %s. "
                "Run claude directly, or use an exe install of Claude Code." % (what, shown, label)
            )
    return '"' + re.sub(r"(\\+)$", r"\1\1", arg) + '"'


def batch_command_line(comspec: str, target: str, args: Sequence[str]) -> str:
    """The one command-line STRING that runs a .cmd/.bat claude.

    It is a string, never a list: handing a list to subprocess would re-quote the
    inner quotes with backslashes, which cmd does not understand.
    """
    inner = " ".join(
        [quote_for_cmd(target, what="claude path")]
        + [quote_for_cmd(a, what="argument %d" % (i + 1)) for i, a in enumerate(args)]
    )
    return '%s /d /v:off /s /c "%s"' % (subprocess.list2cmdline([comspec]), inner)


# --- running the child -------------------------------------------------------


def _exit_status(returncode: int) -> int:
    return returncode if returncode >= 0 else 128 - returncode


def _noop(signum: int, frame: object) -> None:
    return None


@contextlib.contextmanager
def interrupts_deferred() -> Iterator[None]:
    """Ctrl+C and quit reach claude (same process group); the launcher must survive them.

    A no-op handler, never SIG_IGN: an ignored signal is inherited across exec.
    """
    names = ("SIGINT", "SIGBREAK") if os.name == "nt" else ("SIGINT", "SIGQUIT")
    saved = {}
    for name in names:
        number = getattr(signal, name, None)
        if number is not None:
            saved[number] = signal.signal(number, _noop)
    try:
        yield
    finally:
        for number, handler in saved.items():
            signal.signal(number, handler)


def spawn(
    target: str,
    kind: str,
    args: Sequence[str],
    *,
    cwd: Optional[str],
    env: Mapping[str, str],
    windows: bool,
) -> int:
    """Run claude once with inherited stdio and return its exit status."""
    if kind == "batch":
        comspec = env.get("COMSPEC")
        if not comspec:
            raise LaunchError("COMSPEC is not set, so a .cmd claude cannot be run")
        completed = subprocess.run(batch_command_line(comspec, target, args), cwd=cwd, env=dict(env))
    else:
        completed = subprocess.run([target] + list(args), cwd=cwd, env=dict(env))
    return _exit_status(completed.returncode)


# --- the cmd.exe shim boundary ----------------------------------------------


def cmd_shim_arguments(raw_command_line: str) -> List[str]:
    """Re-read the raw command line cmd.exe built from `"py" "launcher" %*`.

    clauder.cmd forwards its arguments with %*, and by the time this process
    starts, cmd has already parsed that text. What reaches us is provably what
    the caller wrote only when every argument is either bare (no quote
    character) or wholly wrapped in one pair of double quotes with no quote
    inside and no backslash before the closing quote. Anything
    else is refused here: cmd and the MSVCRT parser disagree about it.
    """
    text = raw_command_line
    pos = 0
    for _ in range(2):  # the interpreter and the launcher script, both quoted by the shim
        while pos < len(text) and text[pos] in " \t":
            pos += 1
        if pos >= len(text) or text[pos] != '"':
            raise LaunchError("clauder.cmd: unexpected command line, cannot verify its arguments")
        end = text.find('"', pos + 1)
        if end < 0:
            raise LaunchError("clauder.cmd: unexpected command line, cannot verify its arguments")
        pos = end + 1
    out: List[str] = []
    length = len(text)
    while True:
        while pos < length and text[pos] in " \t":
            pos += 1
        if pos >= length:
            return out
        if text[pos] == '"':
            end = text.find('"', pos + 1)
            if end < 0:
                raise UnsafeArgument(_cmd_refusal(text[pos:], "an unterminated double quote"))
            if end + 1 < length and text[end + 1] not in " \t":
                raise UnsafeArgument(_cmd_refusal(text[pos:], "a double quote inside an argument"))
            content = text[pos + 1:end]
            if content.endswith("\\"):
                raise UnsafeArgument(_cmd_refusal(text[pos:end + 1], "a backslash before the closing quote"))
            out.append(content)
            pos = end + 1
        else:
            end = pos
            while end < length and text[end] not in " \t":
                end += 1
            token = text[pos:end]
            if '"' in token:
                raise UnsafeArgument(_cmd_refusal(token, "a double quote inside an argument"))
            out.append(token)
            pos = end


def _cmd_refusal(shown: str, why: str) -> str:
    shown = shown if len(shown) <= 60 else shown[:57] + "..."
    return (
        "clauder.cmd cannot forward %r unaltered: %s. cmd.exe and the program's own parser "
        "would read it differently. Wrap each argument in plain double quotes without quotes "
        "inside, or run claude directly." % (shown, why)
    )


def _windows_raw_command_line() -> str:
    import ctypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    kernel32.GetCommandLineW.restype = ctypes.c_wchar_p
    return kernel32.GetCommandLineW()


# --- token files -------------------------------------------------------------


def rendezvous_dir(env: Mapping[str, str]) -> str:
    root = env.get("CLAUDE_BOOTSTRAP_DATA_ROOT")
    if not root:
        root = os.path.join(os.path.expanduser("~"), ".claude", "plugins", "data")
    return os.path.abspath(os.path.join(root, "plugins-kit", "relaunch-kit", "relaunch"))


@dataclass(frozen=True)
class Request:
    action: str
    session_id: str
    cwd: str
    prompt: Optional[str]
    reason: str
    written_at: float


def _parse_written_at(value: object) -> float:
    if not isinstance(value, str):
        raise RequestRejected("written_at must be a string")
    match = WRITTEN_AT_RE.match(value)
    if match is None:
        raise RequestRejected("written_at %r is not an ISO 8601 UTC time ending in Z" % (value,))
    year, month, day, hour, minute, second = (int(match.group(i)) for i in range(1, 7))
    micro = int((match.group(7) or "0").ljust(6, "0")[:6])
    try:
        moment = datetime.datetime(year, month, day, hour, minute, second, micro,
                                   tzinfo=datetime.timezone.utc)
    except ValueError as exc:
        raise RequestRejected("written_at %r is not a valid time: %s" % (value, exc))
    return moment.timestamp()


def _check_prompt(prompt: object, *, required: bool) -> Optional[str]:
    if prompt is None:
        if required:
            raise RequestRejected("action 'prompt' requires a prompt")
        return None
    if not isinstance(prompt, str):
        raise RequestRejected("prompt must be a string or null")
    if not prompt.strip():
        raise RequestRejected("prompt must not be empty")
    if len(prompt) > PROMPT_MAX:
        raise RequestRejected("prompt is longer than %d characters" % PROMPT_MAX)
    if "\x00" in prompt or "\n" in prompt or "\r" in prompt:
        raise RequestRejected("prompt must be one line without NUL")
    if prompt in claude_args.SUBCOMMANDS:
        raise RequestRejected("prompt %r is a claude subcommand name, so claude would run the subcommand" % prompt)
    return prompt


def load_request(path: str, token: str) -> Request:
    """Read and validate <token>.json (schema v1). Raises RequestRejected."""
    try:
        with open(path, "rb") as handle:
            data = json.loads(handle.read().decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise RequestRejected("cannot read the file as UTF-8 JSON: %s" % exc)
    if not isinstance(data, dict):
        raise RequestRejected("the file does not hold a JSON object")
    missing = sorted(FIELDS - set(data))
    extra = sorted(set(data) - FIELDS)
    if missing or extra:
        raise RequestRejected(
            "fields differ from schema 1 (missing: %s; unexpected: %s)"
            % (", ".join(missing) or "none", ", ".join(extra) or "none")
        )
    schema = data["schema"]
    if isinstance(schema, bool) or not isinstance(schema, int) or schema != SCHEMA:
        raise RequestRejected("schema is %r, this launcher reads schema %d" % (schema, SCHEMA))
    if data["token"] != token:
        raise RequestRejected("token %r is not this launch's token" % (data["token"],))
    action = data["action"]
    if action not in ACTIONS:
        raise RequestRejected("action %r is not one of %s" % (action, ", ".join(ACTIONS)))
    session_id = data["session_id"]
    if not isinstance(session_id, str) or not SESSION_ID_RE.match(session_id):
        raise RequestRejected("session_id %r does not match %s" % (session_id, SESSION_ID_RE.pattern))
    reason = data["reason"]
    if reason not in REASONS:
        raise RequestRejected("reason %r is not one of %s" % (reason, ", ".join(REASONS)))
    if not isinstance(data["writer"], str) or not data["writer"]:
        raise RequestRejected("writer must be a non-empty string")
    cwd = data["cwd"]
    if not isinstance(cwd, str) or not cwd:
        raise RequestRejected("cwd must be a non-empty string")
    prompt = _check_prompt(data["prompt"], required=(action == "prompt"))
    written_at = _parse_written_at(data["written_at"])
    if action != "cancelled":
        if not os.path.isabs(cwd) or not os.path.isdir(cwd):
            raise RequestRejected("cwd %r is not an existing absolute directory" % cwd)
        if action == "resume" and session_id.startswith("-"):
            raise RequestRejected("session_id %r starts with a dash and would be read as a flag" % session_id)
    return Request(action, session_id, cwd, prompt, reason, written_at)


def _reject(path: str, directory: str, token: str) -> str:
    rejected = os.path.join(directory, token + ".rejected.json")
    os.replace(path, rejected)
    return rejected


def sweep(directory: str, now: float) -> None:
    """Delete token files abandoned by closed terminals (24 h), rejected ones after 7 days."""
    for entry in os.listdir(directory):
        if not entry.endswith(".json"):
            continue
        limit = REJECTED_KEEP_S if entry.endswith(".rejected.json") else SWEEP_S
        path = os.path.join(directory, entry)
        try:
            if now - os.stat(path).st_mtime > limit:
                os.unlink(path)
        except FileNotFoundError:
            continue  # another launcher swept it first


def _say(message: str) -> None:
    sys.stderr.write(message + "\n")
    sys.stderr.flush()


def _printable(text: str, limit: int = 200) -> str:
    cleaned = "".join(c if c.isprintable() else "?" for c in text)
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 3] + "..."


# --- the loop ----------------------------------------------------------------


def _request_argv(parsed: claude_args.Parsed, request: Request) -> List[str]:
    if request.action == "resume":
        return claude_args.with_request(parsed.carried, resume_id=request.session_id, prompt=request.prompt)
    return claude_args.with_request(parsed.carried, resume_id=None, prompt=request.prompt)


def run_loop(
    target: str,
    kind: str,
    argv: List[str],
    parsed: claude_args.Parsed,
    env: Mapping[str, str],
    *,
    windows: bool,
) -> int:
    directory = rendezvous_dir(env)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    sweep(directory, time.time())
    base_env: Dict[str, str] = {k: v for k, v in env.items() if k not in (TOKEN_ENV, DIR_ENV, SHIM_ENV)}
    args = list(argv)
    cwd: Optional[str] = None
    fast = 0
    while True:
        token = secrets.token_hex(16)
        child_env = dict(base_env)
        child_env[TOKEN_ENV] = token
        child_env[DIR_ENV] = directory
        started = time.monotonic()
        with interrupts_deferred():
            status = spawn(target, kind, args, cwd=cwd, env=child_env, windows=windows)
        elapsed = time.monotonic() - started
        exited_at = time.time()

        path = os.path.join(directory, token + ".json")
        if not os.path.exists(path):
            return status
        try:
            request = load_request(path, token)
        except RequestRejected as exc:
            kept = _reject(path, directory, token)
            _say("[clauder] not relaunching: %s. The file is kept at %s" % (exc, kept))
            return status or 1
        if request.action == "cancelled":
            os.unlink(path)
            return status
        age = exited_at - request.written_at
        if age > MAX_ACTIONABLE_AGE_S:
            os.unlink(path)
            _say("[clauder] not relaunching: the restart request was written %d s before claude exited "
                 "(limit %d s)" % (age, MAX_ACTIONABLE_AGE_S))
            return status
        fast = fast + 1 if elapsed < FAST_RUN_S else 0
        if fast >= FAST_LIMIT:
            kept = _reject(path, directory, token)
            _say("[clauder] stopping: %d restart requests in a row came from sessions that ran under "
                 "%d s each. The last request is kept at %s" % (fast, FAST_RUN_S, kept))
            return status or 1
        next_args = _request_argv(parsed, request)
        if kind == "batch":
            try:
                batch_command_line(child_env.get("COMSPEC", "cmd.exe"), target, next_args)
            except UnsafeArgument as exc:
                kept = _reject(path, directory, token)
                _say("[clauder] not relaunching: %s The file is kept at %s" % (exc, kept))
                return 2
        os.unlink(path)
        if request.action == "prompt":
            what = request.prompt or ""
        elif request.prompt is not None:
            what = "resume %s, then: %s" % (request.session_id, request.prompt)
        else:
            what = "resume %s" % request.session_id
        _say("[clauder] relaunching (%s): %s" % (request.reason, _printable(what)))
        args = next_args
        cwd = request.cwd


def main(
    argv: Optional[List[str]] = None,
    *,
    env: Optional[Mapping[str, str]] = None,
    interactive: Optional[bool] = None,
    exec_fn: Optional[Callable[[str, List[str], Mapping[str, str]], object]] = None,
    windows: Optional[bool] = None,
    raw_command_line: Optional[Callable[[], str]] = None,
) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    env = dict(os.environ if env is None else env)
    windows = (os.name == "nt") if windows is None else windows
    try:
        return _main(argv, env, interactive, exec_fn, windows, raw_command_line)
    except LaunchError as exc:
        _say("clauder: %s" % exc)
        return 2
    except claude_args.ArgsError as exc:
        _say("clauder: %s" % exc)
        return 2


def _main(argv, env, interactive, exec_fn, windows, raw_command_line) -> int:
    if windows and env.get(SHIM_ENV) == "cmd":
        raw = (raw_command_line or _windows_raw_command_line)()
        seen = cmd_shim_arguments(raw)
        if seen != argv:
            raise LaunchError(
                "clauder.cmd: the arguments claude would receive differ from the command line cmd.exe "
                "built (%r vs %r); refusing to guess" % (argv, seen)
            )
    target = resolve_target(env, windows=windows)
    kind = classify_target(target, windows=windows)
    if interactive is None:
        interactive = sys.stdin.isatty() and sys.stdout.isatty()

    parsed: Optional[claude_args.Parsed] = None
    reason: Optional[str] = None
    if not interactive:
        reason = "stdin or stdout is not a terminal"
    else:
        parsed = claude_args.parse(argv)
        reason = parsed.pass_through
    if reason is not None:
        return _pass_through(target, kind, argv, env, exec_fn, windows)
    assert parsed is not None
    return run_loop(target, kind, argv, parsed, env, windows=windows)


def _pass_through(target, kind, argv, env, exec_fn, windows) -> int:
    """Run claude once, with no token, and leave with its status."""
    child_env = {k: v for k, v in env.items() if k not in (TOKEN_ENV, DIR_ENV, SHIM_ENV)}
    if windows:
        return spawn(target, kind, argv, cwd=None, env=child_env, windows=windows)
    (exec_fn or os.execve)(target, [target] + list(argv), child_env)
    return 0  # reached only when a test injects exec_fn


if __name__ == "__main__":
    sys.exit(main())
