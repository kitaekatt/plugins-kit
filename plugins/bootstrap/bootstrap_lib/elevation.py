"""Elevation queue + per-OS remediation-script generator.

Bootstrap runs as a NON-INTERACTIVE Claude Code SessionStart hook, so it must
never prompt for a sudo password or trigger a UAC dialog (elevation policy,
software-management-strategy section 5). When an operation needs privileges the
engine cannot obtain non-interactively, the strategy that hit it DEFERS instead
of attempting it -- recording a per-item ``needs_elevation`` failure that carries
a structured ``elevation`` descriptor.

This module turns those deferred ops into ONE user-runnable remediation script
per pass:

  * :func:`queue_from_failures` harvests the ``elevation`` descriptors from the
    pass's accumulated failures into an :class:`ElevationQueue`. Three descriptor
    shapes are consumed (analysis-dividing-line.md section 4.3):
      - ``{"method": "apt", "package": <pkg>, "os": "ubuntu"}`` -- a deferred apt
        package (_strategy_apt);
      - ``{"method": "command", "command": <cmd>, "os": <os>}`` -- a deferred
        ``elevated: true`` opaque command (_strategy_install_command);
      - ``{"method": "brew_installer", "os": "macos"}`` -- Homebrew was detected
        missing while brew-backed entries were pending (_strategy_brew); its
        official installer leads the macOS script (strategy section 6).
  * :func:`render_script` emits the OS-appropriate content: bash (``set -euo
    pipefail``) for Ubuntu/macOS with a header comment explaining WHY elevation
    is needed (per the decision record: "a script that tells the user why admin
    access is necessary"); a self-elevating ``.bat`` for Windows on the model of
    python_stub_check's ``fix_python_path.bat`` (UAC relaunch + fsutil admin
    detect), whose queued commands run through the engine-resolved absolute
    Git Bash (:func:`_windows_command_line`) since elevated cmd.exe neither
    tilde-expands nor has bash on PATH.
  * :func:`write_or_clear_script` regenerates the script each pass from the
    current queue and DELETES a stale script when the queue is empty, so a script
    never lingers after its ops succeed.
  * :func:`elevation_script_failure` builds the aggregated fix-all item that
    tells the user the script path and what it will do; the per-item
    ``needs_elevation`` failures keep persisting through the existing machinery.
  * :func:`launch_elevation_script` is the "fix-all is user consent for
    elevation" half: on an INTERACTIVE fix-all engine run (``--fix-all``,
    never SessionStart) the engine launches the Windows .bat itself via
    ``Start-Process -Verb RunAs -Wait`` and blocks (bounded) until the
    elevated process exits, so the UAC prompt is a direct consequence of the
    user's typed command. Launching with ``-Verb RunAs`` directly means the
    .bat's own UAC self-relaunch hop is a no-op (its fsutil admin-detect
    already passes), avoiding the wrapper-exits-early wait pitfall. Unix has
    no equivalent: the fix-all run still executes inside a non-interactive
    hook/Bash-tool subprocess with no TTY, so foreground ``sudo`` would fail
    (``sudo: a terminal is required``); the function returns None there and
    the manual instruction stands.

The queue is derived from the failures the pass already funnels into one list --
the descriptor was designed as the breadcrumb for exactly this step, and 4.3
mandates "Surfacing reuses the existing failure machinery". No mutable queue is
threaded through the strategy call graph.

Stdlib-only. Imports the privilege probes from :mod:`bootstrap_lib.apt`.
"""

import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import List, Optional

from .apt import sudo_noninteractive_available, windows_admin_available
from .atomic_write import write_atomic
from .tool_check import resolve_bash


# Official Homebrew installer (non-interactive-unfriendly: it prompts and may
# sudo, so the engine never runs it; it leads the macOS remediation script).
HOMEBREW_INSTALLER = (
    '/bin/bash -c "$(curl -fsSL '
    'https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
)


