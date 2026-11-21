"""Validated, repository-approved lifecycle settings; user-only disk policy."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator

import worktrees as wt


def home_config() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def read_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return data


def relative_path(value: str) -> str:
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts or value.startswith("-") or "\\" in value or ":" in value or value == ".":
        raise ValueError(f"Expected a path inside the worktree: {value!r}")
    if path.parts[0] == ".git":
        raise ValueError(".git is not a setup output or input")
    return value


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")
    argv: list[str] = Field(min_length=1)
    path_bound: StrictBool = False
    recreate: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_paths(self) -> Step:
        if any(not arg or "\0" in arg for arg in self.argv):
            raise ValueError("argv requires nonempty arguments without NUL")
        for value in self.recreate:
            relative_path(value)
        return self


class Pool(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    size: StrictInt = Field(default=2, ge=1)
    setup: list[Step] = Field(min_length=1)
    rerun_files: dict[str, list[str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def check_map(self) -> Pool:
        ids = [step.id for step in self.setup]
        if len(set(ids)) != len(ids):
            raise ValueError("setup step IDs must be unique")
        for path, selected in self.rerun_files.items():
            relative_path(path)
            if not selected or len(selected) != len(set(selected)) or set(selected) - set(ids):
                raise ValueError(f"rerun_files.{path}: expected distinct known setup step IDs")
        outputs = [p for step in self.setup for p in step.recreate]
        for i, path in enumerate(outputs):
            if any(Path(path) == Path(other) or Path(path).is_relative_to(other) or Path(other).is_relative_to(path)
                   for other in outputs[:i]):
                raise ValueError("setup recreate paths must not overlap")
        return self


class RepositoryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: StrictBool = False
    pool: Pool | None = None

    @model_validator(mode="after")
    def check_enablement(self) -> RepositoryConfig:
        if self.enabled and self.pool is None:
            raise ValueError("Enabled repository needs approved pool settings; run configure --propose")
        return self


def validate_config(raw: dict[str, Any], repo: Path) -> RepositoryConfig:
    config = RepositoryConfig.model_validate(raw)
    if config.pool is None:
        return config
    tracked = set(wt.git(repo, "ls-files", "-z").split("\0"))
    for path in config.pool.rerun_files:
        if path not in tracked:
            raise ValueError(f"rerun_files.{path}: input must be tracked")
    for step in config.pool.setup:
        for path in step.recreate:
            if any(p == path or p.startswith(path.rstrip("/") + "/") for p in tracked if p):
                raise ValueError(f"setup.{step.id}.recreate: tracked data cannot be reproduced: {path}")
    return config


@dataclass(frozen=True)
class Settings:
    raw: dict[str, Any] | None
    config: RepositoryConfig | None
    disk_floor_percent: float


def load(repo: Path) -> Settings:
    # Repository enablement cannot be inherited from user or local overrides.
    manifest = read_object(repo / ".claude/bootstrap.json")
    raw = manifest.get("git_kit", {}).get("worktrees")
    config = validate_config(raw, repo) if raw is not None else None
    floor: float = 10
    for name in ("bootstrap.json", "bootstrap.local.json"):
        value = read_object(home_config() / name).get("git_kit", {}).get("worktrees", {})
        if "disk_floor_percent" in value:
            floor = value["disk_floor_percent"]
    if isinstance(floor, bool) or not isinstance(floor, (int, float)) or not 0 <= floor <= 100:
        raise ValueError("user git_kit.worktrees.disk_floor_percent must be between 0 and 100")
    return Settings(raw, config, float(floor))


def require_install(repo: Path) -> None:
    from bootstrap_lib.marketplace_lifecycle import InventoryError, plugin_installs

    try:
        installs = plugin_installs("plugins-kit:git-kit", str(repo))
    except InventoryError as exc:
        raise ValueError(f"Cannot verify user-level git-kit installation: {exc}") from exc
    if not any(install.scope == "user" for install in installs):
        raise ValueError("Lifecycle requires a usable, enabled user-level git-kit installation; install git-kit@plugins-kit at user scope")
