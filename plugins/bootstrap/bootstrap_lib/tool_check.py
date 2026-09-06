"""Tool installation verification."""

import os
import shutil
import subprocess
import sys
from typing import Optional, Union

from .result import Result
from .path_check import normalize_path_for_compare
from .subprocess_run import run_captured

# Extras carried on tool-check Results:
#   install_cmd -- platform install command when the tool is missing (or None)
#   path        -- absolute path to the resolved binary, when passed=True
#   on_path     -- True when the tool is reachable by bare name on PATH


def _tool_result(name, passed, message, install_cmd=None, path=None, on_path=False):
    return Result(
        passed=passed,
        subject=name,
        message=message,
        extras={"install_cmd": install_cmd, "path": path, "on_path": on_path},
    )


def resolve_bash() -> Optional[str]:
    """Absolute path of the bash behind the engine's Windows/MSYS shim, or None.

    This is the SINGLE discovery path for bash-on-Windows semantics: the
    check/install shims here, :func:`bootstrap_lib.env_features.run_env_command`,
    and elevation's Windows queued-command render all resolve through this
    function, so every consumer agrees on WHICH bash runs a command. It finds
    bash because Claude Code's SessionStart runs inside Git Bash (usr/bin on
    PATH); elevated cmd.exe would not (Git for Windows exposes Git\\cmd only),
    which is exactly why elevation embeds this ABSOLUTE path at render time.
    """
    return shutil.which("bash")


def _dir_on_path(directory: str) -> bool:
    """True if `directory` is present in the current process PATH."""
    target = normalize_path_for_compare(directory)
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if d and normalize_path_for_compare(d) == target:
            return True
    return False


def _run_check_cmd(check_cmd: str) -> bool:
    """Run a manifest `check` command; return True iff it exits 0.

    Uses the same bash-on-Windows shim as run_install so check commands can use
    Unix syntax (command -v, &&, test -f) regardless of the launching shell.
    """
    try:
        if sys.platform == "win32" or "MSYSTEM" in os.environ:
            bash = resolve_bash()
            if bash:
                result = subprocess.run([bash, "-c", check_cmd], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
            else:
                result = subprocess.run(check_cmd, shell=True, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        else:
            result = subprocess.run(check_cmd, shell=True, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def check_tool(
    name: str,
    install_cmds: Optional[dict] = None,
    current_os: Optional[str] = None,
    install_path: Optional[Union[str, list]] = None,
    check_cmd: Optional[str] = None,
) -> Result:
    """Check if a CLI tool is installed.

    Resolution order: installPath candidates (file exists) -> check command
    (exit 0) -> shutil.which(name). The first hit wins.

    Args:
        name: Tool name (e.g. "uv", "git")
        install_cmds: Platform-keyed install commands (e.g. {"macos": "brew install git"})
        current_os: Current OS string from detect_os()
        install_path: Directory (or list of candidate directories) where the tool
                      binary may live. Supports ~ and $VAR expansion. Checked
                      before the check command and before shutil.which().
        check_cmd: Optional shell command whose exit-0 means "present". Used for
                   tools whose presence can't be expressed as name-on-PATH (app
                   bundles, version probes). Resolves the tool but yields no
                   concrete binary path.

    Returns:
        Result. The `on_path` extra reports whether the tool is reachable by bare
        name on the current PATH — a tool can be `passed=True` (found on disk) yet
        `on_path=False` (its directory isn't on PATH), which the engine treats as
        an actionable "link this dir onto PATH" rather than a pass-and-forget.
    """
    # 1. install_path candidates (covers tools not yet in PATH)
    if install_path:
        candidates_dirs = [install_path] if isinstance(install_path, str) else list(install_path)
        for raw_dir in candidates_dirs:
            if not raw_dir:
                continue
            expanded_dir = os.path.expanduser(os.path.expandvars(raw_dir))
            candidates = [os.path.join(expanded_dir, name)]
            if sys.platform == "win32" or "MSYSTEM" in os.environ:
                candidates.append(os.path.join(expanded_dir, name + ".exe"))
            for candidate in candidates:
                if os.path.isfile(candidate):
                    return _tool_result(
                        name, True, f"found at {candidate}",
                        path=candidate,
                        on_path=_dir_on_path(expanded_dir),
                    )

    # 2. check command (exit 0 => present). No concrete binary path is known, so
    # on_path is reported True — the engine has no directory to link onto PATH.
    if check_cmd and _run_check_cmd(check_cmd):
        return _tool_result(name, True, "check command passed", on_path=True)

    # 3. PATH lookup — by definition reachable by name when found here.
    path = shutil.which(name)
    if path:
        return _tool_result(name, True, f"found at {path}", path=path, on_path=True)

    install_cmd = None
    if install_cmds and current_os:
        install_cmd = install_cmds.get(current_os)

    return _tool_result(name, False, "not found in PATH", install_cmd=install_cmd)


# Installers legitimately take minutes: the Claude Code native installer alone
# downloads a ~280MB binary, which needs ~19 Mbit/s sustained to fit inside two
# minutes. The timeout exists to bound a HUNG installer, not to cap a slow link
# -- killing a working download reports "install failed" for a machine that was
# merely on hotel wifi, which is exactly the misleading-failure class this
# module otherwise tries to avoid. Ten minutes still bounds a hang.
INSTALL_TIMEOUT_SECONDS = 600


def run_install(install_cmd: str, timeout: int = INSTALL_TIMEOUT_SECONDS) -> tuple[bool, str]:
    """Run a platform-specific install command.

    On Windows, explicitly uses bash (from Git for Windows) so that install
    commands can use Unix syntax ($HOME, &&, curl pipes, etc.) regardless of
    whether Claude Code was launched from PowerShell or cmd. A command that
    needs PowerShell syntax must therefore invoke it explicitly
    (`powershell -NoProfile -Command "..."`), as the `claude` entry does.

    Returns:
        (success, output) — success=True if returncode==0

    NOTE: a non-zero exit is advisory, not authoritative — some installers exit
    non-zero for "already installed / no upgrade available" (winget exit 43).
    Callers should re-check the tool after install regardless of this bool; the
    re-check, not the exit code, is the source of truth for "is it there now."
    """
    try:
        if sys.platform == "win32" or "MSYSTEM" in os.environ:
            bash = resolve_bash()
            if bash:
                command = [bash, "-c", install_cmd]
            else:
                command = install_cmd
        else:
            command = install_cmd
        returncode, stdout, stderr = run_captured(
            command, timeout=timeout, stdin_devnull=True,
        )
        output = (stdout + stderr).strip()
        return returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, f"install timed out after {timeout}s"
    except Exception as e:
        return False, str(e)
