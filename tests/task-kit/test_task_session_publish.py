"""The session publisher against real git repositories: a bare remote and clones, no network.

Each guard below was shown to fail with its protection removed; the revert is named in the test's docstring.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest
from git_remote_support import (
    BRANCH,
    break_remote,
    clone,
    commit_all,
    git,
    isolate_git_environment,
    make_remote,
    push,
    remote_files,
    remote_show,
    session_path,
    session_text,
    write,
)

from task_system import oslock, session_publish as sp

HOST = "alpha"
PROJECT = "proj"
MINE = session_path(PROJECT, HOST)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    isolate_git_environment(monkeypatch, tmp_path)


@pytest.fixture
def data_dir(tmp_path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def remote(tmp_path) -> Path:
    return make_remote(tmp_path)


@pytest.fixture
def mine(tmp_path, remote) -> Path:
    return clone(tmp_path, remote, "mine")


def own_record(root: Path, revision: int = 1, host: str = HOST) -> None:
    write(root, session_path(PROJECT, host), session_text(host, revision))


def publish(root: Path, data_dir: Path, **kw) -> sp.PublishResult:
    return sp.publish(root, HOST, data_dir=data_dir, **kw)


def rival_push(tmp_path: Path, remote: Path, name: str, rel: str, text: str = "x\n") -> None:
    """Another machine publishes ``rel`` (any path) to the remote."""
    rival = tmp_path / f"rival-{name}"
    if not rival.exists():
        clone(tmp_path, remote, f"rival-{name}")
    git(rival, "pull", "-q", "--no-rebase", "origin", BRANCH)
    commit_all(rival, rel, text, f"rival {name}")
    push(rival)


def inject_before_push(monkeypatch, hook) -> list[list[str]]:
    """Run ``hook(cwd)`` just before every ``git push`` the publisher makes; returns the pushes seen."""
    real = sp._git
    pushes: list[list[str]] = []

    def wrapper(args, cwd, **kw):
        if args and args[0] == "push":
            pushes.append(list(args))
            hook(cwd)
        return real(args, cwd, **kw)

    monkeypatch.setattr(sp, "_git", wrapper)
    return pushes


# ----------------------------------------------------------------------------- commit first, then network


def test_offline_record_is_committed_before_any_network_access(mine, tmp_path, data_dir):
    """Revert: fetch before commit -> the fetch fails first and the file is still modified/untracked (red)."""
    own_record(mine)
    break_remote(mine, tmp_path)

    result = publish(mine, data_dir)

    assert result.status == sp.FETCH_FAILED and result.committed and not result.published
    assert "fetch failed" in result.message and sp.CANNOT_SEE in result.message
    assert git(mine, "status", "--porcelain", "--", MINE).stdout == ""
    assert git(mine, "log", "-1", "--format=%s", "--", MINE).stdout.strip() == f"task-kit sessions: {HOST}"


def test_offline_modification_of_a_tracked_record_is_committed_too(mine, tmp_path, data_dir):
    own_record(mine, 1)
    publish(mine, data_dir)
    own_record(mine, 2)
    break_remote(mine, tmp_path)

    result = publish(mine, data_dir)

    assert result.status == sp.FETCH_FAILED and result.committed
    assert git(mine, "status", "--porcelain", "--", MINE).stdout == ""
    assert sp.pending(mine, HOST) == (MINE,)


def test_committed_unpublished_record_is_delivered_by_a_later_publish(mine, tmp_path, data_dir, remote):
    own_record(mine)
    good_url = break_remote(mine, tmp_path)
    assert publish(mine, data_dir).status == sp.FETCH_FAILED
    assert MINE not in remote_files(remote)
    assert sp.pending(mine, HOST) == (MINE,)

    git(mine, "remote", "set-url", "origin", good_url)
    drained = publish(mine, data_dir)

    assert drained.status == sp.PUBLISHED and drained.published
    assert remote_show(remote, MINE) == session_text(HOST)
    assert sp.pending(mine, HOST) == ()


def test_commit_failure_stops_before_the_network(mine, data_dir, remote):
    own_record(mine)
    hook = mine / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'hook says no' >&2\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)

    result = publish(mine, data_dir)

    assert result.status == sp.NOT_COMMITTED and not result.committed
    assert result.message.startswith("not committed:")
    assert MINE not in remote_files(remote)


# ----------------------------------------------------------------------------- the ordinary paths


def test_in_step_publish_pushes_only_the_record(mine, data_dir, remote):
    own_record(mine)

    result = publish(mine, data_dir)

    assert result.status == sp.PUBLISHED and result.attempts == 1 and result.files == (MINE,)
    assert remote_files(remote) == {"README.md", MINE}


def test_nothing_pending_makes_no_network_call(mine, data_dir, monkeypatch):
    own_record(mine)
    publish(mine, data_dir)
    seen: list[str] = []
    real = sp._git
    monkeypatch.setattr(sp, "_git", lambda args, cwd, **kw: (seen.append(args[0]), real(args, cwd, **kw))[1])

    result = publish(mine, data_dir)

    assert result.status == sp.NOTHING_PENDING and result.published
    assert "fetch" not in seen and "push" not in seen


def test_behind_upstream_is_merged_then_pushed(mine, tmp_path, data_dir, remote):
    rival_push(tmp_path, remote, "a", f"{PROJECT}/task-one/task.yaml", "id: one\n")
    own_record(mine)

    result = publish(mine, data_dir)

    assert result.status == sp.PUBLISHED
    assert remote_files(remote) == {"README.md", MINE, f"{PROJECT}/task-one/task.yaml"}
    assert len(git(mine, "rev-list", "--parents", "-1", "HEAD").stdout.split()) == 3  # a merge commit
    assert (mine / PROJECT / "task-one" / "task.yaml").exists()


def test_upstream_task_record_commit_is_integrated_and_the_push_succeeds(mine, tmp_path, data_dir, remote):
    own_record(mine)
    rival_push(tmp_path, remote, "b", f"{PROJECT}/notes.md", "upstream notes\n")

    result = publish(mine, data_dir)

    assert result.status == sp.PUBLISHED
    assert remote_show(remote, f"{PROJECT}/notes.md") == "upstream notes\n"
    assert remote_show(remote, MINE) == session_text(HOST)


# ----------------------------------------------------------------------------- the push race


def test_a_racing_upstream_push_is_refetched_merged_and_retried(mine, tmp_path, data_dir, remote, monkeypatch):
    own_record(mine)
    raced: list[int] = []

    def rival(_cwd):
        if not raced:
            raced.append(1)
            rival_push(tmp_path, remote, "r", session_path(PROJECT, "beta"), session_text("beta"))

    pushes = inject_before_push(monkeypatch, rival)

    result = publish(mine, data_dir)

    assert result.status == sp.PUBLISHED and result.attempts == 2 and len(pushes) == 2
    assert {MINE, session_path(PROJECT, "beta")} <= remote_files(remote)


def test_two_barrier_synchronized_publishers_both_reach_the_remote(tmp_path, remote, data_dir, monkeypatch):
    """Revert: no retry (MAX_ATTEMPTS = 1) -> the loser's record is missing from the remote (red)."""
    clones = {host: clone(tmp_path, remote, f"clone-{host}") for host in ("alpha", "beta")}
    for host, root in clones.items():
        own_record(root, host=host)
    barrier = threading.Barrier(2, timeout=30)
    first_push = threading.local()
    real = sp._git

    def wrapper(args, cwd, **kw):
        if args and args[0] == "push" and not getattr(first_push, "done", False):
            first_push.done = True
            barrier.wait()  # both are about to push from the same upstream state
        return real(args, cwd, **kw)

    monkeypatch.setattr(sp, "_git", wrapper)
    results: dict[str, sp.PublishResult] = {}

    def run(host: str) -> None:
        results[host] = sp.publish(clones[host], host, data_dir=data_dir / host)

    threads = [threading.Thread(target=run, args=(h,)) for h in clones]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)

    assert {h: (r.status, r.message) for h, r in results.items()} == {"alpha": (sp.PUBLISHED, "published"), "beta": (sp.PUBLISHED, "published")}
    assert {session_path(PROJECT, "alpha"), session_path(PROJECT, "beta")} <= remote_files(remote)
    assert max(r.attempts for r in results.values()) == 2