def privileges_available(current_os: str) -> bool:
    """True when this process can run elevated ops WITHOUT prompting, per OS.

    Windows uses the admin-token check; every other OS uses passwordless-sudo /
    root detection. Callers use this to decide direct-execution vs deferral for
    an ``elevated: true`` command -- the privileged path runs it directly
    (unchanged behavior), the unprivileged path queues it for the script.
    """
    if current_os == "windows":
        return windows_admin_available()
    return sudo_noninteractive_available()


@dataclass
class ElevationQueue:
    """Deferred elevated ops accumulated across one engine pass, for one OS.

    apt_packages / commands preserve manifest (pass) order. brew_installer is a
    macOS-only flag: True when Homebrew was missing while brew entries were
    pending, so the installer leads the script.
    """

    apt_packages: List[str] = field(default_factory=list)
    commands: List[str] = field(default_factory=list)
    brew_installer: bool = False

    def is_empty(self) -> bool:
        return not (self.apt_packages or self.commands or self.brew_installer)


def queue_from_failures(failures, current_os: str) -> ElevationQueue:
    """Harvest ``elevation`` descriptors from the pass's failures into a queue.

    Only descriptors whose ``os`` matches ``current_os`` are collected -- the
    script is per-OS and a pass runs on exactly one OS, so this is a precise
    grouping (not defensive). Order follows the failures list, which is appended
    in pass order.
    """
    queue = ElevationQueue()
    for f in failures:
        desc = f.get("elevation") if isinstance(f, dict) else None
        if not isinstance(desc, dict):
            continue
        if desc.get("os") != current_os:
            continue
        method = desc.get("method")
        if method == "apt":
            pkg = desc.get("package")
            if pkg:
                queue.apt_packages.append(pkg)
        elif method == "command":
            cmd = desc.get("command")
            if cmd:
                queue.commands.append(cmd)
        elif method == "brew_installer":
            queue.brew_installer = True
    return queue


def script_basename(current_os: str) -> str:
    """The remediation script's filename for this OS (.bat on Windows)."""
    return "install-elevated.bat" if current_os == "windows" else "install-elevated.sh"


def script_path(data_dir: str, current_os: str) -> str:
    """Stable, user-findable location: ``<data_dir>/elevate/install-elevated.*``."""
    return os.path.join(data_dir, "elevate", script_basename(current_os))


# --------------------------------------------------------------------------- #
# Script rendering (golden content is pinned by tests)
# --------------------------------------------------------------------------- #

_SH_RERUN = (
    "# When it finishes, start a new Claude Code session (or type 'fix-all')\n"
    "# and bootstrap will re-check and clear these items.\n"
)

# $HOME / ${HOME} references, word-bounded so $HOMEBREW_PREFIX etc. survive.
_HOME_VAR_RE = re.compile(r"\$\{HOME\}|\$HOME(?![A-Za-z0-9_])")
# ~/ at start-of-string or after whitespace (the shell's expansion positions
# we care about; no full shell parsing).
_TILDE_RE = re.compile(r"(?:(?<=\s)|^)~(?=/)")


def _expand_home_refs(command: str) -> str:
    """Expand user-home references in a queued command to the INVOKING user's
    real home, at render time.

    Why: the Ubuntu remediation script is run via ``sudo bash``, under which
    HOME=/root, so a verbatim ``~`` or ``$HOME`` in a queued fix (e.g.
    ``bash ~/.claude/scripts/env/sudoers.sh fix``) would resolve to root's
    home and abort the script. The engine knows the real home -- SessionStart
    runs as the user -- so it bakes it in when rendering.

    The rule (deliberately simple and predictable, no shell parsing): every
    ``~/`` at the start of the string or after whitespace, and every ``$HOME``
    or ``${HOME}`` not followed by an identifier character (so
    ``$HOMEBREW_PREFIX`` is untouched), is replaced with
    ``os.path.expanduser("~")``. Unix renders only: the Windows .bat instead
    runs each queued command through ``bash -c`` (see
    :func:`_windows_command_line`), where ``~``/``$HOME`` expand natively as
    the invoking user (UAC preserves the user profile). In-pass (non-queued)
    execution is unaffected -- it already runs as the user.
    """
    home = os.path.expanduser("~")
    expanded = _HOME_VAR_RE.sub(lambda _m: home, command)
    return _TILDE_RE.sub(lambda _m: home, expanded)


