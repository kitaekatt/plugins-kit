#!/usr/bin/env python3
"""Drive the two tree-wide human-html passes without doing agent work.

Usage:
    python human_html_tree.py placement <repository-root> [--json]
    python human_html_tree.py record-placement <repository-root> <directory> \
        --decision page|none --identity <line> --source-sha <sha> \
        --dirty true|false --brief-sha256 <digest> [--json]
    python human_html_tree.py start-generation <repository-root> \
        --framework <path> [--json]
    python human_html_tree.py generation <repository-root> \
        --framework <path> [--json]
    python human_html_tree.py complete-generation <repository-root> <directory> \
        --framework <path> --framework-sha256 <digest> --source-sha <sha> \
        --run-key <digest> --references-json <json-array> [--json]

The script is the mechanical half of AD-1 and TS-1. It delegates repository
enumeration, territory computation, record status, and ordering to
`discover_human_html.py`. The md-domain lanes perform HC-1 inference and page
writing. This script gives those lanes one restart-safe work item at a time,
persists their narrow record changes, and refuses an out-of-order completion.

The `placement` and `generation` commands are read-only plans. A lane repeats a
plan after each record command. Placement uses fresh decision records as its
finished set. Generation uses a checkpoint that is bound to its complete input
and records a directory only after the contract checker passes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping

_PLUGIN_ROOT = Path(__file__).resolve().parents[3]
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
_MD_DOMAIN_DIR = _SCRIPTS_DIR.parent

from skills_kit_lib import human_html as hh  # noqa: E402

import discover_human_html as discover  # noqa: E402
import human_html_check as checker  # noqa: E402


STATUS_BLOCKED = "blocked"
STATUS_COMPLETE = "complete"
STATUS_WORK = "work"

CHECKPOINT_RELATIVE_PATH = Path(".databench") / "human-tree-generation.json"
CHECKPOINT_SCHEMA_VERSION = 1
CHECKPOINT_STATES = ("in-progress", "complete")
GENERATION_INPUT_PATHS = (
    ("generation-lane", _MD_DOMAIN_DIR / "references" / "lanes" / "generation-lane.md"),
    (
        "human-html-standards",
        _MD_DOMAIN_DIR / "references" / "standards" / "human-html-standards.md",
    ),
    (
        "human-html-presentation",
        _MD_DOMAIN_DIR / "references" / "human-html-presentation.md",
    ),
    # The prose contract. The three documents above describe the job in the
    # generator's own vocabulary -- what a page owns, the territory it covers --
    # and an agent that reads only them publishes that scaffolding into the page
    # it writes. This one says those words are for the instructions, not for the
    # reader, and carries the rest of the writing standard with it.
    ("technical-english", _MD_DOMAIN_DIR / "references" / "technical-english.md"),
)


class DriverError(Exception):
    """The driver cannot proceed without violating a human-html contract."""


def _bool_arg(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise argparse.ArgumentTypeError("expected true or false")


def _root_path(repo_root: str | Path) -> Path:
    root = Path(repo_root).resolve()
    if not root.is_dir():
        raise DriverError("repository root is not a directory: %s" % root)
    return root


def _placement_reasons(entry: Mapping[str, Any]) -> list[str]:
    reasons: list[str] = []
    record_status = entry["record"]["status"]
    if record_status != discover.RECORD_STATUS_FRESH:
        reasons.append("record-%s" % record_status)
    if entry["stale_children"]:
        reasons.append("prerequisite-records:%s" % ",".join(entry["stale_children"]))
    return reasons


def _placement_job(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Return one HC placement brief without recomputing discovery data."""
    job = {
        "action": "decide",
        "directory": entry["directory"],
        "depth": entry["depth"],
        "reasons": _placement_reasons(entry),
        "source_sha": entry["source_sha"],
        "dirty": entry["dirty"],
        "record": entry["record"],
        "territory": entry["territory"],
        "stale_children": entry["stale_children"],
        "nearest_page_ancestor": entry["nearest_page_ancestor"],
        "nearest_page_descendants": entry["nearest_page_descendants"],
    }
    job["brief_sha256"] = _json_digest(job)
    return job


