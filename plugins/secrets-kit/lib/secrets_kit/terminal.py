"""Launch an interactive verb in a real terminal window.

``age`` prompts for the passphrase on the TERMINAL, not on stdin -- that is
what keeps a master passphrase out of every transcript, pipe and log, and it is
deliberate. The cost is that the three interactive verbs cannot run in an
agent's shell at all: there is no tty, so they hang or fail outright.

The old answer was to relay "type this yourself", which spends the user's
attention on clerical work and, when the relayed command is wrong, spends their
one interactive step on nothing. The better answer is to spawn a terminal
WINDOW: the passphrase is still typed into a tty the transcript never sees, so
the security property is untouched, but the agent does the driving.

Everything here is stdlib-only and best-effort by design -- a machine with no
recognizable terminal emulator gets a clear failure naming the manual command,
never a silent no-op.
"""

import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Optional

from . import SecretsError

# Tried in order. x-terminal-emulator first: on Debian/Ubuntu it is the
# user's OWN choice via update-alternatives, which beats us guessing.
_LINUX_TERMINALS = (
    ("x-terminal-emulator", ["-e"]),
    ("gnome-terminal", ["--"]),
    ("konsole", ["-e"]),
    ("xfce4-terminal", ["-e"]),
    ("alacritty", ["-e"]),
    ("kitty", ["-e"]),
    ("xterm", ["-e"]),
)

_MANUAL_HINT = (
    "No terminal emulator could be launched. Run this yourself in a terminal:"
)

# `do script` returns once Terminal has STARTED the command, not when the
# command finishes, so this bounds the ask-Terminal-to-open step only.
_LAUNCH_TIMEOUT = 30


def _MANUAL_HINT_FOR(argv: List[str]) -> str:
    """The manual fallback, in the shape ``launch()`` already uses for it."""
    return f"{_MANUAL_HINT}\n\n    " + " ".join(shlex.quote(a) for a in argv)


