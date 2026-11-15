"""The ``manage:`` section of ``~/.claude/task.local.yaml``: the machine's choices about the manage CLI.

Keys (optional):

- ``commit`` (bool, default true): ``false`` stops a semantic mutation from committing the task files it wrote.
  The edit is still written; the result reports ``commit: null`` and the files stay uncommitted for the user.

An unknown key or a wrong type is an error naming the file, never a silent default. The ``sessions:`` and
``dashboard:`` sections live in the same file and are read elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from . import discovery

KEYS = ("commit",)


class ManageConfigError(ValueError):
    """``task.local.yaml`` has an unusable ``manage`` section."""


@dataclass(frozen=True)
class ManageConfig:
    commit: bool = True


def load_manage_config(path: Path | None = None) -> ManageConfig:
    """The ``manage:`` section of ``task.local.yaml`` (defaults when the file or the section is absent)."""
    path = Path(path) if path is not None else discovery.TASK_CONFIG_PATH
    if not path.exists():
        return ManageConfig()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ManageConfigError(f"cannot read {path}: {exc}") from exc
    if data is None:
        return ManageConfig()
    if not isinstance(data, dict):
        raise ManageConfigError(f"{path}: the file must be a mapping")
    section = data.get("manage")
    if section is None:
        return ManageConfig()
    if not isinstance(section, dict):
        raise ManageConfigError(f"{path}: manage must be a mapping")
    unknown = sorted(str(key) for key in set(section) - set(KEYS))
    if unknown:
        raise ManageConfigError(f"{path}: unknown manage keys {unknown}; known keys: {sorted(KEYS)}")
    commit = section.get("commit", True)
    if not isinstance(commit, bool):
        raise ManageConfigError(f"{path}: manage.commit must be true or false")
    return ManageConfig(commit=commit)