def _json_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _placement_plan(root: Path, result: Mapping[str, Any]) -> dict[str, Any]:
    """Build a placement plan from one CK-2 discovery snapshot."""
    entries = result["directories"]
    pending_entries = [entry for entry in entries if entry["stale"]]
    work = [_placement_job(entry) for entry in pending_entries]
    blockers = [
        {
            "directory": entry["directory"],
            "code": "source-stamp-unavailable",
            "message": entry["stamp_error"],
        }
        for entry in pending_entries
        if entry["source_sha"] is None
    ]
    blockers.extend(
        {
            "directory": entry["directory"],
            "code": "decision-record-invalid",
            "message": entry["record"]["error"],
        }
        for entry in pending_entries
        if entry["record"]["status"] == discover.RECORD_STATUS_INVALID
    )
    if blockers:
        status = STATUS_BLOCKED
        next_job = None
    elif work:
        status = STATUS_WORK
        next_job = work[0]
    else:
        status = STATUS_COMPLETE
        next_job = None
    return {
        "phase": "placement",
        "status": status,
        "repo_root": str(root),
        "order": "deepest-first",
        "subject_count": result["count"],
        "directory_order": [entry["directory"] for entry in entries],
        "fresh_count": len(entries) - len(pending_entries),
        "pending_count": len(work),
        "blocked_count": len(blockers),
        "blockers": blockers,
        "diagnostics": list(result.get("diagnostics", [])),
        "work": work,
        "next": next_job,
    }


def placement_plan(repo_root: str | Path) -> dict[str, Any]:
    """Return the restart-safe, deepest-first PLACEMENT plan."""
    root = _root_path(repo_root)
    return _placement_plan(root, discover.scan(root))


def _load_existing_record(path: Path) -> hh.Record | None:
    if not path.is_file():
        return None
    return hh.load_record(path)


