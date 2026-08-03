"""Permission tightening -- chmod on POSIX, icacls on Windows.

Windows is the reason this is a module rather than a call to ``os.chmod``:
POSIX modes are decorative there, so a 0600 file is world-readable in
practice. The fix is the same one Windows OpenSSH requires for private keys --
strip inheritance and grant only the current user -- and a failure to apply it
is a REAL failure, surfaced, not a warning. A secret written with the wrong
ACL is not a partial success.
"""

import os
import subprocess
import sys
from pathlib import Path

from . import SecretsError

IS_WINDOWS = sys.platform.startswith("win")

_ICACLS_TIMEOUT = 20


def _current_windows_principal() -> str:
    """The user to grant. USERDOMAIN\\USERNAME when both are known."""
    user = os.environ.get("USERNAME", "")
    domain = os.environ.get("USERDOMAIN", "")
    if not user:
        raise SecretsError(
            "cannot determine the current Windows user (USERNAME unset)",
            "secrets-kit needs it to tighten the ACL on a materialized secret.",
        )
    return f"{domain}\\{user}" if domain else user


def tighten(path: Path, mode: int) -> None:
    """Restrict ``path`` to ``mode``, honestly on both platforms.

    ``mode`` is the POSIX mode from the manifest. On Windows anything stricter
    than world-readable (i.e. not 0644) maps to the owner-only ACL; 0644 files
    (the ssh .pub key is the real case) are left with inherited permissions,
    because a public key is public.
    """
    if not IS_WINDOWS:
        try:
            os.chmod(path, mode)
        except OSError as e:
            raise SecretsError(f"chmod {oct(mode)} failed on {path}: {e}")
        return

    if mode == 0o644:
        return

    _icacls(path, "F")


def _icacls(path: Path, rights: str) -> None:
    """Strip inherited ACEs from ``path`` and grant the current user ``rights``.

    ``rights`` is an icacls permission spec. For a DIRECTORY it must carry the
    inheritance flags -- see tighten_dir, where omitting them is not cosmetic.
    """
    principal = _current_windows_principal()
    argv = [
        "icacls",
        str(path),
        "/inheritance:r",
        "/grant:r",
        f"{principal}:{rights}",
    ]
    try:
        proc = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=_ICACLS_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SecretsError(f"icacls failed on {path}: {e}")
    if proc.returncode != 0:
        detail = proc.stdout.decode("utf-8", "replace").strip()
        raise SecretsError(
            f"icacls could not restrict {path}: {detail or 'no output'}"
        )


def tighten_dir(path: Path) -> None:
    """Make a directory owner-only (0700 / owner-only ACL).

    Applied to the data dir holding the unlocked identity and state, so a
    materialized secret is not readable via a permissive parent even if the
    file mode is right.

    The (OI)(CI) inheritance flags are load-bearing, and their absence does NOT
    show up as a failure here -- it corrupts the CHILDREN. ``/inheritance:r``
    removes this directory's inherited ACEs, and Windows propagates that
    removal to every file already inside; a file whose access was entirely
    inherited is left with an EMPTY DACL -- unreadable and unwritable even by
    its owner, reported as ``AccessRuleCount: 0``. Granting the owner an
    inheritable ACE instead both repairs those files (they inherit it
    immediately) and stops new ones from depending on the creating token's
    default DACL. Owner-only is unchanged; the ACE is simply allowed to reach
    the contents, which is what 0700 means on the POSIX side.

    Observed 2026-08-03: without the flags, secrets-kit's own data dir left
    bootstrap.log with an empty DACL, and the PermissionError from appending to
    it aborted the entire bootstrap engine on every SessionStart.
    """
    path.mkdir(parents=True, exist_ok=True)
    if not IS_WINDOWS:
        try:
            os.chmod(path, 0o700)
        except OSError as e:
            raise SecretsError(f"chmod 0700 failed on {path}: {e}")
        return
    _icacls(path, "(OI)(CI)F")


def open_private(path: Path, mode: int) -> int:
    """Create ``path`` for writing at ``mode`` BEFORE any content exists.

    The ordering is the whole point: creating the file at its final (tight)
    mode and then writing into it means decrypted material is never visible at
    a looser mode, not even for the microseconds between write and chmod.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    return os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
