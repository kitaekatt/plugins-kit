"""Validated, repository-approved lifecycle settings; user-only disk policy."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator, model_validator

from bootstrap_guard import EXIT_BOOTSTRAP_MISSING, require_bootstrap

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
    upstream: str | None = None
    setup: list[Step] = Field(min_length=1)
    rerun_files: dict[str, list[str]] = Field(default_factory=dict)
    rebuildable: list[str] = Field(default_factory=list)

    @field_validator("upstream")
    @classmethod
    def check_upstream(cls, value: str | None) -> str | None:
        if value is not None:
            remote, separator, branch = value.partition("/")
            if not remote or not separator or not branch or ".." in value:
                raise ValueError("pool.upstream must have the form remote/branch without '..'")
        return value

    @field_validator("rebuildable")
    @classmethod
    def check_globs(cls, patterns: list[str]) -> list[str]:
        for pattern in patterns:
            relative_path(pattern)
            if pattern.startswith(("!", "#")) or any(c in pattern for c in "\0\r\n"):
                raise ValueError("rebuildable requires positive relative gitignore-style globs")
        return patterns

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
    root: str = "~/.worktrees"
    pool: Pool | None = None
    after_take: list[list[str]] = Field(default_factory=list)
    before_release: list[list[str]] = Field(default_factory=list)

    @field_validator("after_take", "before_release")
    @classmethod
    def check_commands(cls, commands: list[list[str]]) -> list[list[str]]:
        if any(not argv or any(not arg or "\0" in arg for arg in argv) for argv in commands):
            raise ValueError("Lifecycle commands require nonempty argv arrays without NUL")
        return commands

    @field_validator("root")
    @classmethod
    def check_root(cls, value: str) -> str:
        if not value.strip() or not (value.startswith("~/") or Path(value).is_absolute()):
            raise ValueError("git_kit.worktrees.root must be absolute or start with ~/")
        return value

    @model_validator(mode="after")
    def check_enablement(self) -> RepositoryConfig:
        if self.enabled and self.pool is None:
            raise ValueError("Enabled repository needs approved pool settings; run configure --propose")
        return self


def rebuildable_matches(repo: Path, patterns: Sequence[str], *, tracked: bool = False) -> set[str]:
    """Let Git apply its ignore glob semantics without the repository's rules."""
    if not patterns:
        return set()
    selection = ["--cached"] if tracked else ["--others"]
    return {p for p in wt.git(repo, "ls-files", *selection, "--ignored", "-z",
                             *(f"--exclude={pattern}" for pattern in patterns)).split("\0") if p}


def validate_config(raw: dict[str, Any], repo: Path) -> RepositoryConfig:
    config = RepositoryConfig.model_validate(raw)
    if config.pool is None:
        return config
    if config.pool.upstream is not None:
        remote = config.pool.upstream.partition("/")[0]
        if remote not in wt.git(repo, "remote").splitlines():
            raise ValueError(f"pool.upstream remote is not configured: {remote}")
        wt.git(repo, "check-ref-format", f"refs/remotes/{config.pool.upstream}")
    matched = rebuildable_matches(repo, config.pool.rebuildable, tracked=True)
    if matched:
        raise ValueError(f"rebuildable glob matches tracked data: {sorted(matched)}")
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


def inventory_api() -> tuple[type[Exception], Any]:
    """Probe the newest lifecycle API before calling bootstrap shared libraries."""
    try:
        from bootstrap_lib.marketplace_lifecycle import InventoryError, plugin_installs
    except ImportError as exc:
        if isinstance(exc, ModuleNotFoundError) and exc.name == "bootstrap_lib":
            require_bootstrap("git-kit", feature="worktree lifecycle", missing="bootstrap_lib", force=True)
        print(
            "[git-kit] the installed 'plugins-kit:bootstrap' plugin is too old or stale "
            "for the worktree lifecycle (requires bootstrap >= 0.154.1; missing: "
            "bootstrap_lib.marketplace_lifecycle.plugin_installs). Run "
            "`claude plugin update bootstrap@plugins-kit`. Then start a new session and retry.",
            file=sys.stderr,
        )
        raise SystemExit(EXIT_BOOTSTRAP_MISSING) from None
    return InventoryError, plugin_installs


def require_install(repo: Path) -> None:
    InventoryError, plugin_installs = inventory_api()

    try:
        installs = plugin_installs("plugins-kit:git-kit", str(repo))
    except InventoryError as exc:
        raise ValueError(f"Cannot verify user-level git-kit installation: {exc}") from exc
    if not any(install.scope == "user" for install in installs):
        raise ValueError("Lifecycle requires a usable, enabled user-level git-kit installation; install git-kit at user level: claude plugin install git-kit@plugins-kit --scope user")
