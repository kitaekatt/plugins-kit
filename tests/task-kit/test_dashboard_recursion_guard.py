"""A dashboard run may not start, stop or restart its dashboard, start another run, or run another worker.

Every guard here is a TRIPWIRE against a cooperating or careless worker, the same class as the deny floor under
``bypassPermissions``; none of it is confinement. The tests state what each guard stops and what it does not:

- the marker ``TASK_KIT_AGENT_DEPTH`` stops a ``task-kit`` run with the environment it inherited;
- the ancestry check (a live runner of the SAME dashboard, verified by its own ``runner.json``) also stops
  ``env -u``, absolute paths, ``bash -c`` and the ``/task dashboard`` form ``bash <root>/bin/task-kit``;
- a double fork that reparents before the check is NOT stopped (pinned below as the stated residual).
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import psutil
import pytest
from dashboard_support import (
    ENTRYPOINT,
    PLUGIN_ROOT,
    REPO_ROOT,
    FakeEdge,
    Running,
    clean_env,
    dashboard_world,
    runner_environment,
    tree_digest,
)
from rig_support import wait_for

from task_system.dashboard import floor, mutation, proctree, runtime, scheduler, translate
from task_system.dashboard.inventory import project_id_for
from task_system.dashboard.scheduler import QueueRefused

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the stand-in runner and the shell forms are POSIX")

STANDIN = textwrap.dedent('''
    import json, subprocess, sys, time
    from pathlib import Path
    run_dir = Path(sys.argv[1])
    while not (run_dir / "runner.json").exists():
        time.sleep(0.02)
    plan = json.loads((run_dir / "cmd.json").read_text(encoding="utf-8"))
    done = subprocess.run(plan["argv"], capture_output=True, text=True, env=plan["env"], timeout=60)
    (run_dir / "out.json.tmp").write_text(json.dumps({"code": done.returncode, "stdout": done.stdout, "stderr": done.stderr}))
    (run_dir / "out.json.tmp").rename(run_dir / "out.json")
''')


class Launch:
    """A stand-in for a dashboard runner (its command line names ``runner_cli.py <run dir>``) that runs one command."""

    def __init__(self, tmp_path: Path, name: str = "p") -> None:
        self.tmp_path = tmp_path
        self.data = tmp_path / "data"
        self.root = self.data / "dashboard"
        self.run_dir = self.root / "projects" / name / "runs" / "r1"
        self.run_dir.mkdir(parents=True)
        self.script_dir = tmp_path / "standin"
        self.script_dir.mkdir()
        (self.script_dir / "runner_cli.py").write_text(STANDIN, encoding="utf-8")
        self.home = tmp_path / "home"
        venv_bin = self.home / ".claude" / "plugins" / "data" / "plugins-kit" / "task-kit" / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        wrapper = venv_bin / "python"
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)

    def env(self, **extra) -> dict:
        return clean_env({"_BOOTSTRAP_GUARD_SKIP_REEXEC": "1", "PYTHONPATH": str(REPO_ROOT / "plugins" / "skills-kit"),
                          "HOME": str(self.home), "TASK_KIT_NO_OPEN": "1", **extra},
                         base={k: v for k, v in os.environ.items() if k not in ("CLAUDE_BOOTSTRAP_DATA_ROOT", "CLAUDE_PLUGIN_TEST")})

    def status_argv(self, data: Path | None = None) -> list[str]:
        return [sys.executable, str(ENTRYPOINT), "status", "--data-dir", str(data or self.data)]

    def run(self, argv: list[str], env: dict | None = None, timeout: float = 60.0) -> dict:
        """Run ``argv`` as a DESCENDANT of the stand-in runner; its exit code and output."""
        (self.run_dir / "cmd.json").write_text(json.dumps({"argv": argv, "env": env or self.env()}), encoding="utf-8")
        proc = subprocess.Popen([sys.executable, str(self.script_dir / "runner_cli.py"), str(self.run_dir)])
        try:
            created = psutil.Process(proc.pid).create_time()
            (self.run_dir / "runner.json").write_text(json.dumps({"pid": proc.pid, "create_time": created}), encoding="utf-8")
            assert wait_for(lambda: (self.run_dir / "out.json").exists(), timeout), "the stand-in never reported"
            return json.loads((self.run_dir / "out.json").read_text(encoding="utf-8"))
        finally:
            proc.kill()
            proc.wait(timeout=10)


@pytest.fixture
def launch(tmp_path):
    return Launch(tmp_path)


def test_the_inherited_marker_alone_refuses_every_command(launch):
    marked = launch.env(TASK_KIT_AGENT_DEPTH="0")
    for command in ("status", "stop", "restart", "serve"):
        argv = [sys.executable, str(ENTRYPOINT), command, "--data-dir", str(launch.data)]
        # not under a runner at all: the marker alone refuses
        done = subprocess.run(argv, capture_output=True, text=True, env=marked, timeout=60)
        assert done.returncode == 6, (command, done.stderr)
        assert "refused inside a dashboard run" in done.stderr
    assert not launch.root.joinpath("server.json").exists()


@pytest.mark.parametrize("form", ["marker", "env-u", "absolute-entrypoint", "bash-c", "bash-bin-task-kit"])
def test_every_nested_launcher_form_is_refused_without_touching_the_server_record(launch, form):
    """R3. RED: remove the ancestry check and the ``env -u`` form (no marker left to see) is not refused."""
    launch.root.mkdir(parents=True, exist_ok=True)
    (launch.root / "server.json").write_text("this is not a server record", encoding="utf-8")  # any read would exit 1
    py, entry, data = sys.executable, str(ENTRYPOINT), str(launch.data)
    marked = launch.env(TASK_KIT_AGENT_DEPTH="0")
    cases = {
        "marker": ([py, entry, "status", "--data-dir", data], marked),
        "env-u": (["env", "-u", "TASK_KIT_AGENT_DEPTH", py, entry, "status", "--data-dir", data], marked),
        "absolute-entrypoint": ([py, str(ENTRYPOINT.resolve()), "status", "--data-dir", data], launch.env()),
        "bash-c": (["bash", "-c", f'"{py}" "{entry}" status --data-dir "{data}"'], launch.env()),
        "bash-bin-task-kit": (["bash", str(PLUGIN_ROOT / "bin" / "task-kit"), "status", "--data-dir", data], launch.env()),
    }
    argv, env = cases[form]
    result = launch.run(argv, env)
    assert result["code"] == 6, (form, result)
    assert "refused inside a dashboard run" in result["stderr"]
    assert (launch.root / "server.json").read_text(encoding="utf-8") == "this is not a server record"  # neither read nor written


def test_a_double_fork_that_reparents_before_the_check_is_not_refused_the_stated_residual(launch):
    """The ancestry check walks the live parent chain: a process whose parent exited is no longer a descendant.

    This pins the limit the architecture states instead of letting it read as confinement.
    """
    code_file = launch.tmp_path / "late-exit-code"
    script = f'( sleep 1.5; "{sys.executable}" "{ENTRYPOINT}" status --data-dir "{launch.data}" >/dev/null 2>&1; echo $? > "{code_file}" ) >/dev/null 2>&1 &'
    result = launch.run(["bash", "-c", script])
    assert result["code"] == 0  # the shell returned at once
    assert wait_for(code_file.exists, 30), "the orphaned launcher never finished"
    time.sleep(0.2)
    assert code_file.read_text(encoding="utf-8").strip() == "3"  # `status`: not running. Not 6: it was not refused.


def test_an_isolated_dashboard_is_not_refused_because_of_another_dashboards_runner(launch, tmp_path):
    """R4. RED: compare ancestry against nothing (any runner refuses) and a test dashboard started inside a run fails."""
    other = tmp_path / "other-data"
    other.mkdir()
    result = launch.run(launch.status_argv(other))
    assert result["code"] == 3, result  # `status` of that other dashboard: not running
    assert "refused" not in result["stderr"]


def test_a_look_alike_command_line_without_a_matching_runner_record_is_not_a_runner(launch):
    """The ancestor must be the process ``runner.json`` names, not any process that mentions ``runner_cli.py``."""
    proc = subprocess.Popen([sys.executable, str(launch.script_dir / "runner_cli.py"), str(launch.run_dir)])
    try:
        (launch.run_dir / "runner.json").write_text(json.dumps({"pid": proc.pid, "create_time": 1.0}), encoding="utf-8")  # wrong ctime
        (launch.run_dir / "cmd.json").write_text(json.dumps({"argv": launch.status_argv(), "env": launch.env()}), encoding="utf-8")
        assert wait_for(lambda: (launch.run_dir / "out.json").exists(), 60)
        assert json.loads((launch.run_dir / "out.json").read_text(encoding="utf-8"))["code"] == 3
    finally:
        proc.kill()
        proc.wait(timeout=10)


# -- the floor and the dispatch gate ----------------------------------------


def test_the_floor_check_refuses_a_job_missing_a_listed_tool_denial():
    """R5. RED: ignore ``deny_tools`` in ``check_job_document``."""
    full = floor.disallowed_tools_value(floor.deny_tools())
    document = {"jobs": [{"id": "j", "options": {"disallowed_tools": full}}]}
    floor.check_job_document(document, deny_tools=floor.deny_tools())  # a complete job passes
    for tool in ("Agent", "Workflow", "EnterWorktree", "RemoteTrigger", "CronCreate", "ScheduleWakeup"):
        parts = [p for p in full.split(",") if p != tool]
        thin = {"jobs": [{"id": "j", "options": {"disallowed_tools": ",".join(parts)}}]}
        with pytest.raises(floor.FloorPlacementError, match=tool):
            floor.check_job_document(thin, deny_tools=floor.deny_tools())
    bare = {"jobs": [{"id": "j", "options": {"disallowed_tools": floor.CLAUDE_DISALLOWED_TOOLS}}]}
    floor.check_job_document(bare)  # the Bash floor alone still satisfies a check that lists no tool


def test_the_floor_names_the_documented_tool_denials_and_the_completion_endpoint():
    assert set(floor.TOOLS_DENIED_ALWAYS) == {"Workflow", "EnterWorktree", "RemoteTrigger", "CronCreate", "ScheduleWakeup"}
    assert floor.TOOLS_DENIED_WHEN_OFF == ("Agent",)
    assert "Bash(llm-kit completion:*)" in floor.PATTERNS
    assert floor.deny_tools(sub_agents_on=True) == floor.TOOLS_DENIED_ALWAYS
    assert floor.RUN_ENV == {"TASK_KIT_AGENT_DEPTH": "0", "CLAUDE_CODE_DISABLE_WORKFLOWS": "1"}


class World:
    def __init__(self, tmp_path, monkeypatch, behavior="done", **edge_kw):
        runner_environment(monkeypatch)
        monkeypatch.setattr(scheduler, "RUNNER_SLACK_S", 1.0)
        self.devroot, self.project, self.folder = dashboard_world(tmp_path)
        self.edge = FakeEdge(behavior=behavior, **edge_kw)
        self.srv = Running(tmp_path / "data", (self.devroot,), edge=self.edge, config_path=tmp_path / "none.yaml")
        self.pid = project_id_for(self.project)

    def queue(self, item="do-it", options=None):
        return self.srv.post(f"/api/v1/items/{self.pid}/dev%2Ftasks%2Falpha/{item}/queue",
                             {"expected_revision": mutation.folder_revision(self.folder), "options": options or {"timeout_s": 60}})


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    try:
        yield world
    finally:
        world.srv.stop()


@pytest.mark.parametrize("options,code", [({"worker": "gemini"}, "bad-worker"), ({"entry": "or-qwen"}, "model-entry")])
def test_a_non_worker_endpoint_is_refused_at_queue_and_nothing_is_written(w, options, code):
    """R1, a regression pin: the worker allowlist already refuses every name but claude and codex.

    RED: drop the worker allowlist check in ``translate._worker_reasons`` and the first case is queued.
    """
    before = tree_digest(w.folder)
    status, _h, body = w.queue(options={"timeout_s": 60, **options})
    assert status == 422 and body["error"]["code"] == "bad-options", body
    assert code in json.dumps(body)
    assert w.srv.scheduler.db.runs() == []
    assert tree_digest(w.folder) == before


def test_a_job_that_names_a_transport_entry_is_refused_before_any_file_exists(w, monkeypatch):
    """R2. RED: remove ``check_worker_models`` from ``_dispatch`` and the job file is written and the run starts."""
    real = scheduler.translate

    def lying_translation(**kw):
        result = real(**kw)
        import dataclasses

        job = dict(result.spec.document["jobs"][0], models=["or-qwen"])
        spec = dataclasses.replace(result.spec, document={**result.spec.document, "jobs": [job]})
        return translate.Translation(reasons=(), spec=spec)

    monkeypatch.setattr(scheduler, "translate", lying_translation)
    status, _h, body = w.queue()
    assert status == 202, body
    run = w.srv.settle(body["run"]["id"])
    assert run.state == runtime.FAILED and run.code == "dispatch-failed" and "or-qwen" in run.detail
    assert not Path(w.srv.scheduler.run_dir_for(w.pid, run.id)).exists()  # no run directory, so no job.yaml
    assert w.edge.job_files == []


def test_a_run_gets_the_workflow_switch_and_the_marker_and_the_tool_denials(tmp_path, monkeypatch):
    """R7. RED: leave the tool denials out of ``translate.worker_job_options`` and the job options lose Agent and Workflow."""
    world = World(tmp_path, monkeypatch, behavior="env-dump")
    try:
        status, _h, body = world.queue()
        assert status == 202, body
        run = world.srv.settle(body["run"]["id"])
        assert run.state == runtime.ACCEPTED, run
        dump = json.loads((Path(run.run_dir) / "env-dump.json").read_text(encoding="utf-8"))
        assert dump["env"]["TASK_KIT_AGENT_DEPTH"] == "0" and dump["env"]["CLAUDE_CODE_DISABLE_WORKFLOWS"] == "1"
        denied = dump["options"]["disallowed_tools"].split(",")
        assert "Agent" in denied and "Workflow" in denied and "Bash(llm-kit completion:*)" in denied
        spec = json.loads((Path(run.run_dir) / "spec.json").read_text(encoding="utf-8"))
        assert spec["runner"]["env"] == floor.RUN_ENV
        assert spec["subagents"]["state"] == "off" and "Agent" in spec["subagents"]["deny_tools"]
    finally:
        world.srv.stop()
