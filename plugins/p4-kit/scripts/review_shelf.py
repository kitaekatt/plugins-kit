"""Ownership record for the shelf a p4-code-review run creates.

A review shelves a pending CL so the diff is fetchable and stays fixed. The
shelf is the CL's, not the review's, so the review needs to tell its own shelf
from one the user made: it may replace or delete the first and must never touch
the second. The record answers that. It stores the fingerprint (per-file digest
and action) of the shelf the review created, in `<reviews>/<CL>/review-shelf.json`
(a directory the snapshot `prune` and `drop` never delete). A live shelf is
review-created only while it still equals that fingerprint; the moment anyone
reshelves, the fingerprints differ and the shelf is theirs.

Writers: snapshot_review.py freeze (shelve, re-shelve) and prepare_review.py
(its own auto-shelve). Reader: snapshot_review.py freeze. Eraser:
prepare_review.py --cleanup, which deletes the shelf or finds it gone or
changed. Errors raise; nothing is retried or ignored.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

SCHEMA_VERSION = 1
MARKER = "review-shelf.json"


def _upper(digests: dict[str, str]) -> dict[str, str]:
    return {depot: digest.upper() for depot, digest in digests.items()}


def marker_path(cl_dir: Path) -> Path:
    return cl_dir / MARKER


def record_created(cl_dir: Path, cl: str, digests: dict[str, str], actions: dict[str, str]) -> None:
    """Record that the review created the shelf whose fingerprint is given."""
    cl_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "cl": cl,
        "fingerprint": _upper(digests),
        "actions": dict(actions),
        "created_at": time.time(),
    }
    marker_path(cl_dir).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def is_review_created(cl_dir: Path, digests: dict[str, str], actions: dict[str, str]) -> bool:
    """True when a record exists and the live shelf still equals it."""
    path = marker_path(cl_dir)
    if not path.is_file():
        return False
    record = json.loads(path.read_text(encoding="utf-8"))
    if record.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"unsupported review-shelf record schema in {path}")
    return record["fingerprint"] == _upper(digests) and record["actions"] == dict(actions)


def forget(cl_dir: Path) -> None:
    """Erase the record; the shelf it described is gone, changed, or deleted."""
    marker_path(cl_dir).unlink(missing_ok=True)
