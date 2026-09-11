"""Resolve additive, layered regex mechanical checks.

Hard caps are: 1,024 pattern characters, 10,000 scanned characters per line,
100 checks, 50 ms per regex match, and 500 ms total per file/check. Values are
deliberately conservative because configuration is shared repository input.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from string import Formatter
from typing import Any, NoReturn

from bootstrap_lib.code_review._globs import matches_claim

CONFIG_NAME = "mechanical_checks.yaml"
DEFAULTS_PATH = Path(__file__).resolve().parent / "defaults" / CONFIG_NAME
MAX_PATTERN_LENGTH = 1024
MAX_LINE_LENGTH = 10000
MAX_CHECK_COUNT = 100
MATCH_TIMEOUT_MS = 50
CHECK_TOTAL_TIMEOUT_MS = 500
MAX_DETAIL_LENGTH = 120
_FIELDS = {"match", "line", "file", "text"}


class MechanicalConfigError(ValueError):
    """A mechanical-check layer is malformed or violates its contract."""


def _fail(source: Path | str, check_id: Any, message: str) -> NoReturn:
    raise MechanicalConfigError(f"{source}: check {check_id!r}: {message}")


def _home_path(home: str | Path | None) -> Path:
    return Path.home() if home is None else Path(home).expanduser()


def project_config_path(project_root: str | Path) -> Path:
    return Path(project_root).expanduser() / ".claude" / CONFIG_NAME


def layer_paths(project_root: str | Path, *, home: str | Path | None = None) -> list[tuple[str, Path]]:
    return [("shipped", DEFAULTS_PATH), ("user", _home_path(home) / ".claude" / "config" / CONFIG_NAME), ("project", project_config_path(project_root))]


def _load(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        import yaml
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise MechanicalConfigError(f"{path}: cannot load YAML: {exc}") from exc
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise MechanicalConfigError(f"{path}: top level must be a mapping")
    return value


def _string(value: Any, source: Path, check_id: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(source, check_id, f"{field} must be a non-blank string")
    return value


def _validate(record: Any, source: Path) -> dict[str, Any]:
    if not isinstance(record, dict):
        _fail(source, "<missing>", "record must be a mapping")
    check_id = record.get("id", "<missing>")
    allowed = {"id", "phrase", "pattern", "applies_to", "detail"}
    unknown = set(record) - allowed
    if unknown:
        _fail(source, check_id, f"unknown field(s): {', '.join(sorted(map(str, unknown)))}")
    ident = _string(check_id, source, check_id, "id")
    import regex
    if regex.fullmatch(r"[a-z0-9_]+", ident) is None:
        _fail(source, ident, "id must match [a-z0-9_]+")
    phrase = _string(record.get("phrase"), source, ident, "phrase")
    pattern = _string(record.get("pattern"), source, ident, "pattern")
    if len(pattern) > MAX_PATTERN_LENGTH:
        _fail(source, ident, f"pattern exceeds {MAX_PATTERN_LENGTH} characters")
    try:
        compiled = regex.compile(pattern)
    except regex.error as exc:
        _fail(source, ident, f"pattern is invalid: {exc}")
    applies = record.get("applies_to")
    if not isinstance(applies, list) or not applies or any(not isinstance(item, str) or not item.strip() for item in applies):
        _fail(source, ident, "applies_to must be a non-empty list of non-blank strings")
    if not any(not item.startswith("!") for item in applies):
        _fail(source, ident, "applies_to must contain a positive glob")
    detail = record.get("detail", "{match!r} in: {text}")
    detail = _string(detail, source, ident, "detail")
    if len(detail) > MAX_DETAIL_LENGTH:
        _fail(source, ident, f"detail exceeds {MAX_DETAIL_LENGTH} characters")
    for _, field, spec, conversion in Formatter().parse(detail):
        if field is not None and field not in _FIELDS:
            _fail(source, ident, f"detail references unknown field {field!r}")
        if spec or (conversion and detail != "{match!r} in: {text}"):
            _fail(source, ident, "detail cannot use format specifiers or conversions")
    return {"id": ident, "phrase": phrase, "pattern": compiled, "applies_to": applies, "detail": detail, "source": str(source)}


def resolve_config(project_root: str | Path, *, home: str | Path | None = None) -> tuple[dict[str, Any], ...]:
    """Load, validate, and return config records in increasing layer order."""
    records: list[dict[str, Any]] = []
    seen: dict[str, tuple[str, Path]] = {}
    for layer, path in layer_paths(project_root, home=home):
        data = _load(path)
        if data is None:
            if layer == "shipped":
                raise MechanicalConfigError(f"{path}: shipped defaults are missing")
            continue
        if set(data) != {"checks"}:
            unknown = set(data) - {"checks"}
            raise MechanicalConfigError(f"{path}: unknown top-level field(s): {', '.join(sorted(map(str, unknown)))}")
        checks = data["checks"]
        if not isinstance(checks, list):
            raise MechanicalConfigError(f"{path}: check <missing>: checks must be a list")
        for raw in checks:
            record = _validate(raw, path)
            ident = record["id"]
            if ident in seen:
                old_layer, old_path = seen[ident]
                _fail(path, ident, f"duplicate id also defined by {old_layer} layer {old_path}; current layer is {layer} ({path})")
            seen[ident] = (layer, path)
            record["layer"] = layer
            records.append(record)
    if len(records) > MAX_CHECK_COUNT:
        _fail("resolved layers", "<all>", f"check count exceeds {MAX_CHECK_COUNT}")
    return tuple(records)


def normalize_project_path(identifier: str, project_root: str | Path) -> str:
    """Normalize git paths and ``//depot/<project>/...`` identifiers."""
    value = identifier.replace("\\", "/")
    if value.startswith("//"):
        parts = [part for part in value[2:].split("/") if part]
        return "/".join(parts[2:]) if len(parts) >= 2 else "/".join(parts)
    path = Path(value)
    root = Path(project_root).expanduser()
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return value.lstrip("./")


