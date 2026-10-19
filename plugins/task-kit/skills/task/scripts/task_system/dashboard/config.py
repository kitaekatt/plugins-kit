"""The dashboard's user/machine choices, read from ``~/.claude/task.local.yaml``.

Only the ``dashboard:`` mapping is read here (``project_directories`` belongs
to discovery). Every key has a default; an unknown key, a wrong type or a
value out of range is an error naming the file, never a silent default.

Per-run overrides (model, effort, network, timeout) are stored in runtime
state only; these are the machine-wide defaults they override.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from ..discovery import TASK_CONFIG_PATH

WORKERS = ("claude", "codex")

DEFAULT_TIMEOUT_S = 7200.0
DEFAULT_CLAUDE_ENTRY = "opus"
DEFAULT_CODEX_ENTRY = "sol"


class DashboardConfigError(ValueError):
    """``task.local.yaml`` has an unusable ``dashboard`` section."""


@dataclass(frozen=True)
class DashboardConfig:
    max_parallel_projects: int = 2
    default_timeout_s: float = DEFAULT_TIMEOUT_S
    default_worker: str = "claude"
    claude_entry: str = DEFAULT_CLAUDE_ENTRY
    codex_entry: str = DEFAULT_CODEX_ENTRY
    effort: str | None = None
    codex_network: bool = False

    def entry_for(self, worker: str) -> str:
        if worker == "claude":
            return self.claude_entry
        if worker == "codex":
            return self.codex_entry
        raise DashboardConfigError(f"worker {worker!r} is not one of {WORKERS}")


_KEYS = {
    "max_parallel_projects", "default_timeout_s", "default_worker",
    "claude_entry", "codex_entry", "effort", "codex_network",
}


def load_config(path: Path | None = None) -> DashboardConfig:
    """The ``dashboard:`` section of ``task.local.yaml`` (defaults when absent)."""
    path = Path(path) if path is not None else TASK_CONFIG_PATH
    if not path.exists():
        return DashboardConfig()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise DashboardConfigError(f"cannot read {path}: {exc}") from exc
    if data is None:
        return DashboardConfig()
    if not isinstance(data, dict):
        raise DashboardConfigError(f"{path}: the file must be a mapping")
    section = data.get("dashboard")
    if section is None:
        return DashboardConfig()
    if not isinstance(section, dict):
        raise DashboardConfigError(f"{path}: dashboard must be a mapping")
    unknown = sorted(set(section) - _KEYS)
    if unknown:
        raise DashboardConfigError(f"{path}: unknown dashboard keys {unknown}; known keys: {sorted(_KEYS)}")

    def pick(key: str, kind: type | tuple, default):
        if key not in section:
            return default
        value = section[key]
        if isinstance(value, bool) and kind is not bool:
            raise DashboardConfigError(f"{path}: dashboard.{key} has the wrong type")
        if not isinstance(value, kind):
            raise DashboardConfigError(f"{path}: dashboard.{key} has the wrong type")
        return value

    parallel = pick("max_parallel_projects", int, 2)
    timeout = pick("default_timeout_s", (int, float), DEFAULT_TIMEOUT_S)
    worker = pick("default_worker", str, "claude")
    if parallel < 1:
        raise DashboardConfigError(f"{path}: dashboard.max_parallel_projects must be at least 1")
    if timeout <= 0:
        raise DashboardConfigError(f"{path}: dashboard.default_timeout_s must be positive")
    if worker not in WORKERS:
        raise DashboardConfigError(f"{path}: dashboard.default_worker must be one of {WORKERS}")
    return DashboardConfig(
        max_parallel_projects=parallel,
        default_timeout_s=float(timeout),
        default_worker=worker,
        claude_entry=pick("claude_entry", str, DEFAULT_CLAUDE_ENTRY),
        codex_entry=pick("codex_entry", str, DEFAULT_CODEX_ENTRY),
        effort=pick("effort", str, None),
        codex_network=pick("codex_network", bool, False),
    )
