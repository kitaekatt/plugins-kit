"""Root-walk config loader with mtime-invalidated caching.

Finds a project's config root by walking upward from a starting directory
until a marker file is found (the config-in-charge convention: config lives
at a well-known root, not scattered per-caller paths). Loaded config is
cached in-process; the cache is invalidated by the source file's mtime, not
by an explicit clear call, so a config edit between pipeline runs is picked
up automatically without paying a re-parse on every access.

Hashing for downstream change-detection (e.g. "has this config changed since
the last regen") strips doc-block comments before hashing, so editing
documentation inside a config file does not appear as a content change to
anything keying off the hash.
"""

from pathlib import Path


def find_config_root(start: Path, marker: str) -> Path:
    """Walk upward from ``start`` until a directory containing ``marker`` is found.

    Raises FileNotFoundError if no ancestor directory contains the marker.
    """
    raise NotImplementedError


def load_config(root: Path, filename: str) -> dict:
    """Load and cache the config file at ``root / filename``, mtime-invalidated."""
    raise NotImplementedError


def content_hash(text: str) -> str:
    """Hash ``text`` after stripping doc-block comments, for change detection."""
    raise NotImplementedError
