"""Depth-limited cwd-downward directory walk shared by the audit discover
scripts (md-domain/scripts/discover_claude_md.py, md-domain/scripts/discover_skill.py).

One walk implementation, one skip-list. The walk prunes noise directories
(VCS internals, venvs, build output, caches) and every dot-directory except
`.claude` (the one dot-dir the audits must descend into), and never descends
a symlink or a Windows junction.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

# Directory basenames pruned from the walk. Entries are matched against a
# single path component (os.walk dir names), so they must be bare names.
SKIP_DIR_NAMES = {
    ".git", ".venv", "node_modules", "__pycache__", ".pytest_cache",
    "Intermediate", "Saved", "Binaries", "DerivedDataCache", "Build",
    "tmp",
}


def is_link_dir(path: os.PathLike | str) -> bool:
    """True when `path` is a symlink or a Windows junction (a mount-point
    reparse point). `os.path.islink` alone misses a junction."""
    return os.path.islink(path) or os.path.isjunction(path)


def iter_dirs(
    cwd: Path,
    max_depth: int,
    include_dirs: Iterable[str] = (),
    skipped_out: list[Path] | None = None,
) -> Iterator[tuple[Path, list[str]]]:
    """Yield (directory_path, filenames) for cwd and its descendants.

    - Depth is measured in path components below cwd; cwd itself is depth 0.
      Directories deeper than max_depth are pruned.
    - Skips SKIP_DIR_NAMES and dot-directories, except `.claude`.
    - `include_dirs` names directories that bypass the name-based pruning above
      (not the link pruning below) for this call -- a caller whose own first-party directory happens to share a
      noise name (`Build/`, `tmp/`) can opt it back in without editing
      SKIP_DIR_NAMES for everyone else.
    - A directory entry that is a symlink or a Windows junction is never
      descended into, and `include_dirs` does NOT override that: it bypasses
      the name-based pruning only. `os.walk(followlinks=False)` already skips
      a symlink, but it descends into a junction (`os.path.islink` is False
      for one), so the link test is explicit. The walk root itself is not an
      entry and is never pruned -- naming a link as the root is the caller's
      explicit choice.
    - `skipped_out`, when given, has each pruned directory's path appended to
      it as it is pruned, links included (the depth-limit prune is a separate
      boundary and is NOT recorded here) -- so a caller can report what it
      silently walked past instead of dropping the information. Use
      `is_link_dir` to tell a pruned link from a pruned noise name.
    """
    include = set(include_dirs)
    for current_root, dirs, files in os.walk(cwd):
        current_path = Path(current_root)
        kept: list[str] = []
        for d in dirs:
            if is_link_dir(current_path / d):
                if skipped_out is not None:
                    skipped_out.append(current_path / d)
            elif d in include or (d not in SKIP_DIR_NAMES and not d.startswith(".")) or d == ".claude":
                kept.append(d)
            elif skipped_out is not None:
                skipped_out.append(current_path / d)
        dirs[:] = kept
        try:
            rel = current_path.relative_to(cwd)
            depth = len(rel.parts)
        except ValueError:
            depth = max_depth + 1
        if depth > max_depth:
            dirs[:] = []
            continue
        yield current_path, files
