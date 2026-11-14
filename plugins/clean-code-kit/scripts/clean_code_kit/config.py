"""The target's ``.claude/clean-code-kit/config.yaml``: loaded strictly, every key required."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import Refusal
from .languages import EXTENSIONS

CONFIG_REL = Path(".claude") / "clean-code-kit" / "config.yaml"
SCHEMA = 1
SNAPSHOT_MODES = ("head", "working-tree")
COVERAGE_REFUSAL = "coverage-backed complexity is not implemented in this version"

# Per-lane settings: key -> (accepted types, check, description). Every key is required.
_LANE_KEYS: dict[str, dict[str, tuple[tuple[type, ...], Any, str]]] = {
    "duplication": {
        "enabled": ((bool,), None, "true or false"),
        "threshold": ((int, float), lambda v: 0 <= v <= 1, "a number from 0 to 1"),
        "min_lines": ((int,), lambda v: v >= 0, "a non-negative integer"),
        "group_identical": ((bool,), None, "true or false"),
    },
    "complexity": {
        "enabled": ((bool,), None, "true or false"),
        "coverage": ((bool,), None, "true or false"),
        "top": ((int,), lambda v: v >= 1, "a positive integer"),
    },
}
_TOP_KEYS = ("schema", "snapshot", "languages", "source_roots", "test_roots", "exclude", "lanes")


@dataclass(frozen=True)
class Config:
    path: Path
    snapshot: str
    languages: list[str]
    source_roots: list[str]
    test_roots: list[str]
    exclude: list[str]
    lanes: dict[str, dict[str, Any]]

    def enabled_lanes(self) -> list[str]:
        return [name for name, settings in self.lanes.items() if settings["enabled"]]


def config_path(repo: Path) -> Path:
    return repo / CONFIG_REL


def _fail(path: Path, message: str) -> Refusal:
    return Refusal(f"{path}: {message}")


def _check_keys(path: Path, where: str, value: Any, allowed: tuple[str, ...] | list[str]) -> None:
    if not isinstance(value, dict):
        raise _fail(path, f"{where} must be a mapping")
    unknown = [k for k in value if k not in allowed]
    if unknown:
        raise _fail(path, f"unknown key {unknown[0]!r} in {where}; allowed keys: {', '.join(allowed)}")
    missing = [k for k in allowed if k not in value]
    if missing:
        raise _fail(path, f"missing key {missing[0]!r} in {where}")


def _string_list(path: Path, key: str, value: Any, *, non_empty: bool) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise _fail(path, f"{key} must be a list of non-empty strings")
    if non_empty and not value:
        raise _fail(path, f"{key} must name at least one entry")
    if len(set(value)) != len(value):
        raise _fail(path, f"{key} lists an entry twice")
    return list(value)


def _relative_roots(path: Path, key: str, roots: list[str]) -> list[str]:
    for root in roots:
        pure = PurePosixPath(root)
        if pure.is_absolute() or ".." in pure.parts or "\\" in root:
            raise _fail(path, f"{key} entry {root!r} must be a relative posix path inside the repository")
    return roots


def parse(path: Path, data: Any) -> Config:
    _check_keys(path, "the top level", data, _TOP_KEYS)
    if data["schema"] != SCHEMA or isinstance(data["schema"], bool):
        raise _fail(path, f"schema must be {SCHEMA}, got {data['schema']!r}")
    if data["snapshot"] not in SNAPSHOT_MODES:
        raise _fail(path, f"snapshot must be one of {', '.join(SNAPSHOT_MODES)}, got {data['snapshot']!r}")
    languages = _string_list(path, "languages", data["languages"], non_empty=True)
    unknown = [lang for lang in languages if lang not in EXTENSIONS]
    if unknown:
        raise _fail(path, f"unknown language {unknown[0]!r}; known: {', '.join(EXTENSIONS)}")
    source_roots = _relative_roots(
        path, "source_roots", _string_list(path, "source_roots", data["source_roots"], non_empty=True))
    test_roots = _relative_roots(
        path, "test_roots", _string_list(path, "test_roots", data["test_roots"], non_empty=False))
    exclude = _string_list(path, "exclude", data["exclude"], non_empty=False)
    _check_keys(path, "lanes", data["lanes"], tuple(_LANE_KEYS))
    lanes: dict[str, dict[str, Any]] = {}
    for lane, spec in _LANE_KEYS.items():
        settings = data["lanes"][lane]
        _check_keys(path, f"lanes.{lane}", settings, tuple(spec))
        for key, (types, check, description) in spec.items():
            value = settings[key]
            bad_bool = isinstance(value, bool) and bool not in types
            if bad_bool or not isinstance(value, types) or (check is not None and not check(value)):
                raise _fail(path, f"lanes.{lane}.{key} must be {description}, got {value!r}")
        lanes[lane] = dict(settings)
    if lanes["complexity"]["coverage"]:
        raise _fail(path, COVERAGE_REFUSAL)
    return Config(path, data["snapshot"], languages, source_roots, test_roots, exclude, lanes)


def load(repo: Path) -> Config:
    """Read and validate the config of the repository at ``repo``."""
    import yaml

    path = config_path(repo)
    if not path.is_file():
        raise Refusal(
            f"no config at {path}; write one as described in references/configuring.md "
            "of the clean-code-kit code-audit skill"
        )
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise _fail(path, f"not valid YAML: {exc}") from exc
    return parse(path, data)
