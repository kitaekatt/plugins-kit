"""The dashboard's user/machine choices, read from ``~/.claude/task.local.yaml``.

Only the ``dashboard:`` mapping is read here (``project_directories`` belongs
to discovery). Every key has a default; an unknown key, a wrong type or a
value out of range is an error naming the file, never a silent default.

Per-run overrides (model, effort, network, timeout) are stored in runtime
state only; these are the machine-wide defaults they override.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from ..discovery import TASK_CONFIG_PATH
from . import repo_identity

WORKERS = ("claude", "codex")

DEFAULT_TIMEOUT_S = 7200.0
DEFAULT_CLAUDE_ENTRY = "opus"
DEFAULT_CODEX_ENTRY = "sol"

HELPER_NAME = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
DEFAULT_HELPER_MAX_CALLS = 20
DEFAULT_HELPER_TIMEOUT_S = 300.0
DEFAULT_HELPER_MAX_INPUT_CHARS = 100_000
MAX_HELPER_INPUT_CHARS = 1_000_000
MAX_HELPER_PURPOSE_CHARS = 200


class DashboardConfigError(ValueError):
    """``task.local.yaml`` has an unusable ``dashboard`` section."""


@dataclass(frozen=True)
class EndpointHelper:
    """One configured endpoint helper: a text-only model a run may ask, for the repositories that consent to it.

    ``repositories`` holds consent keys (``repo_identity.config_key``): the normalized origin URL, or
    ``path:<realpath>`` for a checkout with no origin. ``max_input_chars`` limits the input and the system
    text together.
    """

    name: str
    models: tuple[str, ...]
    repositories: tuple[str, ...]
    purpose: str = ""
    max_calls: int = DEFAULT_HELPER_MAX_CALLS
    timeout_s: float = DEFAULT_HELPER_TIMEOUT_S
    max_input_chars: int = DEFAULT_HELPER_MAX_INPUT_CHARS

    def offered_to(self, repo_key: str) -> bool:
        return repo_key in self.repositories


@dataclass(frozen=True)
class DashboardConfig:
    max_parallel_projects: int = 2
    default_timeout_s: float = DEFAULT_TIMEOUT_S
    default_worker: str = "claude"
    claude_entry: str = DEFAULT_CLAUDE_ENTRY
    codex_entry: str = DEFAULT_CODEX_ENTRY
    effort: str | None = None
    codex_network: bool = False
    endpoint_helpers: tuple[EndpointHelper, ...] = ()

    def entry_for(self, worker: str) -> str:
        if worker == "claude":
            return self.claude_entry
        if worker == "codex":
            return self.codex_entry
        raise DashboardConfigError(f"worker {worker!r} is not one of {WORKERS}")


_KEYS = {
    "max_parallel_projects", "default_timeout_s", "default_worker",
    "claude_entry", "codex_entry", "effort", "codex_network", "endpoint_helpers",
}
_HELPER_KEYS = {"models", "repositories", "purpose", "max_calls", "timeout_s", "max_input_chars"}


def _helpers(path: Path, value) -> tuple[EndpointHelper, ...]:
    """``dashboard.endpoint_helpers`` as validated helpers (a mapping of name to helper settings)."""
    where = f"{path}: dashboard.endpoint_helpers"
    if value is None:
        return ()
    if not isinstance(value, dict):
        raise DashboardConfigError(f"{where} must be a mapping of helper name to its settings")
    helpers = []
    for name, body in value.items():
        label = f"{where}.{name}"
        if not isinstance(name, str) or not HELPER_NAME.match(name):
            raise DashboardConfigError(f"{where}: helper name {name!r} must match {HELPER_NAME.pattern}")
        if name in WORKERS:
            raise DashboardConfigError(f"{label}: {name!r} is a task worker; a helper never works on a task")
        if not isinstance(body, dict):
            raise DashboardConfigError(f"{label} must be a mapping")
        unknown = sorted(set(body) - _HELPER_KEYS)
        if unknown:
            raise DashboardConfigError(f"{label}: unknown keys {unknown}; known keys: {sorted(_HELPER_KEYS)}")
        models = body.get("models")
        if (not isinstance(models, list) or not models
                or not all(isinstance(m, str) and m.strip() for m in models) or len(set(models)) != len(models)):
            raise DashboardConfigError(f"{label}.models must be a non-empty list of distinct model entry ids")
        repositories = body.get("repositories")
        if not isinstance(repositories, list) or not repositories or not all(isinstance(r, str) and r.strip() for r in repositories):
            raise DashboardConfigError(
                f"{label}.repositories must be a non-empty list of repository origin URLs "
                "(or absolute checkout paths for a checkout with no origin)")
        keys: list[str] = []
        for text in repositories:
            try:
                key = repo_identity.config_key(text)
            except ValueError as exc:
                raise DashboardConfigError(f"{label}.repositories: {exc}") from exc
            if key not in keys:
                keys.append(key)
        purpose = body.get("purpose", "")
        if not isinstance(purpose, str) or "\n" in purpose.strip() or len(purpose.strip()) > MAX_HELPER_PURPOSE_CHARS:
            raise DashboardConfigError(f"{label}.purpose must be one line of at most {MAX_HELPER_PURPOSE_CHARS} characters")
        max_calls = body.get("max_calls", DEFAULT_HELPER_MAX_CALLS)
        if isinstance(max_calls, bool) or not isinstance(max_calls, int) or max_calls < 1:
            raise DashboardConfigError(f"{label}.max_calls must be a whole number of at least 1")
        timeout = body.get("timeout_s", DEFAULT_HELPER_TIMEOUT_S)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise DashboardConfigError(f"{label}.timeout_s must be a positive number")
        limit = body.get("max_input_chars", DEFAULT_HELPER_MAX_INPUT_CHARS)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_HELPER_INPUT_CHARS:
            raise DashboardConfigError(f"{label}.max_input_chars must be a whole number from 1 to {MAX_HELPER_INPUT_CHARS}")
        helpers.append(EndpointHelper(
            name=name, models=tuple(m.strip() for m in models), repositories=tuple(keys), purpose=purpose.strip(),
            max_calls=max_calls, timeout_s=float(timeout), max_input_chars=limit))
    return tuple(helpers)


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
        endpoint_helpers=_helpers(path, section["endpoint_helpers"]) if "endpoint_helpers" in section else (),
    )
