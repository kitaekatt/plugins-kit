#!/usr/bin/env python3
"""Run a Codex unit with a durable, content-addressed dispatch cache."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
from typing import Any, Sequence


DEFAULT_MODEL = "gpt-5.6-sol"
DEFAULT_EFFORT = "high"
DEFAULT_SANDBOX = "workspace-write"
DEFAULT_GLOBAL_CACHE = (
    Path.home() / ".claude" / "plugins" / "data" / "plugins-kit" / "awesome-kit" / "agent-cache"
)
LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
ENTRY_RE = re.compile(r"^\d{8}-\d{6}-[A-Za-z0-9][A-Za-z0-9._-]*$")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="list cached dispatch entries")
    parser.add_argument("--label", help="short slug used in the entry directory name")
    parser.add_argument("--brief", type=Path, help="file containing the unit brief")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--effort", default=DEFAULT_EFFORT)
    parser.add_argument("--cwd", type=Path, help="absolute working directory for Codex")
    parser.add_argument("--add-dir", action="append", default=[], metavar="DIR")
    parser.add_argument("--sandbox", choices=("read-only", "workspace-write"), default=DEFAULT_SANDBOX)
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--ttl-days", type=float, default=7)
    parser.add_argument("--no-cache", action="store_true", help="force a new Codex run")
    parser.add_argument("--print-only", action="store_true", help="show the run without launching Codex")
    return parser


def _absolute(path: Path) -> Path:
    return path.expanduser().resolve()


def _cache_location(cwd: Path, requested: Path | None) -> tuple[Path, str]:
    if requested is not None:
        return _absolute(requested), "explicit"
    project_tmp = cwd / "tmp"
    if project_tmp.is_dir():
        return project_tmp / "agent-cache", "project tmp"
    return DEFAULT_GLOBAL_CACHE, "global fallback"


def _ensure_cache(cache_dir: Path) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)


def _sweep(cache_dir: Path, ttl_days: float, excluded: Path | None = None) -> tuple[int, int]:
    cutoff = time.time() - (ttl_days * 86400)
    swept = 0
    skipped = 0
    for entry in cache_dir.iterdir():
        if entry == excluded:
            continue
        if (
            not entry.is_dir()
            or entry.is_symlink()
            or not ENTRY_RE.fullmatch(entry.name)
            or not (entry / "meta.json").is_file()
        ):
            skipped += 1
            continue
        try:
            is_old = entry.stat().st_mtime < cutoff
        except OSError:
            continue
        if is_old:
            try:
                shutil.rmtree(entry)
            except OSError as exc:
                print(f"dispatch cache: could not sweep {entry}: {exc}", file=sys.stderr)
            else:
                swept += 1
    return swept, skipped


def _entry_path(cache_dir: Path, label: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    candidate = cache_dir / f"{stamp}-{label}"
    suffix = 1
    while candidate.exists():
        candidate = cache_dir / f"{stamp}-{label}-{suffix}"
        suffix += 1
    return candidate


def _cache_key(
    model: str,
    effort: str,
    sandbox: str,
    cwd: Path,
    add_dirs: Sequence[Path],
    brief: bytes,
) -> str:
    components = [
        model.encode("utf-8"),
        effort.encode("utf-8"),
        sandbox.encode("utf-8"),
        str(cwd).encode("utf-8"),
        *[str(path).encode("utf-8") for path in sorted(add_dirs)],
        brief,
    ]
    material = b"\0".join(components)
    return hashlib.sha256(material).hexdigest()


def _cache_hit(
    cache_dir: Path, key: str, cwd: Path, add_dirs: Sequence[Path]
) -> tuple[Path, Path] | None:
    for entry in sorted(cache_dir.iterdir(), reverse=True):
        if not entry.is_dir() or entry.is_symlink():
            continue
        try:
            meta = json.loads((entry / "meta.json").read_text(encoding="utf-8"))
            if not isinstance(meta, dict):
                continue
            result = entry / "result.md"
            # A codex dispatch can fail silently at exit 0, so the exit code is
            # recorded for --list but never consulted here: a hit is judged by the
            # -o file alone, and the caller reads result.md exactly as it would a
            # fresh dispatch (--no-cache forces a re-run).
            if (
                meta.get("key") == key
                and meta.get("cwd") == str(cwd)
                and meta.get("add_dirs") == [str(path) for path in sorted(add_dirs)]
                and result.stat().st_size > 0
            ):
                return entry, result
        except (AttributeError, OSError, TypeError, ValueError):
            continue
    return None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_meta(path: Path, meta: dict[str, Any]) -> None:
    path.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _argv(
    *,
    model: str,
    effort: str,
    sandbox: str,
    cwd: Path,
    add_dirs: Sequence[Path],
    result: Path,
) -> list[str]:
    argv = [
        "codex",
        "exec",
        "-s",
        sandbox,
        "-c",
        "sandbox_workspace_write.network_access=true",
        "-m",
        model,
        "-c",
        f"model_reasoning_effort={effort}",
        "-C",
        str(cwd),
    ]
    if os.name == "nt":
        argv[4:4] = ["-c", 'windows.sandbox="unelevated"']
    for add_dir in add_dirs:
        argv.extend(("--add-dir", str(add_dir)))
    argv.extend(("-o", str(result), "--skip-git-repo-check", "--color", "never", "-"))
    return argv


def _validate_run_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if not args.label or not LABEL_RE.fullmatch(args.label):
        parser.error("--label must be an ASCII slug containing letters, digits, '.', '_' or '-'")
    if args.brief is None:
        parser.error("--brief is required unless --list is used")
    if args.ttl_days < 0:
        parser.error("--ttl-days must be non-negative")


def _list_entries(cache_dir: Path) -> None:
    for entry in sorted(cache_dir.iterdir(), reverse=True):
        if not entry.is_dir() or entry.is_symlink():
            continue
        try:
            meta = json.loads((entry / "meta.json").read_text(encoding="utf-8"))
            if not isinstance(meta, dict):
                continue
        except (AttributeError, OSError, TypeError, ValueError):
            continue
        result = entry / "result.md"
        try:
            result_size = result.stat().st_size
        except OSError:
            result_size = 0
        timestamp = entry.name[:15]
        print(
            f"{timestamp} {meta.get('label', '-')} {meta.get('model', '-')} "
            f"{meta.get('exit_code', '-')} {result_size} {entry} {result}"
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)

    if args.list:
        if args.cwd is not None or args.brief is not None or args.label is not None:
            parser.error("--list accepts --cache-dir and --ttl-days only")
        cwd = Path.cwd().resolve()
        cache_dir, _ = _cache_location(cwd, args.cache_dir)
        _ensure_cache(cache_dir)
        swept, skipped = _sweep(cache_dir, args.ttl_days)
        print(f"SWEPT {swept} entries; skipped {skipped} (not an entry)", file=sys.stderr)
        _list_entries(cache_dir)
        return 0

    _validate_run_args(parser, args)
    cwd = _absolute(args.cwd) if args.cwd is not None else Path.cwd().resolve()
    if not cwd.is_dir():
        parser.error(f"--cwd is not a directory: {cwd}")
    add_dirs = [_absolute(Path(path)) for path in args.add_dir]
    brief_path = _absolute(args.brief)
    try:
        brief = brief_path.read_bytes()
    except OSError as exc:
        parser.error(f"could not read brief: {exc}")

    cache_dir, cache_source = _cache_location(cwd, args.cache_dir)
    _ensure_cache(cache_dir)
    key = _cache_key(args.model, args.effort, args.sandbox, cwd, add_dirs, brief)
    swept, skipped = _sweep(cache_dir, args.ttl_days)
    if not args.no_cache:
        hit = _cache_hit(cache_dir, key, cwd, add_dirs)
        if hit is not None:
            entry, result = hit
            print(f"CACHE HIT {entry}")
            print(f"SWEPT {swept} entries; skipped {skipped} (not an entry)", file=sys.stderr)
            print(result)
            return 0

    entry = _entry_path(cache_dir, args.label)
    result = entry / "result.md"
    run_argv = _argv(
        model=args.model,
        effort=args.effort,
        sandbox=args.sandbox,
        cwd=cwd,
        add_dirs=add_dirs,
        result=result,
    )
    if args.print_only:
        print(entry)
        print(f"ARGV {shlex.join(run_argv)}")
        print(f"SWEPT {swept} entries; skipped {skipped} (not an entry)", file=sys.stderr)
        return 0

    entry.mkdir()
    (entry / "brief.md").write_bytes(brief)
    result.touch()
    log_path = entry / "log.txt"
    log_path.touch()
    started = _utc_now()
    meta: dict[str, Any] = {
        "label": args.label,
        "model": args.model,
        "effort": args.effort,
        "sandbox": args.sandbox,
        "cwd": str(cwd),
        "add_dirs": [str(path) for path in add_dirs],
        "brief_sha256": hashlib.sha256(brief).hexdigest(),
        "started": started,
        "ended": None,
        "exit_code": None,
        "key": key,
        "cache_dir": str(cache_dir),
        "cache_source": cache_source,
    }
    _write_meta(entry / "meta.json", meta)
    print(entry, flush=True)
    print(f"SWEPT {swept} entries", file=sys.stderr, flush=True)

    try:
        with log_path.open("wb") as log:
            completed = subprocess.run(
                run_argv,
                input=brief,
                stdout=log,
                stderr=subprocess.STDOUT,
                cwd=str(cwd),
                check=False,
            )
        exit_code = completed.returncode
    except OSError as exc:
        with log_path.open("ab") as log:
            log.write(f"{type(exc).__name__}: {exc}\n".encode("utf-8", errors="replace"))
        exit_code = 127

    meta["ended"] = _utc_now()
    meta["exit_code"] = exit_code
    _write_meta(entry / "meta.json", meta)
    print(result)
    return int(exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
