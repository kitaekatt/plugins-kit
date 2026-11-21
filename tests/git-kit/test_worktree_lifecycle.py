"""Increment 1 contracts. Run: uv run --extra dev pytest tests/git-kit/test_worktree_lifecycle.py -q.

Triggers: concurrent takes, interrupted operations, upstream/setup changes,
delivery, retirement, maintenance requests, configuration and disk pressure.
All repositories, process paths and both home variables are temporary.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import worktrees as wt


def git(path: Path, *args: str) -> str:
    return wt.git(path, *args)


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def lifecycle(tmp_path, monkeypatch, git_repo):
    import worktree_lifecycle as lc

    home = tmp_path / "home"
    home.mkdir()
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    monkeypatch.delenv("CLAUDE_BOOTSTRAP_DATA_ROOT", raising=False)
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).parents[2] / "plugins/bootstrap"))
    git_repo.commit_file(".gitignore", "output/\ndeps-output/\nlocal.secret\n.claude/bootstrap.local.json\n")
    git_repo.commit_file("lock.txt", "one")
    remote = tmp_path / "remote.git"
    git(git_repo.path, "init", "--bare", str(remote))
    git(git_repo.path, "remote", "add", "origin", str(remote))
    git(git_repo.path, "push", "-u", "origin", "main")
    cache = home / ".claude/plugins/cache/plugins-kit/git-kit/0.58.0"
    write_json(cache / ".claude-plugin/plugin.json", {"name": "git-kit", "version": "0.58.0"})
    write_json(home / ".claude/plugins/installed_plugins.json", {"version": 2, "plugins": {
        "git-kit@plugins-kit": [{"scope": "user", "installPath": str(cache)}]}})
    write_json(home / ".claude/settings.json", {"enabledPlugins": {"git-kit@plugins-kit": True}})
    step = "from pathlib import Path; p=Path('output'); p.mkdir(exist_ok=True); (p/'cwd').write_text(str(Path.cwd()))"
    config = {"enabled": True, "pool": {"size": 1, "setup": [
        {"id": "deps", "argv": [sys.executable, "-c", "from pathlib import Path; p=Path('deps-output'); p.mkdir(exist_ok=True); (p/'value').write_text(Path('lock.txt').read_text())"], "recreate": ["deps-output"]},
        {"id": "paths", "argv": [sys.executable, "-c", step], "recreate": ["output"], "path_bound": True},
        {"id": "other", "argv": [sys.executable, "-c", "pass"]}],
        "rerun_files": {"lock.txt": ["deps"]}}}
    write_json(git_repo.path / ".claude/bootstrap.json", {"git_kit": {"worktrees": config}})
    git(git_repo.path, "add", ".claude/bootstrap.json")
    git(git_repo.path, "commit", "-qm", "configure")
    git(git_repo.path, "push", "origin", "main")
    calls = []
    real_request = lc.Lifecycle.request_maintenance
    monkeypatch.setattr(lc.Lifecycle, "request_maintenance", lambda self: calls.append(self.repo) or {"outcome": "requested"})
    # The capacity belongs to this temporary filesystem, not the host's free disk.
    monkeypatch.setattr(lc.Lifecycle, "disk", lambda self: {"free_percent": 80, "floor_percent": self.settings.disk_floor_percent})
    obj = lc.Lifecycle(git_repo.path)
    obj.requests = calls
    obj.real_request = real_request.__get__(obj)
    return obj


def holder() -> dict:
    import worktree_safety as safety
    return safety.process_identity(os.getpid())


def test_configure_and_install_gates(lifecycle, monkeypatch):
    import worktree_lifecycle as lc
    from worktree_config import validate_config

    obj = lifecycle
    settings_file = obj.repo / ".claude/bootstrap.json"
    settings_file.unlink()
    with pytest.raises(ValueError, match="configure"):
        lc.Lifecycle(obj.repo).take("agent", "session", process=holder())
    before = set(obj.repo.rglob("*"))
    proposal = lc.Lifecycle(obj.repo).configure()
    assert proposal["proposal"]["pool"]["size"] == 2
    assert set(obj.repo.rglob("*")) == before
    bad = {"enabled": True, "pool": {"setup": [{"id": "x", "argv": ["true"]}], "rerun_files": {"missing": ["x"]}}}
    with pytest.raises(ValueError, match="tracked"):
        lc.Lifecycle(obj.repo).configure(bad)
    for patch, match in [({"rerun_files": {"lock.txt": ["missing"]}}, "step"),
                         ({"setup": [{"id": "x", "argv": ["true"]}, {"id": "x", "argv": ["true"]}]}, "unique")]:
        with pytest.raises(ValueError, match=match):
            validate_config({"enabled": True, "pool": {"setup": [{"id": "x", "argv": ["true"]}], **patch}}, obj.repo)
    config = obj.settings.raw
    lc.Lifecycle(obj.repo).configure(config)
    assert json.loads(settings_file.read_text())["git_kit"]["worktrees"] == config
    write_json(Path.home() / ".claude/settings.json", {"enabledPlugins": {"git-kit@plugins-kit": False}})
    with pytest.raises(ValueError, match="user"):
        lc.Lifecycle(obj.repo).maintain()
    assert not obj.namespace.exists()


def test_claim_races_and_crash_recovery(lifecycle, monkeypatch):
    obj = lifecycle
    obj.maintain()
    def take(index):
        try:
            return obj.take(f"a{index}", "s", process=holder())
        except ValueError as exc:
            return str(exc)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(take, range(6)))
    wins = [r for r in results if isinstance(r, dict)]
    assert len(wins) == 1, results
    record = obj.read_records()[0]
    assert Path(record["path"]).name == record["holder"]["agent"]
    assert record["holder"]["claim"] == wins[0]["claim"]
    assert record["setup"]["final_path_result"]["path"] == record["path"]
    with pytest.raises(ValueError, match="claim|destination"):
        obj.take(record["holder"]["agent"], "s", process=holder())
    # Kill-shaped journal: durable reservation written, rename already happened,
    # registration and final setup did not complete. Never return it to ready.
    record["holder"].update(pid=99999999, start="gone")
    (Path(record["path"]) / "unfinished").write_text("preserve")
    record["operation"] = {"name": "take", "phase": "rename", "previous": "ready", "at": "test",
                           "from": record["path"], "to": record["path"]}
    obj.save(record)
    obj.maintain()
    recovered = obj.get(record["id"])
    assert recovered["state"] == "parked" and recovered["holder"] is None
    assert recovered["error"] and Path(recovered["path"]).exists()
    assert git(Path(recovered["path"]), "rev-parse", "--show-toplevel") == recovered["path"]


def test_mapped_reruns_and_path_setup(lifecycle):
    obj = lifecycle
    obj.maintain()
    spare = obj.read_records()[0]
    old_path = spare["path"]
    (obj.repo / "lock.txt").write_text("two")
    git(obj.repo, "add", "lock.txt")
    git(obj.repo, "commit", "-qm", "lock changes")
    git(obj.repo, "push", "origin", "main")
    result = obj.take("mapped", "s", process=holder())
    record = obj.get(result["id"])
    assert record["setup"]["selected"] == ["deps", "paths"]  # once, declared order
    assert (Path(result["path"]) / "deps-output/value").read_text() == "two"
    assert (Path(result["path"]) / "output/cwd").read_text() == result["path"] != old_path
    assert git(Path(result["path"]), "rev-parse", "HEAD") == git(obj.repo, "rev-parse", "origin/main")
    assert record["setup"]["inputs"]["lock.txt"]
    # Rewritten upstream must not reset an acquired spare backwards.
    obj.maintain()
    git(obj.repo, "push", "origin", "HEAD~1:main", "--force")
    with pytest.raises(ValueError, match="fast-forward"):
        obj.take("diverged", "s", process=holder())
    failed = next(r for r in obj.read_records() if Path(r["path"]).name == "diverged")
    assert failed["holder"] and failed["error"]


def test_full_partial_failed_release(lifecycle, monkeypatch):
    obj = lifecycle
    obj.maintain()
    result = obj.take("delivery", "s", process=holder())
    path = Path(result["path"])
    with pytest.raises(ValueError, match="claim"):
        obj.release("wrong")
    (path / "work").write_text("first")
    git(path, "add", "work")
    git(path, "commit", "-qm", "first")
    first = git(path, "rev-parse", "HEAD")
    (path / "work").write_text("second")
    git(path, "commit", "-qam", "second")
    git(path, "push", "origin", f"{first}:main")
    with pytest.raises(ValueError, match="upstream|commits"):
        obj.release(result["claim"])
    assert obj.get(result["id"])["holder"]["claim"] == result["claim"]
    git(path, "push", "origin", "HEAD:main")
    monkeypatch.chdir(path)
    with pytest.raises(ValueError, match="outside"):
        obj.release(result["claim"])
    monkeypatch.chdir(obj.repo)
    assert obj.release(result["claim"])["state"] == "released"
    assert obj.get(result["id"])["holder"] is None
    assert obj.release(result["claim"])["state"] == "released"  # idempotent


@pytest.mark.parametrize("blocker", ["dirty", "untracked", "feature_remote", "holder", "live", "uncertain", "unique_ignored", "main", "locked"])
def test_retirement_safety_matrix(lifecycle, monkeypatch, blocker):
    import worktree_safety as safety
    obj = lifecycle
    obj.maintain()
    record = obj.read_records()[0]
    path = Path(record["path"])
    if blocker == "dirty":
        (path / "lock.txt").write_text("dirty")
    elif blocker == "untracked":
        (path / "new").write_text("untracked")
    elif blocker == "feature_remote":
        (path / "work").write_text("unique")
        git(path, "add", "work")
        git(path, "commit", "-qm", "feature only")
        git(path, "push", "origin", "HEAD:refs/heads/feature")
    elif blocker == "holder":
        record["holder"] = {"agent": "x"}
    elif blocker == "live":
        record["processes"] = [holder()]
    elif blocker == "uncertain":
        record["processes"] = [{"pid": os.getpid()}]
    elif blocker == "unique_ignored":
        (path / "local.secret").write_text("only copy")
    elif blocker == "main":
        record["path"] = str(obj.repo)
    elif blocker == "locked":
        git(obj.repo, "worktree", "lock", str(path))
    obj.save(record)
    if blocker == "main":
        with pytest.raises(ValueError, match="namespace|main"):
            obj.safety(record)
        assert obj.repo.exists()
        return
    evidence = obj.safety(record)
    assert not evidence["safe"] and evidence["blockers"], evidence
    assert path.exists()
    # PID reuse proves the old process is gone; a missing start identity is uncertain.
    assert safety.process_state({**holder(), "start": "different"}) == "dead"


def test_single_flight_recycle_refill(lifecycle, monkeypatch):
    from worktree_pool import _file_lock
    obj = lifecycle
    obj.initialize()
    with _file_lock(obj.state / "maintenance.lock", blocking=False):
        assert obj.maintain()["outcome"] == "already active"
    obj.maintain()
    result = obj.take("recycle", "s", process=holder())
    obj.release(result["claim"])
    obj.maintain()
    recycled = obj.get(result["id"])
    assert recycled["state"] == "ready"
    assert (Path(recycled["path"]) / "output/cwd").read_text() == recycled["path"]
    assert len([r for r in obj.read_records() if r["state"] == "ready"]) == 1
    assert git(obj.repo, "show-ref", "--verify", f"refs/heads/{result['branch']}")
    clone = obj.repo.parent / "clone"
    git(obj.repo, "clone", "-b", "main", str(obj.repo.parent / "remote.git"), str(clone))
    import worktree_lifecycle as lc
    assert lc.Lifecycle(clone).namespace == obj.namespace
    assert lc.Lifecycle(clone).maintain()["built"] == []


def test_disk_floor_only_blocks_build(lifecycle, monkeypatch):
    import worktree_lifecycle as lc
    obj = lifecycle
    obj.maintain()
    write_json(Path.home() / ".claude/bootstrap.json", {"git_kit": {"worktrees": {"disk_floor_percent": 25}}})
    write_json(obj.repo / ".claude/bootstrap.local.json", {"git_kit": {"worktrees": {"disk_floor_percent": 99}}})
    obj = lc.Lifecycle(obj.repo)
    assert obj.settings.disk_floor_percent == 25
    monkeypatch.setattr(lc.Lifecycle, "disk", lambda self: {"free_percent": 25, "floor_percent": 25})
    result = obj.take("lowdisk", "s", process=holder())
    blocked = obj.maintain()
    assert blocked["disk"]["blocked"] and blocked["built"] == []
    obj.release(result["claim"])
    assert obj.maintain()["recycled"] == [result["id"]]


def test_all_operations_request_maintenance(lifecycle):
    obj = lifecycle
    obj.status()
    assert len(obj.requests) == 1
    obj.configure()
    assert len(obj.requests) == 2
    with pytest.raises(ValueError, match="EMPTY"):
        obj.take("empty", "s", process=holder())
    assert len(obj.requests) == 3
    with pytest.raises(ValueError, match="claim"):
        obj.release("missing")
    assert len(obj.requests) == 4


def test_release_never_persists_cleared_holder_before_proof(lifecycle, monkeypatch):
    obj = lifecycle
    obj.maintain()
    result = obj.take("transaction", "s", process=holder())
    saved = obj.save
    observed = []
    def observe(record):
        if record["operation"]["name"] == "release":
            observed.append((record["state"], record["holder"]))
        saved(record)
    monkeypatch.setattr(obj, "save", observe)
    obj.release(result["claim"])
    assert all(owner or state == "released" for state, owner in observed)


def test_legacy_namespace_and_cli_gates(lifecycle):
    obj = lifecycle
    (obj.namespace / "ready/spare-legacy").mkdir(parents=True)
    with pytest.raises(ValueError, match="Legacy.*migration"):
        obj.maintain()
    script = Path(wt.__file__)
    proc = subprocess.run([sys.executable, str(script), "take", "--agent", "a", "--session", "s", "--repo", str(obj.repo)],
                          capture_output=True, text=True)
    assert proc.returncode != 0 and "migration" in proc.stderr
    assert (obj.namespace / "ready/spare-legacy").exists()


def test_replayed_release_cannot_end_another_claim(lifecycle):
    obj = lifecycle
    obj.maintain()
    first = obj.take("first", "s", process=holder())
    obj.release(first["claim"])
    obj.maintain()
    second = obj.take("second", "s", process=holder())
    with pytest.raises(ValueError, match="claim"):
        obj.release(first["claim"])
    assert obj.get(second["id"])["holder"]["claim"] == second["claim"]


@pytest.mark.parametrize("args", [["new", "raw", "--no-setup"], ["take", "old-task"], ["remove", "unused"]])
def test_old_commands_cannot_bypass_lifecycle(lifecycle, args):
    obj = lifecycle
    proc = subprocess.run([sys.executable, str(Path(wt.__file__)), *args], cwd=obj.repo,
                          capture_output=True, text=True)
    assert proc.returncode != 0
    assert "configure" in proc.stderr or "claim" in proc.stderr or "--agent" in proc.stderr
    assert len(wt.records(obj.repo)) == 1


def test_interrupted_build_before_add_does_not_poison_refill(lifecycle):
    obj = lifecycle
    obj.initialize()
    record = obj.new_record()
    assert record["operation"]["phase"] == "reserved"
    record["worker"] = {"pid": 99999999, "start": "gone"}
    obj.save(record)
    obj.maintain()
    assert obj.get(record["id"])["state"] == "removed"
    assert len([r for r in obj.read_records() if r["state"] == "ready"]) == 1


@pytest.mark.parametrize("folder", ["ready", ".staging"])
def test_parent_symlinks_cannot_relocate_outside_namespace(lifecycle, tmp_path, folder):
    obj = lifecycle
    obj.initialize()
    outside = tmp_path / "outside"
    outside.mkdir()
    (obj.namespace / folder).symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink|namespace"):
        obj.maintain()
    assert list(outside.iterdir()) == []


def test_status_observes_plain_commits_and_dirty_files(lifecycle):
    obj = lifecycle
    obj.maintain()
    result = obj.take("status", "s", process=holder())
    path = Path(result["path"])
    (path / "work").write_text("commit")
    git(path, "add", "work")
    git(path, "commit", "-qm", "plain git commit")
    record = obj.status()["records"][0]
    assert record["observed"]["head"] == git(path, "rev-parse", "HEAD") != result["head"]
    (path / "work").write_text("dirty")
    assert obj.status()["records"][0]["observed"]["dirty"]


def test_new_settings_survive_real_bootstrap_script_phase(lifecycle, tmp_path, monkeypatch):
    from bootstrap_lib.engine import _run_script_phase
    import git_kit_bootstrap as gb
    monkeypatch.setattr(gb, "_resolve_gh", lambda: None)
    import shutil
    from bootstrap_lib import tool_paths
    which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda name: None if name == "gh" else which(name))
    monkeypatch.setattr(tool_paths, "resolve", lambda *_: None)
    actions = []
    failures = _run_script_phase({"path": "scripts/bootstrap.py"}, str(Path(wt.__file__).parents[1]),
                                str(tmp_path / "data"), {}, actions, [], plugin_name="git-kit", project_dir=str(lifecycle.repo))
    assert not failures
    assert any("worktrees" in action for action in actions)


def test_claim_races_across_cli_processes(lifecycle):
    obj = lifecycle
    obj.maintain()
    write_json(Path.home() / ".claude/bootstrap.json", {"git_kit": {"worktrees": {"disk_floor_percent": 100}}})
    processes = [subprocess.Popen([sys.executable, str(Path(wt.__file__)), "take", "--agent", f"cli{i}",
        "--session", "s", "--holder-pid", str(os.getpid()), "--repo", str(obj.repo)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for i in range(6)]
    results = [(p, *p.communicate(timeout=40)) for p in processes]
    winners = [json.loads(out) for p, out, err in results if p.returncode == 0]
    assert len(winners) == 1, [(p.returncode, err) for p, out, err in results]
    assert sum(p.returncode == 4 for p, out, err in results) == 5
    assert obj.get(winners[0]["id"])["holder"]["claim"] == winners[0]["claim"]


def test_background_maintenance_and_launcher_failure(lifecycle, monkeypatch):
    obj = lifecycle
    write_json(Path.home() / ".claude/bootstrap.json", {"git_kit": {"worktrees": {"disk_floor_percent": 0}}})
    result = obj.real_request()
    assert result["outcome"] == "started", result
    # Native wait on the accepted child, not a timer or a retry.
    pid, code = os.waitpid(result["pid"], 0)
    assert code == 0, Path(result["log"]).read_text()
    assert json.loads((obj.state / "maintenance.json").read_text())["outcome"] == "complete"
    assert len([r for r in obj.read_records() if r["state"] == "ready"]) == 1
    import worktree_lifecycle as lc
    def refuse(*args, **kwargs):
        raise OSError("test launcher unavailable")
    monkeypatch.setattr(lc.subprocess, "Popen", refuse)
    result = obj.real_request()
    assert result["outcome"] == "failed" and "launcher unavailable" in result["reason"]
    assert "launcher unavailable" in (obj.state / "launcher-error.json").read_text()


def test_take_setup_failure_keeps_claim_and_log(lifecycle):
    import worktree_lifecycle as lc
    obj = lifecycle
    obj.maintain()
    config = json.loads(json.dumps(obj.settings.raw))
    config["pool"]["setup"][1]["argv"] = [sys.executable, "-c", "import sys; print('setup failed'); sys.exit(7)"]
    write_json(obj.repo / ".claude/bootstrap.json", {"git_kit": {"worktrees": config}})
    obj = lc.Lifecycle(obj.repo)
    with pytest.raises(ValueError, match="kept claim"):
        obj.take("failed", "s", process=holder())
    record = obj.read_records()[0]
    assert record["holder"] and record["error"]["exit"] == 7
    assert "setup failed" in Path(record["error"]["log"]).read_text()


def test_reconcile_rename_uses_journal_before_missing_path(lifecycle):
    obj = lifecycle
    obj.maintain()
    record = obj.read_records()[0]
    before = Path(record["path"])
    after = obj.namespace / "crashed"
    record["holder"] = {"agent": "crashed", "session": "s", "claim": "c-crashed", "pid": 99999999, "start": "gone"}
    record["worker"] = {"pid": 99999999, "start": "gone"}
    record["operation"] = {"name": "take", "phase": "rename", "from": str(before), "to": str(after)}
    os.rename(before, after)
    git(after, "worktree", "repair")
    (after / "unfinished").write_text("preserve")
    obj.save(record)  # old path in record; actual Git registration is already repaired
    obj.maintain()
    recovered = obj.get(record["id"])
    assert recovered["path"] == str(after) and recovered["state"] == "parked"
    assert (after / "unfinished").read_text() == "preserve"


def test_reserved_claim_is_never_counted_as_available(lifecycle):
    obj = lifecycle
    obj.maintain()
    record = obj.read_records()[0]
    record["holder"] = {"agent": "reserved", "session": "s", "claim": "c-reserved", **holder()}
    record["worker"] = {"pid": 99999999, "start": "gone"}
    record["operation"] = {"name": "take", "phase": "reserved"}
    obj.save(record)
    obj.maintain()
    assert obj.get(record["id"])["state"] == "taken"
    assert len([r for r in obj.read_records() if r["state"] == "ready" and not r["holder"]]) == 1


def test_release_respects_active_builder(lifecycle):
    obj = lifecycle
    obj.maintain()
    result = obj.take("builder", "s", process=holder())
    record = obj.get(result["id"])
    record["worker"] = holder()
    record["operation"] = {"name": "take", "phase": "setup:paths"}
    obj.save(record)
    with pytest.raises(ValueError, match="builder|worker"):
        obj.release(result["claim"])
    assert obj.get(result["id"])["holder"]["claim"] == result["claim"]


@pytest.mark.parametrize("runtime", [False, True])
def test_unrecorded_process_blocks_retirement(lifecycle, runtime):
    obj = lifecycle
    obj.maintain()
    record = obj.read_records()[0]
    path = Path(record["path"])
    child = subprocess.Popen([sys.executable, "-u", "-c", "import time; print('ready', flush=True); time.sleep(60)", str(path)],
        cwd=obj.repo if runtime else path, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "ready"
        evidence = obj.safety(record)
        assert not evidence["safe"] and any("process" in b or "runtime" in b for b in evidence["blockers"])
    finally:
        child.terminate()
        child.wait(timeout=10)


def test_explicit_disabled_settings_do_not_mutate(lifecycle):
    import worktree_lifecycle as lc
    obj = lifecycle
    write_json(obj.repo / ".claude/bootstrap.json", {"git_kit": {"worktrees": {"enabled": False}}})
    disabled = lc.Lifecycle(obj.repo)
    assert disabled.status()["enabled"] is False
    with pytest.raises(ValueError, match="disabled"):
        disabled.maintain()
    assert not obj.namespace.exists()


def test_linux_runtime_arguments_block_retirement(tmp_path, monkeypatch):
    import worktree_safety as safety
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    process = tmp_path / "proc/1234"
    (process / "fd").mkdir(parents=True)
    (process / "cwd").symlink_to(tmp_path, target_is_directory=True)
    (process / "exe").symlink_to(Path(sys.executable).resolve())
    (process / "cmdline").write_bytes(b"node\0" + str(candidate / "server.js").encode() + b"\0")
    path_class = Path
    monkeypatch.setattr(safety, "Path", lambda value: tmp_path / "proc" if value == "/proc" else path_class(value))
    monkeypatch.setattr(safety.sys, "platform", "linux")
    monkeypatch.setattr(safety.os, "getuid", lambda: process.stat().st_uid, raising=False)
    assert any("runtime" in blocker for blocker in safety.path_processes(candidate))


@pytest.mark.parametrize("case", ["outside", "no_remote", "ambiguous_remote", "unconfigured", "disabled", "disabled_invalid"])
def test_quiet_hook_ignores_repositories_without_lifecycle(lifecycle, tmp_path, capsys, case):
    from worktree_cli import main

    repo = lifecycle.repo
    if case == "outside":
        repo = tmp_path
    elif case == "no_remote":
        git(repo, "remote", "remove", "origin")
    elif case == "ambiguous_remote":
        git(repo, "remote", "rename", "origin", "first")
        git(repo, "remote", "add", "second", "https://example.test/other.git")
    elif case == "unconfigured":
        (repo / ".claude/bootstrap.json").unlink()
    else:
        config = {"enabled": False}
        if case == "disabled_invalid":
            config["pool"] = "invalid"
            write_json(Path.home() / ".claude/bootstrap.json", {"git_kit": {"worktrees": {"disk_floor_percent": "invalid"}}})
        write_json(repo / ".claude/bootstrap.json", {"git_kit": {"worktrees": config}})
    assert main(["--repo", str(repo), "pool", "refill", "--background", "--quiet-if-off"]) == 0
    assert capsys.readouterr() == ("", "")
    assert not lifecycle.requests


@pytest.mark.parametrize("invalid", [False, True])
def test_quiet_hook_checks_enabled_repositories(lifecycle, capsys, invalid):
    from worktree_cli import main

    if invalid:
        write_json(lifecycle.repo / ".claude/bootstrap.json", {"git_kit": {"worktrees": {"enabled": True, "pool": "invalid"}}})
    code = main(["pool", "refill", "--repo", str(lifecycle.repo), "--background", "--quiet-if-off"])
    out, err = capsys.readouterr()
    if invalid:
        assert code == 2 and "git-kit worktrees:" in err and not out
    else:
        assert code == 0 and json.loads(out)["outcome"] == "requested" and not err


@pytest.mark.parametrize("same_owner", [False, True])
def test_linux_inaccessible_fds_only_block_same_uid(tmp_path, monkeypatch, same_owner):
    from types import SimpleNamespace
    import worktree_safety as safety

    process = tmp_path / "proc/1234"
    (process / "fd").mkdir(parents=True)
    (process / "cmdline").write_bytes(b"other-process\0")
    original_iterdir, original_stat = Path.iterdir, Path.stat

    def iterdir(path):
        if path == process / "fd":
            raise PermissionError("fd access denied")
        return original_iterdir(path)

    def stat(path, *args, **kwargs):
        if path == process:
            return SimpleNamespace(st_uid=1000 if same_owner else 2000)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    monkeypatch.setattr(Path, "stat", stat)
    monkeypatch.setattr(safety, "Path", lambda value: tmp_path / "proc" if value == "/proc" else Path(value))
    monkeypatch.setattr(safety.sys, "platform", "linux")
    monkeypatch.setattr(safety.os, "getuid", lambda: 1000, raising=False)
    blockers = safety.path_processes(tmp_path / "candidate")
    if same_owner:
        assert len(blockers) == 1 and "uncertain process 1234" in blockers[0]
    else:
        assert blockers == []


@pytest.mark.parametrize("layer", ["user", "user-local", "project", "project-local"])
def test_lifecycle_root_uses_base_layered_settings(lifecycle, layer):
    import worktree_lifecycle as lc

    obj = lifecycle
    directory = Path.home() / ".claude" if layer.startswith("user") else obj.repo / ".claude"
    target = directory / ("bootstrap.local.json" if layer.endswith("local") else "bootstrap.json")
    payload = json.loads(target.read_text()) if target.exists() else {}
    payload.setdefault("git_kit", {}).setdefault("worktrees", {})["root"] = "~/custom-worktrees"
    write_json(target, payload)
    configured = lc.Lifecycle(obj.repo)
    assert configured.namespace == Path.home() / "custom-worktrees/remote"
    configured.maintain()
    assert Path(configured.read_records()[0]["path"]).is_relative_to(configured.namespace)


@pytest.mark.parametrize("redirect", [False, True])
def test_lifecycle_state_uses_plugin_data_home(lifecycle, tmp_path, monkeypatch, redirect):
    import worktree_lifecycle as lc

    base = Path.home() / ".claude/plugins/data"
    if redirect:
        base = tmp_path / "redirected-data"
        monkeypatch.setenv("CLAUDE_BOOTSTRAP_DATA_ROOT", str(base))
    obj = lc.Lifecycle(lifecycle.repo)
    assert obj.state == base / "plugins-kit/git-kit/worktrees/remote"
    obj.maintain()
    assert (obj.state / "namespace.json").is_file()
    record = obj.read_records()[0]
    assert (obj.state / "records" / f"{record['id']}.json").is_file()
    assert (obj.state / "logs" / f"{record['id']}.log").is_file()
    assert (obj.state / "claim.lock").is_file()
    assert not (obj.namespace / ".state").exists()
    clone = obj.repo.parent / "other-clone"
    git(obj.repo, "clone", "-b", "main", str(obj.repo.parent / "remote.git"), str(clone))
    assert lc.Lifecycle(clone).state == obj.state


@pytest.mark.parametrize("missing", ["absent", "old_module", "old_symbol"])
def test_install_inventory_import_failure_is_diagnosed(lifecycle, capsys, monkeypatch, missing):
    import builtins
    from types import ModuleType
    from worktree_config import require_install

    real_import = builtins.__import__

    def importing(name, *args, **kwargs):
        if name == "bootstrap_lib.marketplace_lifecycle":
            if missing == "old_symbol":
                module = ModuleType(name)
                module.InventoryError = RuntimeError
                return module
            exc = ModuleNotFoundError("test missing inventory")
            exc.name = "bootstrap_lib" if missing == "absent" else name
            raise exc
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", importing)
    with pytest.raises(SystemExit) as error:
        require_install(lifecycle.repo)
    assert error.value.code == 3
    out, err = capsys.readouterr()
    assert not out and "Traceback" not in err
    if missing == "absent":
        assert "not provisioned" in err and "too old" not in err
    else:
        assert "too old" in err and "plugin_installs" in err and "claude plugin update bootstrap@plugins-kit" in err


def test_proposal_covers_each_tracked_npm_lockfile(lifecycle):
    obj = lifecycle
    lockfiles = ["package-lock.json", "tools/package-lock.json", "apps/client/package-lock.json", "apps-client/package-lock.json"]
    for name in lockfiles:
        path = obj.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
        git(obj.repo, "add", name)
    git(obj.repo, "commit", "-qm", "npm lockfiles")
    (obj.repo / "relay").mkdir()
    (obj.repo / "relay/package-lock.json").write_text("{}")  # Untracked files cannot become rerun inputs.
    proposal = obj.configure()["proposal"]
    setup = {step["id"]: step for step in proposal["pool"]["setup"]}
    reruns = proposal["pool"]["rerun_files"]
    assert set(reruns) == set(lockfiles)
    assert len(setup) == len(lockfiles) + 1
    for name in lockfiles:
        assert len(reruns[name]) == 1
        step = setup[reruns[name][0]]
        prefix = Path(name).parent.as_posix()
        assert step["argv"] == ["npm", "ci", *(["--prefix", prefix] if prefix != "." else [])]
        assert step["recreate"] == [(Path(name).parent / "node_modules").as_posix()]
    from worktree_config import validate_config
    validate_config(proposal, obj.repo)


@pytest.mark.parametrize("scope", ["user", "project", "absent"])
def test_lifecycle_user_install_scenarios(lifecycle, scope):
    from worktree_config import require_install

    registry = Path.home() / ".claude/plugins/installed_plugins.json"
    payload = json.loads(registry.read_text())
    if scope == "absent":
        payload["plugins"] = {}
        write_json(Path.home() / ".claude/settings.json", {})
    elif scope == "project":
        record = payload["plugins"]["git-kit@plugins-kit"][0]
        record.update(scope="project", projectPath=str(lifecycle.repo))
        write_json(Path.home() / ".claude/settings.json", {})
        write_json(lifecycle.repo / ".claude/settings.json", {"enabledPlugins": {"git-kit@plugins-kit": True}})
    write_json(registry, payload)
    if scope == "user":
        require_install(lifecycle.repo)
    else:
        with pytest.raises(ValueError, match="install git-kit at user level"):
            require_install(lifecycle.repo)
