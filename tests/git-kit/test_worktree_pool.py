"""Shared ready-worktree pool: refill, atomic take, staleness and the hard block.

Run: uv run --extra dev pytest tests/git-kit/test_worktree_pool.py -q
Real Git repositories; setup steps are small Python commands.
"""

import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from test_worktrees import SCRIPT, cli, manifest, remote_repo, wt  # noqa: F401 (fixtures)

POOL_SCRIPT = SCRIPT.with_name("worktree_pool.py")
HOOK = SCRIPT.parents[1] / "hooks/sessionstart/pool-refill.sh"

# Step 1 proves a build runs in staging and that no spare is visible before its
# steps pass; step 2 leaves ignored setup output, like node_modules.
STEP_CHECK = (
    "import json, os, sys\n"
    "from pathlib import Path\n"
    "cwd = Path.cwd()\n"
    "ready = sorted(p.name for p in cwd.parent.parent.glob('spare-*'))\n"
    "with open(os.environ['POOL_STEPS'], 'a') as f: f.write(json.dumps([str(cwd), ready]) + '\\n')\n"
    "sys.exit(int(os.environ.get('POOL_STEP_EXIT', '0')))\n"
)
STEP_OUTPUT = "from pathlib import Path; Path('built.out').write_text('ok')"


@pytest.fixture
def pool_repo(remote_repo, tmp_path, monkeypatch):  # noqa: F811
    remote_repo.commit_file(".gitignore", "built.out\n")
    remote_repo.commit_file("lock.txt", "v1")
    remote_repo.git("push", "-q", "origin", "main")
    monkeypatch.setenv("POOL_STEPS", str(tmp_path / "steps.jsonl"))
    # Background refills from takes would race the assertions; tests run refill explicitly.
    monkeypatch.setenv("GIT_KIT_POOL_BUILD", "1")
    configure(remote_repo, size=2)
    return remote_repo


def configure(repo, size, **extra):
    pool = {"size": size, "setup": [[sys.executable, "-c", STEP_CHECK], [sys.executable, "-c", STEP_OUTPUT]],
            "rebuild_when_changed": ["lock.txt"], **extra}
    manifest(Path.home(), {"git_kit": {"worktrees": {"enabled": True, "pool": pool}}})


def pool_path():
    return Path.home() / ".worktrees" / "remote" / "ready"


def refill(repo, check=True):
    env = {k: v for k, v in os.environ.items() if k != "GIT_KIT_POOL_BUILD"}
    proc = subprocess.run([sys.executable, str(SCRIPT), "pool", "refill"], cwd=repo.path,
                          capture_output=True, text=True, env=env)
    if check:
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)
    return proc


def spares():
    return sorted(p.name for p in pool_path().glob("spare-*"))


def worktree_paths(repo):
    return [line.split(" ", 1)[1] for line in repo.git("worktree", "list", "--porcelain").stdout.splitlines()
            if line.startswith("worktree ")]


