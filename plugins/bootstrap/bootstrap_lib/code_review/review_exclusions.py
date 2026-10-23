"""The third not-reviewed axis: paths a user or project CONFIGURES out of review.

The content axis (``machine_emitted.py``) reads banners and the declared-path
axis (``machine_emitted_paths.py``) reads where a plugin says it writes. Neither
can know about an asset a project's own tooling regenerates with no banner and
no plugin declaration -- an index page, a rendered report, a pipeline record. The
project knows, so it says so here.

Layers, lowest to highest precedence, all optional:

    ~/.claude/config/review_exclusions.yaml
    <project_root>/.claude/review_exclusions.yaml
    <project_root>/.claude/review_exclusions.local.yaml

The local layer is one developer's override of the shared project file, kept
out of version control like ``settings.local.json``.

Format::

    exclude:
      - ".databench/**"
      - "docs/**/*.webp"
      - "!docs/keep/**"

Each entry is a glob over the repo-relative path, with ``_globs`` semantics: a
``**/`` prefix matches at any depth, and a ``!`` entry is an exclusion from the
exclusion that beats every positive glob. Layers concatenate, so a project
``!`` entry can re-admit a path a user glob excluded, and a local ``!`` entry
can re-admit a path the project excluded.

Unlike the declared-path axis, a malformed layer RAISES. This file is written by
hand, and a review that silently ignores the user's exclusions is a review the
user did not ask for.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, NoReturn, Optional

from bootstrap_lib.code_review._globs import _matches_one_glob

CONFIG_NAME = "review_exclusions.yaml"
LOCAL_CONFIG_NAME = "review_exclusions.local.yaml"
_FIELDS = {"exclude"}


class ReviewExclusionsError(ValueError):
    """A review-exclusions layer is malformed."""


def _fail(source: Path, message: str) -> NoReturn:
    raise ReviewExclusionsError(f"{source}: {message}")


def _home_path(home: str | Path | None) -> Path:
    return Path.home() if home is None else Path(home).expanduser()


def user_config_path(home: str | Path | None = None) -> Path:
    return _home_path(home) / ".claude" / "config" / CONFIG_NAME


def project_config_path(project_root: str | Path) -> Path:
    return Path(project_root).expanduser() / ".claude" / CONFIG_NAME


def local_config_path(project_root: str | Path) -> Path:
    return Path(project_root).expanduser() / ".claude" / LOCAL_CONFIG_NAME


def layer_paths(
    project_root: str | Path, *, home: str | Path | None = None
) -> list[tuple[str, Path]]:
    return [
        ("user", user_config_path(home)),
        ("project", project_config_path(project_root)),
        ("local", local_config_path(project_root)),
    ]


def _load(path: Path) -> list[str] | None:
    if not path.exists():
        return None
    import yaml

    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        _fail(path, f"cannot load YAML: {exc}")
    if value is None:
        return []
    if not isinstance(value, dict):
        _fail(path, "top level must be a mapping")
    unknown = set(value) - _FIELDS
    if unknown:
        _fail(path, f"unknown top-level field(s): {', '.join(sorted(map(str, unknown)))}")
    globs: Any = value.get("exclude", [])
    if not isinstance(globs, list):
        _fail(path, "exclude must be a list of glob strings")
    for glob in globs:
        if not isinstance(glob, str) or not glob.strip() or glob.strip() == "!":
            _fail(path, f"exclude entry {glob!r} must be a non-blank glob string")
    return [glob.strip() for glob in globs]


def resolve_exclusions(
    project_root: str | Path, *, home: str | Path | None = None
) -> list[tuple[str, str]]:
    """(source label, glob) pairs from every present layer, in layer order."""
    out: list[tuple[str, str]] = []
    for layer, path in layer_paths(project_root, home=home):
        globs = _load(path)
        if globs is None:
            continue
        label = f"{layer} {path.name}"
        out.extend((label, glob) for glob in globs)
    return out


def relative_identifier(
    identifier: str, local: Optional[str], workspace_root: Optional[Path]
) -> str:
    """The path the globs match: `local` relative to the root, else `identifier`.

    A Git identifier is already repo-relative; a Perforce identifier is a depot
    path, so the workspace-relative form of `local` is what a project glob
    names in both kits.
    """
    if local and workspace_root is not None:
        try:
            return Path(local).resolve().relative_to(Path(workspace_root).resolve()).as_posix()
        except (OSError, ValueError):
            pass
    return identifier.replace("\\", "/")


def match_exclusion(path: str, rules: list[tuple[str, str]]) -> Optional[str]:
    """`"<source>: <glob>"` for the first positive glob excluding `path`, else None."""
    if not rules:
        return None
    norm = path.replace("\\", "/")
    base = norm.rsplit("/", 1)[-1]
    for _, glob in rules:
        if glob.startswith("!") and _matches_one_glob(norm, base, glob[1:].replace("\\", "/")):
            return None
    for source, glob in rules:
        if not glob.startswith("!") and _matches_one_glob(norm, base, glob.replace("\\", "/")):
            return f"{source}: {glob}"
    return None
