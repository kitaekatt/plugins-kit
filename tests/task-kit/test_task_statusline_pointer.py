"""The statusline pointer: ``work`` records which task folder the session works, ``sessions release`` removes it.

Revert for each guard: drop the pointer call in sessions_cli (register_for_work / release_command) -> red.
"""

from __future__ import annotations

import os

import pytest
from sessions_cli_support import Rig

SID_A = "aaaaaaaa-0000-4000-8000-000000000001"
SID_B = "bbbbbbbb-0000-4000-8000-000000000002"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig-empty"))


@pytest.fixture
def alpha(tmp_path):
    rig = Rig(tmp_path)
    rig.add_task("alpha")
    rig.add_task("beta")
    yield rig
    rig.close()


def pointer(rig: Rig, sid: str):
    return rig.data_dir / "statusline" / sid


def test_work_writes_the_pointer_with_the_task_folder(alpha):
    rig = alpha
    harness = rig.harness(SID_A)
    assert rig.cli(harness, ["work", "dev/tasks/alpha"]).returncode == 0
    text = pointer(rig, SID_A).read_text(encoding="utf-8")
    assert text == os.path.realpath(rig.tasks / "alpha") + "\n"


def test_work_again_is_idempotent_and_a_second_task_repoints(alpha):
    rig = alpha
    harness = rig.harness(SID_A)
    rig.cli(harness, ["work", "dev/tasks/alpha"])
    rig.cli(harness, ["work", "dev/tasks/alpha"])
    assert pointer(rig, SID_A).read_text(encoding="utf-8").endswith("/alpha\n")
    rig.cli(harness, ["work", "dev/tasks/beta"])
    assert pointer(rig, SID_A).read_text(encoding="utf-8").endswith("/beta\n")


def test_release_removes_the_callers_pointer_only(alpha):
    rig = alpha
    first, second = rig.harness(SID_A), rig.harness(SID_B)
    rig.cli(first, ["work", "dev/tasks/alpha"])
    rig.cli(second, ["work", "dev/tasks/alpha"])
    rig.cli(first, ["sessions", "release", "dev/tasks/alpha"])
    assert not pointer(rig, SID_A).exists()
    assert pointer(rig, SID_B).exists()


def test_release_all_local_removes_every_pointer_for_the_task(alpha):
    rig = alpha
    first, second = rig.harness(SID_A), rig.harness(SID_B)
    rig.cli(first, ["work", "dev/tasks/alpha"])
    rig.cli(second, ["work", "dev/tasks/alpha"])
    first.close()
    second.close()
    assert rig.plain(["sessions", "release", "dev/tasks/alpha", "--all-local"]).returncode == 0
    assert not pointer(rig, SID_A).exists()
    assert not pointer(rig, SID_B).exists()


def test_release_of_another_task_keeps_a_pointer_that_names_a_different_task(alpha):
    rig = alpha
    harness = rig.harness(SID_A)
    rig.cli(harness, ["work", "dev/tasks/alpha"])
    rig.cli(harness, ["work", "dev/tasks/beta"])
    rig.cli(harness, ["sessions", "release", "dev/tasks/alpha"])
    assert pointer(rig, SID_A).read_text(encoding="utf-8").endswith("/beta\n")


def test_no_session_id_writes_no_pointer(alpha):
    rig = alpha
    rig.plain(["work", "dev/tasks/alpha"])
    assert not (rig.data_dir / "statusline").exists()
