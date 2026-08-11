"""Detect Homebrew and install brew formulae/casks (macOS).

Homebrew (https://brew.sh) is macOS's package manager: it installs formulae
(CLI packages) and casks (GUI apps) into a user-owned prefix
(``/opt/homebrew`` on Apple Silicon, ``/usr/local`` on Intel).

On macOS, a tool entry can declare a ``brew`` fulfillment inside its ``install``
block instead of a ``download`` url/sha pair or an opaque command::

    "install": { "macos": {"brew": "direnv"} }              # formula shorthand
    "install": { "macos": {"brew": {"cask": "google-chrome"}} }
    "install": { "macos": {"brew": {"formula": "jj", "tap": "tidwall/jj"}} }

and the engine installs it via Homebrew.

Unlike Scoop, brew is **never auto-installed**: its official installer is
interactive and may sudo, so a non-interactive SessionStart hook must not run
it. :func:`ensure_brew` is DETECT-ONLY -- if brew is missing, the entry fails
with a descriptive message (the elevation/remediation queue that would carry
the installer is a later step in the refactor).

macOS-only: every entry point returns a failure / no-op on other platforms.
Stdlib-only (subprocess); never imports the rest of bootstrap_lib.
"""

import os
import shutil
import subprocess
import sys
from typing import NamedTuple, Optional, Tuple


class BrewResult(NamedTuple):
    ok: bool
    path: Optional[str]   # absolute path to the brew binary (ensure_brew) or None
    message: str          # human-readable status / error


# Standard Homebrew binary locations: Apple Silicon, then Intel.
_BREW_CANDIDATES = ("/opt/homebrew/bin/brew", "/usr/local/bin/brew")


def _brew_bin() -> Optional[str]:
    """Absolute path to the ``brew`` binary, or None when it is not present."""
    found = shutil.which("brew")
    if found:
        return found
    for candidate in _BREW_CANDIDATES:
        if os.path.isfile(candidate):
            return candidate
    return None


def brew_available() -> bool:
    """True if Homebrew is already installed and runnable."""
    return _brew_bin() is not None


def ensure_brew() -> BrewResult:
    """DETECT-ONLY: report whether Homebrew is present. NEVER installs it.

    Bootstrap does not auto-install Homebrew -- the official installer is
    interactive and may sudo, which a non-interactive hook must not trigger.
    macOS-only. Returns ok with the brew path when present, else a descriptive
    failure telling the user to install Homebrew.
    """
    if sys.platform != "darwin":
        return BrewResult(False, None, "brew is macOS-only")
    binp = _brew_bin()
    if binp:
        return BrewResult(True, binp, "already installed")
    return BrewResult(
        False, None,
        "Homebrew is not installed. Install it from https://brew.sh "
        "(the installer is interactive), then re-run bootstrap.",
    )


def _run_brew(brew_bin: str, args, timeout: int = 600) -> Tuple[bool, str]:
    """Invoke ``brew <args>`` non-interactively. Returns (ok, combined_output)."""
    try:
        result = subprocess.run(
            [brew_bin] + list(args),
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
        return result.returncode == 0, (result.stdout + result.stderr).strip()
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout}s"
    except Exception as e:  # pragma: no cover - defensive
        return False, str(e)


def brew_install(
    formula: Optional[str] = None,
    cask: Optional[str] = None,
    tap: Optional[str] = None,
    timeout: int = 600,
) -> BrewResult:
    """Install a brew formula or cask (macOS). Provide exactly one of the two.

    ``formula`` installs a CLI package (``brew install <formula>``); ``cask``
    installs a GUI app (``brew install --cask <cask>``). ``tap`` is added first
    when declared (e.g. ``tidwall/jj``) and the install then targets the
    tap-qualified name (``tidwall/jj/jj``) so a same-named homebrew-core
    formula cannot shadow the tapped one. Assumes :func:`ensure_brew` already
    confirmed brew is present; runs unprivileged.
    """
    if sys.platform != "darwin":
        return BrewResult(False, None, "brew is macOS-only")
    if bool(formula) == bool(cask):
        return BrewResult(
            False, None,
            "brew_install requires exactly one of formula or cask",
        )
    binp = _brew_bin()
    if not binp:
        return BrewResult(False, None, "brew not found")

    if tap:
        # 'brew tap' on an already-tapped repo exits 0; a genuine tap failure is
        # not fatal on its own -- the install below is the authoritative check.
        _run_brew(binp, ["tap", tap], timeout=timeout)

    if cask:
        target, args = cask, ["install", "--cask", cask]
    else:
        # Tap-qualify the install target (<tap>/<formula>): homebrew-core can
        # ship a formula with the same bare name (e.g. core's `jj` vs
        # tidwall/jj's `jj`), and a bare `brew install <formula>` resolves the
        # core one even right after tapping. Fully-qualified names always
        # resolve to the tapped formula (manifest-reference: `brew install
        # <tap/>name`).
        target = f"{tap}/{formula}" if tap else formula
        args = ["install", target]

    ok, out = _run_brew(binp, args, timeout=timeout)
    if ok:
        return BrewResult(True, None, f"installed {target} via brew")
    return BrewResult(False, None, f"brew install {target} failed: {out}")