def push_change(repo, name, text):
    repo.commit_file(name, text)
    repo.git("push", "-q", "origin", "main")
    return repo.git("rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def pool_mod(wt):  # noqa: F811
    spec = importlib.util.spec_from_file_location("git_kit_worktree_pool", POOL_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_refill_builds_to_size_in_staging_then_renames(pool_repo):
    result = refill(pool_repo)
    assert len(result["built"]) == 2
    names = spares()
    assert len(names) == 2
    head = pool_repo.git("rev-parse", "origin/main").stdout.strip()
    registered = worktree_paths(pool_repo)
    for name in names:
        spare = pool_path() / name
        assert str(spare.resolve()) in registered  # repaired after the rename
        assert subprocess.run(["git", "-C", str(spare), "rev-parse", "HEAD"], capture_output=True,
                              text=True, check=True).stdout.strip() == head
        assert (spare / "built.out").read_text() == "ok"
    ran = [json.loads(line) for line in Path(os.environ["POOL_STEPS"]).read_text().splitlines()]
    assert len(ran) == 2
    for cwd, ready in ran:
        assert Path(cwd).parent.name == ".staging" and Path(cwd).name not in ready
    assert not list((pool_path() / ".staging").iterdir())
    assert not list((pool_path() / ".state").glob("*.json"))
    assert refill(pool_repo)["built"] == []  # Full pool: idempotent.
    assert cli(pool_repo, "list") == []  # The pool is not reported as task worktrees.


def test_failed_build_records_reason_and_leaves_no_spare(pool_repo, monkeypatch):
    monkeypatch.setenv("POOL_STEP_EXIT", "7")
    proc = refill(pool_repo, check=False)
    assert proc.returncode == 2
    assert "exited 7" in proc.stderr
    assert spares() == []
    records = [json.loads(p.read_text()) for p in (pool_path() / ".state").glob("*.json")]
    assert len(records) == 1 and records[0]["state"] == "failed"
    assert "step 1/2" in records[0]["reason"] and "exited 7" in records[0]["reason"]
    assert Path(records[0]["log"]).is_file()
    assert not list((pool_path() / ".staging").iterdir())
    assert not any(".staging" in path for path in worktree_paths(pool_repo))


def test_take_renames_a_spare_and_creates_the_task_branch(pool_repo):
    configure(pool_repo, size=1)
    refill(pool_repo)
    spare = spares()[0]
    output = cli(pool_repo, "take", "feature")
    task = Path(output["path"])
    assert task == Path.home() / ".worktrees" / "remote" / "feature"
    assert output["spare"] == spare and spares() == []
    assert (task / "built.out").is_file()
    assert subprocess.run(["git", "-C", str(task), "branch", "--show-current"], capture_output=True,
                          text=True, check=True).stdout.strip() == "feature"
    assert str(task) in worktree_paths(pool_repo)
    assert [row["path"] for row in cli(pool_repo, "list")] == [str(task)]
    assert cli(pool_repo, "take", "feature", check=False).returncode == 2  # branch exists
    assert cli(pool_repo, "take", "ready", check=False).returncode == 2  # reserved
    cli(pool_repo, "remove", str(task))
    assert not task.exists()


def test_concurrent_takes_never_share_a_spare(pool_repo):
    refill(pool_repo)
    procs = [subprocess.Popen([sys.executable, str(SCRIPT), "take", f"task-{i}"], cwd=pool_repo.path,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for i in range(6)]
    results = [(p.wait(), *p.communicate()) for p in procs]
    won = [json.loads(out) for code, out, _ in results if code == 0]
    blocked = [err for code, _, err in results if code == 4]
    assert len(won) == 2 and len(blocked) == 4, results
    assert len({w["spare"] for w in won}) == 2 and len({w["path"] for w in won}) == 2
    assert all("hard block" in err for err in blocked)


def test_claim_rename_has_one_winner_under_threads(pool_mod, tmp_path):
    pool = tmp_path / "ready"
    (pool / "spare-1").mkdir(parents=True)
    barrier = threading.Barrier(16)
    wins = []

    def taker(i):
        barrier.wait()
        if pool_mod.claim(pool, tmp_path / f"task-{i}") is not None:
            wins.append(i)

    threads = [threading.Thread(target=taker, args=(i,)) for i in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(wins) == 1
    assert (tmp_path / f"task-{wins[0]}").is_dir()


def test_empty_pool_hard_block_names_builds_failures_and_holders(pool_repo, pool_mod, monkeypatch):
    configure(pool_repo, size=1)
    refill(pool_repo)
    taken = Path(cli(pool_repo, "take", "holder")["path"])
    monkeypatch.setenv("POOL_STEP_EXIT", "9")
    refill(pool_repo, check=False)
    monkeypatch.delenv("POOL_STEP_EXIT")
    steps_before = Path(os.environ["POOL_STEPS"]).read_text()
    state = pool_path() / ".state"
    (state / "spare-busy.json").write_text(json.dumps(
        {"id": "spare-busy", "state": "building", "started": "now", "step": "2/2: npm ci", "log": "busy.log"}))
    with pool_mod.refill_lock(pool_path()) as held:
        assert held
        proc = cli(pool_repo, "take", "blocked", check=False)
    assert proc.returncode == 4
    assert "hard block" in proc.stderr
    assert "spare-busy: 2/2: npm ci" in proc.stderr
    assert "exited 9" in proc.stderr
    assert f"holder: {taken}" in proc.stderr
    assert not (pool_path().parent / "blocked").exists()
    assert Path(os.environ["POOL_STEPS"]).read_text() == steps_before  # no setup ran
    assert not Path(os.environ["BOOTSTRAP_TEST_RECORD"]).exists()


def test_stale_spare_moves_to_upstream_and_reruns_setup_only_for_lockfiles(pool_repo):
    refill(pool_repo)
    head = push_change(pool_repo, "other.txt", "x")
    first = cli(pool_repo, "take", "plain")
    assert first["rebased"] is True and first["head"] == head
    assert first["setup"] == {"ran": False, "exit": None, "changed": []}
    head = push_change(pool_repo, "lock.txt", "v2")
    runs = len(Path(os.environ["POOL_STEPS"]).read_text().splitlines())
    second = cli(pool_repo, "take", "locked")
    assert second["head"] == head
    assert second["setup"] == {"ran": True, "exit": 0, "changed": ["lock.txt"]}
    assert len(Path(os.environ["POOL_STEPS"]).read_text().splitlines()) == runs + 1
    assert subprocess.run(["git", "-C", second["path"], "rev-parse", "HEAD"], capture_output=True,
                          text=True, check=True).stdout.strip() == head


def test_abandoned_build_is_recorded_and_replaced(pool_repo):
    configure(pool_repo, size=1)
    refill(pool_repo)
    cli(pool_repo, "take", "first")
    staging = pool_path() / ".staging" / "spare-dead"
    pool_repo.git("worktree", "add", "--detach", str(staging), "origin/main")
    (pool_path() / ".state" / "spare-dead.json").write_text(json.dumps(
        {"id": "spare-dead", "state": "building", "started": "then", "step": "1/2", "log": "dead.log"}))
    assert len(refill(pool_repo)["built"]) == 1
    record = json.loads((pool_path() / ".state" / "spare-dead.json").read_text())
    assert record["state"] == "failed" and record["reason"].startswith("abandoned")
    assert not staging.exists()


def test_second_refill_stands_down_while_one_runs(pool_repo, pool_mod):
    with pool_mod.refill_lock(pool_path()) as held:
        assert held
        result = refill(pool_repo)
    assert result["skipped"] == "another refill is running" and spares() == []


@pytest.mark.parametrize("pool,match", [
    ([], "must be an object"),
    ({"size": 0}, "size"),
    ({"size": True}, "size"),
    ({"setup": []}, "setup"),
    ({"setup": [["ok"], []]}, "setup"),
    ({"rebuild_when_changed": "lock"}, "rebuild_when_changed"),
    ({"bogus": 1}, "Unknown"),
    ({"after_take": {"remove": ["../x"]}}, "after_take.remove"),
    ({"after_take": {"remove": ["/abs"]}}, "after_take.remove"),
    ({"after_take": {"steps": [[]]}}, "after_take.steps"),
    ({"after_take": {"other": 1}}, "after_take"),
])
def test_pool_settings_are_validated(wt, tmp_path, pool, match):  # noqa: F811
    manifest(Path.home(), {"git_kit": {"worktrees": {"enabled": True, "pool": pool}}})
    with pytest.raises(ValueError, match=match):
        wt.resolve_settings(tmp_path)


def test_pool_defaults(wt, tmp_path):  # noqa: F811
    manifest(Path.home(), {"git_kit": {"worktrees": {"enabled": True, "pool": {}}}})
    pool = wt.resolve_settings(tmp_path).pool
    assert (pool.size, pool.setup, pool.rebuild_when_changed) == (2, [["bootstrap", "run"]], [])
    assert (list(pool.after_take_remove), list(pool.after_take)) == ([], [])
    manifest(Path.home(), {"git_kit": {"worktrees": {"enabled": True}}})
    assert wt.resolve_settings(tmp_path).pool is None


def test_pool_folder_shared_by_remote_refuses_another_remote(pool_repo):
    refill(pool_repo)
    record = pool_path() / "pool.json"
    data = json.loads(record.read_text())
    record.write_text(json.dumps({**data, "remote": "https://example.invalid/other/remote.git"}))
    proc = cli(pool_repo, "take", "x", check=False)
    assert proc.returncode == 2 and "two remotes share the folder name" in proc.stderr


def test_session_hook_starts_a_background_refill(pool_repo, tmp_path):
    configure(pool_repo, size=1)
    env = {k: v for k, v in os.environ.items() if k != "GIT_KIT_POOL_BUILD"}
    env.update(BOOTSTRAP_PYTHON=sys.executable, CLAUDE_PROJECT_DIR=str(pool_repo.path))
    started = time.monotonic()
    proc = subprocess.run(["sh", str(HOOK)], capture_output=True, text=True, env=env)
    assert proc.returncode == 0 and proc.stdout == "", proc.stderr
    deadline = started + 60
    while not spares() and time.monotonic() < deadline:
        time.sleep(0.2)
    assert len(spares()) == 1
    outside = tmp_path / "not-git"
    outside.mkdir()
    proc = subprocess.run(["sh", str(HOOK)], capture_output=True, text=True,
                          env={**env, "CLAUDE_PROJECT_DIR": str(outside)})
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")


# Writes the current directory into built.out, as a venv records its own path.
STEP_RECORD_PATH = "from pathlib import Path; Path('built.out').write_text(str(Path.cwd()))"


def test_after_take_remakes_path_bound_setup_at_the_task_path(pool_repo):
    configure(pool_repo, size=1, setup=[[sys.executable, "-c", STEP_RECORD_PATH]],
              after_take={"remove": ["built.out"], "steps": [[sys.executable, "-c", STEP_RECORD_PATH]]})
    refill(pool_repo)
    built_in = (pool_path() / spares()[0] / "built.out").read_text()
    assert ".staging" in built_in  # the stale absolute path a moved venv would keep
    output = cli(pool_repo, "take", "moved")
    task = Path(output["path"])
    assert output["after_take"] == {"removed": ["built.out"], "exit": 0}
    assert Path((task / "built.out").read_text()).resolve() == task.resolve()


def test_after_take_failure_keeps_the_worktree_and_exits_with_the_step_code(pool_repo):
    configure(pool_repo, size=1, after_take={"steps": [[sys.executable, "-c", "raise SystemExit(5)"]]})
    refill(pool_repo)
    proc = cli(pool_repo, "take", "broken", check=False)
    assert proc.returncode == 5
    assert "after_take step" in proc.stderr
    assert (pool_path().parent / "broken").is_dir()


def test_after_take_never_removes_a_path_git_tracks(pool_repo):
    configure(pool_repo, size=1, after_take={"remove": ["lock.txt"]})
    refill(pool_repo)
    proc = cli(pool_repo, "take", "tracked", check=False)
    assert proc.returncode == 2 and "not ignored" in proc.stderr
    assert (pool_path().parent / "tracked" / "lock.txt").is_file()
