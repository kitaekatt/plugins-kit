"""Lazily provision Scoop and install Scoop packages (Windows userspace).

Scoop (https://scoop.sh) is the Windows userspace package manager: it installs
portable apps into ``~/scoop`` in the user profile with NO administrator rights
and NO UAC prompt -- matching bootstrap's local-first, ``~/.local`` philosophy.

On Windows, a tool entry can declare a ``scoop`` fulfillment inside its
``download`` block instead of a ``url``/``sha256`` pair::

    "download": { "windows-amd64": { "scoop": "main/p4" } }

and the engine installs it via Scoop. Scoop itself is installed LAZILY -- the
first tool that needs it triggers :func:`ensure_scoop` -- so machines that never
use a Scoop-backed tool never get Scoop.

Windows-only: every entry point returns a failure / no-op on other platforms.
Stdlib-only (subprocess to PowerShell); never imports the rest of bootstrap_lib.
"""

import os
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
            capture_output=True, text=True, timeout=timeout,
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


def scoop_install(package: str, tool_name: Optional[str] = None) -> ScoopResult:
    """Install a Scoop package, adding its bucket first for ``bucket/pkg`` form.

    ``package`` is ``"pkg"`` (default ``main`` bucket) or ``"bucket/pkg"``
    (e.g. ``"main/p4"``, ``"extras/perforce"``). On success returns the installed
    shim path for ``tool_name`` (defaults to the package's bare name).
    Windows-only; assumes :func:`ensure_scoop` already succeeded.
    """
    if sys.platform != "win32":
        return ScoopResult(False, None, "scoop is Windows-only")
    bucket, pkg = (package.split("/", 1) if "/" in package else (None, package))
    if bucket:
        # 'bucket add' on an already-added bucket exits nonzero with a benign
        # "already added" -- not fatal; the subsequent install is the real check.
        _scoop_cmd(f"bucket add {bucket}")
    ok, out = _scoop_cmd(f"install {package}")
    shim = _find_shim(tool_name or pkg)
    if shim:
        return ScoopResult(True, shim, f"installed {package} via scoop")
    if ok:
        return ScoopResult(True, None, f"installed {package} via scoop (shim not located)")
    return ScoopResult(False, None, f"scoop install {package} failed: {out}")
