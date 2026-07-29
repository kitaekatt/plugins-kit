"""Machine-local convergence state.

``state.json`` records, per materialized entry, the sha256 of the BLOB it came
from and of the PLAINTEXT that was written, plus the mode applied. That pair
is what makes the steady-state pass free: if both hashes still match, nothing
is decrypted and no passphrase-derived material is touched at all.

Hashing the plaintext is a deliberate choice worth defending: it detects local
truncation, tampering, and deletion, which turns "partially materialized" into
a self-healing state rather than a failure mode. It adds no exposure -- the
file sits in the same trust domain as the plaintext it describes, and a hash
of high-entropy material is not a meaningful oracle.
"""

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .perms import open_private


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> Optional[str]:
    """Hash a file, or None if it is not there (a normal, expected state)."""
    try:
        with open(path, "rb") as fh:
            digest = hashlib.sha256()
            for chunk in iter(lambda: fh.read(65536), b""):
                digest.update(chunk)
            return digest.hexdigest()
    except OSError:
        return None


class State:
    """Read/modify/write of state.json, always atomically."""

    def __init__(self, path: Path, rows: Dict[str, Dict[str, Any]]) -> None:
        self.path = path
        self.rows = rows

    @classmethod
    def load(cls, path: Path) -> "State":
        """A missing or corrupt state file is not an error.

        It is only a cache of what we believe we already wrote; losing it costs
        one round of re-decryption and nothing else. Treating a parse failure
        as fatal would turn a trivial recoverable condition into a blocked
        machine.
        """
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls(path, {})
        rows = data.get("entries") if isinstance(data, dict) else None
        if not isinstance(rows, dict):
            return cls(path, {})
        return cls(path, rows)

    def get(self, name: str) -> Dict[str, Any]:
        row = self.rows.get(name)
        return row if isinstance(row, dict) else {}

    def record(
        self, name: str, *, blob_sha: str, dest_sha: str, mode: int, dest: str
    ) -> None:
        """Record a materialized entry.

        ``dest`` is stored because the orphan sweep needs to delete a file
        whose manifest entry no longer exists -- by then there is nothing left
        to resolve the path from, so the state file has to remember it.
        """
        self.rows[name] = {
            "blob_sha256": blob_sha,
            "dest_sha256": dest_sha,
            "mode": format(mode, "04o"),
            "dest": dest,
            "written_at": int(time.time()),
        }

    def forget(self, name: str) -> None:
        self.rows.pop(name, None)

    def save(self) -> None:
        """Atomic write at 0600 -- the file names every secret this machine holds.

        It carries no secret VALUES, but the entry list plus destination paths
        is still a map of where the credentials are, so it gets the same
        treatment as the material it describes.
        """
        payload = json.dumps({"version": 1, "entries": self.rows}, indent=2) + "\n"
        tmp = self.path.with_name(self.path.name + f".tmp-{os.getpid()}")
        fd = open_private(tmp, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
