"""Atomic file writes shared across bootstrap modules.

Single implementation of the tmp-file + ``os.replace`` pattern. The temp
file is created with ``tempfile.mkstemp`` in the *destination's* directory:

- same directory => ``os.replace`` is a same-filesystem rename (atomic; a
  fixed system tmp dir could be a different filesystem -> EXDEV on Linux),
- ``mkstemp`` => a random name, so concurrent writers never collide on a
  fixed ``path + ".tmp"``.

Previously engine.py carried its own fixed-name ``_write_atomic`` copy and
tool_paths.py carried the mkstemp one; this module unifies them (B13).
"""

import os
import tempfile


def write_atomic(path, content, newline=None):
    """Write ``content`` (text) to ``path`` atomically via mkstemp + os.replace.

    Creates the parent directory if missing. On any failure the temp file is
    removed and the exception re-raised; the destination is either fully
    updated or untouched.

    ``newline`` is passed through to the writer (same semantics as ``open``).
    The default (None) keeps platform newline translation, unchanged for all
    existing callers. Pass ``newline="\\r\\n"`` for files that must be CRLF on
    every platform (e.g. Windows ``.bat`` scripts, whose body is authored with
    plain ``\\n``) -- mirroring python_stub_check's fix-script writer. Without
    this, a body pre-joined with ``\\r\\n`` would be mangled to ``\\r\\r\\n`` by
    text-mode translation on Windows.
    """
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".atomic.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline=newline) as f:
            f.write(content)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
