#!/usr/bin/env python3
"""Freeze, verify, drop and prune review snapshots for p4-code-review.

Usage:
    snapshot_review.py freeze <CL> [--use-shelf]
    snapshot_review.py verify <record>
    snapshot_review.py drop <record> [--tree-only]
    snapshot_review.py prune [--older-than-days N]

A p4 review snapshot is the CL's SHELF, pinned by the shelf fingerprint (the
server's per-file digest and action) recorded at freeze time. A shelf is
server-side and does not change until someone reshelves, so a background review
reads a fixed subject while the agent keeps editing the workspace.

freeze   Shelve the CL when it has no shelf (`p4 shelve -c`, the same
         mutation prepare_review.py makes on its own). When a shelf already
         exists it must match the CL's open files and the workspace content;
         otherwise exit 1 naming the files: replace it with
         `p4 shelve -f -c <CL>`, or pass --use-shelf to review the shelf as it
         stands. Freeze never overwrites a shelf. Writes the record
         `<data>/reviews/<CL>/snap-<id12>/freeze.json` and prints JSON
         {cl, snapshot, record, bundle_dir, auto_shelved, files}. Exit 1 when
         the CL has nothing to review. Runs `prune` afterwards.
verify   Read-only. Exit 0 when the CL's open files (membership, actions) and
         their workspace content still equal the frozen shelf, else exit 1.
         Prints JSON {cl, snapshot, matches, changed, opened_after, not_open,
         unverifiable}.
drop     Delete a record directory, or with --tree-only just its `tree/`.
prune    Drop record directories older than N days (default 7).

There is no land step: submitting stays the user's `p4 submit -c <CL>`.
`verify` is what ties that submit to the review -- run it first, and freeze and
review again when it reports a difference. Only
`<data>/reviews/<digits>/snap-<12 hex>` directories are ever deleted, and no
shelf is touched by drop or prune. Content equality is md5 of the workspace
file against the server digest, retried with CRLF normalized to LF; a file type
the server normalizes differently (utf16, keyword expansion) reads as changed,
which only ever refuses. Errors exit non-zero with a message; nothing is
retried or ignored.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from bootstrap_guard import data_dir

SCHEMA_VERSION = 1
DEFAULT_PRUNE_DAYS = 7.0
_CL = re.compile(r"^[0-9]+$")
_SNAP_DIR = re.compile(r"^snap-[0-9a-f]{12}$")
_DELETE_ACTIONS = {"delete", "move/delete", "purge"}
_P4_TIMEOUT_ENV_VAR = "P4KIT_VCS_TIMEOUT_S"
_P4_DEFAULT_TIMEOUT_S = 60.0
_P4_PATH_BATCH = 100


class SnapshotError(Exception):
    """A refusal or failure that the caller must see; main() exits 1."""


# ---------------------------------------------------------------- p4 plumbing


def _timeout() -> float:
    """`P4KIT_VCS_TIMEOUT_S`, read exactly as prepare_review.py reads it."""
    raw = os.environ.get(_P4_TIMEOUT_ENV_VAR)
    if not raw:
        return _P4_DEFAULT_TIMEOUT_S
    try:
        return float(raw)
    except ValueError:
        return _P4_DEFAULT_TIMEOUT_S


def run_p4(args: list[str]) -> tuple[int, str, str]:
    """Run p4 with UTF-8 decoding and the shared timeout knob."""
    proc = subprocess.run(
        ["p4", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_timeout(),
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _p4_ok(args: list[str]) -> str:
    rc, out, err = run_p4(args)
    if rc != 0:
        raise SnapshotError(f"p4 {' '.join(args)} failed: {(err or out).strip() or f'exit {rc}'}")
    return out


def _ztag_records(output: str) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    record: dict[str, str] = {}
    for line in output.splitlines():
        if not line.strip():
            if record:
                records.append(record)
                record = {}
            continue
        if not line.startswith("... "):
            continue
        name, _, value = line[4:].partition(" ")
        if name in record and name in {"depotFile", "clientFile"}:
            records.append(record)
            record = {}
        record[name] = value.strip()
    if record:
        records.append(record)
    return records


def _shelf(cl: str) -> tuple[dict[str, str], dict[str, str]]:
    """(digests, actions) of the CL's shelf; both empty when there is no shelf."""
    rc, out, err = run_p4(["-ztag", "fstat", "-Ol", f"//...@={cl}"])
    if rc != 0:
        detail = (err or out).strip()
        lowered = detail.lower()
        if "no such file" in lowered or "no file(s)" in lowered:
            return {}, {}
        raise SnapshotError(f"could not read the shelf of CL {cl}: {detail or f'exit {rc}'}")
    digests: dict[str, str] = {}
    actions: dict[str, str] = {}
    for record in _ztag_records(out):
        depot = record.get("depotFile")
        if not depot:
            continue
        digests[depot] = record.get("digest", "").upper()
        action = record.get("action") or record.get("headAction") or ""
        if action:
            actions[depot] = action
    return digests, actions


