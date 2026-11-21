"""Shared pool of ready task worktrees: build (refill), take, status.

One pool serves every clone of one remote. Layout under <root>/<repo>/ready/,
where repo is the remote URL's last path component:

  pool.json           {"remote": <url>, "source": <clone that builds spares>}
  spare-<id>/         a ready worktree: every setup step passed
  .staging/<id>/      a build in progress (never handed out)
  .state/<id>.json    build record (building or failed) and <id>.log
  .taken.jsonl        one line per take
  .refill.lock        held by the one running refill
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import worktrees as wt

POOL_DIR = "ready"
SPARE_PREFIX = "spare-"
BUILD_ENV = "GIT_KIT_POOL_BUILD"
EXIT_EMPTY = 4
KEEP_FAILED = 5


class PoolEmpty(Exception):
    """The hard block: no ready worktree. The message names the reasons."""


def remote_url(repo: Path) -> str:
    remotes = wt.git(repo, "remote").splitlines()
    name = "origin" if "origin" in remotes else (remotes[0] if len(remotes) == 1 else None)
    if name is None:
        raise ValueError(f"{repo}: the pool needs a remote named origin or exactly one remote")
    return wt.git(repo, "remote", "get-url", "--", name)


def repo_identity(url: str) -> str:
    name = re.split(r"[/:\\]", url.rstrip("/\\"))[-1].removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name) or name.endswith("."):
        raise ValueError(f"Cannot name a pool folder from remote URL: {url}")
    return name


def pool_dir(settings: wt.WorktreeSettings, url: str) -> Path:
    return settings.root / repo_identity(url) / POOL_DIR


def _write_json(path: Path, data: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def pool_source(pool: Path, repo: Path, url: str) -> Path:
    """The one clone that builds spares. The first clone to refill records itself."""
    record = pool / "pool.json"
    if not record.exists():
        pool.mkdir(parents=True, exist_ok=True)
        _write_json(record, {"remote": url, "source": str(wt.project_root(repo))})
    data = json.loads(record.read_text(encoding="utf-8"))
    if data.get("remote") != url:
        raise ValueError(f"{record} serves {data.get('remote')}, not {url}; two remotes share the folder name")
    source = Path(data["source"])
    if not source.is_dir() or remote_url(source) != url:
        raise ValueError(f"{record}: source clone {source} is missing or has another remote; set source to a clone of {url}")
    return source


def spares(pool: Path) -> list[Path]:
    if not pool.is_dir():
        return []
    return sorted(p for p in pool.iterdir() if p.name.startswith(SPARE_PREFIX) and p.is_dir())


@contextmanager
def _file_lock(path: Path, blocking: bool) -> Iterator[bool]:
    """Yield True when this process holds the lock; the OS frees it at exit."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as handle:
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except OSError:
            if blocking:
                raise
            yield False
            return
        try:
            yield True
        finally:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def refill_lock(pool: Path):
    """Held by the one running refill; a second refill stands down."""
    return _file_lock(pool / ".refill.lock", blocking=False)


def fresh_upstream(pool: Path, source: Path) -> tuple[str, str]:
    """Fetch the source clone (one fetch at a time: parallel fetches race on ref
    locks) and return (upstream ref, its commit)."""
    upstream = wt.main_upstream(source)
    with _file_lock(pool / ".fetch.lock", blocking=True):
        wt.refresh(source)
    return upstream, wt.git(source, "rev-parse", "--verify", f"{upstream}^{{commit}}")


def _records(pool: Path) -> list[dict[str, Any]]:
    folder = pool / ".state"
    if not folder.is_dir():
        return []
    return sorted((json.loads(p.read_text(encoding="utf-8")) for p in folder.glob("*.json")),
                  key=lambda r: r["started"])


def _remove_build(source: Path, staging: Path) -> None:
    """A staging tree is a detached pool build; refuse it if someone committed there."""
    if not staging.exists():
        return
    head = wt.git(staging, "rev-parse", "HEAD")
    if wt.unpublished(staging, head):
        raise ValueError(f"Refusing to remove pool build with unpublished commits: {staging}")
    wt.git(source, "worktree", "remove", "--force", str(staging))
    wt.git(source, "worktree", "prune")


def _fail(pool: Path, record: dict[str, Any], reason: str) -> None:
    record.update(state="failed", reason=reason, finished=_now())
    _write_json(pool / ".state" / f"{record['id']}.json", record)
    failed = [r for r in _records(pool) if r["state"] == "failed"]
    for old in failed[:-KEEP_FAILED]:
        for suffix in (".json", ".log"):
            (pool / ".state" / f"{old['id']}{suffix}").unlink(missing_ok=True)