def _last_line(text: str) -> str:
    """The most informative line of a tool's stderr, for a one-line message."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[-1] if lines else "no error output"


def _hold_open_posix(argv: List[str]) -> str:
    """The command line to run, then wait so the window does not vanish.

    A window that closes the instant the verb finishes takes its output with
    it -- including the error the user needs when something went wrong.

    The wait is `printf` + a bare `read`, NOT `read -p`. `-p` is a bash
    extension; in zsh it means "read from a coprocess", so the line dies with
    `read: -p: no coprocess` and the window drops straight back to a prompt.
    zsh is the macOS login-shell default and Terminal's `do script` runs the
    user's login shell, so on a stock Mac the hold-open never worked -- the
    error the window exists to display scrolled past as ordinary shell noise,
    and a user who had never seen the prompt reasonably concluded no window
    had opened. printf + bare `read` is POSIX and behaves the same in sh,
    bash and zsh.
    """
    inner = " ".join(shlex.quote(a) for a in argv)
    hold = 'printf %s "Press Enter to close this window..."; read -r _'
    return f"{inner}; echo; {hold}"


def _launch_windows(argv: List[str], title: str) -> str:
    """Spawn via a throwaway .cmd, rather than fighting ``start``'s quoting.

    ``start`` re-parses its arguments with rules that differ from every other
    Windows command, and the paths here reliably contain spaces. A generated
    script sidesteps the whole problem and is also what makes the window's
    hold-open behavior readable.
    """
    # list2cmdline, not naive "%s" wrapping: it is the exact inverse of the
    # Windows argument parser, so an argument containing quotes or trailing
    # backslashes survives instead of silently truncating the command.
    quoted = subprocess.list2cmdline(argv)
    script = (
        "@echo off\r\n"
        f"title {title}\r\n"
        f"{quoted}\r\n"
        "echo.\r\n"
        "pause\r\n"
        # Last line, so the launcher does not accumulate a file per invocation.
        # cmd tolerates a script deleting itself once it is the final command.
        'del "%~f0"\r\n'
    )
    fd, path = tempfile.mkstemp(prefix="secrets-kit-", suffix=".cmd", text=True)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(script)
    # `start "" <script>` opens a new console window using the user's default
    # console host -- Windows Terminal on a modern machine, without us having
    # to detect or hard-code it.
    subprocess.Popen(
        ["cmd.exe", "/c", "start", "", path],
        close_fds=True,
    )
    return "a new console window"


def _launch_macos(argv: List[str], title: str) -> str:
    command = _hold_open_posix(argv)
    # Nested quoting: the shell string becomes an AppleScript string literal,
    # so its embedded quotes and backslashes have to survive one more level.
    escaped = command.replace("\\", "\\\\").replace('"', '\\"')
    script = f'tell application "Terminal" to do script "{escaped}"'
    # WAIT on osascript rather than firing and forgetting. `do script` returns
    # as soon as Terminal has started the command, so this does not block on
    # the verb itself -- but it does surface the failures a bare Popen swallowed
    # while this function still returned "a new Terminal window": Automation
    # (TCC) permission denied, Terminal scriptable but not installed, an
    # AppleScript syntax error from the nested escaping. Reporting success for
    # a window that never opened sends the user hunting for a prompt that does
    # not exist.
    try:
        proc = subprocess.run(
            ["osascript", "-e", script, "-e", 'tell application "Terminal" to activate'],
            capture_output=True,
            text=True,
            timeout=_LAUNCH_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise SecretsError(
            "timed out asking Terminal to open a window",
            _MANUAL_HINT_FOR(argv),
        )
    if proc.returncode != 0:
        raise SecretsError(
            f"could not open a Terminal window: {_last_line(proc.stderr)}",
            _MANUAL_HINT_FOR(argv),
        )
    return "a new Terminal window"


def _launch_linux(argv: List[str], title: str) -> Optional[str]:
    from shutil import which

    command = _hold_open_posix(argv)
    for name, flag in _LINUX_TERMINALS:
        if not which(name):
            continue
        subprocess.Popen(
            [name, *flag, "bash", "-lc", command],
            start_new_session=True,
            close_fds=True,
        )
        return f"a new {name} window"
    return None


def _current_platform() -> str:
    """Indirection so a test can select a platform without touching sys.

    Monkeypatching ``sys.platform`` is global state that other fixtures read
    too -- here it made the repo's PATH-mutation guard misfire at teardown.
    """
    return sys.platform


def launch(argv: List[str], *, title: str = "secrets-kit") -> str:
    """Open a terminal window running ``argv``. Returns what was opened.

    Raises :class:`SecretsError` naming the manual fallback when no terminal
    can be opened -- a machine where this fails is not a machine where the
    verb is impossible, only one where the user has to type it.
    """
    manual = " ".join(shlex.quote(a) for a in argv)
    platform = _current_platform()
    try:
        if platform == "win32":
            return _launch_windows(argv, title)
        if platform == "darwin":
            return _launch_macos(argv, title)
        opened = _launch_linux(argv, title)
        if opened:
            return opened
    except OSError as e:
        raise SecretsError(
            f"could not open a terminal window: {e}",
            f"{_MANUAL_HINT}\n\n    {manual}",
        )
    raise SecretsError(
        "no supported terminal emulator found",
        f"{_MANUAL_HINT}\n\n    {manual}",
    )


def relaunch_self(verb: str, extra: Optional[List[str]] = None) -> str:
    """Re-run this CLI's ``verb`` in a fresh terminal window.

    Rebuilt from ``sys.executable`` and the CLI's own path rather than the
    ``secrets-kit`` shim: the shim is not on PATH, and re-deriving its location
    here would be a second copy of a rule that already exists in one place.
    """
    cli = Path(__file__).resolve().parents[1] / ".." / "scripts" / "secrets_kit_cli.py"
    argv = [sys.executable, str(cli.resolve()), verb, *(extra or [])]
    return launch(argv, title=f"secrets-kit {verb}")
