"""Re-evaluate parked work under the maintenance and claim locks."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import worktree_migration as migration
import worktree_safety as safety
import worktrees as wt
from worktree_config import read_object

if TYPE_CHECKING:
    from worktree_lifecycle import Lifecycle


def evidence(obj: Lifecycle, record: dict[str, Any], *, recover_removed: bool = False) -> dict[str, Any]:
    path = Path(record["path"])
    op = record["operation"]
    if (recover_removed and op.get("name") == "parked-recheck" and op.get("phase") == "reserved"
            and not path.exists() and not path.is_symlink()):
        obj.record_path(record)
        if not any(Path(row["worktree"]).absolute() == path for row in wt.records(Path(record["repo"]["source_clone"]))):
            return {"safe": True, "at": time.time(), "blockers": [], "absent": True,
                    "reason": "reserved removal completed; directory and registration absent"}
    problem = migration.legacy_problem(obj, record)
    if problem:
        return {"safe": False, "at": time.time(), "blockers": [problem]}
    obj.record_path(record)
    return migration.evidence(obj, record) if record.get("migration") else obj.safety(record, persist=False)


def recheck_parked(obj: Lifecycle) -> dict[str, Any]:
    """Caller holds maintenance.lock and claim.lock through reservation/removal."""
    from worktree_lifecycle import atomic_json

    result: dict[str, Any] = {"rechecked": 0, "removed": 0, "still_parked": 0, "by_reason": {}}
    journal_path = obj.state / "migration.json"
    journal = read_object(journal_path)
    for record in obj.read_records():
        if record["state"] != "parked":
            continue
        blockers = []
        worker = record.get("worker")
        if record.get("holder"):
            blockers = ["holder claim remains"]
        elif worker and safety.process_state(worker) != "dead":
            blockers = ["live or uncertain worker"]
        else:
            result["rechecked"] += 1
            row = next((row for row in journal.get("candidates", [])
                        if row["id"] == record["id"] and row["path"] == record["path"]), None)
            try:
                if row and record.get("migration"):
                    # A prior record may have inherited the configured clone.
                    # The journal mapping still has to pass live Git validation.
                    record["repo"] = {"remote": obj.url, "source_clone": row["source_clone"], "common_dir": row["common_dir"]}
                record["safety"] = evidence(obj, record, recover_removed=True)
                if record["safety"].get("absent"):
                    record.update(state="removed", waiting=None, error=None)
                elif record["safety"]["safe"]:
                    record["worker"] = safety.process_identity(os.getpid())
                    obj.transition(record, "parked-recheck", "reserved")
                    # Re-read Git, ignored outputs and process evidence after
                    # persisting the reservation, just as migration does.
                    record["safety"] = evidence(obj, record)
                    migration.process_check([record])
                    if record["safety"]["safe"]:
                        path = Path(record["path"])
                        safety.remove_outputs(path, record["safety"]["reproducible"])
                        wt.git(Path(record["repo"]["source_clone"]), "worktree", "remove", str(path))
                        record.update(state="removed", waiting=None, error=None)
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                detail = exc.stderr if isinstance(exc, subprocess.CalledProcessError) else str(exc)
                record["safety"] = {"safe": False, "at": time.time(), "blockers": [f"safety evidence unavailable: {detail}"]}
            blockers = record["safety"]["blockers"]
            if record["state"] == "removed":
                result["removed"] += 1
            record["worker"] = None
            obj.transition(record, "parked-recheck", "complete")
            if row:
                row.update(phase=record["state"], safety=record["safety"])
                atomic_json(journal_path, journal)
        if record["state"] == "parked":
            result["still_parked"] += 1
            for reason in {blocker.split(":", 1)[0] for blocker in blockers}:
                result["by_reason"][reason] = result["by_reason"].get(reason, 0) + 1
    return result