def _clear_abandoned(pool: Path, source: Path) -> None:
    """Under the lock, a 'building' record has no builder: its process died."""
    for record in _records(pool):
        if record["state"] == "building":
            _remove_build(source, pool / ".staging" / record["id"])
            _fail(pool, record, "abandoned: the build process exited before it finished")


def _run_steps(steps: list[list[str]], cwd: Path, log: Path, on_step=None) -> tuple[int, list[str], int] | None:
    """Run each step; return (index, step, exit) of the first failure, else None."""
    env = {**os.environ, BUILD_ENV: "1"}
    for index, step in enumerate(steps):
        if on_step:
            on_step(index, step)
        executable = shutil.which(step[0]) or step[0]
        with open(log, "a", encoding="utf-8") as stream:
            stream.write(f"$ {' '.join(step)}\n")
            stream.flush()
            try:
                code = subprocess.run([executable, *step[1:]], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                      stdout=stream, stderr=subprocess.STDOUT, check=False).returncode
            except OSError as exc:
                stream.write(f"{exc}\n")
                code = 127
        if code != 0:
            return index, step, code
    return None


def build_one(pool: Path, source: Path, steps: list[list[str]]) -> Path:
    """Build in .staging, rename into the pool only after every step passed."""
    build_id = f"{SPARE_PREFIX}{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    for folder in (".staging", ".state", ".nohooks"):
        (pool / folder).mkdir(parents=True, exist_ok=True)
    staging = pool / ".staging" / build_id
    log = pool / ".state" / f"{build_id}.log"
    record: dict[str, Any] = {"id": build_id, "state": "building", "started": _now(), "pid": os.getpid(),
                              "step": "git worktree add", "log": str(log)}
    record_path = pool / ".state" / f"{build_id}.json"
    _write_json(record_path, record)
    try:
        _, commit = fresh_upstream(pool, source)
        # The declared steps replace checkout hooks, so a hook cannot race them.
        wt.git(source, "-c", f"core.hooksPath={pool / '.nohooks'}", "worktree", "add", "--detach",
               str(staging), commit)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        detail = exc.stderr if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        _remove_build(source, staging)
        _fail(pool, record, f"git worktree add failed: {str(detail).strip()}")
        raise ValueError(f"pool build {build_id} failed at git worktree add: {detail}") from exc

    def on_step(index: int, step: list[str]) -> None:
        record["step"] = f"{index + 1}/{len(steps)}: {' '.join(step)}"
        _write_json(record_path, record)

    failure = _run_steps(steps, staging, log, on_step)
    if failure is not None:
        index, step, code = failure
        _remove_build(source, staging)
        reason = f"step {index + 1}/{len(steps)} `{' '.join(step)}` exited {code}"
        _fail(pool, record, reason)
        raise ValueError(f"pool build {build_id} failed: {reason}; log: {log}")
    spare = pool / build_id
    os.rename(staging, spare)
    wt.git(spare, "worktree", "repair")
    record_path.unlink()
    log.unlink(missing_ok=True)
    return spare


def refill(repo: Path, settings: wt.WorktreeSettings) -> dict[str, Any]:
    """Build spares up to the pool size, one at a time; a second refill stands down."""
    config = _require_pool(settings)
    url = remote_url(repo)
    pool = pool_dir(settings, url)
    source = pool_source(pool, repo, url)
    built: list[str] = []
    with refill_lock(pool) as held:
        if not held:
            return {"pool": str(pool), "built": built, "skipped": "another refill is running"}
        _clear_abandoned(pool, source)
        while len(spares(pool)) < config.size:
            built.append(str(build_one(pool, source, config.setup)))
    return {"pool": str(pool), "built": built}


def start_refill(repo: Path, script: Path) -> None:
    """Start a detached refill; its output goes to the pool's refill log."""
    if os.environ.get(BUILD_ENV):
        return
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([sys.executable, str(script), "pool", "refill", "--log"], cwd=repo,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)


def _require_pool(settings: wt.WorktreeSettings) -> wt.PoolSettings:
    if not settings.enabled:
        raise ValueError("git_kit.worktrees.enabled is false; opt in through bootstrap.json")
    if settings.pool is None:
        raise ValueError("git_kit.worktrees.pool is not configured for this repository")
    return settings.pool


def status(repo: Path, settings: wt.WorktreeSettings) -> dict[str, Any]:
    url = remote_url(repo)
    pool = pool_dir(settings, url)
    with refill_lock(pool) as free:
        pass
    records = _records(pool)
    building = [r for r in records if r["state"] == "building"]
    taken = []
    taken_log = pool / ".taken.jsonl"
    if taken_log.exists():
        for line in taken_log.read_text(encoding="utf-8").splitlines():
            entry = json.loads(line)
            if Path(entry["path"]).is_dir():
                taken.append(entry)
    return {
        "pool": str(pool),
        "size": settings.pool.size if settings.pool else None,
        "spares": [str(p) for p in spares(pool)],
        "building": building if not free else [],
        "abandoned": building if free else [],
        "failed": [r for r in records if r["state"] == "failed"],
        "taken": taken,
    }


