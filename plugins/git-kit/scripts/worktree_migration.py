"""Journaled cutover of legacy worktrees, with no implicit discard or branch deletion."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from contextlib import ExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Any

import worktrees as wt
import worktree_safety as safety
from worktree_config import read_object
from worktree_pool import _file_lock, remote_url

if TYPE_CHECKING:
    from worktree_lifecycle import Lifecycle


def claude_command(executable: str, command: str) -> bool:
    """Identify the native Claude executable or its Node/Bun CLI entry point."""
    names = {"claude", "claude.exe", "claude-code", "claude-code.exe"}
    name = re.split(r"[/\\]", executable.strip())[-1].lower()
    if name in names:
        return True
    # ps joins argv without shell escaping. Literal quotes in an unrelated
    # process argument are valid and must not turn the census into a parse error.
    first_match = re.match(r'^\s*(?:"([^"]+)"|(\S+))', command)
    first = re.split(r"[/\\]", next((s for s in first_match.groups() if s), ""))[-1].lower() if first_match else ""
    if first in names:
        return True
    if name not in {"node", "node.exe", "bun", "bun.exe", "nodejs"}:
        return False
    arguments = command[first_match.end():].lstrip() if first_match else ""
    if re.match(r"(?:-e|--eval|-p|--print)(?:\s|=|$)", arguments):
        return False
    return bool(re.search(r'(?:^|[/\\])@anthropic-ai[/\\]claude-code[/\\](?:cli|index)\.(?:[cm]?js)(?:[\s\"\']|$)', arguments))


def claude_processes() -> list[dict[str, Any]]:
    """Inspect live process names and argv. A failed census refuses cutover."""
    if os.name == "nt":
        result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
            "@(Get-CimInstance Win32_Process | Select-Object ProcessId,Name,CommandLine) | ConvertTo-Json -Compress"],
            capture_output=True, text=True, check=True, timeout=30)
        rows = json.loads(result.stdout)
        if not isinstance(rows, list):
            raise ValueError("Claude process check returned an invalid process census")
        return [{"pid": row["ProcessId"], "executable": row["Name"], "command": row.get("CommandLine") or ""}
                for row in rows if claude_command(row["Name"], row.get("CommandLine") or "")]
    names = subprocess.run(["ps", "-axo", "pid=,comm="], capture_output=True, text=True, check=True, timeout=15)
    commands = subprocess.run(["ps", "-axo", "pid=,args="], capture_output=True, text=True, check=True, timeout=15)
    by_pid = {}
    for line in commands.stdout.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) == 2:
            by_pid[int(fields[0])] = fields[1]
    found = []
    for line in names.stdout.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) != 2:
            raise ValueError(f"Claude process check returned an invalid process row: {line!r}")
        pid, executable = int(fields[0]), fields[1]
        if pid in by_pid and claude_command(executable, by_pid[pid]):
            found.append({"pid": pid, "executable": executable, "command": by_pid[pid]})
    return found


def claude_file_evidence(pids: set[int]) -> tuple[dict[int, list[Path]], dict[int, str | None], list[str]]:
    """Read all selected macOS processes once, including cwd in the same query."""
    paths: dict[int, list[Path]] = {pid: [] for pid in pids}
    cwds: dict[int, str | None] = {pid: None for pid in pids}
    executable = safety.shutil.which("lsof")
    if not executable:
        return paths, cwds, ["uncertain process evidence: lsof is unavailable"]
    try:
        # f identifies cwd without a second, per-pid `lsof -d cwd` query.
        result = subprocess.run([executable, "-nP", "-Fpnf", "-a", "-p", ",".join(str(pid) for pid in sorted(pids))],
                                capture_output=True, text=True, check=False, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return paths, cwds, [f"uncertain process evidence: {exc}"]
    if result.returncode != 0 or result.stderr.strip():
        return paths, cwds, [f"uncertain process evidence: {result.stderr.strip() or result.returncode}"]
    pid, descriptor = None, None
    for line in result.stdout.splitlines():
        if line.startswith("p"):
            pid, descriptor = int(line[1:]), None
        elif line.startswith("f"):
            descriptor = line[1:]
        elif line.startswith("n") and pid in paths and Path(line[1:]).is_absolute():
            paths[pid].append(Path(line[1:]))
            if descriptor == "cwd":
                cwds[pid] = line[1:]
    uncertain = [f"uncertain process evidence: lsof returned no cwd for pid {pid}" for pid in sorted(pids) if cwds[pid] is None]
    return paths, cwds, uncertain


def candidate_contains(candidate: Path, opened: Path) -> bool:
    """Compare text only for process paths; filesystem probes belong to our root."""
    root = candidate.resolve()
    insensitive = False
    if root.name and root.name.swapcase() != root.name:
        # Probe an alias of our own directory, never an lsof-reported ancestor.
        alias = root.with_name(root.name.swapcase())
        try:
            insensitive = root.samefile(alias)
        except FileNotFoundError:
            pass  # The case alias is absent on a case-sensitive filesystem.

    def normalized(path: Path) -> Path:
        text = os.path.normpath(str(path))
        return Path(text.casefold() if insensitive else text)

    target = normalized(opened)
    return any(target.is_relative_to(normalized(form)) for form in (candidate.absolute(), root))


def process_check(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    processes = claude_processes()
    pids = {row["pid"] for row in processes}
    paths, cwds, uncertain = claude_file_evidence(pids) if pids and safety.sys.platform == "darwin" else ({}, {}, [])
    blockers = []
    for row in candidates:
        candidate = Path(row["path"])
        path = candidate.resolve()
        if pids and path.exists():
            if safety.sys.platform == "darwin":
                blockers.extend(f"{path}: {detail}" for detail in uncertain)
                for pid, opened in paths.items():
                    matching = [target for target in opened if candidate_contains(candidate, target)]
                    if matching:
                        blockers.append(f"{path}: live process/runtime: p{pid}\n" + "\n".join(f"n{target}" for target in matching))
            else:
                blockers.extend(f"{path}: {detail}" for detail in safety.path_processes(path, pids=pids))
    result = {"checked": True, "description": "Claude cwd and open files checked against migration candidates; uncertain evidence blocks migration",
              "running": processes, "blockers": blockers, "at": time.time()}
    if blockers:
        raise ValueError("Claude processes may be using migration candidates; exit Claude sessions using candidate worktrees "
                         f"or resolve uncertain process evidence. Process check: {json.dumps(result)}")
    result["ignored_claude_processes"] = [{"pid": row["pid"], "cwd": cwds[row["pid"]] if row["pid"] in cwds else safety.process_cwd(row["pid"])} for row in processes]
    return result


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def plain_path(root: Path, path: Path) -> None:
    """Guard metadata reads and writes against links below their named home."""
    if not path.is_absolute() or not path.is_relative_to(root) or ".." in path.parts:
        raise ValueError(f"Metadata path escapes its lifecycle home: {path}")
    current = path
    while current != root.parent:
        if current.is_symlink():
            raise ValueError(f"Refusing symlink in lifecycle metadata path: {current}")
        current = current.parent


def legacy_metadata(obj: Lifecycle) -> list[Path]:
    """Only recognized metadata is retired; worktree contents never enter this list."""
    paths: set[Path] = set()
    for root in (obj.namespace, obj.namespace / "ready"):
        plain_path(obj.namespace, root)
        for name in ("pool.json", ".taken.jsonl"):
            plain_path(obj.namespace, root / name)
            if (root / name).is_file() and not (root / name).is_symlink():
                paths.add(root / name)
        for directory in (root / ".state", root / ".state/records"):
            plain_path(obj.namespace, directory)
            paths.update(p for p in directory.glob("*.json") if p.is_file() and not p.is_symlink())
            paths.update(p for p in directory.glob("*.log") if p.is_file() and not p.is_symlink())
    return sorted(paths)


def legacy_values(paths: list[Path]) -> list[tuple[Path, dict[str, Any]]]:
    values = []
    for path in paths:
        if path.name.endswith(".jsonl"):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError(f"{path}:{number}: expected a legacy record object")
                values.append((path, value))
        elif path.suffix == ".json":
            values.append((path, read_object(path)))
    return values


def discover(obj: Lifecycle, clones: list[Path], previous: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[Path]]:
    metadata = legacy_metadata(obj)
    old = legacy_values(metadata)
    sources: dict[str, dict[str, Any]] = {}
    queue: list[tuple[Path, bool]] = [(obj.repo, True), *((p, True) for p in clones)]
    for value in [read_object(obj.state / "namespace.json"), *obj.read_records(), *(r for _, r in old)]:
        nested = value.get("repo", value)
        for key in ("source_clone", "source"):
            if nested.get(key):
                queue.append((Path(nested[key]), True))
    queue.extend((Path(source["path"]), True) for source in previous.get("sources", []))
    loose: set[Path] = set()
    if obj.namespace.exists():
        for path in obj.namespace.iterdir():
            if path.name not in {"ready", ".state", ".staging"} and path.is_dir():
                loose.add(path)
        for directory in (obj.namespace / "ready", obj.namespace / "ready/.staging", obj.namespace / ".staging"):
            loose.update(p for p in directory.glob("*") if p.is_dir() and p.name not in {".state", ".staging", ".nohooks"})
    for path in loose:
        if path.is_symlink() or (path / ".git").is_symlink() or not (path / ".git").is_file():
            continue
        # A linked worktree identifies the source clone even outside sibling roots.
        marker = (path / ".git").read_text(encoding="utf-8", errors="replace").strip()
        if not marker.startswith("gitdir: "):
            continue  # Malformed legacy markers are inventoried and parked below.
        admin = Path(marker.removeprefix("gitdir: "))
        if not admin.is_absolute():
            admin = path / admin
        common = admin.parent.parent
        if common.name == ".git" and common.is_dir():
            queue.append((common.parent, True))
    scanned_parents: set[Path] = set()
    while queue:
        path, required = queue.pop(0)
        path = path.absolute()
        if str(path) in sources:
            continue
        if path.is_symlink() or not (path / ".git").exists():
            if required:
                raise ValueError(f"Recorded or explicit source clone is missing or symlinked: {path}")
            continue
        if remote_url(path) != obj.url:
            if required:
                raise ValueError(f"Source clone remote identity differs from repository: {path}")
            continue
        main = wt.project_root(path)
        common = wt.git(main, "rev-parse", "--path-format=absolute", "--git-common-dir")
        sources[str(main)] = {"path": str(main), "common_dir": common}
        if main.parent not in scanned_parents:
            scanned_parents.add(main.parent)
            queue.extend((sibling, False) for sibling in sorted(main.parent.iterdir())
                         if sibling.is_dir() and not sibling.is_symlink() and (sibling / ".git").exists())
    entries: dict[str, dict[str, Any]] = {}
    existing = {r["path"]: r for r in obj.read_records()}
    known = {r["path"]: r for r in previous.get("candidates", [])}

    def add(path: Path, source: dict[str, Any], origin: str, value: dict[str, Any] | None = None) -> None:
        path = path.absolute()
        key = str(path)
        if str(path) in sources:
            return
        if key not in entries:
            prior = known.get(key, {})
            identifier = existing.get(key, {}).get("id") or prior.get("id") or "w-" + hashlib.sha256((obj.url + "\0" + key).encode()).hexdigest()[:32]
            entries[key] = {"id": identifier, "path": key, "source_clone": source["path"],
                            "common_dir": source["common_dir"], "origins": [], "legacy": {}, "phase": "pending"}
        candidate = entries[key]
        if candidate["source_clone"] != source["path"] or candidate["common_dir"] != source["common_dir"]:
            raise ValueError(f"Ambiguous registration/source mapping for {path}")
        if origin not in candidate["origins"]:
            candidate["origins"].append(origin)
        if value:
            candidate["legacy"].update(value)

    for source in sources.values():
        for row in wt.records(Path(source["path"]))[1:]:
            add(Path(row["worktree"]), source, "registered")
        for path in sorted((Path(source["path"]) / ".claude/worktrees").glob("*")):
            if path.is_dir() or path.is_symlink():
                add(path, source, "legacy-agent")
    default_source = sources[str(obj.repo)]
    for path in sorted(loose):
        if str(path) not in entries:
            add(path, default_source, "namespace-filesystem")
    for metadata_path, value in old:
        if metadata_path.name == "pool.json":
            if value.get("remote") != obj.url:
                raise ValueError(f"Legacy namespace remote identity collision: {metadata_path}")
            continue
        nested = value.get("repo", value)
        source = sources.get(nested.get("source_clone") or nested.get("source") or "", default_source)
        path = value.get("path")
        if not path and value.get("id"):
            identifier = str(value["id"])
            if "/" in identifier or "\\" in identifier or identifier in {".", ".."}:
                raise ValueError(f"Invalid legacy record ID: {metadata_path}")
            pool = metadata_path.parent.parent
            alternatives = [pool / identifier, pool / ".staging" / identifier, obj.namespace / ".staging" / identifier]
            path = str(next((p for p in alternatives if p.exists()), alternatives[1]))
        if path:
            if str(Path(path).absolute()) in entries:
                source = {"path": entries[str(Path(path).absolute())]["source_clone"],
                          "common_dir": entries[str(Path(path).absolute())]["common_dir"]}
            add(Path(path), source, str(metadata_path), value)
    return sorted(sources.values(), key=lambda row: row["path"]), sorted(entries.values(), key=lambda row: row["path"]), metadata


def legacy_path(obj: Lifecycle, record: dict[str, Any]) -> bool:
    """An exact journal mapping permits an old path until adoption relocates it."""
    journal = read_object(obj.state / "migration.json")
    return (journal.get("version") == 1 and journal.get("remote") == obj.url
            and any(row.get("id") == record["id"] and row.get("path") == record["path"]
                    and row.get("source_clone") == record["repo"]["source_clone"]
                    and row.get("common_dir") == record["repo"]["common_dir"]
                    for row in journal.get("candidates", [])))


def legacy_problem(obj: Lifecycle, record: dict[str, Any]) -> str | None:
    """Keep structurally unsafe mapped paths visible without probing through links."""
    if not record.get("migration") or not legacy_path(obj, record):
        return None
    path = Path(record["path"])
    if path.is_symlink() or any(p.is_symlink() for p in path.parents) or (path / ".git").is_symlink():
        return "Legacy path or Git marker traverses a symlink; files preserved"
    if not path.is_dir() or not (path / ".git").exists():
        return "Legacy directory or Git marker is missing; files and registration preserved"
    if (path / ".git").is_file():
        marker = (path / ".git").read_text(encoding="utf-8", errors="replace").strip()
        if not marker.startswith("gitdir: ") or not marker.removeprefix("gitdir: ").strip():
            return "Legacy Git marker is malformed; files and registration preserved"
        admin = Path(marker.removeprefix("gitdir: "))
        if not admin.is_absolute():
            admin = path / admin
        if not admin.is_dir():
            return "Legacy Git directory is missing; files and registration preserved"
    try:
        common = wt.git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    except subprocess.CalledProcessError as exc:
        if exc.returncode != 128:
            raise
        return f"Legacy Git metadata cannot be verified (git exit 128: {exc.stderr.strip()}); files and registration preserved"
    if common != record["repo"]["common_dir"] or remote_url(path) != obj.url:
        return "Legacy Git identity disagrees with the journal mapping; files preserved"
    return None


def evidence(obj: Lifecycle, record: dict[str, Any]) -> dict[str, Any]:
    path = Path(record["path"])
    source = Path(record["repo"]["source_clone"])
    problem = legacy_problem(obj, record)
    if problem:
        return {"safe": False, "at": time.time(), "blockers": [problem]}
    ref = wt.main_upstream(source)
    with _file_lock(obj.state / "fetch.lock", blocking=True):
        wt.refresh(source)
        oid = wt.git(source, "rev-parse", "--verify", f"{ref}^{{commit}}")
    result = safety.inspect(record, oid, obj.declared_outputs(), obj.settings.config.pool.rebuildable)
    result["upstream_ref"] = ref
    return result


def migrated_record(obj: Lifecycle, row: dict[str, Any]) -> dict[str, Any]:
    prior = next((r for r in obj.read_records() if r["id"] == row["id"]), None)
    old = row["legacy"]
    record = prior or {"version": 1, "id": row["id"],
        "repo": {"remote": obj.url, "source_clone": row["source_clone"], "common_dir": row["common_dir"]},
        "path": row["path"], "state": "parked", "operation": {}, "holder": None,
        "task": old.get("task"), "branch": old.get("branch"), "head": old.get("head"),
        "upstream": old.get("upstream"), "boundary": old.get("boundary"), "waiting": None,
        "setup": old.get("setup") or {}, "processes": list(old.get("processes") or []),
        "extension": old.get("extension"), "safety": None, "worker": None, "error": old.get("error")}
    # End legacy ownership without treating an ended Claude as a stopped server.
    for key in ("holder", "worker"):
        identity = record.get(key) or old.get(key)
        if identity:
            if key == "holder":
                record["last_holder"] = identity
            if safety.process_state(identity) != "dead" and identity not in record["processes"]:
                record["processes"].append(identity)
    if old.get("pid") and not old.get("worker"):
        identity = {"pid": old["pid"], "start": old.get("start")}
        if safety.process_state(identity) != "dead":
            record["processes"].append(identity)
    record.update(holder=None, worker=None, state="parked", migration={"legacy": old, "origins": row["origins"]})
    return record


def refresh_inventory(obj: Lifecycle, journal: dict[str, Any], clones: list[Path]) -> dict[str, Any]:
    sources, candidates, metadata = discover(obj, clones, journal)
    previous = {row["path"]: row for row in journal.get("candidates", [])}
    for row in candidates:
        old = previous.get(row["path"])
        if old:
            if any(old[key] != row[key] for key in ("id", "source_clone", "common_dir")):
                raise ValueError(f"Migration candidate mapping changed after inventory: {row['path']}")
            row.update(phase=old["phase"], legacy={**old["legacy"], **row["legacy"]})
            if "safety" in old:
                row["safety"] = old["safety"]
        previous[row["path"]] = row
    known_metadata = {row["path"]: row for row in journal.get("metadata", [])}
    for path in metadata:
        if str(path) not in known_metadata:
            known_metadata[str(path)] = {"path": str(path), "sha256": digest(path), "phase": "pending"}
        elif digest(path) != known_metadata[str(path)]["sha256"]:
            raise ValueError(f"Legacy metadata changed after inventory; preserved: {path}")
    return {**journal, "version": 1, "remote": obj.url, "phase": journal.get("phase", "inventory"),
        "sources": sources, "candidates": sorted(previous.values(), key=lambda row: row["path"]),
        "metadata": sorted(known_metadata.values(), key=lambda row: row["path"]),
        "process_check": process_check(list(previous.values())), "at": journal.get("at", time.time())}


def verify_agreement(obj: Lifecycle, journal: dict[str, Any]) -> None:
    """A finished journal phase alone never authorizes retiring legacy metadata."""
    for row in journal["candidates"]:
        matches = [record for record in obj.read_records() if record["id"] == row["id"]]
        if len(matches) != 1:
            raise ValueError(f"Migration agreement requires exactly one durable record: {row['id']}")
        record = matches[0]
        path, source = Path(row["path"]), Path(row["source_clone"])
        if (record["path"] != row["path"] or record["repo"]["source_clone"] != row["source_clone"]
                or record["repo"]["common_dir"] != row["common_dir"] or record.get("holder")
                or record["state"] != row["phase"]):
            raise ValueError(f"Migration Git/state agreement failed for record: {row['id']}")
        registered = any(Path(r["worktree"]).absolute() == path for r in wt.records(source)[1:])
        if row["phase"] == "removed" and (path.exists() or path.is_symlink() or registered):
            raise ValueError(f"Migration removal agreement failed: {path}")
        if row["phase"] == "parked" and not path.exists() and not path.is_symlink() and not registered:
            raise ValueError(f"Migration parked-path agreement failed: {path}")
        if path.is_dir() and not legacy_problem(obj, record):
            if wt.git(path, "rev-parse", "--path-format=absolute", "--git-common-dir") != row["common_dir"]:
                raise ValueError(f"Migration Git registration agreement failed: {path}")


def run(obj: Lifecycle, *, apply: bool = False, clones: list[Path] | None = None) -> dict[str, Any]:
    """Inventory is durable; apply resumes each mapped candidate under the locks."""
    from worktree_lifecycle import atomic_json

    obj.gate(namespace=False)
    if ".claude/bootstrap.json" not in wt.git(obj.repo, "ls-files", "-z", "--", ".claude/bootstrap.json").split("\0"):
        raise ValueError("Migration requires approved settings in tracked .claude/bootstrap.json; configure and track the file first")
    if obj.namespace.is_symlink() or obj.state.is_symlink():
        raise ValueError("Refusing symlinked lifecycle namespace or state")
    for relative in ("ready", ".state", ".state/records", "ready/.state", "ready/.state/records", ".staging", "ready/.staging"):
        plain_path(obj.namespace, obj.namespace / relative)
    for relative in ("records", "logs", "migration-legacy", "migration.json", "namespace.json",
                     "migration.lock", "maintenance.lock", "claim.lock", "fetch.lock", "launch.lock"):
        plain_path(obj.state, obj.state / relative)
    identity = read_object(obj.state / "namespace.json")
    if identity and (identity.get("version") != 1 or identity.get("remote") != obj.url):
        raise ValueError("Namespace remote identity collision during migration")
    # Discover without writing so the process guard precedes locks and journals.
    previous = read_object(obj.state / "migration.json")
    _, candidates, _ = discover(obj, clones or [], previous)
    check = process_check([*previous.get("candidates", []), *candidates])
    with ExitStack() as stack:
        for lock in (obj.state / "migration.lock", obj.state / "maintenance.lock", obj.state / "claim.lock"):
            if not stack.enter_context(_file_lock(lock, blocking=False)):
                raise ValueError(f"Migration blocked by an active lifecycle worker or claim: {lock}")
        pool = obj.namespace / "ready"
        legacy_lock = pool / ".refill.lock"
        plain_path(obj.namespace, legacy_lock)
        # Compatibility coordinates an existing old worker only. All newly
        # created lifecycle locks belong to the plugin's data home.
        if legacy_lock.exists() and not stack.enter_context(_file_lock(legacy_lock, blocking=False)):
            raise ValueError("Migration blocked by a live legacy refill worker")
        journal_path = obj.state / "migration.json"
        journal = read_object(journal_path)
        if journal and (journal.get("version") != 1 or journal.get("remote") != obj.url):
            raise ValueError("Migration journal remote identity or version mismatch")
        if journal.get("phase") == "complete":
            if any(str(wt.project_root(clone)) not in {source["path"] for source in journal["sources"]} for clone in clones or []):
                raise ValueError("Migration is complete; an additional clone cannot be silently added to the completed cutover")
            result = {**journal, "process_check": check}
        else:
            journal = refresh_inventory(obj, journal, clones or [])
            check = journal["process_check"]
            atomic_json(journal_path, journal)
            if not apply:
                return {**journal, "process_check": check, "ignored_claude_processes": check["ignored_claude_processes"]}
            journal["phase"] = "classify"
            atomic_json(journal_path, journal)
            for row in journal["candidates"]:
                if row["phase"] in {"removed", "parked"}:
                    continue
                path, source = Path(row["path"]), Path(row["source_clone"])
                if remote_url(source) != obj.url or wt.git(source, "rev-parse", "--path-format=absolute", "--git-common-dir") != row["common_dir"]:
                    raise ValueError(f"Migration source identity changed: {source}")
                if path.absolute() in {Path(s["path"]).absolute() for s in journal["sources"]}:
                    raise ValueError(f"Main checkout cannot be a migration candidate: {path}")
                record = migrated_record(obj, row)
                registered = any(Path(r["worktree"]).absolute() == path for r in wt.records(source)[1:])
                if not path.exists() and not path.is_symlink() and not registered:
                    check = process_check([row])
                    record.update(state="removed", safety={"safe": True, "at": time.time(), "blockers": [], "reason": "directory and registration absent"})
                    record["operation"] = {"name": "migrate", "phase": "complete", "previous": "legacy", "at": time.time()}
                    obj.save(record)
                    row["phase"] = "removed"
                    atomic_json(journal_path, journal)
                    continue
                record["safety"] = evidence(obj, record)
                record["operation"] = {"name": "migrate", "phase": "remove" if record["safety"]["safe"] else "park", "previous": "legacy", "at": time.time()}
                if record["safety"]["safe"]:
                    row["phase"] = "removing"
                    atomic_json(journal_path, journal)
                    obj.save(record)  # Durable reservation precedes destructive work.
                    record["safety"] = evidence(obj, record)
                check = process_check([row])
                if record["safety"]["safe"]:
                    safety.remove_outputs(path, record["safety"]["reproducible"])
                    wt.git(source, "worktree", "remove", str(path))
                    record["state"] = "removed"
                    row["phase"] = "removed"
                if record["state"] != "removed":
                    record.update(state="parked", waiting="Legacy work preserved during migration; inspect blockers before adoption")
                    row["phase"] = "parked"
                record["operation"]["phase"] = "complete"
                obj.save(record)
                row["safety"] = record["safety"]
                atomic_json(journal_path, journal)
            verify_agreement(obj, journal)
            journal["phase"] = "metadata"
            atomic_json(journal_path, journal)
            for row in journal["metadata"]:
                if row["phase"] == "retired":
                    continue
                path = Path(row["path"])
                backup = obj.state / "migration-legacy" / hashlib.sha256(str(path).encode()).hexdigest() / path.name
                plain_path(obj.namespace, path)
                plain_path(obj.state, backup)
                if path.exists():
                    if path.is_symlink() or digest(path) != row["sha256"]:
                        raise ValueError(f"Legacy metadata changed after inventory; preserved: {path}")
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    payload = path.read_bytes()
                    backup.write_bytes(payload)
                    if digest(backup) != row["sha256"]:
                        raise ValueError(f"Legacy metadata backup verification failed: {path}")
                    row.update(phase="backed-up", backup=str(backup))
                    atomic_json(journal_path, journal)
                    plain_path(obj.namespace, path)
                    path.unlink()
                elif not backup.exists() or digest(backup) != row["sha256"]:
                    raise ValueError(f"Legacy metadata and verified backup are missing: {path}")
                row.update(phase="retired", backup=str(backup))
                atomic_json(journal_path, journal)
            # Namespace activation is last: ordinary lifecycle operations refuse
            # every incomplete journal, even when an old namespace record existed.
            obj.namespace.mkdir(parents=True, exist_ok=True)
            source = next(s for s in journal["sources"] if s["path"] == str(obj.repo)) if not identity else {
                "path": identity["source_clone"], "common_dir": identity["common_dir"]}
            atomic_json(obj.state / "namespace.json", {"version": 1, "remote": obj.url,
                "source_clone": source["path"], "common_dir": source["common_dir"], "sources": journal["sources"]})
            journal.update(phase="complete", process_check=check, finished=time.time())
            atomic_json(journal_path, journal)
            result = journal
    if apply:
        result = {**result, "maintenance": obj.request_maintenance()}
    return {**result, "ignored_claude_processes": check["ignored_claude_processes"]}
