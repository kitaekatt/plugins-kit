"""Detect privileges and install apt packages (Ubuntu), plus privilege probes.

apt is Ubuntu's system package manager. Unlike Scoop (userspace, no admin) and
Homebrew (user-owned prefix), apt installs system-wide and ALWAYS requires root.
On Ubuntu a tool entry can declare an ``apt`` fulfillment inside its ``install``
block instead of a ``download`` url/sha pair or an opaque command::

    "install": { "ubuntu": {"apt": "net-tools"} }

and the engine installs it via ``apt-get`` -- but only when it can do so
NON-INTERACTIVELY. SessionStart is a background hook, so this module never
prompts: it detects privileges first and, when they are missing, defers instead
of blocking on a sudo password or UAC dialog.

Privilege model (elevation policy, software-management-strategy section 5):
  * root (euid 0) is privileged and needs no sudo;
  * otherwise passwordless sudo (``sudo -n true`` exits 0) is the Ubuntu happy
    path -- env-config's sudoers rules open apt/dpkg on Christina's boxes, so
    apt entries install silently at SessionStart;
  * when neither holds, :func:`apt_install` NEVER attempts the operation and
    returns a needs-elevation outcome. The engine surfaces that as a persistent
    manual-attention item; accumulating deferred ops into ONE remediation script
    is a later step (this module only reports "needs elevation, here is what to
    run").

This module also hosts the small privilege probes that step 8's elevation queue
needs on both platforms -- :func:`sudo_noninteractive_available` (Unix) and
:func:`windows_admin_available` (the Windows admin-token check) -- because
privilege-awareness enters the engine with the apt backend. A later step may
lift them into a dedicated elevation module.

Stdlib-only (subprocess, list argv); never imports the rest of bootstrap_lib.
"""

import os
import shutil
import subprocess
import sys
from typing import NamedTuple


class AptResult(NamedTuple):
    ok: bool               # True when the package is installed / already present
    needs_elevation: bool  # True when the op could not run for lack of privilege
    message: str           # human-readable status / error


def is_root() -> bool:
    """True when the current process runs as root (euid 0), else False.

    Root needs no sudo. On platforms without ``os.geteuid`` (Windows) this is
    always False -- privilege there is decided by :func:`windows_admin_available`.
    """
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None:
        return False
    return geteuid() == 0


