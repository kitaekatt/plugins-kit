#!/usr/bin/env python3
"""project_root.py -- the one VCS-agnostic project-root walk the discover scripts share.

A project root is the nearest ancestor (inclusive) holding a marker that says
"a project starts here". The marker set is deliberately NOT git-only: an audited
project may be a Perforce workspace (`.p4config.txt` at its root and no `.git`
anywhere), and a git-only walk returns None for every directory inside it. That
None is not inert -- callers that scope a walk to the project treat it as "no
project", which silently truncates ancestor collection to the named directory
itself.

Only ONE resolver in the md-domain scripts is intentionally git-only:
`discover_claude_md.find_project_root`, whose boundary is the git repository
because the claude-md audit lane's ancestor scope is defined that way. Everything
whose question is "where does this PROJECT start" imports from here.

Stdlib-only.
"""

from pathlib import Path

# Project-root markers, VCS-agnostic: git, mercurial, svn, AND perforce
# (.p4config.txt) -- the audited project may not be a git repo.
PROJECT_MARKERS = (".git", ".hg", ".svn", ".p4config.txt")


def find_project_root(start: Path) -> Path | None:
    """Nearest ancestor of `start` (inclusive) holding a project marker, else None.

    None means the path is genuinely outside any project, which callers use as a
    hard stop on upward walks -- never as a reason to widen the search.
    """
    current = start if start.is_dir() else start.parent
    while True:
        if any((current / marker).exists() for marker in PROJECT_MARKERS):
            return current
        if current == current.parent:
            return None
        current = current.parent