def _opened(cl: str) -> dict[str, str]:
    out = _p4_ok(["-ztag", "opened", "-c", cl])
    opened: dict[str, str] = {}
    for record in _ztag_records(out):
        depot = record.get("depotFile")
        action = record.get("action")
        if depot and action:
            opened[depot] = action
    return opened


def _where(depots: list[str]) -> dict[str, str]:
    local: dict[str, str] = {}
    for i in range(0, len(depots), _P4_PATH_BATCH):
        out = _p4_ok(["-ztag", "where", *depots[i:i + _P4_PATH_BATCH]])
        for record in _ztag_records(out):
            depot = record.get("depotFile")
            path = record.get("path")
            if depot and path and "unmap" not in record:
                local[depot] = path
    return local


def _client() -> str:
    out = _p4_ok(["-ztag", "info"])
    for record in _ztag_records(out):
        name = record.get("clientName", "")
        if name and name.lower() not in {"unknown", "*unknown*"}:
            return name
    raise SnapshotError("no p4 client workspace resolves here (p4 info names no clientName)")


# ------------------------------------------------------------ content checks


def _content_matches(path: str, digest: str) -> bool:
    data = Path(path).read_bytes()
    if hashlib.md5(data).hexdigest().upper() == digest:
        return True
    return hashlib.md5(data.replace(b"\r\n", b"\n")).hexdigest().upper() == digest


def _compare(
    digests: dict[str, str],
    actions: dict[str, str],
    opened: dict[str, str],
    local: dict[str, str],
) -> dict[str, list[str]]:
    """Differences between the frozen shelf and the CL's open files on disk."""
    out: dict[str, list[str]] = {
        "changed": [],
        "opened_after": sorted(set(opened) - set(digests)),
        "not_open": sorted(set(digests) - set(opened)),
        "unverifiable": [],
    }
    for depot in sorted(set(digests) & set(opened)):
        action = actions.get(depot, "")
        if action and opened[depot] != action:
            out["changed"].append(depot)
            continue
        path = local.get(depot)
        if not path:
            out["unverifiable"].append(depot)
            continue
        if action in _DELETE_ACTIONS:
            if os.path.lexists(path):
                out["changed"].append(depot)
            continue
        if not digests[depot]:
            out["unverifiable"].append(depot)
        elif not os.path.isfile(path) or not _content_matches(path, digests[depot]):
            out["changed"].append(depot)
    return out


def _differs(diff: dict[str, list[str]]) -> bool:
    return any(diff.values())


def _describe_diff(diff: dict[str, list[str]]) -> str:
    parts = []
    for key, label in (
        ("changed", "content or action differs"),
        ("opened_after", "opened after shelving"),
        ("not_open", "shelved but not open"),
        ("unverifiable", "cannot compare"),
    ):
        if diff[key]:
            parts.append(f"{label}: {', '.join(diff[key])}")
    return "; ".join(parts)


# ------------------------------------------------------------------- records


def _reviews_root() -> Path:
    return data_dir("p4-kit") / "reviews"


def _record_dir(cl: str, snap: str) -> Path:
    return _reviews_root() / cl / f"snap-{snap}"


def _owned_dir(path: Path) -> Path:
    """Refuse any directory that is not `<reviews>/<digits>/snap-<12 hex>`."""
    resolved = path.resolve()
    root = _reviews_root().resolve()
    if (
        resolved.parent.parent != root
        or not _CL.match(resolved.parent.name)
        or not _SNAP_DIR.match(resolved.name)
    ):
        raise SnapshotError(f"not a p4-kit review snapshot directory: {path}")
    return resolved


def load_record(arg: str) -> tuple[dict, Path]:
    """Read a freeze record from its file or its directory."""
    path = Path(arg)
    if path.is_dir():
        path = path / "freeze.json"
    if not path.is_file():
        raise SnapshotError(f"no freeze record at {arg}")
    _owned_dir(path.parent)
    record = json.loads(path.read_text(encoding="utf-8"))
    if record.get("schema_version") != SCHEMA_VERSION:
        raise SnapshotError(f"unsupported freeze record schema in {path}")
    return record, path.parent


def _rmtree(path: Path) -> None:
    """Remove a tree, clearing the read-only bits `p4 print -o` leaves; failures raise."""

    def _onexc(func, target, exc):  # noqa: ANN001
        if isinstance(exc, PermissionError):
            os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
            func(target)
            return
        raise exc

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_onexc)
    else:  # pragma: no cover - the repo pins 3.12
        shutil.rmtree(path, onerror=lambda f, p, e: _onexc(f, p, e[1]))


# ------------------------------------------------------------------ commands


