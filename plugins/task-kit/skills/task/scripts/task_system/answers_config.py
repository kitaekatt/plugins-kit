"""Configuration of the task answers chain (summary-on-commit-plan.md D2; databench-integration-plan.md M5, U3 re-cut).

Two layers, the later one replacing the earlier key by key:

1. The shipped defaults, ``<skill>/defaults/answers.yaml``.
2. The fleet file, ``~/.claude/config/task-kit.yaml``, section ``answers:``.

Keys (both optional in the fleet file):

- ``models`` (list of llm-kit entry ids, preference order): the declaration the chain dispatches. A fleet value
  replaces the shipped list whole.
- ``effort`` (string): the one effort every entry is asked for.

There is no switch for the chain or for paid usage here. Which cost classes may run is the caller's decision
(``answer_chain.resolve_allow``): llm-kit's consent layer (scope ``task-kit``) by hand, or the host's ``--allow``. An unknown key (a
retired ``service`` or ``spend`` switch included) or a wrong type is an error naming the file, never a silent
default. Other top-level keys of the fleet file belong to other task-kit sections and are not read here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

SCHEMA_VERSION = "1"
KEYS = ("models", "effort")


class AnswersConfigError(ValueError):
    """A configuration file is unusable. The message names the file."""


@dataclass(frozen=True)
class AnswersConfig:
    models: tuple[str, ...]
    effort: str
    sources: tuple[Path, ...]  # the files that supplied a value, shipped first


def default_shipped_path() -> Path:
    """The defaults file shipped with the skill."""
    return Path(__file__).resolve().parents[2] / "defaults" / "answers.yaml"


def default_fleet_path() -> Path:
    """The fleet file, resolved on each call."""
    return Path.home() / ".claude" / "config" / "task-kit.yaml"


def _read_mapping(path: Path) -> Mapping[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise AnswersConfigError(f"cannot read {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise AnswersConfigError(f"{path}: the file must be a mapping")
    return data


def _check_section(path: Path, section: Any) -> dict[str, Any]:
    """The validated keys of an ``answers`` section: ``models`` as a tuple, ``effort`` as a string."""
    if not isinstance(section, dict):
        raise AnswersConfigError(f"{path}: answers must be a mapping")
    unknown = sorted(str(key) for key in set(section) - set(KEYS))
    if unknown:
        raise AnswersConfigError(f"{path}: unknown answers keys {unknown}; known keys: {sorted(KEYS)}")
    out: dict[str, Any] = {}
    if "models" in section:
        models = section["models"]
        if not isinstance(models, list) or not models:
            raise AnswersConfigError(f"{path}: answers.models must be a nonempty list of entry ids")
        for item in models:
            if not isinstance(item, str) or not item.strip() or item != item.strip() or any(c in item for c in ", \t"):
                raise AnswersConfigError(f"{path}: answers.models holds {item!r}; each must be an entry id")
        if len(set(models)) != len(models):
            raise AnswersConfigError(f"{path}: answers.models lists an entry twice")
        out["models"] = tuple(models)
    if "effort" in section:
        effort = section["effort"]
        if not isinstance(effort, str) or not effort.strip():
            raise AnswersConfigError(f"{path}: answers.effort must be a nonempty string")
        out["effort"] = effort.strip()
    return out


def _shipped(path: Path) -> dict[str, Any]:
    data = _read_mapping(path)
    unknown = sorted(str(key) for key in set(data) - {"_schema_version", "answers"})
    if unknown:
        raise AnswersConfigError(f"{path}: unknown keys {unknown}")
    if data.get("_schema_version") != SCHEMA_VERSION:
        raise AnswersConfigError(f"{path}: _schema_version must be {SCHEMA_VERSION!r}")
    values = _check_section(path, data.get("answers"))
    missing = [key for key in KEYS if key not in values]
    if missing:
        raise AnswersConfigError(f"{path}: the shipped defaults must set {missing}")
    return values


def load(*, shipped_path: Path | None = None, fleet_path: Path | None = None) -> AnswersConfig:
    """The effective configuration: shipped defaults, then the fleet file's ``answers:`` section."""
    shipped = Path(shipped_path) if shipped_path is not None else default_shipped_path()
    values = _shipped(shipped)
    sources = [shipped]
    fleet = Path(fleet_path) if fleet_path is not None else default_fleet_path()
    if fleet.exists():
        data = _read_mapping(fleet)
        if data.get("answers") is not None:
            values.update(_check_section(fleet, data["answers"]))
            sources.append(fleet)
    return AnswersConfig(models=values["models"], effort=values["effort"], sources=tuple(sources))