def build_check(record: dict[str, Any], project_root: str | Path):
    """Build one MechanicalCheck from a validated record."""
    import regex
    from bootstrap_lib.code_review.mechanical import MechanicalCheck

    compiled = record["pattern"]
    source = record["source"]
    detail_template = record["detail"]

    def precondition(snapshot: Any) -> bool:
        """Decline unless this file is in scope AND added lines were parsed.

        `applies_to` is tested HERE and not inside `scan`. A check that ran but
        looked at nothing is still recorded in `checks_run`, which tells the
        lane the file was covered when it was not -- the precise failure this
        whole mechanism exists to remove. An out-of-scope file is an unmet
        precondition: the check emits nothing and is omitted from coverage.
        """
        if snapshot.added_lines is None:
            return False
        normalized = normalize_project_path(snapshot.file, project_root)
        return matches_claim(normalized, record["applies_to"])

    def scan(snapshot: Any) -> tuple[dict[str, Any], ...]:
        import time
        start = time.monotonic()
        found: list[dict[str, Any]] = []
        for line_number, text in snapshot.added_lines or ():
            if len(text) > MAX_LINE_LENGTH:
                text = text[:MAX_LINE_LENGTH]
            remaining = CHECK_TOTAL_TIMEOUT_MS / 1000 - (time.monotonic() - start)
            if remaining <= 0:
                raise TimeoutError
            try:
                match = compiled.search(text, timeout=min(MATCH_TIMEOUT_MS / 1000, remaining))
            except TimeoutError as exc:
                raise TimeoutError(f"check {record['id']!r} on file {snapshot.file!r} from {source}") from exc
            if match:
                values = {"match": match.group(0), "line": line_number, "file": snapshot.file, "text": text}
                detail = detail_template.format(**values)
                found.append({"check": record["id"], "line": line_number, "detail": detail[:MAX_DETAIL_LENGTH]})
        return tuple(found)

    return MechanicalCheck(record["id"], record["phrase"], frozenset({"added_lines"}), precondition, scan, source)