def freeze(cl: str, *, use_shelf: bool = False) -> dict:
    if not _CL.match(cl):
        raise SnapshotError(f"CL must be a positive integer, got {cl!r}")
    client = _client()
    opened = _opened(cl)
    digests, actions = _shelf(cl)
    auto_shelved = False
    if not digests:
        if not opened:
            raise SnapshotError(f"CL {cl} has no open files and no shelf: nothing to review")
        _p4_ok(["shelve", "-c", cl])
        digests, actions = _shelf(cl)
        if not digests:
            raise SnapshotError(f"p4 shelve -c {cl} reported success but the shelf is empty")
        auto_shelved = True
    local = _where(sorted(digests))
    if not auto_shelved and not use_shelf:
        diff = _compare(digests, actions, opened, local)
        if not opened:
            raise SnapshotError(
                f"CL {cl} has no files open in this workspace; pass --use-shelf to review its shelf"
            )
        if _differs(diff):
            raise SnapshotError(
                f"CL {cl}'s shelf differs from the workspace ({_describe_diff(diff)}). "
                f"To review the workspace, replace the shelf with `p4 shelve -f -c {cl}` and "
                f"freeze again; to review the shelf as it stands, pass --use-shelf"
            )
    frozen_at = time.time()
    canonical = json.dumps([cl, sorted(digests.items()), sorted(actions.items()), frozen_at])
    snap = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    bundle_dir = _record_dir(cl, snap)
    bundle_dir.mkdir(parents=True, exist_ok=False)
    record = {
        "schema_version": SCHEMA_VERSION,
        "cl": cl,
        "snapshot": snap,
        "client": client,
        "fingerprint": digests,
        "actions": actions,
        "local": local,
        "auto_shelved": auto_shelved,
        "use_shelf": use_shelf,
        "frozen_at": frozen_at,
    }
    record_path = bundle_dir / "freeze.json"
    record_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "cl": cl,
        "snapshot": snap,
        "record": str(record_path),
        "bundle_dir": str(bundle_dir),
        "auto_shelved": auto_shelved,
        "files": sorted(digests),
    }


def verify(arg: str) -> dict:
    record, _ = load_record(arg)
    cl = record["cl"]
    opened = _opened(cl)
    local = dict(record["local"])
    missing = sorted(set(opened) - set(local))
    if missing:
        local.update(_where(missing))
    diff = _compare(record["fingerprint"], record["actions"], opened, local)
    return {"cl": cl, "snapshot": record["snapshot"], "matches": not _differs(diff), **diff}


def drop(arg: str, *, tree_only: bool = False) -> dict:
    path = Path(arg)
    directory = path if path.is_dir() else path.parent
    directory = _owned_dir(directory)
    if not directory.is_dir():
        raise SnapshotError(f"no snapshot directory at {arg}")
    target = directory / "tree" if tree_only else directory
    if target.exists():
        _rmtree(target)
    return {"dropped": str(target)}


def prune(older_than_days: float) -> dict:
    cutoff = time.time() - older_than_days * 86400
    dropped: list[str] = []
    root = _reviews_root()
    if root.is_dir():
        for directory in sorted(root.glob("*/snap-*")):
            if not (_CL.match(directory.parent.name) and _SNAP_DIR.match(directory.name)):
                continue
            marker = directory / "freeze.json"
            age_from = marker if marker.exists() else directory
            if age_from.stat().st_mtime < cutoff:
                _rmtree(directory)
                dropped.append(str(directory))
    return {"pruned": dropped}


# ---------------------------------------------------------------------- CLI


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="snapshot_review.py", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p_freeze = sub.add_parser("freeze", help="pin the CL's shelf as the review subject")
    p_freeze.add_argument("cl")
    p_freeze.add_argument("--use-shelf", action="store_true", help="review an existing shelf as it stands")
    sub.add_parser("verify", help="does the CL still equal the frozen shelf?").add_argument("record")
    p_drop = sub.add_parser("drop", help="delete a snapshot record directory")
    p_drop.add_argument("record")
    p_drop.add_argument("--tree-only", action="store_true", help="remove only <bundle>/tree/")
    p_prune = sub.add_parser("prune", help="drop record directories older than N days")
    p_prune.add_argument("--older-than-days", type=float, default=DEFAULT_PRUNE_DAYS, metavar="N")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "freeze":
            result = freeze(args.cl, use_shelf=args.use_shelf)
            print(json.dumps(result, indent=2))
            pruned = prune(DEFAULT_PRUNE_DAYS)
            if pruned["pruned"]:
                print(f"snapshot_review: pruned {len(pruned['pruned'])} old snapshot(s)", file=sys.stderr)
            return 0
        if args.command == "verify":
            result = verify(args.record)
            print(json.dumps(result, indent=2))
            return 0 if result["matches"] else 1
        if args.command == "drop":
            result = drop(args.record, tree_only=args.tree_only)
        else:
            result = prune(args.older_than_days)
        print(json.dumps(result, indent=2))
        return 0
    except SnapshotError as exc:
        print(f"snapshot_review: {exc}", file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        if exc.filename == "p4":
            print("snapshot_review: 'p4' executable not found on PATH", file=sys.stderr)
            return 1
        raise


if __name__ == "__main__":
    sys.exit(main())
