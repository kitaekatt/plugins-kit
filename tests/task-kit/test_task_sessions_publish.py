"""The publisher's answer-file ownership and the shared repository lock, against real git repositories.

Extends the session publisher (``test_task_session_publish.py`` holds its session-file cases) with D3/U5 of the
task answers design: this machine's answer files are published, another machine's are never, and the
per-repository lock is shared with the answer engine. Each guard names the revert that turns its test red.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from git_remote_support import (
    BRANCH,
    clone,
    commit_all,
    git,
    isolate_git_environment,
    make_remote,
    push,
    remote_files,
    session_path,
    session_text,
    write,
)

from task_system import oslock, session_publish as sp
from task_system.machine import Machine

HOST = "alpha"
MINE_FP = "3f9a0c1d2e4b5a6c"
OTHER_FP = "aaaaaaaaaaaaaaaa"
ME = Machine(HOST, MINE_FP, "macos")
TASK = "proj/stub"


def answer_path(host: str = HOST, qid: str = "orientation", task: str = TASK) -> str:
    return f"{task}/answers/{qid}/{host}.yaml"


def answer_text(fingerprint: str, host: str = HOST) -> str:
    return (f'_schema_version: "1"\nhost: {host}\nmachine: {fingerprint}\nos_instance: macos\n'
            "question: orientation\nanswer:\n  headline: h\n  machine: nested-is-not-the-owner\n")


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


def rival_push(tmp_path: Path, remote: Path, rel: str, text: str) -> None:
    rival = tmp_path / "rival"
    if not rival.exists():
        clone(tmp_path, remote, "rival")
    git(rival, "pull", "-q", "--no-rebase", "origin", BRANCH)
    commit_all(rival, rel, text, "rival")
    push(rival)


def publish(root: Path, data_dir: Path, machine=ME, **kw) -> sp.PublishResult:
    return sp.publish(root, machine, data_dir=data_dir, **kw)


# ----------------------------------------------------------------------------- the one content parser


@pytest.mark.parametrize("text, expected", [
    ("machine: 3f9a0c1d2e4b5a6c\n", "3f9a0c1d2e4b5a6c"),
    ('host: a\nmachine: "3f9a0c1d2e4b5a6c"  # c\n', "3f9a0c1d2e4b5a6c"),
    ("machine: '1234567890123456'\n", "1234567890123456"),
    ("machine: 12345e6789012345\n", "12345e6789012345"),
    ("answer:\n  machine: nested\n", None),
    ("host: a\n", None),
])
def test_answer_record_machine_reads_the_top_level_line_only(text, expected):
    assert sp.answer_record_machine(text) == expected


def test_answer_path_shape():
    assert sp.is_answer_path(answer_path(), HOST)
    assert not sp.is_answer_path(answer_path("beta"), HOST)
    assert not sp.is_answer_path(f"{TASK}/answers/{HOST}.yaml", HOST)  # no question folder
    assert not sp.is_answer_path(f"{TASK}/notes/orientation/{HOST}.yaml", HOST)


# ----------------------------------------------------------------------------- publishing own answer files


def test_publishes_a_commit_holding_only_this_hosts_answer_files(mine, remote, data_dir):
    commit_all(mine, answer_path(), answer_text(MINE_FP), "answer")
    commit_all(mine, answer_path(qid="decisions"), answer_text(MINE_FP), "answer 2")

    result = publish(mine, data_dir)

    assert result.status == sp.PUBLISHED and result.published
    assert {answer_path(), answer_path(qid="decisions")} <= remote_files(remote)


def test_a_committed_unpublished_answer_is_pending_and_not_nothing_pending(mine, remote, data_dir):
    """Revert: pending() looks at session files only -> NOTHING_PENDING, the answer never leaves (red)."""
    commit_all(mine, answer_path(), answer_text(MINE_FP), "answer")

    assert sp.pending(mine, ME) == (answer_path(),)
    result = publish(mine, data_dir)

    assert result.status == sp.PUBLISHED
    assert answer_path() in remote_files(remote)
    assert sp.pending(mine, ME) == ()


def test_session_and_answer_files_publish_together(mine, remote, data_dir):
    commit_all(mine, answer_path(), answer_text(MINE_FP), "answer")
    write(mine, session_path("proj", HOST), session_text(HOST))

    result = publish(mine, data_dir)

    assert result.status == sp.PUBLISHED
    assert {answer_path(), session_path("proj", HOST)} <= remote_files(remote)


# ----------------------------------------------------------------------------- refusals


def test_refuses_a_commit_holding_another_hosts_answer_file(mine, remote, data_dir):
    """Revert: treat any answers/<qid>/*.yaml as own -> pushed (red)."""
    commit_all(mine, answer_path(), answer_text(MINE_FP), "own")
    commit_all(mine, answer_path("beta"), answer_text(OTHER_FP, "beta"), "foreign")

    result = publish(mine, data_dir)

    assert result.status == sp.FOREIGN_LOCAL and not result.published
    assert answer_path("beta") in result.message and "answer files" in result.message
    assert remote_files(remote) == {"README.md"}


def test_refuses_a_file_named_for_this_host_with_another_fingerprint(mine, remote, data_dir):
    """Revert: judge the path by name only (skip the fingerprint) -> pushed (red)."""
    commit_all(mine, answer_path(), answer_text(OTHER_FP), "collision")
    commit_all(mine, answer_path(qid="decisions"), answer_text(MINE_FP), "own, so there is something to publish")

    result = publish(mine, data_dir)

    assert result.status == sp.FOREIGN_LOCAL and not result.published
    assert answer_path() in result.message
    assert remote_files(remote) == {"README.md"}


def test_refuses_to_take_over_a_file_another_machine_wrote(mine, tmp_path, remote, data_dir):
    """A modification is judged by its preimage too: the file was another machine's before this commit."""
    rival_push(tmp_path, remote, answer_path(), answer_text(OTHER_FP))
    git(mine, "pull", "-q", "--no-rebase", "origin", BRANCH)
    commit_all(mine, answer_path(), answer_text(MINE_FP), "takeover")
    commit_all(mine, answer_path(qid="decisions"), answer_text(MINE_FP), "own, so there is something to publish")

    result = publish(mine, data_dir)

    assert result.status == sp.FOREIGN_LOCAL and not result.published


def test_refuses_a_deletion_whose_preimage_carries_another_fingerprint(mine, tmp_path, remote, data_dir):
    """Revert: a deletion is judged by name only (skip the preimage check) -> pushed, remote loses the file (red)."""
    rival_push(tmp_path, remote, answer_path(), answer_text(OTHER_FP))
    git(mine, "pull", "-q", "--no-rebase", "origin", BRANCH)
    git(mine, "rm", "-q", "--", answer_path())
    git(mine, "commit", "-q", "-m", "prune", "--", answer_path())
    commit_all(mine, answer_path(qid="decisions"), answer_text(MINE_FP), "own, so there is something to publish")

    result = publish(mine, data_dir)

    assert result.status == sp.FOREIGN_LOCAL and not result.published
    assert answer_path() in remote_files(remote)


def test_publishes_a_deletion_whose_preimage_carries_this_fingerprint(mine, remote, data_dir):
    commit_all(mine, answer_path(), answer_text(MINE_FP), "answer")
    assert publish(mine, data_dir).status == sp.PUBLISHED
    git(mine, "rm", "-q", "--", answer_path())
    git(mine, "commit", "-q", "-m", "prune", "--", answer_path())

    result = publish(mine, data_dir)

    assert result.status == sp.PUBLISHED
    assert answer_path() not in remote_files(remote)


def test_a_bare_host_id_owns_no_answer_files(mine, remote, data_dir):
    """A host-id string carries no fingerprint, so it can publish session files only."""
    commit_all(mine, answer_path(), answer_text(MINE_FP), "answer")
    write(mine, session_path("proj", HOST), session_text(HOST))

    result = publish(mine, data_dir, machine=HOST)

    assert result.status == sp.FOREIGN_LOCAL
    assert remote_files(remote) == {"README.md"}


def test_net_diff_check_judges_a_deletion_by_the_upstream_preimage(mine, tmp_path, remote):
    """The net-diff guard reads the preimage at the upstream tracking ref (revert: skip it -> empty list, red)."""
    rival_push(tmp_path, remote, answer_path(), answer_text(OTHER_FP))
    git(mine, "pull", "-q", "--no-rebase", "origin", BRANCH)
    git(mine, "rm", "-q", "--", answer_path())
    git(mine, "commit", "-q", "-m", "prune", "--", answer_path())

    assert sp._foreign_in_diff(mine, ME, "origin/main", "HEAD") == [answer_path()]


# ----------------------------------------------------------------------------- the shared repository lock


def test_repo_lock_blocks_a_second_holder_and_releases(tmp_path, data_dir):
    """Revert: repo_lock returns a no-op context -> the second holder enters at once (red)."""
    top = tmp_path / "repo"
    top.mkdir()
    with sp.repo_lock(top, data_dir, 5.0):
        started = time.monotonic()
        with pytest.raises(oslock.LockTimeout):
            with sp.repo_lock(top, data_dir, 0.3):
                pytest.fail("a second holder entered while the lock was held")
        assert 0.25 <= time.monotonic() - started < 3.0
    with sp.repo_lock(top, data_dir, 0.3):  # released on exit
        pass


def test_repo_lock_waiter_proceeds_when_the_holder_releases_within_wait(tmp_path, data_dir):
    top = tmp_path / "repo"
    top.mkdir()
    entered = threading.Event()
    waited: list[float] = []

    def waiter() -> None:
        started = time.monotonic()
        with sp.repo_lock(top, data_dir, 10.0):
            waited.append(time.monotonic() - started)
        entered.set()

    with sp.repo_lock(top, data_dir, 1.0):
        thread = threading.Thread(target=waiter)
        thread.start()
        time.sleep(0.4)
        assert not entered.is_set()
    thread.join(10)
    assert entered.is_set() and waited and waited[0] >= 0.3


def test_repo_lock_is_the_lock_publish_takes(mine, data_dir):
    """publish() reports BUSY while a caller holds repo_lock, and publishes once it is released."""
    write(mine, session_path("proj", HOST), session_text(HOST))
    with sp.repo_lock(mine, data_dir, 1.0):
        busy = publish(mine, data_dir, lock_wait_s=0.2)
    assert busy.status == sp.BUSY and not busy.committed
    assert publish(mine, data_dir).status == sp.PUBLISHED


def test_publish_holding_lock_publishes_inside_the_callers_lock(mine, remote, data_dir, monkeypatch):
    """Revert: publish_holding_lock calls repo_lock itself -> the guard below raises (red)."""
    commit_all(mine, answer_path(), answer_text(MINE_FP), "answer")

    with sp.repo_lock(mine, data_dir, 1.0):
        def no_second_lock(*_args, **_kw):
            raise AssertionError("publish_holding_lock took the repository lock itself")

        monkeypatch.setattr(sp, "repo_lock", no_second_lock)
        monkeypatch.setattr(sp, "FileLock", no_second_lock)
        result = sp.publish_holding_lock(mine, ME)

    assert result.status == sp.PUBLISHED
    assert answer_path() in remote_files(remote)


def test_publish_holding_lock_outside_a_work_tree_is_refused(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert sp.publish_holding_lock(plain, ME).status == sp.REFUSED