def sudo_noninteractive_available() -> bool:
    """True when this user can run sudo WITHOUT a password prompt (or is root).

    Root counts as privileged immediately. Otherwise probe ``sudo -n true``:
    exit 0 means passwordless sudo is configured. A missing ``sudo`` binary, any
    nonzero exit, or a timeout all mean elevation is unavailable -- the caller
    must defer, never prompt (SessionStart is non-interactive). Cheap
    (sub-millisecond); not cached -- a single engine pass calls it at most once
    per deferred apt entry.
    """
    if is_root():
        return True
    sudo = shutil.which("sudo")
    if not sudo:
        return False
    try:
        result = subprocess.run(
            [sudo, "-n", "true"],
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False
    except Exception:  # pragma: no cover - defensive
        return False


def windows_admin_available() -> bool:
    """True when this Windows process holds an elevated (admin) token.

    Uses ``shell32.IsUserAnAdmin`` via ctypes. Exists for step 8's elevation
    queue (its Windows branch); on non-Windows it is a no-op False, since Unix
    elevation is decided by :func:`sudo_noninteractive_available`. Minimal by
    design -- detect only, never elevates.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover - defensive
        return False


def dpkg_installed(pkg: str) -> bool:
    """True when dpkg reports ``pkg`` as installed. Rootless (queries the DB only).

    Runs ``dpkg-query -W -f '${db:Status-Status}' <pkg>`` and checks for the
    "installed" status. This is the apt backend's INTERNAL knowledge -- used by
    :func:`apt_install` as a cheap idempotency guard so a present package is not
    needlessly re-installed (and does not trigger a false needs-elevation defer
    when passwordless sudo is absent). The ENGINE's authority for "is the tool
    present" remains its check-first resolve/re-check; dpkg-query is never
    consulted as that authority (detection policy, section 8).
    """
    dpkg_query = shutil.which("dpkg-query")
    if not dpkg_query:
        return False
    try:
        result = subprocess.run(
            [dpkg_query, "-W", "-f", "${db:Status-Status}", pkg],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return False
    except Exception:  # pragma: no cover - defensive
        return False
    return result.returncode == 0 and result.stdout.strip() == "installed"


# Once-per-pass guard for `apt-get update`. apt's package lists can be stale or
# empty on a fresh machine, so a direct `apt-get install` may fail to find an
# otherwise-available package. We refresh the lists ONCE, right before the first
# DIRECT apt install a pass performs -- never per package (wasteful) and never on
# the deferred/elevation path (that update leads the emitted remediation script,
# see elevation.py). The engine calls reset_apt_pass_state() at the start of each
# pass so the next pass refreshes again. This is the whole caching story: a single
# boolean, no config knob, no persistence.
_apt_update_ran = False


def reset_apt_pass_state() -> None:
    """Re-arm the once-per-pass `apt-get update` guard for a new engine pass.

    Called by the engine at pass start. A production pass is a fresh process, so
    this only matters when several passes share one process (the test suite, the
    harvest): without it the first pass's update would suppress every later pass's.
    """
    global _apt_update_ran
    _apt_update_ran = False


def _apt_get_update_once(prefix, timeout: int = 600) -> None:
    """Run `apt-get update` at most once per pass, before the first direct install.

    ``prefix`` is the same privilege prefix the install uses (``[]`` as root, else
    ``["sudo", "-n"]``); the caller has already confirmed non-interactive privilege,
    so this never prompts. A failed refresh is non-fatal -- the subsequent install's
    authoritative re-check surfaces any real problem -- but the guard still flips so
    the pass does not retry update for every package.
    """
    global _apt_update_ran
    if _apt_update_ran:
        return
    _apt_update_ran = True
    _run(prefix + ["apt-get", "update"], timeout=timeout)


def _run(argv, timeout: int = 600):
    """Invoke ``argv`` non-interactively. Returns (ok, combined_output).

    The never-prompts contract is structural, not merely timeout-bounded:
    stdin is closed (DEVNULL) so a debconf question or dpkg conffile prompt
    cannot read the inherited stdin and stall, and DEBIAN_FRONTEND=
    noninteractive tells debconf to take defaults instead of asking.
    """
    try:
        result = subprocess.run(
            list(argv),
            capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL,
            env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"},
        )
        return result.returncode == 0, (result.stdout + result.stderr).strip()
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout}s"
    except Exception as e:  # pragma: no cover - defensive
        return False, str(e)


def apt_install(pkg: str, timeout: int = 600) -> AptResult:
    """Install an apt package non-interactively, or defer when it can't elevate.

    Runs ``apt-get install -y <pkg>`` -- prefixed with ``sudo -n`` unless already
    root. Never prompts. Order of decisions:

      1. Already installed (dpkg guard) -> ok, no elevation needed.
      2. No passwordless sudo and not root -> NEVER attempt; return a
         needs-elevation outcome describing the manual command to run.
      3. Otherwise run the install; the caller re-checks (that re-check, not the
         apt exit code, is authoritative).

    Ubuntu/Linux-only; a no-op failure elsewhere (the engine only routes ubuntu
    ``install.ubuntu.apt`` entries here).
    """
    if sys.platform != "linux":
        return AptResult(False, False, "apt is Linux-only")

    # Cheap rootless idempotency guard: a package already present must not defer
    # for elevation (nor shell out to apt-get) even when sudo is unavailable.
    if dpkg_installed(pkg):
        return AptResult(True, False, f"{pkg} already installed (dpkg)")

    if not sudo_noninteractive_available():
        return AptResult(
            False, True,
            f"apt install {pkg} requires elevation: passwordless sudo is not "
            f"available (sudo -n failed) and this process is not root",
        )

    prefix = [] if is_root() else ["sudo", "-n"]
    # Refresh package lists once per pass before the first direct install so a
    # stale/empty index does not fail an otherwise-installable package.
    _apt_get_update_once(prefix, timeout=timeout)
    ok, out = _run(prefix + ["apt-get", "install", "-y", pkg], timeout=timeout)
    if ok:
        return AptResult(True, False, f"installed {pkg} via apt")
    return AptResult(False, False, f"apt-get install {pkg} failed: {out}")
