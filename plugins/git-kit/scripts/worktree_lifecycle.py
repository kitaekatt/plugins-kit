"""Opt-in take/work/push/reuse transactions shared by all clones of a remote."""

from __future__ import annotations

import functools
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import worktrees as wt
from bootstrap_guard import data_dir
from worktree_config import Step, home_config, inventory_api, load, read_object, require_install, validate_config
from worktree_pool import _file_lock, remote_url, repo_identity
import worktree_safety as safety

ERRORS = (OSError, ValueError, subprocess.SubprocessError)


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(json.dumps(value, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def operation(method: Callable) -> Callable:
    """One request per public operation, even on refusal. Internal work stays inline."""
    @functools.wraps(method)
    def run(self: Lifecycle, *args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            result = method(self, *args, **kwargs)
        except ERRORS as exc:
            outcome = self.request_maintenance()
            raise ValueError(f"{method.__name__}: {exc}; maintenance: {json.dumps(outcome)}") from exc
        result["maintenance"] = self.request_maintenance()
        return result
    return run


class Lifecycle:
    def __init__(self, repo: Path) -> None:
        self.repo = wt.project_root(repo)
        self.url = remote_url(self.repo)
        inventory_api()
        self.settings = load(self.repo)
        self.namespace = wt.resolve_settings(self.repo).root / repo_identity(self.url)
        self.state = data_dir("git-kit") / "worktrees" / repo_identity(self.url)

    def gate(self, *, namespace: bool = True) -> None:
        require_install(self.repo)
        if self.settings.config is None:
            raise ValueError("Repository is unconfigured; run worktrees.py configure --propose, then --apply after approval")
        if not self.settings.config.enabled:
            raise ValueError("Repository lifecycle is disabled; configure --propose to enable it")
        if namespace:
            self.check_namespace()

    def check_namespace(self) -> None:
        if self.namespace.is_symlink() or self.state.is_symlink():
            raise ValueError(f"Refusing symlinked lifecycle namespace: {self.namespace}")
        for name in ("ready", ".staging"):
            self.validate_path(self.namespace / name)
        identity = self.state / "namespace.json"
        if identity.exists():
            data = read_object(identity)
            if data.get("version") != 1 or data.get("remote") != self.url:
                raise ValueError(f"Namespace remote identity collision: {identity}")
        else:
            entries = set(self.namespace.iterdir()) if self.namespace.exists() else set()
            state_entries = set(self.state.iterdir()) if self.state.exists() else set()
            if entries or any(p.name != "claim.lock" for p in state_entries):
                raise ValueError(f"Legacy namespace requires migration before lifecycle mutation: {self.namespace}")

    def validate_path(self, path: Path) -> None:
        if not path.is_absolute() or not path.resolve().is_relative_to(self.namespace.resolve()):
            raise ValueError(f"Path escapes lifecycle namespace: {path}")
        for parent in (path, *path.parents):
            if parent == self.namespace.parent:
                break
            if parent.is_symlink():
                raise ValueError(f"Refusing symlink in lifecycle namespace: {parent}")

    def initialize(self) -> None:
        self.gate()
        with _file_lock(self.state / "claim.lock", blocking=True):
            self.check_namespace()
            identity = self.state / "namespace.json"
            if not identity.exists():
                self.namespace.mkdir(parents=True, exist_ok=True)
                atomic_json(identity, {"version": 1, "remote": self.url, "source_clone": str(self.repo),
                                       "common_dir": wt.git(self.repo, "rev-parse", "--path-format=absolute", "--git-common-dir")})

    def source(self) -> Path:
        data = read_object(self.state / "namespace.json")
        source = Path(data["source_clone"])
        if not source.is_dir() or remote_url(source) != self.url:
            raise ValueError(f"Recorded source clone missing or identity changed: {source}")
        if wt.git(source, "rev-parse", "--path-format=absolute", "--git-common-dir") != data["common_dir"]:
            raise ValueError("Source clone common directory changed; repair registrations before use")
        return source

    def read_records(self) -> list[dict[str, Any]]:
        return [read_object(p) for p in sorted((self.state / "records").glob("*.json"))]

    def save(self, record: dict[str, Any]) -> None:
        if not isinstance(record.get("id"), str) or not record["id"].startswith("w-") or not all(c in "w-0123456789abcdef" for c in record["id"]):
            raise ValueError("Invalid stable worktree ID")
        atomic_json(self.state / "records" / f"{record['id']}.json", record)

    def get(self, identifier: str) -> dict[str, Any]:
        matches = [r for r in self.read_records() if r["id"] == identifier]
        if len(matches) != 1:
            raise ValueError(f"Unknown or duplicate worktree ID: {identifier}")
        return matches[0]

    def disk(self) -> dict[str, Any]:
        parent = self.namespace
        while not parent.exists():
            parent = parent.parent
        usage = shutil.disk_usage(parent)
        return {"free_percent": usage.free * 100 / usage.total, "floor_percent": self.settings.disk_floor_percent}

    def fresh(self) -> tuple[str, str]:
        source = self.source()
        ref = wt.main_upstream(source)
        with _file_lock(self.state / "fetch.lock", blocking=True):
            wt.refresh(source)
            return ref, wt.git(source, "rev-parse", "--verify", f"{ref}^{{commit}}")

    def declared_outputs(self) -> list[str]:
        return [p for step in self.settings.config.pool.setup for p in step.recreate]

    def safety(self, record: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
        ref, oid = self.fresh()
        path = self.record_path(record)
        evidence = safety.inspect(record, oid, self.declared_outputs())
        evidence["upstream_ref"] = ref
        record["safety"] = evidence
        if persist:
            self.save(record)
        return evidence

    def record_path(self, record: dict[str, Any]) -> Path:
        path = Path(record["path"])
        self.validate_path(path)
        source = Path(record["repo"]["source_clone"])
        if remote_url(source) != self.url or wt.git(source, "rev-parse", "--path-format=absolute", "--git-common-dir") != record["repo"]["common_dir"]:
            raise ValueError(f"Record repository identity changed: {record['id']}")
        if path.exists() and wt.git(path, "rev-parse", "--path-format=absolute", "--git-common-dir") != record["repo"]["common_dir"]:
            raise ValueError(f"Worktree common directory disagrees with record: {path}")
        return path

    def transition(self, record: dict[str, Any], name: str, phase: str, **extra: Any) -> None:
        record["operation"] = {"name": name, "phase": phase, "previous": record["state"], "at": time.time(), **extra}
        self.save(record)

    def fail(self, record: dict[str, Any], exc: BaseException) -> None:
        detail = exc.stderr if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        record["error"] = {"step": record["operation"]["phase"], "reason": detail,
                           "exit": getattr(exc, "returncode", None), "log": str(self.state / "logs" / f"{record['id']}.log")}
        self.save(record)

    def setup(self, record: dict[str, Any], selected: list[Step]) -> None:
        path = self.record_path(record)
        log = self.state / "logs" / f"{record['id']}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        record["setup"]["selected"] = [s.id for s in selected]
        for step in selected:
            self.transition(record, record["operation"]["name"], f"setup:{step.id}")
            safety.remove_outputs(path, step.recreate)
            with log.open("a", encoding="utf-8") as stream:
                stream.write(f"cwd={path}\n$ {' '.join(step.argv)}\n")
                stream.flush()
                executable = shutil.which(step.argv[0])
                if executable is None:
                    raise ValueError(f"setup {step.id}: executable missing: {step.argv[0]}; log: {log}")
                subprocess.run([executable, *step.argv[1:]], cwd=path, stdin=subprocess.DEVNULL,
                               stdout=stream, stderr=subprocess.STDOUT, check=True)
        config = self.settings.config.pool
        record["setup"].update(built_oid=wt.git(path, "rev-parse", "HEAD"),
            definition_hash=hashlib.sha256(config.model_dump_json().encode()).hexdigest(),
            inputs={p: wt.git(path, "rev-parse", f"HEAD:{p}") for p in config.rerun_files},
            final_path_result={"path": str(path), "at": time.time(), "passed": True})
        self.save(record)

    def relocate(self, record: dict[str, Any], destination: Path, name: str) -> None:
        previous = self.record_path(record)
        self.validate_path(destination)
        if destination.exists() or destination.is_symlink():
            raise ValueError(f"Claim destination already exists: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        self.transition(record, name, "rename", **{"from": str(previous), "to": str(destination)})
        os.rename(previous, destination)
        wt.git(destination, "worktree", "repair")
        record["path"] = str(destination)
        self.transition(record, name, "relocated")

    @operation
    def configure(self, approved: dict[str, Any] | None = None) -> dict[str, Any]:
        require_install(self.repo)
        if approved is None:
            steps = []
            reruns: dict[str, list[str]] = {}
            tracked = {p for p in wt.git(self.repo, "ls-files", "-z").split("\0") if p}
            lockfiles = sorted(p for p in tracked if Path(p).name == "package-lock.json")
            for index, lockfile in enumerate(lockfiles):
                directory = Path(lockfile).parent
                identifier = "root" if directory == Path(".") else f"npm-{index}"
                steps.append({"id": identifier, "argv": ["npm", "ci", *(["--prefix", directory.as_posix()] if directory != Path(".") else [])],
                              "recreate": [(directory / "node_modules").as_posix()]})
                reruns[lockfile] = [identifier]
            if steps and ".nvmrc" in tracked:
                reruns[".nvmrc"] = [step["id"] for step in steps]
            steps.append({"id": "bootstrap", "argv": ["bootstrap", "run", "--project-only"],
                          "path_bound": True, "recreate": [".venv"]})
            if "uv.lock" in tracked:
                reruns["uv.lock"] = ["bootstrap"]
            return {"defaults": {"enabled": False, "pool_size": 2, "disk_floor_percent": 10},
                    "proposal": {"enabled": True, "pool": {"size": 2, "setup": steps, "rerun_files": reruns}},
                    "approval": "Review and approve the proposal, then pass that JSON file to configure --apply"}
        validate_config(approved, self.repo)
        self.check_namespace()
        target = self.repo / ".claude/bootstrap.json"
        payload = read_object(target)
        payload.setdefault("git_kit", {})["worktrees"] = approved
        atomic_json(target, payload)
        self.settings = load(self.repo)
        self.namespace = wt.resolve_settings(self.repo).root / repo_identity(self.url)
        return {"applied": str(target), "settings": approved}

    @operation
    def take(self, agent: str, session: str, task: str | None = None, *, process: dict[str, Any] | None = None) -> dict[str, Any]:
        self.initialize()
        wt.validate_task(self.repo, agent)
        if agent in {".state", ".staging"} or not session.strip():
            raise ValueError("take requires a portable agent ID and a nonempty session")
        identity = process or safety.process_identity(os.getppid())
        if safety.process_state(identity) != "live":
            raise ValueError("Holder process identity must be live and certain")
        record = None
        with _file_lock(self.state / "claim.lock", blocking=True):
            rows = self.read_records()
            if any(r.get("holder", {}).get("agent") == agent for r in rows if r.get("holder")):
                raise ValueError(f"Agent {agent} already holds a claim")
            destination = self.namespace / agent
            if destination.exists():
                raise ValueError(f"Claim destination already exists: {destination}")
            ready = [r for r in rows if r["state"] == "ready" and not r["holder"]]
            if not ready:
                raise ValueError(f"READY_POOL_EMPTY: {json.dumps(self.snapshot())}")
            record = ready[0]
            record["holder"] = {"agent": agent, "session": session, "claim": f"c-{uuid.uuid4().hex}", "since": time.time(), **identity}
            record["worker"] = safety.process_identity(os.getpid())
            record["task"] = task
            # Reserve before rename; a crash leaves a holder and a journal.
            try:
                record["state"] = "taken"
                self.transition(record, "take", "reserved")
                self.relocate(record, destination, "take")
            except ERRORS as exc:
                self.fail(record, exc)
                raise
        try:
            ref, oid = self.fresh()
            path = self.record_path(record)
            if wt.git(path, "status", "--porcelain=v1", "--untracked-files=all"):
                raise ValueError("Ready spare has uncommitted work; preserving claim")
            base = wt.git(path, "rev-parse", "HEAD")
            try:
                wt.git(path, "merge-base", "--is-ancestor", base, oid)
            except subprocess.CalledProcessError as exc:
                raise ValueError(f"Cannot prove fast-forward from {base} to fresh upstream {oid}") from exc
            self.transition(record, "take", "fast-forward")
            wt.git(path, "merge", "--ff-only", oid)
            branch = f"agent/{agent}/{record['holder']['claim']}"
            wt.git(path, "checkout", "--no-track", "-b", branch)
            record.update(branch=branch, head=oid, upstream={"ref": ref, "oid": oid})
            config = self.settings.config.pool
            definition = hashlib.sha256(config.model_dump_json().encode()).hexdigest()
            changed = {p for p in config.rerun_files if record["setup"].get("inputs", {}).get(p) != wt.git(path, "rev-parse", f"HEAD:{p}")}
            selected_ids = {step for p in changed for step in config.rerun_files[p]}
            selected = [step for step in config.setup if step.path_bound or step.id in selected_ids or record["setup"].get("definition_hash") != definition]
            self.setup(record, selected)
            record["worker"] = None
            record["error"] = None
            self.transition(record, "take", "complete")
            return {"id": record["id"], "path": record["path"], "claim": record["holder"]["claim"],
                    "branch": branch, "head": oid, "disk": self.disk(), "setup": record["setup"]}
        except ERRORS as exc:
            record["worker"] = None
            self.fail(record, exc)
            raise ValueError(f"take kept claim {record['holder']['claim']} at {record['path']}: {exc}; log: {record['error']['log']}") from exc

    @operation
    def release(self, claim: str) -> dict[str, Any]:
        self.initialize()
        with _file_lock(self.state / "claim.lock", blocking=True):
            rows = self.read_records()
            matches = [r for r in rows if (r.get("holder") or {}).get("claim") == claim
                       or (not r.get("holder") and r.get("last_claim") == claim)]
            if len(matches) != 1:
                raise ValueError(f"Unknown or duplicate claim: {claim}")
            record = matches[0]
            if not record["holder"]:
                return {"id": record["id"], "state": record["state"]}
            if record.get("worker") and safety.process_state(record["worker"]) != "dead":
                raise ValueError("Release blocked by a live or uncertain builder worker")
            path = self.record_path(record)
            if Path.cwd().resolve().is_relative_to(path.resolve()):
                raise ValueError("Run release outside the candidate directory")
            saved = record["holder"]
            self.transition(record, "release", "safety")
            record["holder"] = None  # Clear only in memory until safety is proven.
            try:
                evidence = self.safety(record, persist=False)
                if not evidence["safe"]:
                    raise ValueError(f"Release safety failed: {evidence['blockers']}")
                record.update(state="released", last_claim=claim, error=None, worker=None, head=evidence["head"])
                self.transition(record, "release", "complete")
            except ERRORS as exc:
                record["holder"] = saved
                self.fail(record, exc)
                raise ValueError(f"release failed; claim {claim} retained; push outcome belongs to caller: {exc}") from exc
            return {"id": record["id"], "state": "released"}

    def snapshot(self) -> dict[str, Any]:
        rows = self.read_records()
        for record in rows:
            if record["state"] == "removed":
                continue
            path = self.record_path(record)
            if path.exists():
                record["observed"] = {"head": wt.git(path, "rev-parse", "HEAD"),
                    "branch": wt.git(path, "branch", "--show-current"),
                    "dirty": bool(wt.git(path, "status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none")),
                    "holder_process": safety.process_state(record["holder"]) if record["holder"] else None,
                    "process_blockers": safety.path_processes(path), "at": time.time()}
                record["safety_is_historical"] = record.get("safety") is not None
        return {"namespace": str(self.namespace), "enabled": bool(self.settings.config and self.settings.config.enabled),
                "configured": self.settings.config is not None, "records": rows, "disk": self.disk(),
                "failures": [r["error"] for r in rows if r.get("error")],
                "worker": read_object(self.state / "maintenance.json"),
                "launcher_error": read_object(self.state / "launcher-error.json")}

    @operation
    def status(self) -> dict[str, Any]:
        self.check_namespace()
        return self.snapshot()

    def reconcile(self) -> None:
        for record in self.read_records():
            if record["state"] == "removed":
                continue
            worker = record.get("worker")
            if worker and safety.process_state(worker) != "dead":
                continue
            op = record["operation"]
            if op.get("phase") == "rename":
                before, after = Path(op["from"]), Path(op["to"])
                self.validate_path(before)
                self.validate_path(after)
                if before != after and before.exists() == after.exists():
                    raise ValueError(f"Ambiguous interrupted rename: {record['id']}")
                path = after if after.exists() else before
                wt.git(path, "worktree", "repair")
                record["path"] = str(path)
            path = self.record_path(record)
            registered = any(Path(r["worktree"]).resolve() == path.resolve()
                             for r in wt.records(Path(record["repo"]["source_clone"])))
            if not path.exists() and not registered:
                record.update(state="removed", holder=None, worker=None)
                self.transition(record, "reconcile", "never-created or already removed")
                continue
            if record.get("holder"):
                record["state"] = "taken"
                state = safety.process_state(record["holder"])
                if state == "dead":
                    record["last_holder"] = record["holder"]
                    record["holder"] = None
                    record["state"] = "parked"
                    record["waiting"] = "Holder ended; inspect preserved work before adoption"
                    self.fail(record, ValueError("Interrupted claim or dead holder; files preserved"))
                elif worker:
                    self.fail(record, ValueError("Interrupted take; holder claim and files preserved"))
            elif worker:
                record["state"] = "failed"
                self.fail(record, ValueError("Interrupted build/recycle; unavailable until safety reconciliation"))
            record["worker"] = None
            self.save(record)

    def new_record(self) -> dict[str, Any]:
        identifier = f"w-{uuid.uuid4().hex}"
        source = self.source()
        return {"version": 1, "id": identifier, "repo": {"remote": self.url, "source_clone": str(source),
                "common_dir": wt.git(source, "rev-parse", "--path-format=absolute", "--git-common-dir")},
                "path": str(self.namespace / ".staging" / identifier), "state": "building",
                "operation": {"name": "build", "phase": "reserved", "previous": None, "at": time.time()},
                "holder": None, "task": None, "branch": None, "head": None, "upstream": None,
                "boundary": None, "waiting": None, "setup": {}, "processes": [], "extension": None,
                "safety": None, "worker": safety.process_identity(os.getpid()), "error": None}

    def prepare(self, record: dict[str, Any], *, recycle: bool) -> None:
        try:
            ref, oid = self.fresh()
            if recycle:
                record["state"] = "recycled"
                self.relocate(record, self.namespace / "ready" / record["id"], "recycle")
                path = self.record_path(record)
                wt.git(path, "checkout", "--detach", oid)
            else:
                path = Path(record["path"])
                self.validate_path(path)
                path.parent.mkdir(parents=True, exist_ok=True)
                self.transition(record, "build", "git worktree add")
                wt.git(self.source(), "worktree", "add", "--detach", str(path), oid)
            record["upstream"] = {"ref": ref, "oid": oid}
            self.setup(record, self.settings.config.pool.setup)
            if not recycle:
                self.relocate(record, self.namespace / "ready" / record["id"], "build")
                self.setup(record, [s for s in self.settings.config.pool.setup if s.path_bound])
            if wt.git(Path(record["path"]), "status", "--porcelain=v1", "--untracked-files=all"):
                raise ValueError("Setup left uncommitted files; cannot publish ready")
            record.update(state="ready", worker=None, error=None, head=oid, branch=None)
            self.transition(record, "build", "complete")
        except ERRORS as exc:
            record["state"] = "failed"
            record["worker"] = None
            self.fail(record, exc)
            raise ValueError(f"prepare {record['id']} kept {record['path']}: {exc}; log: {record['error']['log']}") from exc

    def maintain(self) -> dict[str, Any]:
        self.initialize()
        with _file_lock(self.state / "maintenance.lock", blocking=False) as held:
            if not held:
                return {"outcome": "already active", "built": []}
            worker = {"outcome": "running", **safety.process_identity(os.getpid()), "at": time.time()}
            # The launcher holds launch.lock while recording the accepted child.
            # This handshake prevents a fast child outcome being overwritten.
            with _file_lock(self.state / "launch.lock", blocking=True):
                atomic_json(self.state / "maintenance.json", worker)
            result: dict[str, Any] = {"outcome": "complete", "built": [], "recycled": [], "removed": []}
            try:
                with _file_lock(self.state / "claim.lock", blocking=True):
                    self.reconcile()
                for record in self.read_records():
                    if record["state"] in {"ready", "taken", "removed", "building", "recycled"}:
                        continue
                    with _file_lock(self.state / "claim.lock", blocking=True):
                        record = self.get(record["id"])
                        if record["holder"]:
                            continue
                        if not self.safety(record)["safe"]:
                            continue
                        ready = sum(r["state"] in {"ready", "building", "recycled"} for r in self.read_records())
                        record["worker"] = safety.process_identity(os.getpid())
                        self.transition(record, "recycle" if ready < self.settings.config.pool.size else "remove", "reserved")
                        if ready < self.settings.config.pool.size:
                            record["state"] = "recycled"
                            self.save(record)
                            recycle = True
                        else:
                            path = self.record_path(record)
                            safety.remove_outputs(path, self.declared_outputs())
                            wt.git(Path(record["repo"]["source_clone"]), "worktree", "remove", str(path))
                            record.update(state="removed", worker=None)
                            self.transition(record, "remove", "complete")
                            result["removed"].append(record["id"])
                            recycle = False
                    if recycle:
                        self.prepare(record, recycle=True)
                        result["recycled"].append(record["id"])
                while sum(r["state"] in {"ready", "building", "recycled"} for r in self.read_records()) < self.settings.config.pool.size:
                    disk = self.disk()
                    disk["blocked"] = disk["free_percent"] <= disk["floor_percent"]
                    result["disk"] = disk
                    if disk["blocked"]:
                        break
                    record = self.new_record()
                    self.save(record)
                    self.prepare(record, recycle=False)
                    result["built"].append(record["id"])
                atomic_json(self.state / "maintenance.json", {**worker, **result, "finished": time.time()})
                return result
            except ERRORS as exc:
                atomic_json(self.state / "maintenance.json", {**worker, "outcome": "failed", "reason": str(exc), "finished": time.time()})
                raise

    def request_maintenance(self) -> dict[str, Any]:
        if not self.settings.config or not self.settings.config.enabled:
            return {"outcome": "disabled or unconfigured"}
        try:
            self.initialize()
            with _file_lock(self.state / "launch.lock", blocking=True):
                previous = read_object(self.state / "maintenance.json")
                if previous.get("outcome") in {"starting", "running"} and safety.process_state(previous) != "dead":
                    return {"outcome": "already active", "worker": previous}
                log = self.state / "maintenance.log"
                kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
                with log.open("a", encoding="utf-8") as stream:
                    child = subprocess.Popen([sys.executable, str(Path(__file__).with_name("worktrees.py")),
                        "maintenance", "--repo", str(self.repo)], cwd=self.repo, stdin=subprocess.DEVNULL,
                        stdout=stream, stderr=stream, **kwargs)
                    identity = safety.process_identity(child.pid)
                    atomic_json(self.state / "maintenance.json", {"outcome": "starting", **identity, "log": str(log)})
                return {"outcome": "started", **identity, "log": str(log)}
        except ERRORS as exc:
            # A rejected gate never creates a namespace merely to log its refusal.
            outcome = {"outcome": "failed", "reason": str(exc)}
            if (self.state / "namespace.json").exists():
                atomic_json(self.state / "launcher-error.json", outcome)
            return outcome


def release(repo: Path, claim: str) -> dict[str, Any]:
    """A full-push caller invokes this outside the claimed worktree after delivery."""
    return Lifecycle(repo).release(claim)