def _render_ubuntu(queue: ElevationQueue, path: str) -> str:
    lines = [
        "#!/usr/bin/env bash",
        "# Bootstrap elevated-install remediation (Ubuntu)",
        "#",
        "# Bootstrap runs as a non-interactive Claude Code SessionStart hook, so it",
        "# must never prompt for a sudo password or block on a dialog. The steps",
        "# below need root (apt installs system-wide), so bootstrap deferred them",
        "# into this script instead of attempting them. Review it, then run:",
        "#",
        f'#     sudo bash "{path}"',
        "#",
        "# Command paths (~, $HOME) were pre-expanded to the invoking user's home.",
        _SH_RERUN.rstrip("\n"),
        "set -euo pipefail",
        "",
    ]
    if queue.apt_packages:
        lines.append("# Refresh package lists (fresh machines can have stale/empty lists).")
        lines.append("apt-get update")
        lines.append("")
        lines.append("# apt packages deferred for elevation:")
        lines.append("apt-get install -y " + " ".join(queue.apt_packages))
        lines.append("")
    for cmd in queue.commands:
        cmd = _expand_home_refs(cmd)
        # Label as a plain comment, NOT an echo: embedding the command inside a
        # quoted echo would let an unbalanced quote break the parse (bypassing
        # set -euo pipefail) and $(...)/backticks execute during the label.
        lines.append(f"# bootstrap-elevate: {cmd}")
        lines.append(cmd)
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def _render_macos(queue: ElevationQueue, path: str) -> str:
    lines = [
        "#!/usr/bin/env bash",
        "# Bootstrap elevated-install remediation (macOS)",
        "#",
        "# Bootstrap runs as a non-interactive Claude Code SessionStart hook and",
        "# must never prompt for a password or block on a dialog. The steps below",
        "# need elevated/interactive setup, so bootstrap deferred them into this",
        "# script. Run it as your normal admin user (it prompts where needed):",
        "#",
        f'#     bash "{path}"',
        "#",
        "# Command paths (~, $HOME) were pre-expanded to the invoking user's home.",
        _SH_RERUN.rstrip("\n"),
        "set -euo pipefail",
        "",
    ]
    if queue.brew_installer:
        lines.append("# Homebrew is required but not installed; install it first")
        lines.append("# (its official installer is interactive).")
        lines.append(HOMEBREW_INSTALLER)
        lines.append("")
    for cmd in queue.commands:
        cmd = _expand_home_refs(cmd)
        # Comment label, not echo -- see _render_ubuntu (quoting/execution surface).
        lines.append(f"# bootstrap-elevate: {cmd}")
        lines.append(cmd)
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def _windows_command_line(cmd: str, bash: str) -> str:
    """Render one queued ``method:"command"`` entry as a .bat line.

    The .bat runs under elevated cmd.exe, where a verbatim queued command
    breaks twice: cmd.exe never tilde-expands (``~/...`` stays a literal
    argv), and ``bash`` is not resolvable on elevated cmd's PATH (Git for
    Windows exposes Git\\cmd only; the engine finds bash because SessionStart
    runs inside Git Bash). So the command is rendered through the
    engine-resolved ABSOLUTE bash -- ``"<bash.exe>" -c "<command>"`` --
    mirroring :func:`bootstrap_lib.env_features.run_env_command`'s in-pass
    semantics exactly: ``~``/``$HOME`` expand INSIDE ``bash -c``, and the
    launch is PATH-independent. ``bash`` comes from
    :func:`bootstrap_lib.tool_check.resolve_bash` at RENDER time, on the same
    machine that will run the .bat.

    Quoting rule (deliberately simple, no cmd.exe escaping): the command is
    embedded verbatim between ONE pair of double quotes, so it must not
    itself contain a double quote -- current consumers (env_check fixes,
    elevated install commands) do not, and the caller rejects one
    descriptively rather than emitting a broken .bat.
    """
    return f'"{bash}" -c "{cmd}"'