def test_retries_are_bounded_and_name_the_last_git_line(mine, tmp_path, data_dir, remote, monkeypatch):
    """Revert: raise MAX_ATTEMPTS (or drop the bound) -> more than three pushes are made (red)."""
    own_record(mine)
    counter = [0]

    def always_race(_cwd):
        counter[0] += 1
        rival_push(tmp_path, remote, "loop", session_path(PROJECT, f"rival{counter[0]}"), "x\n")

    pushes = inject_before_push(monkeypatch, always_race)

    result = publish(mine, data_dir)

    assert result.status == sp.EXHAUSTED and not result.published and result.committed
    assert len(pushes) == sp.MAX_ATTEMPTS == 3 and result.attempts == 3
    assert "not published after 3 attempts" in result.message and "failed to push" in result.message
    assert MINE not in remote_files(remote)
    assert git(mine, "status", "--porcelain", "--", MINE).stdout == ""


def test_attempts_stop_when_the_time_window_is_spent(mine, tmp_path, data_dir, remote, monkeypatch):
    own_record(mine)
    pushes = inject_before_push(
        monkeypatch, lambda _c: rival_push(tmp_path, remote, "w", session_path(PROJECT, "w1"), "x\n"))

    result = publish(mine, data_dir, window_s=0.0)

    assert result.status == sp.EXHAUSTED and result.attempts == 1 and len(pushes) == 1


