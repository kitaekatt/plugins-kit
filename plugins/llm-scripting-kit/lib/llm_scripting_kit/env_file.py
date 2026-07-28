"""Minimal KEY=VALUE .env reader/writer.

Mirrors the parsing rules of loc-ops's existing ``load_env`` helper so the
two formats stay interoperable: blank/comment lines skipped, ``key=value``
with optional surrounding double or single quotes on the value.

We do NOT implement variable interpolation, multi-line values, or export
syntax -- the .env files this plugin manages contain a single API key.
"""

import os
from pathlib import Path
from typing import Dict


def read_env_file(path: Path) -> Dict[str, str]:
    """Parse a KEY=VALUE .env file. Returns empty dict if the file is absent.

    Raises ValueError on malformed lines (missing '=') so silent corruption
    of a credential file is surfaced loudly.
    """
    path = Path(path)
    if not path.is_file():
        return {}

    result: Dict[str, str] = {}
    with path.open("r", encoding="utf-8") as f:
        for lineno, raw in enumerate(f, start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                raise ValueError(f"Malformed .env line {lineno} in {path}: missing '='")
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            result[key.strip()] = value
    return result


def write_env_file(path: Path, values: Dict[str, str]) -> None:
    """Write KEY=VALUE pairs to a .env file with restricted permissions.

    Creates parent directories as needed. On POSIX, the file is created with
    mode 0600 (owner read/write only) at creation time -- never a post-hoc
    chmod, so the key is never world-readable, even briefly. On Windows, the
    mode argument is largely ignored and the default ACL of paths under the
    user profile already restricts access to the current user, so we do not
    add explicit ACL manipulation.

    Existing keys not in ``values`` are dropped -- this is for a small,
    plugin-managed credential file, not a general-purpose .env editor.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    body = "".join(f"{k}={v}\n" for k, v in values.items())
    # Write atomically: write to a temp file in the same directory, then
    # rename. Prevents a half-written credential file if the process dies.
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(body)
    os.replace(tmp, path)
