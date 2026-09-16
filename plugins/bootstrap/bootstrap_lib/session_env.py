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
- **Removals are explicit.** ``forget`` is the one way a name leaves the
  block (an opted-out project's ``BOOTSTRAP_PROJECT_PYTHON``); a name that is
  merely not recorded this pass keeps its earlier line.
- **Atomic.** The block is written to a sibling temporary file and moved over
  the env file with ``os.replace``, so a reader observes one complete block
  or the other, never a truncated file. The temporary name (``TMP_PREFIX`` +
  basename + ``TMP_SUFFIX``, e.g. ``.sessionstart-hook-0.sh.bootstrap-tmp``)
  starts with a dot and does not end in ``.sh``, so it never matches
  ``sessionstart-hook-*`` (which ``<env_file>.bootstrap-tmp`` would). When
  the replace fails -- on Windows a reader holding the env file open makes it
  raise -- the block is written directly instead: exports are preserved,
  never lost.
- **One encoding.** UTF-8 with ``\\n`` endings, rather than the platform
  default that differed between call sites.
"""

import os
import re
import shlex
from collections import OrderedDict
from typing import Dict, Optional

_EXPORT_RE = re.compile(r"^export ([A-Za-z_][A-Za-z0-9_]*)=")

TMP_PREFIX = "."
TMP_SUFFIX = ".bootstrap-tmp"

# name -> value, in first-seen order, for the pass currently running.
_pending: "OrderedDict[str, str]" = OrderedDict()

# Names this pass decided must NOT be exported; flush drops their lines.
_forgotten: "set[str]" = set()


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


def forget(name: str) -> Optional[str]:
    """Buffer the REMOVAL of ``name`` from the block for this pass.

    Cancels a buffered ``record`` of the same name, and makes ``flush`` drop
    an ``export NAME=`` line an earlier writer (a previous pass, the
    SessionStart hook prelude) left in the file. A later ``record`` of the
    name still wins: ``flush`` drops forgotten lines before it writes the
    buffered exports. Same no-op rule and return value as ``record``.
    """
    if not os.environ.get("CLAUDE_ENV_FILE"):
        return None
    _pending.pop(name, None)
    _forgotten.add(name)
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
    that exported nothing never erases an earlier pass's work. A pass that only
    ``forget``-s names rewrites the file only when it holds one of them.

    Returns:
        The number of export lines written, or 0 when nothing was written.
    """
    env_file = os.environ.get("CLAUDE_ENV_FILE")
    if not env_file or not (_pending or _forgotten):
        return 0

    lines = _read_existing(env_file)
    if not _pending and not any(name in lines for name in _forgotten):
        _forgotten.clear()
        return 0
    for name in _forgotten:
        lines.pop(name, None)
    for name, value in _pending.items():
        lines[name] = "export {}={}\n".format(name, shlex.quote(value))
        lines.move_to_end(name)

    content = "".join(lines.values())
    if not _write_block(env_file, content):
        return 0

    _pending.clear()
    _forgotten.clear()
    return len(lines)


def tmp_path_for(env_file: str) -> str:
    """The sibling temporary file ``flush`` writes before ``os.replace``."""
    directory, base = os.path.split(env_file)
    return os.path.join(directory, TMP_PREFIX + base + TMP_SUFFIX)


def _write_direct(env_file: str, content: str) -> bool:
    try:
        with open(env_file, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
    except OSError:
        return False
    return True


def _write_block(env_file: str, content: str) -> bool:
    """Temp file + ``os.replace``; the direct write is the fallback.

    The fallback exists because a failed replace must not cost the session its
    exports: the direct write is the pre-atomic behaviour, which a reader can
    at worst observe mid-write, whereas skipping the write would lose every
    export this pass earned.
    """
    tmp = tmp_path_for(env_file)
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        os.replace(tmp, env_file)
        return True
    except OSError:
        pass
    try:
        os.remove(tmp)
    except OSError:
        pass
    return _write_direct(env_file, content)


def reset() -> None:
    """Drop the buffer without writing. For tests."""
    _pending.clear()
    _forgotten.clear()
