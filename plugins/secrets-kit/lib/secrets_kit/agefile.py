"""Thin subprocess wrapper around the ``age`` CLI.

Design note that shapes this whole module: **the passphrase never passes
through Python.** age reads a passphrase only from a terminal, and the three
operations that need one (``init``, ``unlock``, ``rotate-identity``) are all
run BY THE USER via a bang-prefixed prompt command, so age inherits the real
terminal and does its own hidden prompt. That is strictly safer than a getpass
in our process -- there is no interval where the secret sits in our memory, no
argv exposure, and nothing to accidentally log.

Everything the unattended session pass does (decrypt a blob with an already-
unlocked identity, encrypt a new blob to the public key) needs no passphrase
at all, so the pass never wants a terminal.
"""

import os
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

from . import DecryptError, SecretsError

# age is fast (scrypt work is only on the passphrase paths, which are
# interactive and unbounded by this). A generous ceiling on the non-interactive
# calls stops a wedged binary from hanging a session forever.
_TIMEOUT_SECONDS = 30

_INSTALL_HINT = (
    "age is not installed. It is declared in secrets-kit's bootstrap.json "
    "(scoop/brew/apt), so the usual fix is to let bootstrap provision it: "
    "restart Claude Code. Do not hand-install it -- the manifest is the "
    "single source of truth."
)


def _resolve(binary: str) -> str:
    found = shutil.which(binary)
    if not found:
        raise SecretsError(f"{binary} not found on PATH", _INSTALL_HINT)
    return found


def age_available() -> bool:
    """True when both binaries this plugin needs are resolvable."""
    return bool(shutil.which("age")) and bool(shutil.which("age-keygen"))


def _run(argv: List[str], *, stdin: Optional[bytes] = None) -> bytes:
    """Run a non-interactive age invocation and return stdout bytes.

    Never used for the passphrase paths -- those inherit the terminal via
    :func:`run_interactive` instead.
    """
    try:
        proc = subprocess.run(
            argv,
            input=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        raise SecretsError(
            f"{argv[0]} timed out after {_TIMEOUT_SECONDS}s",
            "This usually means age is waiting for input it will never get. "
            "Check that the identity file is an age identity and not a "
            "passphrase-wrapped file.",
        )
    except OSError as e:
        raise SecretsError(f"could not run {argv[0]}: {e}", _INSTALL_HINT)

    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise SecretsError(f"{argv[0]} failed: {detail or 'no stderr'}")
    return proc.stdout


def run_interactive(argv: List[str]) -> int:
    """Run age with the terminal INHERITED so it can prompt for a passphrase.

    Returns the exit code rather than raising: the callers (unlock, init) want
    to say "incorrect passphrase, try again" rather than surface a traceback,
    and a wrong passphrase is an ordinary outcome, not an error condition.
    """
    try:
        return subprocess.call(argv)
    except OSError as e:
        raise SecretsError(f"could not run {argv[0]}: {e}", _INSTALL_HINT)


def keygen() -> Tuple[str, str]:
    """Generate a fresh age identity.

    Returns ``(identity_text, recipient)``. The identity text is the full
    age-keygen output (comment lines + the AGE-SECRET-KEY line); the recipient
    is the public key, which is the only half that ever leaves this machine in
    the clear (it lives in manifest.json).
    """
    out = _run([_resolve("age-keygen")]).decode("utf-8")
    recipient = ""
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.startswith("# public key:"):
            recipient = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("age1") and not recipient:
            recipient = stripped
    if not recipient:
        raise SecretsError(
            "age-keygen produced no public key",
            "Unexpected age-keygen output format; report this.",
        )
    return out, recipient


def wrap_identity(identity_text: str, out_path: Path) -> int:
    """Passphrase-wrap an identity to ``out_path`` (age -p).

    INTERACTIVE: age prompts for the passphrase and its confirmation on the
    terminal. The identity text is fed on stdin, which age reads as the
    plaintext to encrypt -- so the identity never lands unencrypted on disk on
    the way to the repo.

    Note this deliberately does NOT go through ``_run``: that captures stdout
    and stderr, which would swallow age's prompt. Here stdout is redirected to
    the output file while stderr (the prompt) stays on the terminal.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    age = _resolve("age")
    try:
        with open(out_path, "wb") as fh:
            proc = subprocess.Popen(
                [age, "-p", "-a"],
                stdin=subprocess.PIPE,
                stdout=fh,
            )
            proc.communicate(identity_text.encode("utf-8"))
            return proc.returncode
    except OSError as e:
        raise SecretsError(f"could not run age: {e}", _INSTALL_HINT)


def unwrap_identity(wrapped_path: Path, out_path: Path) -> int:
    """Decrypt a passphrase-wrapped identity to ``out_path`` (INTERACTIVE).

    Returns age's exit code; a non-zero code is almost always a wrong
    passphrase, which the caller reports as such. The output file is created
    0600 BEFORE any plaintext reaches it (see :func:`write_private`), because
    a private key briefly sitting at the default umask is exactly the kind of
    window that never shows up in testing.
    """
    if not wrapped_path.is_file():
        raise SecretsError(f"no wrapped identity at {wrapped_path}")
    age = _resolve("age")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(out_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            proc = subprocess.Popen(
                [age, "-d", str(wrapped_path)],
                stdout=fh,
            )
            proc.communicate()
            return proc.returncode
    except OSError as e:
        raise SecretsError(f"could not run age: {e}", _INSTALL_HINT)


def encrypt_to_recipient(recipient: str, plaintext: bytes, out_path: Path) -> None:
    """Encrypt bytes to a public key. No passphrase, no terminal, any machine.

    This is what makes day-to-day use cheap: adding or rotating a secret is a
    public-key operation, so it never touches the root of trust.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    data = _run(
        [_resolve("age"), "-a", "-r", recipient],
        stdin=plaintext,
    )
    out_path.write_bytes(data)


def decrypt_with_identity(identity_path: Path, blob_path: Path) -> bytes:
    """Decrypt a blob using an unlocked identity file. Returns plaintext bytes.

    Returning bytes rather than writing a file is deliberate: the caller
    creates the destination with tight permissions FIRST and then writes into
    it, so decrypted material never exists at a loose mode.
    """
    if not identity_path.is_file():
        raise DecryptError(f"no unlocked identity at {identity_path}")
    if not blob_path.is_file():
        raise SecretsError(f"missing blob {blob_path}")
    try:
        return _run(
            [_resolve("age"), "-d", "-i", str(identity_path), str(blob_path)]
        )
    except SecretsError as e:
        # age exiting non-zero here means this identity cannot open this blob.
        # Re-typed so the caller can offer "unlock again" instead of a generic
        # "fix it" -- the two remedies have nothing in common.
        raise DecryptError(
            f"cannot decrypt {blob_path.name} with the identity on this machine",
            e.remedy,
        )
