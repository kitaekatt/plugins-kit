"""Cutover contracts.

Run: uv run --extra dev pytest tests/git-kit/test_worktree_migration.py -q.
Triggers: legacy pool/Agent paths, interrupted removal, live Claude processes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from test_worktree_lifecycle import git, lifecycle, write_json


@pytest.fixture
def migration(lifecycle, monkeypatch):
    import worktree_migration as migration

    monkeypatch.setattr(migration, "claude_processes", lambda: [])
    return lifecycle


def legacy(obj, name: str, *, source: Path | None = None, agent: bool = False) -> Path:
    source = source or obj.repo
    path = source / ".claude/worktrees" / name if agent else obj.namespace / name
    path.parent.mkdir(parents=True, exist_ok=True)
    git(source, "worktree", "add", "-b", name, str(path), "origin/main")
    return path


def test_migration_resumes_idempotently(migration, monkeypatch) -> None:
    obj = migration
    path = legacy(obj, "safe")
    branches = git(obj.repo, "for-each-ref", "--format=%(refname)", "refs/heads")
    inventory = obj.migrate()
    assert inventory["phase"] == "inventory"
    assert path.exists() and not (obj.state / "namespace.json").exists()
    journal_path = obj.state / "migration.json"
    assert journal_path.exists() and not journal_path.is_relative_to(obj.namespace)
    before = json.loads(journal_path.read_text())
    assert before["candidates"][0]["path"] == str(path)
    save = obj.save
    interrupted = False

    def crash(record: dict) -> None:
        nonlocal interrupted
        if record["state"] == "removed" and not interrupted:
            interrupted = True
            raise OSError("interrupted after Git removal")
        save(record)

    monkeypatch.setattr(obj, "save", crash)
    with pytest.raises(OSError, match="interrupted"):
        obj.migrate(apply=True)
    assert not path.exists()
    assert json.loads(journal_path.read_text())["phase"] != "complete"
    monkeypatch.setattr(obj, "save", save)
    result = obj.migrate(apply=True)
    assert result["phase"] == "complete" and result["process_check"]["checked"]
    assert "Claude" in result["process_check"]["description"]
    assert obj.repo.is_dir() and (obj.repo / ".git").is_dir()
    assert git(obj.repo, "for-each-ref", "--format=%(refname)", "refs/heads") == branches
    records = obj.read_records()
    assert len(records) == 1 and records[0]["state"] == "removed"
    assert obj.migrate(apply=True)["candidates"] == result["candidates"]
    assert obj.read_records() == records


@pytest.mark.parametrize("kind", ["dirty", "unpublished", "ignored", "locked", "orphan", "process", "symlink", "malformed-git", "missing-git-admin", "invalid-git-admin"])
def test_unsafe_legacy_is_preserved(migration, monkeypatch, kind: str) -> None:
    import worktree_safety as safety

    obj = migration
    if kind == "symlink":
        path = obj.repo / ".claude/worktrees/unsafe-symlink"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(obj.repo, target_is_directory=True)
    else:
        path = legacy(obj, f"unsafe-{kind}", agent=kind != "missing-git-admin")
    if kind == "dirty":
        (path / "unfinished").write_text("keep dirty work")
    elif kind == "unpublished":
        (path / "unfinished").write_text("keep unique commit")
        git(path, "add", "unfinished")
        git(path, "commit", "-qm", "unique")
    elif kind == "ignored":
        (path / "local.secret").write_text("keep unique ignored data")
    elif kind == "locked":
        git(obj.repo, "worktree", "lock", str(path))
    elif kind == "orphan":
        # Preserve even an old Agent directory whose registration is missing.
        marker = (path / ".git").read_text()
        admin = Path(marker.removeprefix("gitdir: ").strip())
        (admin / "gitdir").write_text(str(path / "missing-git-marker"))
        (path / ".git").unlink()
        (path / "unfinished").write_text("orphan files")
    elif kind == "malformed-git":
        (path / ".git").write_text("not a Git marker")
    elif kind == "missing-git-admin":
        (path / ".git").write_text(f"gitdir: {obj.namespace / 'missing-admin'}\n")
    elif kind == "invalid-git-admin":
        admin = obj.repo.parent / "empty-admin"
        admin.mkdir()
        (path / ".git").write_text(f"gitdir: {admin}\n")
    elif kind == "process":
        real = safety.path_processes
        monkeypatch.setattr(safety, "path_processes", lambda candidate: ["uncertain process evidence"] if candidate == path else real(candidate))
    files = {} if kind == "symlink" else {p.relative_to(path): p.read_bytes() for p in path.rglob("*") if p.is_file()}
    result = obj.migrate(apply=True)
    assert result["phase"] == "complete"
    record = next(r for r in obj.read_records() if r["path"] == str(path))
    assert record["state"] == "parked" and record["holder"] is None
    assert record["safety"]["safe"] is False and record["safety"]["blockers"]
    if kind == "symlink":
        assert path.is_symlink() and path.resolve() == obj.repo
    else:
        assert files == {p.relative_to(path): p.read_bytes() for p in path.rglob("*") if p.is_file()}
    if kind in {"orphan", "symlink", "malformed-git", "missing-git-admin", "invalid-git-admin"}:
        assert obj.maintain()["built"]
        assert next(r for r in obj.snapshot()["records"] if r["id"] == record["id"])["state"] == "parked"


def test_migration_discovers_clones_and_legacy_records(migration, tmp_path) -> None:
    obj = migration
    clone = tmp_path / "another-clone"
    git(obj.repo, "clone", "--branch", "main", obj.url, str(clone))
    safe = legacy(obj, "clone-safe", source=clone)
    unsafe = legacy(obj, "agent-dirty", source=clone, agent=True)
    (unsafe / "unfinished").write_text("preserve")
    pool = obj.namespace / "ready"
    write_json(pool / "pool.json", {"remote": obj.url, "source": str(clone)})
    write_json(pool / ".state/old-build.json", {"id": "old-build", "state": "failed", "started": "old", "pid": 99999999, "reason": "setup failed"})
    (pool / ".taken.jsonl").write_text(json.dumps({"task": "legacy-task", "path": str(unsafe), "time": "old"}) + "\n")
    namespace_files = {p.relative_to(obj.namespace): p.read_bytes() for p in obj.namespace.rglob("*") if p.is_file()}
    inventory = obj.migrate()
    assert namespace_files == {p.relative_to(obj.namespace): p.read_bytes() for p in obj.namespace.rglob("*") if p.is_file()}
    assert {str(obj.repo), str(clone)} <= {s["path"] for s in inventory["sources"]}
    assert {str(safe), str(unsafe)} <= {r["path"] for r in inventory["candidates"]}
    assert all(r["path"] not in {str(obj.repo), str(clone)} for r in inventory["candidates"])
    result = obj.migrate(apply=True)
    assert result["phase"] == "complete" and not safe.exists()
    assert (clone / ".git").exists() and (unsafe / "unfinished").read_text() == "preserve"
    record = next(r for r in obj.read_records() if r["path"] == str(unsafe))
    assert record["task"] == "legacy-task" and record["migration"]["legacy"]
    assert (obj.state / "migration-legacy").is_dir()
    assert not (pool / ".taken.jsonl").exists()
    assert not (pool / "pool.json").exists()
    assert any(r.get("migration", {}).get("legacy", {}).get("id") == "old-build" for r in obj.read_records())


def test_migration_legacy_adoption_uses_verified_mapping(migration) -> None:
    from test_worktree_lifecycle import holder

    obj = migration
    path = legacy(obj, "adopt-old", agent=True)
    (path / "unfinished").write_text("keep")
    branch = git(path, "branch", "--show-current")
    obj.migrate(apply=True)
    record = next(r for r in obj.read_records() if r["path"] == str(path))
    result = obj.adopt(record["id"], "adopting-agent", "s", process=holder())
    assert (Path(result["path"]) / "unfinished").read_text() == "keep"
    assert git(Path(result["path"]), "branch", "--show-current") == branch
    assert Path(result["path"]) == obj.namespace / "adopting-agent" and not path.exists()


def test_migrated_adoption_recovers_interrupted_external_rename(migration, monkeypatch) -> None:
    from test_worktree_lifecycle import holder
    import worktrees as wt

    obj = migration
    path = legacy(obj, "adopt-interrupted", agent=True)
    (path / "unfinished").write_text("recover this work")
    obj.migrate(apply=True)
    record = next(r for r in obj.read_records() if r["path"] == str(path))
    real_git = wt.git

    def crash(directory: Path, *args: str) -> str:
        if args == ("worktree", "repair"):
            raise OSError("interrupted after rename before registration repair")
        return real_git(directory, *args)

    monkeypatch.setattr(wt, "git", crash)
    with pytest.raises(ValueError, match="kept claim"):
        obj.adopt(record["id"], "recovering-agent", "s", process=holder())
    current = obj.get(record["id"])
    assert current["operation"]["phase"] == "rename" and current["path"] == str(path)
    assert not path.exists() and (obj.namespace / "recovering-agent/unfinished").read_text() == "recover this work"
    current["holder"].update(pid=99999999, start="exited")
    obj.save(current)
    monkeypatch.setattr(wt, "git", real_git)
    obj.reconcile()
    recovered = obj.get(record["id"])
    assert recovered["state"] == "parked" and recovered["holder"] is None
    assert recovered["path"] == str(obj.namespace / "recovering-agent")
    assert git(Path(recovered["path"]), "rev-parse", "--show-toplevel") == recovered["path"]
    assert (Path(recovered["path"]) / "unfinished").read_text() == "recover this work"


def test_migration_requires_settings_and_install(lifecycle, monkeypatch) -> None:
    import worktree_lifecycle as lc

    obj = lifecycle
    (obj.repo / ".claude/bootstrap.json").unlink()
    with pytest.raises(ValueError, match="configur"):
        lc.Lifecycle(obj.repo).migrate()
    write_json(obj.repo / ".claude/bootstrap.json", {"git_kit": {"worktrees": obj.settings.raw}})
    write_json(Path.home() / ".claude/settings.json", {"enabledPlugins": {"git-kit@plugins-kit": False}})
    with pytest.raises(ValueError, match="user"):
        obj.migrate(apply=True)
    assert not obj.state.exists() and not obj.namespace.exists()


def test_migration_refuses_untracked_settings(lifecycle) -> None:
    obj = lifecycle
    git(obj.repo, "rm", "--cached", ".claude/bootstrap.json")
    with pytest.raises(ValueError, match="tracked"):
        obj.migrate()
    assert not obj.state.exists()


def test_live_claude_refuses_migration(lifecycle, tmp_path, monkeypatch, capsys) -> None:
    import worktree_migration as migration

    obj = lifecycle
    path = legacy(obj, "live-safe")
    # A real process named claude exercises discovery without starting the harness.
    executable = tmp_path / ("claude.exe" if os.name == "nt" else "claude")
    executable.symlink_to(sys.executable)
    child = subprocess.Popen([str(executable), "-c", "import time; time.sleep(120)"], cwd=tmp_path)
    try:
        assert any(row["pid"] == child.pid for row in migration.claude_processes())
        for apply in (False, True):
            with pytest.raises(ValueError, match="Claude.*running|running.*Claude"):
                obj.migrate(apply=apply)
        assert path.exists() and not obj.state.exists()
    finally:
        child.terminate()
        child.wait(timeout=10)


def test_migration_cli_states_process_check(migration, capsys) -> None:
    import worktree_cli

    result = worktree_cli.main(["migrate", "--repo", str(migration.repo), "--inventory"])
    assert result == 0
    output = json.loads(capsys.readouterr().out)
    assert output["phase"] == "inventory" and output["process_check"]["checked"]


def test_inventory_rescans_added_clones_before_apply(migration, tmp_path) -> None:
    obj = migration
    first = legacy(obj, "first")
    obj.migrate()
    clone = tmp_path / "elsewhere" / "additional-clone"
    clone.parent.mkdir()
    git(obj.repo, "clone", "--branch", "main", obj.url, str(clone))
    added = legacy(obj, "after-inventory", source=clone, agent=True)
    result = obj.migrate(apply=True, clones=[clone])
    assert {str(first), str(added)} <= {r["path"] for r in result["candidates"]}
    assert not added.exists() and not first.exists() and (clone / ".git").exists()


def test_metadata_retirement_requires_record_and_git_agreement(migration) -> None:
    obj = migration
    path = legacy(obj, "unfinished")
    (path / "unfinished").write_text("keep")
    metadata = obj.namespace / "ready/pool.json"
    write_json(metadata, {"remote": obj.url, "source": str(obj.repo)})
    obj.migrate()
    journal_path = obj.state / "migration.json"
    journal = json.loads(journal_path.read_text())
    journal["candidates"][0]["phase"] = "parked"
    write_json(journal_path, journal)
    with pytest.raises(ValueError, match="record|agreement"):
        obj.migrate(apply=True)
    assert metadata.exists() and (path / "unfinished").read_text() == "keep"


@pytest.mark.parametrize(("executable", "command", "expected"), [
    ("/usr/bin/python", "python -c print(\"it's\")", False),
    ("/usr/bin/node", "node /usr/lib/node_modules/@anthropic-ai/claude-code/cli.js --resume", True),
    ("/usr/bin/node", "node -e console.log('@anthropic-ai/claude-code/cli.js')", False),
    (r"C:\node\node.exe", r'"C:\node\node.exe" "C:\npm\node_modules\@anthropic-ai\claude-code\cli.js"', True),
])
def test_claude_census_identifies_executable_not_unrelated_argument(executable: str, command: str, expected: bool) -> None:
    from worktree_migration import claude_command

    assert claude_command(executable, command) is expected


@pytest.mark.parametrize("relative", [".state", "ready", "ready/.state"])
def test_migration_refuses_symlink_metadata_ancestors(migration, tmp_path, relative: str) -> None:
    obj = migration
    unrelated = tmp_path / "unrelated-data"
    unrelated.mkdir()
    payload = unrelated / "preserve.json"
    payload.write_text('{"id":"unrelated"}')
    link = obj.namespace / relative
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(unrelated, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        obj.migrate(apply=True)
    assert payload.read_text() == '{"id":"unrelated"}'
