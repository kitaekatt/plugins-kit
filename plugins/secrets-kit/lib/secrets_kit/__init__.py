"""secrets-kit -- fleet secrets provisioning over age-encrypted blobs.

Stdlib-only. The package is imported both by ``custom_bootstrap.py`` (the
session-start convergence pass) and by the ``secrets-kit`` CLI; nothing here
imports bootstrap engine internals, so the whole package can be folded into
the engine later without a rewrite (see the integration seam in the design).

Trust model in one breath: blobs are encrypted TO a public key, so adding or
rotating a secret needs no passphrase. Only ``identity.age`` is passphrase-
wrapped, and only unlocking a machine needs the passphrase -- once, ever.
"""

from pathlib import Path

__all__ = [
    "SecretsError",
    "DecryptError",
    "cli_command",
]


def cli_command(verb: str = "") -> str:
    """The invocation that actually runs this plugin's CLI.

    The shim lives in the plugin's version-keyed install directory, which is
    NOT on PATH -- so a bare ``secrets-kit`` is not a command anyone can type,
    and any message that prints one hands out an instruction that fails with
    "command not found". Every literal command we emit for a human or an agent
    to run is rendered through here instead, from this package's own location,
    so it is correct for whichever version is doing the printing.

    Rendered with ``~`` when under the home directory: the ``!`` prompt and the
    plugin's own shell hooks both run under a POSIX shell, where the tilde form
    is both shorter and unambiguous.
    """
    shim = Path(__file__).resolve().parents[2] / "bin" / "secrets-kit"
    text = shim.as_posix()
    home = Path.home().as_posix()
    if text.lower().startswith(home.lower() + "/"):
        text = "~/" + text[len(home) + 1 :]
    return f"{text} {verb}".rstrip()


class SecretsError(Exception):
    """Any condition secrets-kit can describe to a user or an agent.

    Carries a ``remedy`` so callers do not have to re-derive what to do about
    it: the CLI prints it, and the bootstrap pass forwards it into the
    agent-facing failure message.
    """

    def __init__(self, message: str, remedy: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.remedy = remedy

    def __str__(self) -> str:
        if self.remedy:
            return f"{self.message}\n{self.remedy}"
        return self.message


class DecryptError(SecretsError):
    """A blob could not be decrypted with the identity this machine holds.

    Its own type because the REMEDY is categorically different from every
    other failure: not "fix the manifest" but "unlock again", the fleet
    identity having been rotated upstream. Sniffing that out of an age error
    string would be guesswork; raising it explicitly is not.
    """

