"""Depth-limited cwd-downward directory walk shared by the audit discover
scripts (claude-md-audit/scripts/discover.py, skill-audit/scripts/discover.py).

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


def iter_dirs(cwd: Path, max_depth: int) -> Iterator[tuple[Path, list[str]]]:
    """Yield (directory_path, filenames) for cwd and its descendants.

    - Depth is measured in path components below cwd; cwd itself is depth 0.
      Directories deeper than max_depth are pruned.
    - Skips SKIP_DIR_NAMES and dot-directories, except `.claude`.
    """
    for current_root, dirs, files in os.walk(cwd):
        current_path = Path(current_root)
        # In-place filter: keep d when (not a skip name AND not hidden) OR
        # it is .claude -- the one dot-dir the audits descend into.
        dirs[:] = [
            d for d in dirs
            if (d not in SKIP_DIR_NAMES and not d.startswith(".")) or d == ".claude"
        ]
        try:
            rel = current_path.relative_to(cwd)
            depth = len(rel.parts)
        except ValueError:
            depth = max_depth + 1
        if depth > max_depth:
            dirs[:] = []
            continue
        yield current_path, files
