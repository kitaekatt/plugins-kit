"""Repository-owned lifecycle commands and their durable process/resource reports."""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator


class ExtensionProcess(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    pid: StrictInt = Field(gt=0)
    start: str = Field(min_length=1)
    runtime_paths: list[str] = Field(default_factory=list)

    @field_validator("start")
    @classmethod
    def check_start(cls, value: str) -> str:
        if not value.strip() or "\0" in value:
            raise ValueError("process start identity must be nonempty without NUL")
        return value

    @field_validator("runtime_paths")
    @classmethod
    def check_runtime_paths(cls, values: list[str]) -> list[str]:
        if any(not Path(value).is_absolute() or "\0" in value for value in values):
            raise ValueError("runtime_paths must contain absolute paths without NUL")
        return values


class ExtensionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    processes: list[ExtensionProcess] = Field(default_factory=list)
    resources: list[Any] = Field(default_factory=list)
    result: Any = None


class ExtensionError(ValueError):
    def __init__(self, reason: str, log: Path, returncode: int | None = None) -> None:
        self.log = str(log)
        self.returncode = returncode
        super().__init__(f"{reason}; log: {log}")


def pending_blockers(record: dict[str, Any]) -> list[str]:
    """Unresolved script/report evidence cannot authorize any lifecycle disposal."""
    extension = record.get("extension")
    if extension is None:
        return []
    if not isinstance(extension, dict):
        return ["uncertain extension history: expected phase records"]
    blockers = []
    for phase, entries in extension.items():
        if not isinstance(entries, list):
            blockers.append(f"uncertain extension {phase}: expected command records")
            continue
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                blockers.append(f"uncertain extension {phase}[{index}]: invalid command record")
            elif entry.get("outcome") not in ("complete", "failed") or entry.get("report_uncertain"):
                blockers.append(f"uncertain extension {phase}[{index}]: interrupted command or unreadable report; "
                                f"result: {entry.get('result_path')}; log: {entry.get('log')}")
    return blockers


def merge_report(record: dict[str, Any], entry: dict[str, Any], result_path: Path) -> None:
    """An empty report never erases earlier process evidence or resource history."""
    if result_path.is_symlink():
        raise ValueError(f"extension result must be a regular JSON file: {result_path}")
    if not result_path.exists():
        return
    raw = json.loads(result_path.read_text(encoding="utf-8"))
    # Keep identifiable processes even if another part of the report is invalid.
    # Invalid process evidence is deliberately retained as an uncertain blocker.
    if isinstance(raw, dict) and "processes" in raw:
        processes = raw["processes"]
        if not isinstance(processes, list):
            processes = [{"error": "invalid extension processes", "reported": processes}]
        for process in processes:
            try:
                preserved = ExtensionProcess.model_validate(process).model_dump()
            except ValueError:
                preserved = process if isinstance(process, dict) else {"error": "invalid extension process", "reported": process}
            if preserved not in record["processes"]:
                record["processes"].append(preserved)
    result = ExtensionResult.model_validate(raw)
    entry.update(result=result.result, resources=result.resources,
                 processes=[process.model_dump() for process in result.processes])
    for process in result.processes:
        normalized = process.model_dump()
        if normalized not in record["processes"]:
            record["processes"].append(normalized)


def run(record: dict[str, Any], phase: str, commands: list[list[str]], state: Path,
        save: Callable[[dict[str, Any]], None]) -> None:
    """Run sequential argv commands at the final path, preserving results on failure."""
    if phase not in {"after_take", "before_release"}:
        raise ValueError(f"Unknown lifecycle extension phase: {phase}")
    if not commands:
        return
    if record.get("extension") is None:
        record["extension"] = {}
    record.setdefault("processes", [])
    entries = record["extension"].setdefault(phase, [])
    for index, argv in enumerate(commands):
        token = uuid.uuid4().hex
        stem = f"{record['id']}-{phase}-{token}"
        log = state / "logs" / f"{stem}.log"
        result_path = state / "extensions" / f"{stem}.json"
        log.parent.mkdir(parents=True, exist_ok=True)
        result_path.parent.mkdir(parents=True, exist_ok=True)
        entry: dict[str, Any] = {"argv": argv, "path": record["path"], "at": time.time(),
            "log": str(log), "result_path": str(result_path), "exit": None,
            "outcome": "running", "result": None, "processes": [], "resources": [],
            "report_uncertain": False}
        entries.append(entry)
        record["operation"]["phase"] = f"{phase}:{index}"
        save(record)
        env = {**os.environ, "GIT_KIT_WORKTREE_ID": record["id"],
               "GIT_KIT_WORKTREE_PATH": record["path"],
               "GIT_KIT_WORKTREE_CLAIM": (record.get("holder") or {}).get("claim", ""),
               "GIT_KIT_EXTENSION_PHASE": phase, "GIT_KIT_EXTENSION_RESULT": str(result_path)}
        try:
            with log.open("a", encoding="utf-8") as stream:
                stream.write(f"cwd={record['path']}\nargv={json.dumps(argv)}\n")
                stream.flush()
                completed = subprocess.run(argv, cwd=record["path"], env=env,
                    stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT, check=False)
            entry["exit"] = completed.returncode
            entry["report_uncertain"] = True
            merge_report(record, entry, result_path)
            entry["report_uncertain"] = False
            if completed.returncode:
                raise ValueError(f"command exited {completed.returncode}")
            entry["outcome"] = "complete"
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            entry.update(outcome="failed", reason=str(exc), finished=time.time())
            save(record)
            raise ExtensionError(f"{phase}[{index}] {argv!r}: {exc}", log, entry["exit"]) from exc
        entry["finished"] = time.time()
        save(record)