def _write_record_atomic(
    repo_root: Path,
    path: Path,
    record: hh.Record | Mapping[str, Any],
) -> hh.Record:
    """Replace one validated decision record without exposing a partial write."""
    record_key = hashlib.sha256(
        path.relative_to(repo_root).as_posix().encode("utf-8")
    ).hexdigest()
    temporary = repo_root / ".databench" / (".human-record-%s.tmp" % record_key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            hh.dumps_record(record),
            encoding="ascii",
            newline="\n",
        )
        temporary.replace(path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise DriverError("cannot write decision record %s: %s" % (path, exc)) from exc
    return hh.load_record(path)


def record_placement(
    repo_root: str | Path,
    directory: str | Path,
    decision: str,
    identity: str,
    source_sha: str,
    dirty: bool,
    brief_sha256: str,
) -> dict[str, Any]:
    """Persist the decision, territory stamp, dirty state, and identity (DR-1)."""
    root = _root_path(repo_root)
    plan = placement_plan(root)
    if plan["status"] == STATUS_BLOCKED:
        raise DriverError("placement is blocked: %s" % json.dumps(plan["blockers"]))
    job = plan["next"]
    if job is None:
        raise DriverError("placement is complete; no decision record is pending")

    normalized = hh.normalize_directory(directory)
    if normalized != job["directory"]:
        raise DriverError(
            "placement order violation: expected %r, got %r"
            % (job["directory"], normalized)
        )
    if brief_sha256 != job["brief_sha256"]:
        raise DriverError(
            "placement brief changed for %r: expected sha256=%s, got %s. "
            "read the next placement plan again"
            % (normalized, brief_sha256, job["brief_sha256"])
        )
    if source_sha != job["source_sha"] or dirty is not job["dirty"]:
        raise DriverError(
            "placement brief changed for %r: expected source_sha=%s dirty=%s, "
            "got source_sha=%s dirty=%s. Read the next placement plan again"
            % (
                normalized,
                job["source_sha"],
                str(job["dirty"]).lower(),
                source_sha,
                str(dirty).lower(),
            )
        )
    if decision not in hh.DECISIONS:
        raise DriverError("decision must be page or none, got %r" % decision)
    if decision == hh.DECISION_NONE and identity != "":
        raise DriverError("identity must be an empty string for a none decision")

    path = hh.record_path(root, normalized)
    previous = _load_existing_record(path)
    references = previous.references if previous and decision == hh.DECISION_PAGE else ()
    record = hh.Record(
        directory=normalized,
        decision=decision,
        source_sha=source_sha,
        dirty=dirty,
        identity=identity,
        instructions=hh.read_instructions(path),
        references=references,
    )
    written = _write_record_atomic(root, path, record)

    affected: list[str] = []
    if previous is not None and previous.decision != decision:
        nearest_ancestor = job["nearest_page_ancestor"]
        if nearest_ancestor is not None:
            affected.append(nearest_ancestor)
        affected.extend(
            candidate
            for candidate in job["nearest_page_descendants"]
            if candidate not in affected
        )
    findings = [
        {
            "level": checker.INFO,
            "code": "STALE",
            "directory": page,
            "message": "placement change at %s alters this page's territory or navigation"
            % normalized,
        }
        for page in affected
    ]

    return {
        "phase": "placement",
        "status": "recorded",
        "directory": normalized,
        "record_path": path.relative_to(root).as_posix(),
        "record": written.to_dict(),
        "placement_drift": bool(previous and previous.decision != decision),
        "affected_pages": affected,
        "findings": findings,
    }


def _framework_info(path: str | Path) -> dict[str, Any]:
    framework_path = Path(path).resolve()
    if not framework_path.is_file():
        raise DriverError("generation framework is not a file: %s" % framework_path)
    try:
        data = framework_path.read_bytes()
    except OSError as exc:
        raise DriverError("cannot read generation framework %s: %s" % (framework_path, exc)) from exc
    return {
        "path": str(framework_path),
        "sha256": hashlib.sha256(data).hexdigest(),
        "byte_count": len(data),
        "delivery": "verbatim",
    }


def _generation_input_info() -> list[dict[str, Any]]:
    """Identify each complete plugin file supplied to the generating agent."""
    inputs: list[dict[str, Any]] = []
    for name, path in GENERATION_INPUT_PATHS:
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise DriverError("cannot read generation input %s: %s" % (path, exc)) from exc
        inputs.append(
            {
                "name": name,
                "path": str(path),
                "sha256": hashlib.sha256(data).hexdigest(),
                "byte_count": len(data),
                "delivery": "complete-file",
            }
        )
    return inputs


def _generation_run_key(
    entries: list[Mapping[str, Any]],
    framework: Mapping[str, Any],
    generation_inputs: list[Mapping[str, Any]],
) -> str:
    """Identify the exact placement and framework a generation run consumes."""
    records = [
        {
            "directory": entry["directory"],
            "decision": entry["record"]["decision"],
            "source_sha": entry["record"]["source_sha"],
            "dirty": entry["record"]["dirty"],
            "identity": entry["record"]["identity"],
            "instructions": entry["record"]["instructions"],
        }
        for entry in entries
    ]
    payload = {
        "framework_sha256": framework["sha256"],
        "generation_inputs": [
            {"name": item["name"], "sha256": item["sha256"]}
            for item in generation_inputs
        ],
        "records": records,
    }
    return _json_digest(payload)


def _checkpoint_path(repo_root: Path) -> Path:
    return repo_root / CHECKPOINT_RELATIVE_PATH


def _read_checkpoint(
    repo_root: Path,
    run_key: str,
    subjects: set[str],
) -> tuple[str, set[str], str | None]:
    """Return checkpoint freshness, finished directories, and run state."""
    path = _checkpoint_path(repo_root)
    if not path.is_file():
        return "missing", set(), None
    try:
        data = json.loads(path.read_text(encoding="ascii"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise DriverError("generation checkpoint is unreadable: %s (%s)" % (path, exc)) from exc
    if not isinstance(data, Mapping):
        raise DriverError("generation checkpoint must be a JSON object: %s" % path)
    if set(data) != {"schema_version", "run_key", "state", "finished"}:
        raise DriverError("generation checkpoint has invalid fields: %s" % path)
    if (
        not isinstance(data["schema_version"], int)
        or isinstance(data["schema_version"], bool)
        or data["schema_version"] != CHECKPOINT_SCHEMA_VERSION
    ):
        raise DriverError(
            "generation checkpoint schema_version must be %d: %s"
            % (CHECKPOINT_SCHEMA_VERSION, path)
        )
    if (
        not isinstance(data["run_key"], str)
        or data["state"] not in CHECKPOINT_STATES
        or not isinstance(data["finished"], list)
    ):
        raise DriverError("generation checkpoint has invalid value types: %s" % path)
    finished: list[str] = []
    for raw in data["finished"]:
        if not isinstance(raw, str):
            raise DriverError("generation checkpoint has a non-string finished item: %s" % path)
        normalized = hh.normalize_directory(raw)
        if normalized != raw:
            raise DriverError(
                "generation checkpoint has a non-normalized directory %r: %s" % (raw, path)
            )
        finished.append(normalized)
    if len(finished) != len(set(finished)):
        raise DriverError("generation checkpoint has duplicate finished directories: %s" % path)
    if data["run_key"] != run_key:
        return "stale", set(), data["state"]
    unknown = sorted(set(finished) - subjects)
    if unknown:
        raise DriverError(
            "generation checkpoint names non-subject directories %s: %s"
            % (", ".join(repr(item) for item in unknown), path)
        )
    return "active", set(finished), data["state"]


def _write_checkpoint(
    repo_root: Path,
    run_key: str,
    finished: set[str],
    state: str,
) -> Path:
    """Persist the generation finished set atomically in deterministic JSON."""
    if state not in CHECKPOINT_STATES:
        raise DriverError("generation checkpoint state must be in-progress or complete")
    path = _checkpoint_path(repo_root)
    temporary = path.with_name(".%s.tmp" % path.name)
    data = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "run_key": run_key,
        "state": state,
        "finished": sorted(finished, key=lambda item: (-discover.depth_of(item), item)),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(data, indent=2, ensure_ascii=True, sort_keys=False) + "\n",
            encoding="ascii",
            newline="\n",
        )
        temporary.replace(path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise DriverError("cannot write generation checkpoint %s: %s" % (path, exc)) from exc
    return path


def _navigation_target(
    directory: str,
    relationship: str,
    records: Mapping[str, hh.Record],
) -> dict[str, Any]:
    record = records.get(directory)
    if record is None:
        raise DriverError("navigation target has no valid record: %s" % directory)
    return {
        "directory": directory,
        "relationship": relationship,
        "label": hh.navigation_label(directory),
        "identity": record.identity,
    }


def _generation_job(
    entry: Mapping[str, Any],
    records: Mapping[str, hh.Record],
    findings: list[dict[str, Any]],
    framework: Mapping[str, Any],
    run_key: str,
) -> dict[str, Any]:
    directory = entry["directory"]
    decision = entry["record"]["decision"]
    up = entry["nearest_page_ancestor"]
    down = entry["nearest_page_descendants"]
    navigation = {
        "up": _navigation_target(up, "nearest-page-ancestor", records) if up else None,
        "down": [
            _navigation_target(child, "nearest-page-descendant", records)
            for child in down
        ],
    }
    return {
        "action": "generate" if decision == hh.DECISION_PAGE else "remove",
        "directory": directory,
        "depth": entry["depth"],
        "source_sha": entry["source_sha"],
        "dirty": entry["dirty"],
        "record": entry["record"],
        "territory": entry["territory"],
        "navigation": navigation,
        "framework": dict(framework),
        "run_key": run_key,
        "existing_output": {
            "page": entry["page_file"],
            "references": entry["reference_files"],
        },
        "findings": findings,
    }


def generation_plan(repo_root: str | Path, framework_path: str | Path) -> dict[str, Any]:
    """Return the leaf-first GENERATION plan, gated on complete placement."""
    root = _root_path(repo_root)
    framework = _framework_info(framework_path)
    generation_inputs = _generation_input_info()
    discovery = discover.scan(root)
    placement = _placement_plan(root, discovery)
    base: dict[str, Any] = {
        "phase": "generation",
        "repo_root": str(root),
        "order": "leaf-first",
        "subject_count": placement["subject_count"],
        "directory_order": placement["directory_order"],
        "framework": framework,
        "generation_inputs": generation_inputs,
    }
    if placement["status"] != STATUS_COMPLETE:
        return {
            **base,
            "status": STATUS_BLOCKED,
            "blockers": [
                {
                    "code": "placement-incomplete",
                    "message": "%d placement record(s) are not fresh"
                    % placement["pending_count"],
                },
                *placement["blockers"],
            ],
            "placement_pending_count": placement["pending_count"],
            "page_count": None,
            "current_count": 0,
            "pending_count": 0,
            "work": [],
            "next": None,
        }

    entries = discovery["directories"]
    entries_by_directory = {entry["directory"]: entry for entry in entries}
    records, _record_errors = discover.load_records(root)
    run_key = _generation_run_key(entries, framework, generation_inputs)
    checkpoint_status, finished, checkpoint_state = _read_checkpoint(
        root,
        run_key,
        set(entries_by_directory),
    )
    stored_finished = set(finished)
    checked = checker.check(root)
    verified_discovery = discover.scan(root)
    if _json_digest(verified_discovery["directories"]) != _json_digest(entries):
        return {
            **base,
            "status": STATUS_BLOCKED,
            "blockers": [
                {
                    "code": "tree-changed-during-plan",
                    "message": (
                        "human-html inputs changed while generation was being planned. "
                        "Run the plan again"
                    ),
                }
            ],
            "placement_pending_count": 0,
            "run_key": run_key,
            "checkpoint": {
                "path": _checkpoint_path(root).relative_to(root).as_posix(),
                "status": checkpoint_status,
                "run_state": checkpoint_state if checkpoint_status == "active" else None,
                "finished_count": len(finished),
            },
            "page_count": None,
            "current_count": 0,
            "pending_count": 0,
            "work": [],
            "next": None,
        }
    findings_by_directory: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for finding in checked["findings"]:
        findings_by_directory[finding["directory"]].append(finding)

    blockers = [
        finding
        for finding in checked["findings"]
        if finding["level"] == checker.FAIL
        and finding["directory"] not in entries_by_directory
    ]
    invalid_finished: list[str] = []
    for entry in entries:
        directory = entry["directory"]
        if directory not in finished:
            continue
        local_findings = findings_by_directory[directory]
        failures = [finding for finding in local_findings if finding["level"] == checker.FAIL]
        stale = [finding for finding in local_findings if finding["code"] == "STALE"]
        if entry["record"]["decision"] == hh.DECISION_PAGE:
            invalid = bool(failures or stale)
        else:
            invalid = bool(entry["page_file"] or entry["reference_files"] or failures)
        if invalid:
            invalid_finished.append(directory)
    if invalid_finished:
        finished.difference_update(invalid_finished)
    work: list[dict[str, Any]] = []
    current_count = 0
    page_count = 0
    for entry in entries:
        directory = entry["directory"]
        decision = entry["record"]["decision"]
        local_findings = findings_by_directory[directory]
        failures = [finding for finding in local_findings if finding["level"] == checker.FAIL]
        stale = [finding for finding in local_findings if finding["code"] == "STALE"]
        if decision == hh.DECISION_PAGE:
            page_count += 1
            needs_work = directory not in finished or bool(failures or stale)
        else:
            needs_work = bool(entry["page_file"] or entry["reference_files"] or failures)
        if needs_work:
            work.append(_generation_job(entry, records, local_findings, framework, run_key))
        else:
            current_count += 1

    if blockers:
        status = STATUS_BLOCKED
        next_job = None
    elif work:
        status = STATUS_WORK
        next_job = work[0]
    else:
        status = STATUS_COMPLETE
        next_job = None
    return {
        **base,
        "status": status,
        "blockers": blockers,
        "placement_pending_count": 0,
        "run_key": run_key,
        "checkpoint": {
            "path": _checkpoint_path(root).relative_to(root).as_posix(),
            "status": checkpoint_status,
            "run_state": checkpoint_state if checkpoint_status == "active" else None,
            "finished_count": len(finished),
            "stored_finished_count": len(stored_finished),
            "finished": [
                entry["directory"]
                for entry in entries
                if entry["directory"] in finished
            ],
            "invalidated": [
                entry["directory"]
                for entry in entries
                if entry["directory"] in stored_finished - finished
            ],
        },
        "page_count": page_count,
        "current_count": current_count,
        "pending_count": len(work),
        "work": work,
        "next": next_job,
    }


def _blocked_generation_start(
    root: Path,
    plan: Mapping[str, Any],
    mode: str | None,
) -> dict[str, Any]:
    return {
        "phase": "generation",
        "status": STATUS_BLOCKED,
        "mode": mode,
        "repo_root": str(root),
        "blockers": plan["blockers"],
        "placement_pending_count": plan["placement_pending_count"],
    }


def start_generation(repo_root: str | Path, framework_path: str | Path) -> dict[str, Any]:
    """Start a full generation run, or resume its incomplete checkpoint."""
    root = _root_path(repo_root)
    plan = generation_plan(root, framework_path)
    if plan["status"] == STATUS_BLOCKED:
        return _blocked_generation_start(root, plan, None)

    checkpoint = plan["checkpoint"]
    reopened = list(checkpoint.get("invalidated", []))
    if checkpoint["status"] == "active" and checkpoint["run_state"] == "in-progress":
        mode = "resume"
        if reopened:
            _write_checkpoint(
                root,
                plan["run_key"],
                set(checkpoint["finished"]),
                "in-progress",
            )
    else:
        _write_checkpoint(root, plan["run_key"], set(), "in-progress")
        mode = "restart" if checkpoint["status"] == "active" else "start"

    refreshed = generation_plan(root, framework_path)
    if refreshed["status"] == STATUS_BLOCKED:
        return _blocked_generation_start(root, refreshed, mode)
    if refreshed["status"] == STATUS_COMPLETE:
        completed = set(refreshed["checkpoint"]["finished"])
        _write_checkpoint(
            root,
            refreshed["run_key"],
            completed,
            "complete",
        )
        refreshed = generation_plan(root, framework_path)
        if refreshed["status"] == STATUS_BLOCKED:
            return _blocked_generation_start(root, refreshed, mode)
    return {
        "phase": "generation",
        "status": refreshed["status"],
        "mode": mode,
        "reopened": reopened,
        "repo_root": str(root),
        "run_key": refreshed["run_key"],
        "checkpoint": refreshed["checkpoint"],
        "pending_count": refreshed["pending_count"],
        "next": refreshed["next"],
        "blockers": refreshed["blockers"],
    }


def _parse_references(text: str) -> list[dict[str, str]]:
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise DriverError("references JSON is invalid: %s" % exc) from exc
    if not isinstance(value, list):
        raise DriverError("references JSON must be an array")
    return value


def complete_generation(
    repo_root: str | Path,
    directory: str | Path,
    framework_path: str | Path,
    framework_sha256: str,
    source_sha: str,
    run_key: str,
    references_json: str,
) -> dict[str, Any]:
    """Record only `references`, then check one finished generation item."""
    root = _root_path(repo_root)
    normalized = hh.normalize_directory(directory)
    plan = generation_plan(root, framework_path)
    if plan["framework"]["sha256"] != framework_sha256:
        raise DriverError(
            "generation framework changed: expected sha256=%s, got %s. Read the plan again"
            % (framework_sha256, plan["framework"]["sha256"])
        )
    if plan["status"] == STATUS_BLOCKED:
        raise DriverError("generation is blocked: %s" % json.dumps(plan["blockers"]))
    if plan.get("run_key") != run_key:
        raise DriverError(
            "generation inputs changed: expected run_key=%s, got %s. Read the plan again"
            % (run_key, plan.get("run_key"))
        )

    order_index = {item: index for index, item in enumerate(plan["directory_order"])}
    if normalized not in order_index:
        raise DriverError("directory is not a human-html subject: %r" % normalized)
    pending = {job["directory"] for job in plan["work"]}
    current_record = hh.load_record(hh.record_path(root, normalized))
    # Removing a none directory's generated files makes its work item disappear
    # before completion. A clean none record can still complete this run item.
    if normalized not in pending and current_record.decision != hh.DECISION_NONE:
        raise DriverError("directory is not pending generation: %r" % normalized)
    earlier = [
        job["directory"]
        for job in plan["work"]
        if order_index[job["directory"]] < order_index[normalized]
    ]
    if earlier:
        raise DriverError(
            "generation order violation: finish %s before %s"
            % (", ".join(earlier), normalized)
        )

    path = hh.record_path(root, normalized)
    record = current_record
    if record.source_sha != source_sha:
        raise DriverError(
            "generation brief changed for %r: expected source_sha=%s, got %s. "
            "read the plan again" % (normalized, source_sha, record.source_sha)
        )
    references = _parse_references(references_json)
    before = record.to_dict()
    references_changed = False
    if record.decision == hh.DECISION_NONE:
        if references:
            raise DriverError("references must be empty for a none decision")
    else:
        updated = dict(before)
        updated["references"] = references
        written = _write_record_atomic(root, path, updated)
        after = written.to_dict()
        for field in hh.RECORD_FIELDS:
            if field != "references" and after[field] != before[field]:
                raise DriverError("generation changed record field %r" % field)
        references_changed = after["references"] != before["references"]

    result = checker.check(root, normalized)
    stale = [finding for finding in result["findings"] if finding["code"] == "STALE"]
    checkpoint: str | None = None
    if result["fail_count"] == 0 and not stale:
        finished = set(plan["checkpoint"]["finished"])
        finished.add(normalized)
        remaining = {
            job["directory"]
            for job in plan["work"]
            if job["directory"] != normalized
        }
        state = "complete" if not remaining else "in-progress"
        checkpoint = _write_checkpoint(
            root,
            plan["run_key"],
            finished,
            state,
        ).relative_to(root).as_posix()
    return {
        "phase": "generation",
        "status": (
            STATUS_COMPLETE
            if result["fail_count"] == 0 and not stale
            else STATUS_BLOCKED
        ),
        "directory": normalized,
        "record_path": path.relative_to(root).as_posix(),
        "checkpoint_path": checkpoint,
        "references_changed": references_changed,
        "fail_count": result["fail_count"],
        "info_count": result["info_count"],
        "findings": result["findings"],
    }


def render_placement(plan: Mapping[str, Any]) -> str:
    lines = [
        "PLACEMENT %s: %d subjects; %d fresh; %d to decide; order=%s"
        % (
            plan["status"].upper(),
            plan["subject_count"],
            plan["fresh_count"],
            plan["pending_count"],
            plan["order"],
        )
    ]
    for blocker in plan["blockers"]:
        lines.append("BLOCKED %s: %s" % (blocker["directory"], blocker["message"]))
    for job in plan["work"]:
        lines.append(
            "WOULD DECIDE %s depth=%d (%s)"
            % (job["directory"], job["depth"], "; ".join(job["reasons"]))
        )
    if plan["next"] is not None:
        lines.append("NEXT %s" % plan["next"]["directory"])
    return "\n".join(lines)


def render_record_placement(result: Mapping[str, Any]) -> str:
    lines = ["RECORDED PLACEMENT %s" % result["directory"]]
    for finding in result["findings"]:
        lines.append(
            "%s %s %s: %s"
            % (
                finding["level"],
                finding["code"],
                finding["directory"],
                finding["message"],
            )
        )
    return "\n".join(lines)


def render_generation(plan: Mapping[str, Any]) -> str:
    lines = [
        "GENERATION %s: %d subjects; order=%s; framework-sha256=%s"
        % (
            plan["status"].upper(),
            plan["subject_count"],
            plan["order"],
            plan["framework"]["sha256"],
        )
    ]
    if plan["status"] == STATUS_BLOCKED and plan["placement_pending_count"]:
        lines.append(
            "BLOCKED placement-incomplete: %d record(s) require placement; no page work can start"
            % plan["placement_pending_count"]
        )
    for blocker in plan["blockers"]:
        if blocker.get("code") == "placement-incomplete":
            continue
        lines.append(
            "BLOCKED %s: %s"
            % (blocker.get("directory", "."), blocker.get("message", blocker.get("code")))
        )
    for job in plan["work"]:
        lines.append("WOULD %s %s depth=%d" % (job["action"].upper(), job["directory"], job["depth"]))
    if plan["next"] is not None:
        lines.append("NEXT %s" % plan["next"]["directory"])
    return "\n".join(lines)


def _emit(result: Mapping[str, Any], as_json: bool, renderer: Any) -> None:
    if as_json:
        print(json.dumps(result, indent=2, ensure_ascii=True))
    else:
        print(renderer(result))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plan and checkpoint the two human-html tree passes.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    placement = subparsers.add_parser("placement", help="print the placement plan")
    placement.add_argument("repo_root")
    placement.add_argument("--json", action="store_true")

    record_placement_parser = subparsers.add_parser(
        "record-placement", help="persist one placement decision"
    )
    record_placement_parser.add_argument("repo_root")
    record_placement_parser.add_argument("directory")
    record_placement_parser.add_argument("--decision", choices=hh.DECISIONS, required=True)
    record_placement_parser.add_argument("--identity", default="")
    record_placement_parser.add_argument("--source-sha", required=True)
    record_placement_parser.add_argument("--dirty", type=_bool_arg, required=True)
    record_placement_parser.add_argument("--brief-sha256", required=True)
    record_placement_parser.add_argument("--json", action="store_true")

    generation = subparsers.add_parser("generation", help="print the generation plan")
    generation.add_argument("repo_root")
    generation.add_argument("--framework", required=True)
    generation.add_argument("--json", action="store_true")

    start_generation_parser = subparsers.add_parser(
        "start-generation", help="start a full generation run or resume an incomplete one"
    )
    start_generation_parser.add_argument("repo_root")
    start_generation_parser.add_argument("--framework", required=True)
    start_generation_parser.add_argument("--json", action="store_true")

    complete = subparsers.add_parser(
        "complete-generation", help="persist references and check one generated directory"
    )
    complete.add_argument("repo_root")
    complete.add_argument("directory")
    complete.add_argument("--framework", required=True)
    complete.add_argument("--framework-sha256", required=True)
    complete.add_argument("--source-sha", required=True)
    complete.add_argument("--run-key", required=True)
    complete.add_argument("--references-json", default="[]")
    complete.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "placement":
            result = placement_plan(args.repo_root)
            _emit(result, args.json, render_placement)
            return 1 if result["status"] == STATUS_BLOCKED else 0
        if args.command == "record-placement":
            result = record_placement(
                args.repo_root,
                args.directory,
                args.decision,
                args.identity,
                args.source_sha,
                args.dirty,
                args.brief_sha256,
            )
            _emit(result, args.json, render_record_placement)
            return 0
        if args.command == "generation":
            result = generation_plan(args.repo_root, args.framework)
            _emit(result, args.json, render_generation)
            return 1 if result["status"] == STATUS_BLOCKED else 0
        if args.command == "start-generation":
            result = start_generation(args.repo_root, args.framework)
            _emit(
                result,
                args.json,
                lambda value: "GENERATION %s: mode=%s; %s pending"
                % (
                    value["status"].upper(),
                    value["mode"] or "blocked",
                    value.get("pending_count", 0),
                ),
            )
            return 1 if result["status"] == STATUS_BLOCKED else 0
        result = complete_generation(
            args.repo_root,
            args.directory,
            args.framework,
            args.framework_sha256,
            args.source_sha,
            args.run_key,
            args.references_json,
        )
        _emit(
            result,
            args.json,
            lambda value: "GENERATION %s %s: %d FAIL, %d INFO"
            % (
                value["status"].upper(),
                value["directory"],
                value["fail_count"],
                value["info_count"],
            ),
        )
        return 1 if result["status"] == STATUS_BLOCKED else 0
    except (
        DriverError,
        discover.DiscoveryError,
        hh.HumanHtmlError,
        checker.standards_resolve.StandardsConfigError,
    ) as exc:
        print("human_html_tree: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
