"""Shared --include-dir resolution for the md-domain discover scripts."""
from __future__ import annotations

import os

INCLUDE_DIRS_ENV = "MD_DOMAIN_INCLUDE_DIRS"


def resolve_include_dirs(cli_values: list[str] | None) -> list[str]:
    """--include-dir values, falling back to the MD_DOMAIN_INCLUDE_DIRS
    environment variable (os.pathsep-separated) when the flag was not passed.
    One variable opts a directory back in for every md-domain discover script."""
    if cli_values:
        return cli_values
    env = os.environ.get(INCLUDE_DIRS_ENV)
    return [name for name in env.split(os.pathsep) if name] if env else []