def test_a_remote_rejection_that_is_not_a_race_stops_without_retry(mine, data_dir, remote):
    own_record(mine)
    hook = remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'protected branch' >&2\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)

    result = publish(mine, data_dir)

    assert result.status == sp.PUSH_FAILED and result.attempts == 1 and result.committed
    assert "push failed" in result.message


# ----------------------------------------------------------------------------- only this machine's files


def test_a_foreign_local_commit_blocks_integration_and_push(mine, data_dir, remote):
    """Revert: remove the local-commit guard -> the foreign commit reaches the remote (red)."""
    commit_all(mine, f"{PROJECT}/stray.md", "not a session file\n", "unpushed work")
    own_record(mine)

    result = publish(mine, data_dir)

    assert result.status == sp.FOREIGN_LOCAL and result.committed and not result.published
    assert "unpushed commits outside this machine's session records" in result.message
    assert remote_files(remote) == {"README.md"}
    assert git(mine, "status", "--porcelain", "--", MINE).stdout == ""


def test_another_hosts_local_session_commit_is_foreign_too(mine, data_dir, remote):
    commit_all(mine, session_path(PROJECT, "beta"), session_text("beta"), "wrong host")
    own_record(mine)

    result = publish(mine, data_dir)

    assert result.status == sp.FOREIGN_LOCAL
    assert remote_files(remote) == {"README.md"}


def test_the_push_never_carries_a_path_that_is_not_this_machines(mine, data_dir, remote):
    """Revert: skip the net-diff check -> an evil merge (foreign file only in the merge) is pushed (red)."""
    git(mine, "checkout", "-q", "-b", "side")
    git(mine, "commit", "-q", "--allow-empty", "-m", "side")
    git(mine, "checkout", "-q", BRANCH)
    write(mine, f"{PROJECT}/smuggled.md", "only in the merge commit\n")
    git(mine, "merge", "-q", "--no-ff", "--no-commit", "side", check=False)
    git(mine, "add", f"{PROJECT}/smuggled.md")
    git(mine, "commit", "-q", "-m", "merge side")
    assert len(git(mine, "rev-list", "--parents", "-1", "HEAD").stdout.split()) == 3
    own_record(mine)

    result = publish(mine, data_dir)

    assert result.status == sp.FOREIGN_DIFF and result.committed
    assert "smuggled.md" in result.message
    assert remote_files(remote) == {"README.md"}


def test_pre_staged_unrelated_content_survives_and_is_not_published(mine, data_dir, remote):
    write(mine, f"{PROJECT}/other.txt", "someone else's staged work\n")
    git(mine, "add", f"{PROJECT}/other.txt")
    own_record(mine)

    result = publish(mine, data_dir)

    assert result.status == sp.PUBLISHED
    assert git(mine, "diff", "--cached", "--name-only").stdout.split() == [f"{PROJECT}/other.txt"]
    assert remote_files(remote) == {"README.md", MINE}


def test_a_merge_git_refuses_is_reported_and_leaves_no_merge_state(mine, tmp_path, data_dir, remote):
    rival_push(tmp_path, remote, "c", f"{PROJECT}/collide.md", "upstream\n")
    write(mine, f"{PROJECT}/collide.md", "untracked local file\n")  # incoming path blocked by a local file
    own_record(mine)

    result = publish(mine, data_dir)

    assert result.status == sp.INTEGRATE_FAILED and result.committed and "not published" in result.message
    assert not (mine / ".git" / "MERGE_HEAD").exists()
    assert (mine / PROJECT / "collide.md").read_text(encoding="utf-8") == "untracked local file\n"
    assert MINE not in remote_files(remote)


# ----------------------------------------------------------------------------- preflight


def test_preflight_accepts_a_publishable_durable_root(mine):
    (mine / PROJECT).mkdir()
    verdict = sp.preflight(mine / PROJECT, machine=HOST, location="dev/tasks")
    assert verdict.ok and verdict.kind == sp.KIND_DURABLE and verdict.repo_root == mine.resolve()


def test_preflight_refuses_a_missing_task_root(tmp_path):
    verdict = sp.preflight(tmp_path / "absent", location="dev/tasks")
    assert not verdict.ok and "does not exist" in verdict.reason


def test_preflight_refuses_a_non_git_root_with_a_reason(tmp_path):
    folder = tmp_path / "plain"
    folder.mkdir()
    verdict = sp.preflight(folder, location="dev/tasks")
    assert not verdict.ok and "not inside a git work tree" in verdict.reason
    assert list(folder.iterdir()) == []


