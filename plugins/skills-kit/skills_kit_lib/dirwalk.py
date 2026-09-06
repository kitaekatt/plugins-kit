"""Depth-limited cwd-downward directory walk shared by the audit discover
scripts (md-domain/scripts/discover_claude_md.py, md-domain/scripts/discover_skill.py).

One walk implementation, one skip-list. The walk prunes noise directories
(VCS internals, venvs, build output, caches) and every dot-directory except
`.claude` (the one dot-dir the audits must descend into).
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
    - `include_dirs` names directories that bypass ALL of the above pruning for
      this call -- a caller whose own first-party directory happens to share a
      noise name (`Build/`, `tmp/`) can opt it back in without editing
      SKIP_DIR_NAMES for everyone else.
    - `skipped_out`, when given, has each pruned directory's path appended to
      it as it is pruned (the depth-limit prune is a separate boundary and is
      NOT recorded here) -- so a caller can report what it silently walked
      past instead of dropping the information.
    """
    include = set(include_dirs)
    for current_root, dirs, files in os.walk(cwd):
        current_path = Path(current_root)
        kept: list[str] = []
        for d in dirs:
            if d in include or (d not in SKIP_DIR_NAMES and not d.startswith(".")) or d == ".claude":
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
