"""The ``sessions:`` section of ``~/.claude/task.local.yaml``: the machine's choices about working-session records.

Keys (both optional):

- ``record`` (bool, default true): ``false`` turns recording off outright -- ``task.py work`` registers nothing and
  publishes nothing, and says so on a ``session:`` line.
- ``host`` (string): replaces the host id derived from the host name, to resolve a real name clash between two
  machines (see ``machine.host_id``).

An unknown key or a wrong type is an error naming the file, never a silent default. The dashboard's own section
(``dashboard:``) and discovery's ``project_directories`` live in the same file and are read elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from . import discovery

KEYS = ("record", "host")


class SessionsConfigError(ValueError):
    """``task.local.yaml`` has an unusable ``sessions`` section."""


@dataclass(frozen=True)
class SessionsConfig:
    record: bool = True
    host: str | None = None


def load_sessions_config(path: Path | None = None) -> SessionsConfig:
    """The ``sessions:`` section of ``task.local.yaml`` (defaults when the file or the section is absent)."""
    path = Path(path) if path is not None else discovery.TASK_CONFIG_PATH
    if not path.exists():
        return SessionsConfig()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise SessionsConfigError(f"cannot read {path}: {exc}") from exc
    if data is None:
        return SessionsConfig()
    if not isinstance(data, dict):
        raise SessionsConfigError(f"{path}: the file must be a mapping")
    section = data.get("sessions")
    if section is None:
        return SessionsConfig()
    if not isinstance(section, dict):
        raise SessionsConfigError(f"{path}: sessions must be a mapping")
    unknown = sorted(str(key) for key in set(section) - set(KEYS))
    if unknown:
        raise SessionsConfigError(f"{path}: unknown sessions keys {unknown}; known keys: {sorted(KEYS)}")
    record = section.get("record", True)
    if not isinstance(record, bool):
        raise SessionsConfigError(f"{path}: sessions.record must be true or false")
    host = section.get("host")
    if host is not None and (not isinstance(host, str) or not host.strip()):
        raise SessionsConfigError(f"{path}: sessions.host must be a nonempty string")
    return SessionsConfig(record=record, host=host.strip() if host is not None else None)