def _render_windows(queue: ElevationQueue) -> str:
    bash = None
    if queue.commands:
        bash = resolve_bash()
        if not bash:
            raise RuntimeError(
                "cannot render install-elevated.bat: bash not found on PATH at "
                "render time, and the queued elevated command(s) must run via "
                'Git for Windows bash ("<bash.exe>" -c ...) because elevated '
                "cmd.exe neither tilde-expands nor has bash on its PATH. "
                "Install Git for Windows (or run the session from Git Bash) "
                "and start a new session."
            )
        for cmd in queue.commands:
            if '"' in cmd:
                raise ValueError(
                    f"cannot render install-elevated.bat: queued command {cmd!r} "
                    'contains a double quote, which the "<bash.exe>" -c '
                    '"<command>" cmd.exe line cannot carry (no escaping rule; '
                    "see _windows_command_line). Rewrite the command with "
                    "single quotes."
                )
    body = [
        "@echo off",
        "REM ============================================================",
        "REM  install-elevated.bat",
        "REM  Generated by plugins-kit bootstrap.",
        "REM",
        "REM  Bootstrap runs as a non-interactive Claude Code SessionStart hook and",
        "REM  must never trigger a UAC prompt. The commands below need administrator",
        "REM  rights, so bootstrap deferred them into this self-elevating script.",
        "REM",
        'REM  Double-click it (it self-elevates via UAC) or right-click "Run as',
        'REM  administrator". After it finishes, start a new Claude Code session',
        "REM  (or type 'fix-all') so bootstrap re-checks and clears these items.",
        "REM ============================================================",
        "",
        "setlocal enableextensions",
        "",
        "REM When the bootstrap engine launches this script on a fix-all run it",
        "REM passes /engine as the first argument. The engine is waiting on this",
        "REM process, so the success path skips its pause (the window closes when",
        "REM done); failures always pause so errors stay readable.",
        'set "BOOTSTRAP_ENGINE_LAUNCH="',
        'if /I "%~1"=="/engine" set "BOOTSTRAP_ENGINE_LAUNCH=1"',
        "",
        "REM --- Admin detection (fsutil requires admin; redirect noise) ---",
        "fsutil dirty query %SystemDrive% >nul 2>&1",
        "if %errorlevel% neq 0 goto :not_admin",
        "goto :is_admin",
        "",
        ":not_admin",
        "echo.",
        "echo This script needs administrator privileges.",
        "echo Attempting to relaunch with elevation...",
        "echo.",
        "powershell -NoProfile -Command \"Start-Process -FilePath '%~f0' -Verb RunAs\" 1>nul 2>nul",
        "if %errorlevel% equ 0 exit /b",
        "echo.",
        "echo Could not relaunch automatically.",
        'echo Right-click "%~nx0" and choose "Run as administrator".',
        "echo.",
        "pause",
        "exit /b 1",
        "",
        ":is_admin",
        "echo Running with administrator privileges.",
        "echo.",
        "",
        "REM Commands deferred for elevation (each runs through Git Bash by",
        "REM absolute path, so ~ and $HOME resolve and PATH does not matter):",
    ]
    for cmd in queue.commands:
        body.append(f"echo bootstrap-elevate: {cmd}")
        body.append(_windows_command_line(cmd, bash))
        body.append("if %errorlevel% neq 0 goto :failed")
    body.extend([
        "",
        "echo.",
        "echo Done. Start a new Claude Code session or type 'fix-all'.",
        "echo This script will now delete itself.",
        "echo.",
        "if not defined BOOTSTRAP_ENGINE_LAUNCH pause",
        "endlocal",
        'REM Self-delete: (goto) releases the file lock, then del removes this script.',
        '(goto) 2>nul & del "%~f0"',
        "goto :eof",
        "",
        ":failed",
        "echo.",
        "echo ERROR: a command failed with exit code %errorlevel%.",
        "echo This script will NOT delete itself so you can retry.",
        "echo.",
        "pause",
        "endlocal",
        "exit /b 2",
    ])
    # Authored with plain \n; the writer translates to CRLF via newline="\r\n"
    # (mirrors python_stub_check's fix-script writer). Pre-joining with \r\n
    # would be double-translated to \r\r\n by text mode on Windows.
    return "\n".join(body) + "\n"


