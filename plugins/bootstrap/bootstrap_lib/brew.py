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


# --------------------------------------------------------------------------- #
# Root-requirement detection for casks
# --------------------------------------------------------------------------- #
#
# A cask whose payload is a signed .pkg is installed by Homebrew with
# `sudo /usr/sbin/installer`. Bootstrap has no TTY, so sudo dies with "a
# terminal is required to read the password" and 25+ lines of raw brew output
# (caveats, license notice, download progress) land in the failure message --
# after a pointless partial download. Detecting the requirement BEFORE the
# attempt routes the cask to the elevation queue instead, where the user has a
# console.
#
# `brew info --json=v2 --cask <token>` reports the cask's `artifacts`, which is
# what distinguishes a `binary`/`app` cask (no root) from a `pkg` or sudo
# `installer` one (root). It reads the already-cloned homebrew/cask tap, so it
# is metadata only -- it downloads no payload.

# Artifact keys that mean "Homebrew will invoke sudo during install".
#   pkg       -- handed to `sudo /usr/sbin/installer`, always.
#   installer -- root ONLY for `{"script": {..., "sudo": true}}`; an
#                `{"manual": "Foo.app"}` installer needs no privilege at all.
_ROOT_ARTIFACT_PKG = "pkg"
_ROOT_ARTIFACT_INSTALLER = "installer"


class CaskInfo(NamedTuple):
    needs_root: bool          # True when installing this cask will invoke sudo
    reason: str               # short, human-readable; "" when needs_root is False
    caveats: str              # the cask's own caveats text, or ""
    known: bool               # False when the query failed / could not be parsed


def _installer_needs_root(entries) -> bool:
    """True when an `installer` artifact declares a sudo script."""
    if not isinstance(entries, list):
        return False
    for entry in entries:
        if isinstance(entry, dict):
            script = entry.get("script")
            if isinstance(script, dict) and script.get("sudo"):
                return True
    return False


def cask_root_requirement(cask: str, timeout: int = 120) -> CaskInfo:
    """Ask Homebrew whether installing ``cask`` will need root, before trying.

    FAILS OPEN: when brew is absent, the query errors, or the JSON has a shape
    this does not recognise, the result is ``known=False, needs_root=False`` and
    the caller installs inline exactly as before. An unrecognised cask must not
    become an elevation prompt the user cannot act on -- the after-the-fact
    sudo/TTY signature (:func:`is_sudo_tty_failure`) is the backstop for
    anything this misses.
    """
    if sys.platform != "darwin":
        return CaskInfo(False, "", "", False)
    binp = _brew_bin()
    if not binp:
        return CaskInfo(False, "", "", False)
    ok, out = _run_brew(binp, ["info", "--json=v2", "--cask", cask], timeout=timeout)
    if not ok:
        return CaskInfo(False, "", "", False)
    try:
        import json as _json
        data = _json.loads(out)
        casks = data.get("casks") or []
        info = casks[0]
    except Exception:
        return CaskInfo(False, "", "", False)
    if not isinstance(info, dict):
        return CaskInfo(False, "", "", False)

    caveats = info.get("caveats") or ""
    if not isinstance(caveats, str):
        caveats = ""
    caveats = caveats.strip()

    artifacts = info.get("artifacts")
    if not isinstance(artifacts, list):
        return CaskInfo(False, "", caveats, False)
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        if artifact.get(_ROOT_ARTIFACT_PKG):
            return CaskInfo(
                True,
                "the cask ships a signed .pkg, which Homebrew installs with "
                "`sudo /usr/sbin/installer`",
                caveats, True,
            )
        if _installer_needs_root(artifact.get(_ROOT_ARTIFACT_INSTALLER)):
            return CaskInfo(
                True,
                "the cask runs an installer script Homebrew declares as "
                "`sudo: true`",
                caveats, True,
            )
    return CaskInfo(False, "", caveats, True)


# Substrings sudo emits when it has no terminal to read a password from, plus
# brew's own wrapper wording. Matched case-insensitively against the combined
# brew output. This is the BACKSTOP for a root requirement cask_root_requirement
# did not predict; matching it lets the caller suppress the raw brew dump and
# re-route the cask to the elevation queue instead of surfacing 25 lines of
# download progress as an error message.
_SUDO_TTY_MARKERS = (
    "a terminal is required to read the password",
    "no tty present and no askpass program specified",
    "sudo: a password is required",
    "requires a password to be entered",
)


def is_sudo_tty_failure(output: str) -> bool:
    """True when brew output shows the install died for want of a TTY/password."""
    if not output:
        return False
    low = output.lower()
    return any(marker in low for marker in _SUDO_TTY_MARKERS)
