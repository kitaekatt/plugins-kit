"""secrets-kit -- fleet secrets provisioning over age-encrypted blobs.

Stdlib-only. The package is imported both by ``custom_bootstrap.py`` (the
session-start convergence pass) and by the ``secrets-kit`` CLI; nothing here
imports bootstrap engine internals, so the whole package can be folded into
the engine later without a rewrite (see the integration seam in the design).

Trust model in one breath: blobs are encrypted TO a public key, so adding or
rotating a secret needs no passphrase. Only ``identity.age`` is passphrase-
wrapped, and only unlocking a machine needs the passphrase -- once, ever.
"""

__all__ = [
    "SecretsError",
    "DecryptError",
]


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

