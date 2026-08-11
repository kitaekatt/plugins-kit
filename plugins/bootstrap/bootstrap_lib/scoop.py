"""Lazily provision Scoop and install Scoop packages (Windows userspace).

Scoop (https://scoop.sh) is the Windows userspace package manager: it installs
portable apps into ``~/scoop`` in the user profile with NO administrator rights
and NO UAC prompt -- matching bootstrap's local-first, ``~/.local`` philosophy.

On Windows, a tool entry declares a ``scoop`` fulfillment under its
``install`` block instead of a ``url``/``sha256`` download::

    "install": { "windows": { "scoop": "main/p4" } }

and the engine installs it via Scoop. (The legacy spelling
``"download": { "windows-amd64": { "scoop": "main/p4" } }`` is still READ --
``_normalize_tool_entry`` promotes it to the canonical ``install.<os>.scoop``
in memory -- but new manifests should use the ``install`` form above.) Scoop
itself is installed LAZILY -- the
first tool that needs it triggers :func:`ensure_scoop` -- so machines that never
use a Scoop-backed tool never get Scoop.

Windows-only: every entry point returns a failure / no-op on other platforms.
Stdlib-only (subprocess to PowerShell); never imports the rest of bootstrap_lib.
"""

import os
import re
import shutil
import subprocess
import sys
from typing import NamedTuple, Optional, Tuple


class ScoopResult(NamedTuple):
    ok: bool
    path: Optional[str]   # absolute path to the installed shim on success (scoop_install)
    message: str          # human-readable status / error


def _scoop_root() -> str:
    return os.environ.get("SCOOP") or os.path.expanduser("~/scoop")


def _shims_dir() -> str:
    return os.path.join(_scoop_root(), "shims")


def scoop_available() -> bool:
    """True if Scoop is already installed and runnable."""
    if shutil.which("scoop"):
        return True
    shims = _shims_dir()
    return (os.path.isfile(os.path.join(shims, "scoop.ps1"))
            or os.path.isfile(os.path.join(shims, "scoop.cmd")))