def render_script(queue: ElevationQueue, current_os: str, path: str) -> str:
    """Render the OS-appropriate remediation script content for ``queue``.

    Unix renders pre-expand ``~``/``$HOME`` in queued commands to the invoking
    user's real home (see :func:`_expand_home_refs`): the script runs under
    sudo where HOME is root's, so verbatim home references would resolve
    wrongly. The Windows render instead wraps each queued command in the
    engine-resolved absolute Git Bash -- ``"<bash.exe>" -c "<command>"`` (see
    :func:`_windows_command_line`) -- because elevated cmd.exe neither
    tilde-expands nor has bash on PATH; it raises descriptively when bash
    cannot be resolved or a command carries a double quote, rather than emit
    a broken .bat.
    """
    if current_os == "ubuntu":
        return _render_ubuntu(queue, path)
    if current_os == "macos":
        return _render_macos(queue, path)
    if current_os == "windows":
        return _render_windows(queue)
    # No other OS reaches here (detect_os yields ubuntu/macos/windows).
    raise ValueError(f"no elevation script for os {current_os!r}")


def write_or_clear_script(queue: ElevationQueue, data_dir: str,
                          current_os: str) -> Optional[str]:
    """Regenerate the remediation script, or remove a stale one when empty.

    Returns the script path when a script was written (queue non-empty), else
    None (and any stale script from a prior pass is deleted). This is what makes
    the script disappear once the deferred ops succeed and the queue empties.
    """
    path = script_path(data_dir, current_os)
    if queue.is_empty():
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError:
            pass
        return None
    content = render_script(queue, current_os, path)
    # Windows .bat files must be CRLF on every platform; the body is authored
    # with plain \n, so the writer performs the translation exactly once.
    write_atomic(path, content, newline="\r\n" if current_os == "windows" else None)
    return path


# --------------------------------------------------------------------------- #
# Interactive launch (fix-all only -- NEVER SessionStart)
# --------------------------------------------------------------------------- #

# Generous bounded wait for the elevated script: covers a slow install, but a
# user who walks away from the UAC prompt cannot hang the fix-all run forever.
ELEVATION_LAUNCH_TIMEOUT = 600


@dataclass
class LaunchResult:
    """Outcome of an engine-initiated elevation-script launch."""

    launched: bool
    succeeded: bool
    detail: str


