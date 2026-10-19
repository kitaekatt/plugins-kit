"""How a run that ended without an acceptance verdict is labelled, and the Resolve commands.

A model call that runs into the job's own timeout ends the llm-kit job as ``failed``
(error code ``execution``) with an attempt whose status is ``timeout``; the dashboard
shows that as a timed-out run, not a failed one.
"""

from __future__ import annotations

import shutil

import pytest
from dashboard_support import FakeEdge, Running, dashboard_world, runner_environment
from test_dashboard_blocked_flow import Rig

from task_system.dashboard import runtime, scheduler, snapshot


@pytest.fixture
def rig(tmp_path, monkeypatch):
    runner_environment(monkeypatch)
    monkeypatch.setattr(scheduler, "RUNNER_SLACK_S", 1.0)
    devroot, project, folder = dashboard_world(tmp_path)
    edge = FakeEdge()
    srv = Running(tmp_path / "data", (devroot,), edge=edge, config_path=tmp_path / "no-config.yaml")
    rig = Rig(srv, edge, devroot, project, folder, tmp_path)
    try:
        yield rig
    finally:
        srv.stop()


# -- timeout label --------------------------------------------------------------------


def test_a_job_that_ends_on_its_own_attempt_timeout_is_a_timed_out_run_and_retry_is_offered(rig):
    rig.edge.behavior = "attempt-timeout"
    run = rig.run_to_end()
    assert (run.state, run.code) == (runtime.TIMEOUT, "timeout"), (run.state, run.code, run.detail)
    assert "claude -p exceeded 30.0s timeout" in run.detail and "Retry" in run.detail
    assert rig.task_status() == "active" and "do-it" in rig.items()  # a timeout blocks nothing
    row = snapshot.run_json(run, "alpha")
    assert row["state"] == "timeout" and row["retryable"] is True
    rig.edge.behavior = "done"
    retried = rig.srv.settle(rig.srv.post(f"/api/v1/runs/{run.id}/retry", {"expected_revision": rig.revision()})[2]["run"]["id"])
    assert retried.state == runtime.ACCEPTED


def test_an_ordinary_failure_is_still_failed(rig):
    rig.edge.behavior = "exit-1"
    run = rig.run_to_end()
    assert (run.state, run.code) == (runtime.FAILED, "llm-kit-exit-1")


def test_job_timeout_reads_the_attempt_status_and_nothing_else():
    assert scheduler.job_timeout({"attempts": [{"status": "error", "error": {"message": "claude -p exceeded 30s timeout"}}]}) is None
    assert scheduler.job_timeout({"attempts": [{"status": "timeout", "error": {"message": "slow\nmore"}}]}) == "slow"
    assert scheduler.job_timeout({"attempts": [{"status": "timeout"}]}) == "the attempt timed out"
    assert scheduler.job_timeout({"jobs": [{"state": "failed"}]}) is None and scheduler.job_timeout(None) is None


# -- Resolve in Claude Code: two copy boxes -----------------------------------------------


def handoff(root: str, task_id: str = "dev/tasks/x") -> dict:
    from task_system.dashboard.inventory import TaskEntry

    entry = TaskEntry(project_id="p", project_name="repo", project_root=root, checkout_id="c", location="dev/tasks",
                      stub=task_id.split("/")[-1], id=task_id, folder=root + "/" + task_id, core=None)
    return snapshot.handoff_commands(entry)


def test_the_handoff_is_exactly_two_commands_start_claude_there_and_paste_into_a_session():
    boxes = handoff("/work/plugins-kit3", "dev/tasks/task-kit-extraction")["commands"]
    assert [b["label"] for b in boxes] == ["Start Claude Code on this task", "Or paste into a running session"]
    assert boxes[0]["command"] == 'cd "/work/plugins-kit3" && claude "/task-kit:task work dev/tasks/task-kit-extraction"'
    assert boxes[1]["command"] == "/task-kit:task work dev/tasks/task-kit-extraction"


@pytest.mark.parametrize("root,expected", [
    ("/Users/me/My Projects/kit", 'cd "/Users/me/My Projects/kit" && claude '),
    ("/tmp/it's here", 'cd "/tmp/it\'s here" && claude '),  # an apostrophe is inert inside double quotes
    ('/tmp/say "hi"', "cd '/tmp/say \"hi\"' && claude "),
    ("/tmp/$HOME/`x`", "cd '/tmp/$HOME/`x`' && claude "),
    ("/tmp/bang!", "cd '/tmp/bang!' && claude "),
])
def test_paths_with_spaces_and_shell_characters_are_quoted_so_the_shell_reads_one_word(root, expected):
    command = handoff(root)["commands"][0]["command"]
    assert command.startswith(expected) and command.endswith(' "/task-kit:task work dev/tasks/x"')


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")
def test_the_quoted_command_round_trips_through_a_real_shell(tmp_path):
    import shlex
    import subprocess

    root = tmp_path / "it's a \"dir\" with $x"
    root.mkdir()
    command = handoff(str(root))["commands"][0]["command"]
    words = shlex.split(command.split(" && ")[0])
    assert words == ["cd", str(root)]
    out = subprocess.run(["bash", "-c", command.replace("claude", "printf '%s|%s' \"$PWD\"")], capture_output=True, text=True)
    assert out.stdout == f"{root}|/task-kit:task work dev/tasks/x"
