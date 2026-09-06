"""Minimal KEY=VALUE .env reader/writer.

Mirrors the parsing rules of loc-ops's existing ``load_env`` helper so the
two formats stay interoperable: blank/comment lines skipped, ``key=value``
with optional surrounding double or single quotes on the value. A leading
``export `` (the shell idiom, e.g. ``export KEY=value``) is stripped before
parsing.

We do NOT implement variable interpolation or multi-line values -- the .env
files this plugin manages contain a single API key.

``write_env_file`` double-quotes a value exactly when it needs it (leading or
trailing whitespace, or an embedded ``"`` / ``\\``), escaping ``\\`` and ``"``
so ``read_env_file`` round-trips it byte-identically. A value needing no
protection is written bare, so a hand-edited or existing plain file stays
diffable.
"""

import os
from pathlib import Path
from typing import Dict


def _needs_quoting(value: str) -> bool:
    """True when ``value`` cannot round-trip through the bare KEY=VALUE form."""
    if value != value.strip():
        return True
    return '"' in value or "\\" in value


def _quote_value(value: str) -> str:
    """Double-quote ``value``, escaping ``\\`` and ``"``.

    Backslash is escaped FIRST so a literal backslash already present in the
    value is not mistaken for the start of an escape sequence this function
    itself just introduced.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _unquote_double(inner: str) -> str:
    """Undo :func:`_quote_value` on ``inner`` (the content between the quotes).

    A single left-to-right pass, not a blind ``str.replace`` chain: replacing
    ``\\"`` then ``\\\\`` (or the reverse order) misreads a value that itself
    contains a backslash immediately followed by a quote, because each
    ``replace`` call cannot tell an escape sequence it should consume from one
    a previous replace call already produced.
    """
    out = []
    i = 0
    n = len(inner)
    while i < n:
        ch = inner[i]
        if ch == "\\" and i + 1 < n and inner[i + 1] in ('"', "\\"):
            out.append(inner[i + 1])
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


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
            # The `export KEY=value` shell idiom -- strip it before splitting
            # so it does not silently become the key "export KEY". Only the
            # bare keyword followed by whitespace counts; a key that
            # genuinely starts with "export" (no space) is left alone.
            if line.startswith("export ") or line.startswith("export\t"):
                line = line[len("export"):].lstrip()
            if "=" not in line:
                raise ValueError(f"Malformed .env line {lineno} in {path}: missing '='")
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                if value[0] == '"':
                    # Undo exactly the escaping write_env_file applies to a
                    # double-quoted value, so a reader accepts whatever the
                    # writer emits, byte-identically.
                    value = _unquote_double(value[1:-1])
                else:
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

    body = "".join(
        f"{k}={_quote_value(v) if _needs_quoting(v) else v}\n" for k, v in values.items()
    )
    # Write atomically: write to a temp file in the same directory, then
    # rename. Prevents a half-written credential file if the process dies.
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(body)
    os.replace(tmp, path)