def _powershell_exe() -> str:
    """Absolute Windows PowerShell path (hook PATH can be stripped of System32)."""
    sysroot = os.environ.get("SystemRoot") or os.environ.get("SYSTEMROOT") or r"C:\Windows"
    ps = os.path.join(sysroot, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    return ps if os.path.exists(ps) else "powershell.exe"


def launch_elevation_script(path: str, current_os: str,
                            timeout: int = ELEVATION_LAUNCH_TIMEOUT) -> Optional[LaunchResult]:
    """Launch the remediation script interactively and wait for it (Windows).

    ONLY called on a fix-all engine run -- the user's typed 'fix-all' is the
    consent that makes the UAC prompt acceptable. SessionStart never calls this.

    Windows: the engine launches the .bat with ``Start-Process -Verb RunAs
    -Wait -PassThru`` directly. Launching elevated up front means the .bat's
    own self-elevation relaunch never fires (its fsutil admin-detect passes
    immediately), so the ``-Wait`` covers the REAL elevated process -- not an
    unelevated wrapper that relaunches itself and exits early. ``/engine`` is
    passed so the .bat skips its success-path ``pause`` (the engine, not a
    human at the console, is the waiter); the failure path still pauses so the
    window stays visible long enough to read errors. A declined UAC prompt
    makes ``Start-Process -Verb RunAs`` throw (non-zero powershell exit); a
    user who walks away hits the bounded ``timeout``.

    Unix (ubuntu/macos): returns None -- no launch is attempted. The fix-all
    run executes inside a non-interactive hook/Bash-tool subprocess with no
    TTY, so a foreground ``sudo bash script`` would fail immediately
    (``sudo: a terminal is required``); the manual instruction remains the
    only honest path there.
    """
    if current_os != "windows":
        return None
    # PowerShell single-quoted literal: escape embedded quotes by doubling.
    ps_path = path.replace("'", "''")
    ps_cmd = (
        "$ErrorActionPreference = 'Stop'; "
        f"$p = Start-Process -FilePath '{ps_path}' -ArgumentList '/engine' "
        "-Verb RunAs -Wait -PassThru; "
        "exit $p.ExitCode"
    )
    try:
        proc = subprocess.run(
            [_powershell_exe(), "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return LaunchResult(
            launched=True, succeeded=False,
            detail=f"timed out after {timeout}s waiting for the elevated script",
        )
    except OSError as e:
        return LaunchResult(launched=False, succeeded=False,
                            detail=f"could not launch: {e}")
    if proc.returncode == 0:
        return LaunchResult(launched=True, succeeded=True, detail="exit code 0")
    stderr = (proc.stderr or "").strip()
    # A declined UAC prompt surfaces as a powershell error line
    # ("The operation was canceled by the user"); a failed command inside the
    # .bat surfaces as its exit code (2 = :failed path).
    detail = stderr.splitlines()[-1] if stderr else f"exit code {proc.returncode}"
    return LaunchResult(launched=True, succeeded=False, detail=detail)


def _run_instruction(current_os: str, path: str) -> str:
    if current_os == "ubuntu":
        return f'sudo bash "{path}"'
    if current_os == "macos":
        return f'bash "{path}"'
    return f'double-click "{path}" (it self-elevates via UAC)'


def elevation_script_failure(queue: ElevationQueue, current_os: str,
                             path: str,
                             launch_detail: Optional[str] = None) -> dict:
    """Build the aggregated fix-all item naming the script and what it will do.

    The per-item ``needs_elevation`` failures keep persisting on their own; this
    one item summarizes the single script the user runs to satisfy all of them
    (mirrors how python_stub renders a focused, manual-only remediation).

    ``launch_detail`` is set when a fix-all run LAUNCHED the script but it did
    not complete (UAC declined, a command failed, or the bounded wait timed
    out): the messages then lead with that outcome and fall back to the manual
    instruction -- the engine never re-prompts in a loop.
    """
    what = []
    if queue.brew_installer:
        what.append("install Homebrew")
    if queue.apt_packages:
        what.append("apt-get install " + " ".join(queue.apt_packages))
    if queue.commands:
        what.append(f"run {len(queue.commands)} elevated command(s)")
    what_str = "; ".join(what) if what else "run the deferred elevated operations"
    run = _run_instruction(current_os, path)
    user_msg = (
        f"Bootstrap deferred operations that need elevation. Run the remediation "
        f"script it wrote ({run}); it will {what_str}. Then type 'fix-all'."
    )
    agent_msg = (
        f"Bootstrap could not run {_count(queue)} operation(s) that require "
        f"elevation (a background SessionStart hook must not sudo or trigger UAC). "
        f"It wrote ONE remediation script to {path} that will {what_str}. Tell the "
        f"user to run it: {run}. After it succeeds, they should start a new Claude "
        f"Code session or type 'fix-all' so bootstrap re-checks and clears these "
        f"items. Do NOT attempt to run it yourself -- it needs the user's "
        f"credentials."
    )
    if launch_detail:
        prefix = (
            f"fix-all launched the elevation script but it did not complete "
            f"({launch_detail}). "
        )
        user_msg = prefix + user_msg
        agent_msg = prefix + agent_msg
    return {
        "type": "elevation_script",
        "name": "elevation_script",
        "message": user_msg,
        "user_msg": user_msg,
        "agent_msg": agent_msg,
        "script_path": path,
        "plugin": "bootstrap",
        "persist_across_sessions": True,
    }


def _count(queue: ElevationQueue) -> int:
    return len(queue.apt_packages) + len(queue.commands) + (1 if queue.brew_installer else 0)