def test_preflight_refuses_a_git_ignored_session_file(mine):
    write(mine, ".gitignore", f"{PROJECT}/.sessions/\n")
    (mine / PROJECT).mkdir()
    verdict = sp.preflight(mine / PROJECT, machine=HOST, location="dev/tasks")
    assert not verdict.ok and "git ignores" in verdict.reason


def test_preflight_refuses_a_branch_without_an_upstream(tmp_path):
    lonely = tmp_path / "lonely"
    lonely.mkdir()
    git(lonely, "init", "-q", f"--initial-branch={BRANCH}")
    verdict = sp.preflight(lonely, location="dev/tasks")
    assert not verdict.ok and "no upstream" in verdict.reason


def test_preflight_refuses_a_detached_head(mine):
    git(mine, "checkout", "-q", "--detach")
    verdict = sp.preflight(mine, location="dev/tasks")
    assert not verdict.ok and "detached" in verdict.reason


def test_preflight_keeps_tmp_local_and_off_git(tmp_path):
    verdict = sp.preflight(tmp_path / "project" / "tmp")
    assert verdict.ok and verdict.kind == sp.KIND_LOCAL and verdict.repo_root is None


def test_preflight_infers_durable_from_dev_tasks_and_honours_record_false(mine):
    inferred = mine / "dev" / "tasks"
    inferred.mkdir(parents=True)
    assert sp.preflight(inferred, machine=HOST).kind == sp.KIND_DURABLE
    off = sp.preflight(inferred, machine=HOST, record=False)
    assert not off.ok and off.kind == sp.KIND_DISABLED and off.reason == "sessions.record is false"


def test_publish_with_record_false_does_nothing(mine, data_dir, remote):
    own_record(mine)
    result = publish(mine, data_dir, record=False)
    assert result.status == sp.DISABLED
    assert git(mine, "status", "--porcelain", "--", MINE).stdout.startswith("??")
    assert remote_files(remote) == {"README.md"}


# ----------------------------------------------------------------------------- pending, log, lock, runner


def test_pending_lists_uncommitted_and_unpublished_files_and_is_read_only(mine, tmp_path, data_dir):
    assert sp.pending(mine, HOST) == ()
    own_record(mine)
    assert sp.pending(mine, HOST) == (MINE,)  # untracked
    break_remote(mine, tmp_path)
    publish(mine, data_dir)
    assert sp.pending(mine, HOST) == (MINE,)  # committed, not on the upstream
    other = session_path(PROJECT, "beta")
    write(mine, other, session_text("beta"))
    assert sp.pending(mine, HOST) == (MINE,)  # another host's file is not this machine's
    assert sp.pending(tmp_path, HOST) == ()  # not a work tree


def test_every_outcome_including_failures_is_returned_and_nothing_is_logged(mine, tmp_path, data_dir):
    """Failures reach the caller as the returned result; the data directory keeps no record of outcomes (only the
    information-free repository lock file may appear there). Revert: write a log line again -> the file check is red."""
    own_record(mine)
    break_remote(mine, tmp_path)
    failed = publish(mine, data_dir)
    refused = publish(tmp_path / "nowhere", data_dir)

    assert failed.status == sp.FETCH_FAILED and "fetch failed" in failed.message
    assert refused.status == sp.REFUSED
    written = [p for p in data_dir.rglob("*") if p.is_file()]
    assert all(p.suffix == ".lock" and p.parent.name == "locks" for p in written), written


def test_a_busy_repository_lock_reports_not_committed(mine, data_dir):
    own_record(mine)
    top = mine.resolve()
    with oslock.FileLock(sp.lock_path(top, data_dir), 5):
        result = publish(mine, data_dir, lock_wait_s=0.2)
    assert result.status == sp.BUSY and not result.committed
    assert git(mine, "status", "--porcelain", "--", MINE).stdout.startswith("??")


def test_the_runner_kills_a_command_that_outlives_its_bound():
    outcome = sp._run([sys.executable, "-c", "import time; time.sleep(30)"], cwd=Path.cwd(), timeout=0.5)
    assert outcome.timed_out and "timed out" in outcome.stderr


def test_the_runner_closes_stdin_and_forbids_prompts(monkeypatch):
    monkeypatch.setenv("GIT_DIR", "/nonexistent")
    probe = ("import os,sys; sys.stdout.write(os.environ.get('GIT_TERMINAL_PROMPT','')+'|'+"
             "str(os.environ.get('GIT_DIR'))+'|'+repr(sys.stdin.read()))")
    outcome = sp._run([sys.executable, "-c", probe], cwd=Path.cwd(), timeout=10)
    assert outcome.stdout == "0|None|''"
