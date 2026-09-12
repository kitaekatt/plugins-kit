"""The pass's ``$CLAUDE_ENV_FILE`` block: buffered, deduplicated, written once.

Claude Code splices every session-env hook file into the FRONT of the
``bash -c`` string it builds for each Bash tool call. The block is therefore
charged against the command-line length limit on every command the user runs,
not just once at session start.

The engine clears ``filechanged-hook-*`` and ``cwdchanged-hook-*`` files
between passes, but NOT ``sessionstart-hook-*``. So appending one line per
exported variable, on every pass, grows the block without bound. Past roughly
8 KB on Windows the assembled command line is cut mid-string, and the cut
surfaces as an error on whatever command the user happened to run:

    /usr/bin/bash: -c: line 94: unexpected EOF while looking for matching `''
    /usr/bin/bash: line 94: /c/Users/<user>/AppData/: Is a directory

Neither error names this file, and the file on disk stays complete and
quote-balanced, so the cause is easy to misread as a quoting fault in the
user's own command.

Measured 2026-09-11 on a Windows 11 box: a 7,739-byte block left about 56
bytes of headroom, so ``echo probe`` still succeeded and anything longer
failed. Deduplicating the block reclaimed 1,484 bytes and the failures
stopped.

This module therefore buffers a pass's exports and writes the block ONCE, at
the end of the pass:

- **Deduplicated.** One line per NAME, last value wins. This is what bounds
  the size.
- **Merged, not replaced.** A partial pass must not drop variables an earlier
  pass exported, so the existing block is read back and overlaid rather than
  truncated blindly. Lines that do not parse as ``export NAME=...`` are
  dropped, which also repairs a block damaged by an earlier partial write.
- **One write.** A single ``write()`` removes the window in which a reader can
  observe a half-written file.
- **One encoding.** UTF-8 with ``\\n`` endings, rather than the platform
  default that differed between call sites.
"""

import os
import re
import shlex
from collections import OrderedDict
from typing import Dict, Optional

_EXPORT_RE = re.compile(r"^export ([A-Za-z_][A-Za-z0-9_]*)=")

# name -> value, in first-seen order, for the pass currently running.
_pending: "OrderedDict[str, str]" = OrderedDict()


def record(name: str, value: str) -> Optional[str]:
    """Buffer one ``export`` for this pass.

    No-ops (returning ``None``) when ``CLAUDE_ENV_FILE`` is unset or empty --
    the console and test paths -- so callers keep the same "did we export it"
    signal they had when each of them wrote the file directly.

    Returns:
        ``name`` when the export was buffered, else ``None``.
    """
    if not os.environ.get("CLAUDE_ENV_FILE"):
        return None
    _pending[name] = value
    _pending.move_to_end(name)
    return name


def _read_existing(env_file: str) -> "Dict[str, str]":
    """Existing ``export NAME=...`` lines, keyed by NAME, last occurrence wins.

    A line is kept only when it BOTH looks like an export and survives
    ``shlex.split``. The shape test alone is not enough: ``export X='oops``
    matches the pattern and would be carried forward for the rest of the
    session, and that single unbalanced quote is the whole failure mode this
    module exists to prevent. Parsing also repairs a block damaged by an
    earlier partial write, since the damaged line simply does not survive.
    """
    kept: "OrderedDict[str, str]" = OrderedDict()
    try:
        with open(env_file, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.rstrip("\r\n")
                match = _EXPORT_RE.match(line)
                if not match:
                    continue
                try:
                    shlex.split(line)
                except ValueError:
                    continue  # unbalanced quote: never carry it forward
                kept[match.group(1)] = line + "\n"
                kept.move_to_end(match.group(1))
    except OSError:
        return OrderedDict()
    return kept


def flush() -> int:
    """Write this pass's block, merged over whatever the file already held.

    Safe to call when nothing was recorded: the file is left alone, so a pass
    that exported nothing never erases an earlier pass's work.

    Returns:
        The number of export lines written, or 0 when nothing was written.
    """
    env_file = os.environ.get("CLAUDE_ENV_FILE")
    if not env_file or not _pending:
        return 0

    lines = _read_existing(env_file)
    for name, value in _pending.items():
        lines[name] = "export {}={}\n".format(name, shlex.quote(value))
        lines.move_to_end(name)

    try:
        with open(env_file, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("".join(lines.values()))
    except OSError:
        return 0

    _pending.clear()
    return len(lines)


def reset() -> None:
    """Drop the buffer without writing. For tests."""
    _pending.clear()