def block_message(state: dict[str, Any]) -> str:
    lines = [f"git-kit worktrees: hard block: no ready worktree in {state['pool']}.",
             "No fresh setup was started for this task. Report these details to the user and stop the task."]
    lines.append("Builds in progress:" if state["building"] else "Builds in progress: none")
    lines += [f"  {r['id']}: {r['step']} (started {r['started']}; log {r['log']})" for r in state["building"]]
    if state["abandoned"]:
        lines.append("Abandoned builds (the next refill records them as failed):")
        lines += [f"  {r['id']}: stopped at {r['step']} (started {r['started']})" for r in state["abandoned"]]
    lines.append("Failed builds:" if state["failed"] else "Failed builds: none")
    lines += [f"  {r['id']}: {r['reason']} (log {r['log']})" for r in state["failed"]]
    lines.append("Taken worktrees:" if state["taken"] else "Taken worktrees: none")
    lines += [f"  {t['task']}: {t['path']} (taken {t['time']})" for t in state["taken"]]
    lines.append("A background refill was started. To build one now in the foreground: worktrees.py pool refill.")
    return "\n".join(lines)


def claim(pool: Path, destination: Path) -> Path | None:
    """Rename the oldest spare to destination; the rename is the claim, so two
    takers can never get the same spare. None when every spare is gone."""
    for spare in spares(pool):
        if destination.exists():
            raise ValueError(f"Task worktree already exists: {destination}")
        try:
            os.rename(spare, destination)
        except FileNotFoundError:
            continue  # Another taker renamed this spare first.
        return spare
    return None


def take(repo: Path, settings: wt.WorktreeSettings, task: str, script: Path) -> dict[str, Any]:
    config = _require_pool(settings)
    wt.validate_task(repo, task)
    url = remote_url(repo)
    pool = pool_dir(settings, url)
    source = pool_source(pool, repo, url)
    if wt.git(source, "branch", "--list", task):
        raise ValueError(f"Branch {task} already exists in the pool's source clone {source}")
    destination = wt.contained_task(settings.root, pool.parent / task)
    # The rename alone is the claim on POSIX; the lock also serializes it where
    # the filesystem gives no such promise (Windows).
    with _file_lock(pool / ".claim.lock", blocking=True):
        spare = claim(pool, destination)
    if spare is None:
        # Read the state before the refill starts: the lock probe must not take its lock.
        message = block_message(status(repo, settings))
        start_refill(repo, script)
        raise PoolEmpty(message)
    start_refill(repo, script)
    wt.git(destination, "worktree", "repair")
    base = wt.git(destination, "rev-parse", "HEAD")
    upstream, commit = fresh_upstream(pool, source)
    wt.git(destination, "checkout", "--no-track", "-b", task, commit)
    with open(pool / ".taken.jsonl", "a", encoding="utf-8") as stream:
        stream.write(json.dumps({"task": task, "path": str(destination), "spare": spare.name, "time": _now()}) + "\n")
    changed = (wt.git(source, "diff", "--name-only", base, commit, "--", *config.rebuild_when_changed)
               if config.rebuild_when_changed and base != commit else "")
    result: dict[str, Any] = {"path": str(destination), "branch": task, "from": upstream, "spare": spare.name,
                              "base": base, "head": commit, "rebased": base != commit,
                              "setup": {"ran": bool(changed), "exit": None, "changed": changed.splitlines()},
                              "after_take": {"removed": [], "exit": None}}
    # Setup output with absolute paths (a Python venv's scripts) breaks on the move:
    # delete it and make it again at the final path.
    for relative in config.after_take_remove:
        target = destination / relative
        if not (target.exists() or target.is_symlink()):
            continue
        if subprocess.run(["git", "-C", str(destination), "check-ignore", "-q", "--", relative],
                          check=False).returncode != 0:
            raise ValueError(f"after_take.remove path is not ignored by Git, so it may be work: {target}")
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.exists():
            shutil.rmtree(target)
        else:
            continue
        result["after_take"]["removed"].append(relative)
    log = pool / ".state" / f"take-{task}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    phases = ([("setup", config.setup)] if changed else []) + [("after_take", config.after_take)]
    for phase, steps in phases:
        if not steps:
            continue
        failure = _run_steps(steps, destination, log)
        result[phase]["exit"] = 0 if failure is None else failure[2]
        if failure is not None:
            print(f"git-kit worktrees: kept {destination}; {phase} step `{' '.join(failure[1])}` "
                  f"exited {failure[2]}; log {log}", file=sys.stderr)
            return result
    log.unlink(missing_ok=True)
    return result
