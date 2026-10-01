"""Run provenance record: what ran, with which inputs, under which versions.

Stdlib only. Everything is written under a caller-supplied ``bundle_dir``;
paths inside the record are relative to a caller-supplied ``root`` (never an
absolute machine path). Serialization is pluggable: ``dump(data, path)`` and
``load(path)`` default to JSON.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import shutil
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, List, Mapping, Optional, Sequence

SCHEMA = "content-pipeline-provenance/1"

_SEGMENT_PREFIX = "segment-"
_INPUTS_DIR = "inputs"


def json_dump(data: Mapping[str, Any], path: Path) -> None:
    """Write ``data`` as ASCII JSON (the default record format)."""
    Path(path).write_text(
        json.dumps(data, indent=2, sort_keys=False, ensure_ascii=True) + "\n",
        encoding="ascii",
    )


def json_load(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


@dataclass(frozen=True)
class SourceLink:
    """Set by a replay: names the run and snapshot the replay started from."""

    run_id: str
    record: str
    cycle: int
    stage: str
    snapshot: str
    snapshot_sha256: str


@dataclass
class ProvenanceRecord:
    run_id: str
    started_at: str
    argv: List[str]
    params: Mapping[str, Any]
    env: Mapping[str, Optional[str]]
    modules: Mapping[str, Mapping[str, Optional[str]]]
    inputs: Mapping[str, Optional[Mapping[str, Any]]]
    source: Optional[SourceLink] = None
    edits: List[str] = field(default_factory=list)
    status: str = "running"
    finished_at: Optional[str] = None
    error: Optional[str] = None
    result: Optional[Mapping[str, Any]] = None
    segments: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["schema"] = SCHEMA
        # schema first for readability
        return {"schema": data.pop("schema"), **data}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def portable_path(path: Path, *, root: Path) -> str:
    """Return ``path`` relative to ``root`` (forward slashes).

    Outside ``root`` the home prefix becomes ``~``; anything else is reduced
    to ``<external>/<name>`` so no absolute machine path is recorded.
    """
    resolved = Path(os.path.abspath(path))
    base = Path(os.path.abspath(root))
    try:
        return resolved.relative_to(base).as_posix() or "."
    except ValueError:
        pass
    home = Path(os.path.abspath(Path.home()))
    try:
        return "~/" + resolved.relative_to(home).as_posix()
    except ValueError:
        return "<external>/" + resolved.name


def snapshot_file(src: Path, dst: Path) -> bool:
    """Copy ``src`` to ``dst`` (parents created). False when ``src`` is absent."""
    src, dst = Path(src), Path(dst)
    if not src.is_file():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


def _resolve_module(name: str, root: Path) -> dict:
    """Version and location of ``name`` without importing it."""
    version: Optional[str] = None
    for dist in (name, name.replace("_", "-")):
        try:
            version = importlib.metadata.version(dist)
            break
        except importlib.metadata.PackageNotFoundError:
            continue
        except Exception:
            continue
    module = sys.modules.get(name)
    if version is None and module is not None:
        raw = getattr(module, "__version__", None)
        version = str(raw) if raw is not None else None
    location: Optional[str] = None
    module_file = getattr(module, "__file__", None) if module is not None else None
    if module_file:
        location = portable_path(Path(module_file).parent, root=root)
    return {"version": version, "location": location}


def _segment_dirs(bundle_dir: Path) -> List[str]:
    if not bundle_dir.is_dir():
        return []
    return sorted(
        p.name for p in bundle_dir.iterdir()
        if p.is_dir() and p.name.startswith(_SEGMENT_PREFIX)
    )


def _suffixes():
    yield ""
    for letter in "bcdefghijklmnopqrstuvwxyz":
        yield "-" + letter


def _move_or_trim(src: Path, dst: Path) -> None:
    """Move ``src``; a file held open elsewhere is copied then trimmed."""
    try:
        shutil.move(str(src), str(dst))
        return
    except OSError:
        if not src.is_file():
            raise
    shutil.copy2(src, dst)
    with open(src, "r+b") as handle:
        handle.truncate(0)


def rotate_prior_run(
    bundle_dir: Path,
    *,
    label: Optional[str] = None,
    filename: str = "run.json",
    logs: Sequence[str] = (),
) -> List[str]:
    """Move the prior record, its ``inputs/`` and named logs to a segment.

    The segment is ``segment-<label>`` (``label`` defaults to the next
    number); when that directory already holds any of the files to move, a
    suffix ``-b``, ``-c`` ... is used. Returns the names moved (relative to
    ``bundle_dir``); empty when there was no prior record.
    """
    bundle_dir = Path(bundle_dir)
    if not (bundle_dir / filename).is_file():
        return []
    to_move = [filename]
    if (bundle_dir / _INPUTS_DIR).is_dir():
        to_move.append(_INPUTS_DIR)
    to_move.extend(n for n in logs if (bundle_dir / n).is_file())

    if label is None:
        label = str(len(_segment_dirs(bundle_dir)) + 1)
    base = _SEGMENT_PREFIX + label
    segment: Optional[Path] = None
    for suffix in _suffixes():
        candidate = bundle_dir / (base + suffix)
        if not any((candidate / name).exists() for name in to_move):
            segment = candidate
            break
    if segment is None:
        raise RuntimeError("no free segment name for %s" % base)
    segment.mkdir(parents=True, exist_ok=True)
    for name in to_move:
        _move_or_trim(bundle_dir / name, segment / name)
    return [segment.name + "/" + name for name in to_move]


class RunRecorder:
    """Context manager around a started run; rewrites the record on finish."""

    def __init__(self, record: ProvenanceRecord, path: Path, dump: Callable):
        self.record = record
        self.path = path
        self._dump = dump

    def _write(self) -> None:
        self._dump(self.record.to_dict(), self.path)

    def finish(self, *, result: Optional[Mapping[str, Any]] = None) -> None:
        self.record.status = "ok"
        self.record.finished_at = _now()
        if result is not None:
            self.record.result = dict(result)
        self._write()

    def fail(self, exc: BaseException) -> None:
        self.record.status = "error"
        self.record.finished_at = _now()
        self.record.error = "%s: %s" % (type(exc).__name__, exc)
        self._write()

    def __enter__(self) -> "RunRecorder":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is not None:
            self.fail(exc)
        return False


def start_run(
    bundle_dir,
    *,
    root,
    params: Optional[Mapping[str, Any]] = None,
    inputs: Optional[Mapping[str, Optional[Path]]] = None,
    copy_inputs: bool = False,
    env_values: Sequence[str] = (),
    env_presence: Sequence[str] = (),
    modules: Sequence[str] = ("content_pipeline", "llm_scripting_kit"),
    argv: Optional[Sequence[str]] = None,
    source: Optional[SourceLink] = None,
    edits: Sequence[str] = (),
    rotate_label: Optional[str] = None,
    dump: Callable = json_dump,
    filename: str = "run.json",
) -> RunRecorder:
    bundle_dir = Path(bundle_dir)
    root = Path(root)
    bundle_dir.mkdir(parents=True, exist_ok=True)
    if rotate_label is not None:
        rotate_prior_run(bundle_dir, label=rotate_label, filename=filename)

    env: dict = {}
    for name in env_values:
        env[name] = os.environ.get(name)
    for name in env_presence:
        env[name] = "set" if name in os.environ else "unset"

    recorded_inputs: dict = {}
    for label, src in (inputs or {}).items():
        if src is None:
            recorded_inputs[label] = None
            continue
        src = Path(src)
        if not src.is_file():
            recorded_inputs[label] = None
            continue
        recorded_inputs[label] = {
            "path": portable_path(src, root=root),
            "sha256": sha256_file(src),
            "bytes": src.stat().st_size,
        }
        if copy_inputs:
            snapshot_file(src, bundle_dir / _INPUTS_DIR / src.name)

    record = ProvenanceRecord(
        run_id=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8],
        started_at=_now(),
        argv=list(sys.argv if argv is None else argv),
        params=dict(params or {}),
        env=env,
        modules={name: _resolve_module(name, root) for name in modules},
        inputs=recorded_inputs,
        source=source,
        edits=list(edits),
        segments=_segment_dirs(bundle_dir),
    )
    recorder = RunRecorder(record, bundle_dir / filename, dump)
    recorder._write()
    return recorder

__all__ = [
    "SCHEMA",
    "SourceLink",
    "ProvenanceRecord",
    "RunRecorder",
    "start_run",
    "rotate_prior_run",
    "snapshot_file",
    "sha256_file",
    "portable_path",
    "json_dump",
    "json_load",
]