def _run_powershell(command: str, timeout: int = 300) -> Tuple[bool, str]:
    """Run a PowerShell command non-interactively. Returns (ok, output).

    ``-ExecutionPolicy Bypass`` lets the install/`scoop` scripts run at Process
    scope without changing the user's persistent policy. (A GPO-enforced
    MachinePolicy/UserPolicy outranks Process scope and would still block --
    that case is out of scope by design: it surfaces as a normal failure.)
    """
    ps = shutil.which("powershell") or shutil.which("pwsh")
    if not ps:
        return False, "powershell not found"
    try:
        result = subprocess.run(
            [ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-Command", command],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
        return result.returncode == 0, (result.stdout + result.stderr).strip()
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout}s"
    except Exception as e:  # pragma: no cover - defensive
        return False, str(e)


def ensure_scoop() -> ScoopResult:
    """Install Scoop into ``~/scoop`` if absent (no admin, no UAC). Idempotent.

    Returns ok immediately when Scoop is already present. Windows-only.
    """
    if sys.platform != "win32":
        return ScoopResult(False, None, "scoop is Windows-only")
    if scoop_available():
        return ScoopResult(True, None, "already installed")
    # Official userspace installer; runs entirely within the user profile.
    _run_powershell("Invoke-RestMethod -Uri 'https://get.scoop.sh' | Invoke-Expression")
    if scoop_available():
        return ScoopResult(True, None, "installed scoop")
    return ScoopResult(False, None, "scoop install failed (is the network reachable?)")


def _scoop_cmd(args: str, timeout: int = 300) -> Tuple[bool, str]:
    """Invoke ``scoop <args>`` via PowerShell, resolving the freshly-installed
    shim even when ``~/scoop/shims`` isn't on this process's PATH yet."""
    shim = os.path.join(_shims_dir(), "scoop.ps1")
    if os.path.isfile(shim):
        return _run_powershell(f"& '{shim}' {args}", timeout=timeout)
    return _run_powershell(f"scoop {args}", timeout=timeout)


def _find_shim(name: str) -> Optional[str]:
    """Locate the shim Scoop created for ``name`` (extension varies by app)."""
    d = _shims_dir()
    for ext in (".exe", ".cmd", ".bat", ".ps1", ".shim", ""):
        p = os.path.join(d, name + ext)
        if os.path.isfile(p):
            return p
    return None


# Failure markers inside `scoop install` output. scoop's `error` helper
# prints an "ERROR <msg>" line but the scoop process can still EXIT 0
# (observed live: extras/tailscale's pre_install admin gate errors + breaks;
# files land under ~/scoop/apps/<pkg>, `scoop list` shows "Install failed",
# no shim is created) -- so the exit code alone is not trustworthy.
_ERROR_LINE_RE = re.compile(r"^\s*ERROR\b", re.IGNORECASE)
_FAILED_TEXT_RE = re.compile(r"install(ation)?[^\n]*\bfailed\b", re.IGNORECASE)


def _install_failure_detail(ok: bool, out: str) -> Optional[str]:
    """None when the install output looks successful, else the error detail.

    A failure is a non-zero exit AND/OR failure text in the output (an
    ``ERROR ...`` line or an "install ... failed" phrase). The detail
    prefers the failure lines themselves; a silent non-zero exit falls back
    to the last non-empty output line.
    """
    error_lines = [
        ln.strip() for ln in out.splitlines()
        if _ERROR_LINE_RE.match(ln) or _FAILED_TEXT_RE.search(ln)
    ]
    if error_lines:
        return "; ".join(error_lines)
    if not ok:
        tail = [ln for ln in out.strip().splitlines() if ln.strip()]
        return tail[-1].strip() if tail else "no output"
    return None


def scoop_install(package: str, tool_name: Optional[str] = None) -> ScoopResult:
    """Install a Scoop package, adding its bucket first for ``bucket/pkg`` form.

    ``package`` is ``"pkg"`` (default ``main`` bucket) or ``"bucket/pkg"``
    (e.g. ``"main/p4"``, ``"extras/perforce"``). On success returns the installed
    shim path for ``tool_name`` (defaults to the package's bare name).

    Success requires BOTH a clean install (exit 0, no failure text -- see
    :func:`_install_failure_detail`) AND a locatable shim; anything else is a
    failure carrying the captured scoop error. (A package that legitimately
    ships no shim under the tool's name still succeeds at the engine level
    when the entry's own ``check`` re-check resolves it -- the engine consults
    its re-check BEFORE this result.) Windows-only; assumes
    :func:`ensure_scoop` already succeeded.
    """
    if sys.platform != "win32":
        return ScoopResult(False, None, "scoop is Windows-only")
    bucket, pkg = (package.split("/", 1) if "/" in package else (None, package))
    if bucket:
        # 'bucket add' on an already-added bucket exits nonzero with a benign
        # "already added" -- not fatal; the subsequent install is the real check.
        _scoop_cmd(f"bucket add {bucket}")
    ok, out = _scoop_cmd(f"install {package}")
    failure = _install_failure_detail(ok, out)
    if failure is None:
        shim = _find_shim(tool_name or pkg)
        if shim:
            return ScoopResult(True, shim, f"installed {package} via scoop")
        failure = (
            f"scoop reported success but no shim for "
            f"'{tool_name or pkg}' exists in {_shims_dir()}"
        )
    return ScoopResult(False, None, f"scoop install {package} failed: {failure}")


def elevated_install_command(package: str) -> str:
    """The user-runnable command for a scoop install deferred for elevation.

    Rendered into the Windows remediation .bat, whose queued commands run
    through Git Bash (``"<bash.exe>" -c "<cmd>"`` -- see
    :mod:`bootstrap_lib.fix_runner`). ``scoop`` is a
    PowerShell function, so the command shells out to ``powershell -Command``
    explicitly instead of relying on bash resolving the shim; the elevated
    process keeps the invoking user's profile (UAC same-user elevation), so
    the user-PATH ``~/scoop/shims`` entry resolves ``scoop`` there. Single
    quotes only: the ``powershell -Command '...'`` wrapper cannot carry an
    unescaped double quote. The bucket add is included for ``bucket/pkg`` form
    (idempotent; benign when already added).
    """
    bucket, _pkg = (package.split("/", 1) if "/" in package else (None, package))
    steps = []
    if bucket:
        steps.append(f"scoop bucket add {bucket}")
    steps.append(f"scoop install {package}")
    inner = "; ".join(steps)
    return f"powershell -NoProfile -ExecutionPolicy Bypass -Command '{inner}'"
