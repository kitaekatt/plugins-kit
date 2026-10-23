"""Shell startup-file discovery shared by persistent shell features."""

import os
from pathlib import Path


BASH_LOGIN_FILENAMES = (".bash_profile", ".bash_login", ".profile")


def bash_login_candidates(home: Path) -> tuple[Path, ...]:
    """Return Bash login files in Bash's documented precedence order."""
    return tuple(home / name for name in BASH_LOGIN_FILENAMES)


def effective_bash_login_file(home: Path) -> Path:
    """Select the first existing readable Bash login file.

    Creating ``.profile`` when none is readable avoids shadowing a user's
    existing Bash-specific login file.
    """
    for candidate in bash_login_candidates(home):
        if candidate.is_file() and os.access(candidate, os.R_OK):
            return candidate
    return home / ".profile"
