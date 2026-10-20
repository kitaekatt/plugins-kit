"""The per-binary observation of the Claude CLI's denials, run off the scheduler's locks.

A fake ``claude`` on PATH stands in for the CLI: it records the argv it received and prints the stream-json events
the real CLI authors (a ``system``/``init`` tool list; a Bash ``tool_result`` error and a ``permission_denials``
entry), honoring or ignoring the denials as a per-test setting. The live half (a real ``claude``, one haiku-sized
session) runs only with ``TASK_KIT_LIVE_CLAUDE=1``.
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import stat
import sys
import threading
import time
from pathlib import Path

import pytest
from dashboard_support import (
    REPO_ROOT,
    FakeEdge,
    Running,
    clean_env,
    dashboard_world,
    runner_environment,
    tree_digest,
)
from rig_support import processes_running, wait_for

from task_system.dashboard import cli_check, floor, mutation, runtime, scheduler, translate
from task_system.dashboard.config import load_config
from task_system.dashboard.inventory import project_id_for
from task_system.dashboard.scheduler import QueueRefused

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the fake claude is a POSIX script")

LIVE = os.environ.get("TASK_KIT_LIVE_CLAUDE") == "1"

FAKE_CLAUDE = '''#!{python}
import json, sys, time
from pathlib import Path
here = Path(__file__).resolve().parent
args = sys.argv[1:]
if args == ["--version"]:
    print("9.9.9 (Fake Claude Code)")
    raise SystemExit(0)
with open(here / "argv.log", "a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\\n")
behavior = json.loads((here / "behavior.json").read_text(encoding="utf-8"))
prompt = sys.stdin.read()
time.sleep(behavior.get("init_delay", 0))
if behavior.get("no_init"):
    if behavior.get("stderr"):
        print(behavior["stderr"], file=sys.stderr)
    raise SystemExit(1)
value = args[args.index("--disallowedTools") + 1] if "--disallowedTools" in args else ""
denied = set(value.split(","))
tools = ["Bash", "Edit", "Read"] + (["Task"] if behavior.get("has_task_tool", True) else []) + ["Workflow", "EnterWorktree", "RemoteTrigger", "CronCreate", "ScheduleWakeup"]
if behavior.get("honor", True):
    tools = [t for t in tools if t not in denied and not (t == "Task" and "Agent" in denied)]
print(json.dumps({{"type": "system", "subtype": "init", "tools": tools}}), flush=True)
if "git commit" in prompt:
    mode = behavior.get("bash", "denied")
    if mode == "none":
        print(json.dumps({{"type": "result", "subtype": "success", "permission_denials": []}}), flush=True)
        raise SystemExit(0)
    command = "git commit --dry-run -m probe"
    print(json.dumps({{"type": "assistant", "message": {{"content": [{{"type": "tool_use", "id": "t1", "name": "Bash", "input": {{"command": command}}}}]}}}}), flush=True)
    if mode == "denied":
        result = {{"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": "Permission to use Bash with command " + command + " has been denied."}}
        denials = [{{"tool_name": "Bash", "tool_use_id": "t1", "tool_input": {{"command": command}}}}]
    elif mode == "executed":  # what the real CLI sends: git ran in a directory that is no repository and exited 128
        result = {{"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": "Exit code 128\\nfatal: not a git repository (or any of the parent directories): .git"}}
        denials = []
    else:  # executed-clean: git ran and exited 0
        result = {{"type": "tool_result", "tool_use_id": "t1", "is_error": False, "content": "nothing to commit"}}
        denials = []
    print(json.dumps({{"type": "user", "message": {{"content": [result]}}}}), flush=True)
    print(json.dumps({{"type": "result", "subtype": "success", "permission_denials": denials}}), flush=True)
    raise SystemExit(0)
time.sleep(30)
'''


class FakeCli:
    def __init__(self, tmp_path: Path, monkeypatch, **behavior) -> None:
        self.dir = tmp_path / "fake-bin"
        self.dir.mkdir()
        self.path = self.dir / "claude"
        self.path.write_text(FAKE_CLAUDE.format(python=sys.executable), encoding="utf-8")
        self.path.chmod(self.path.stat().st_mode | stat.S_IEXEC)
        self.set(**behavior)
        monkeypatch.setenv("PATH", f"{self.dir}{os.pathsep}{os.environ['PATH']}")

    def set(self, **behavior) -> None:
        (self.dir / "behavior.json").write_text(json.dumps(behavior), encoding="utf-8")

    def argvs(self) -> list[list[str]]:
        log = self.dir / "argv.log"
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.is_file() else []


def shaped_argv(entry, *, effort, disallowed_tools, cwd) -> list[str]:
    """A worker argv shaped like llm-kit's, for the edge-free tests."""
    argv = [shutil.which("claude"), "-p", "--model", entry, "--append-system-prompt", "system", "--output-format", "json",
            "--no-session-persistence", "--permission-mode", "bypassPermissions", "--allowedTools", "",
            "--disallowedTools", disallowed_tools]
    if effort:
        argv += ["--effort", effort]
    return argv


def observer_for(tmp_path, edge, config_path, **kw) -> cli_check.CliObserver:
    return cli_check.CliObserver(root=tmp_path / "data" / "dashboard", edge=edge, load_config=lambda: load_config(config_path),
                                 init_timeout_s=kw.pop("init_timeout_s", 5.0), result_timeout_s=kw.pop("result_timeout_s", 5.0), **kw)


def observe(observer, key=None, timeout=30.0):
    key = key or cli_check.claude_key()
    assert key is not None
    observer.request(key)
    assert wait_for(lambda: observer.state_for(key), timeout), "the observation never finished"
    return observer.state_for(key)


def _edge_from_working_copy(monkeypatch):
    monkeypatch.delenv("CLAUDE_BOOTSTRAP_DATA_ROOT", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_TEST", raising=False)
    monkeypatch.syspath_prepend(str(REPO_ROOT / "plugins" / "llm-kit" / "lib"))
    importlib.invalidate_caches()
    from task_system.dashboard.jobs_edge import JobsEdge

    edge = JobsEdge(ttl_s=0.0)
    if not edge.status().available:
        pytest.skip(f"llm-kit is not importable here: {edge.status().detail}")
    return edge


@pytest.fixture
def real_edge(monkeypatch, tmp_path):
    """The real JobsEdge over llm-kit from the working copy and a scratch HOME (its shipped model baseline)."""
    home = tmp_path / "home"
    home.mkdir()
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(home))
    return _edge_from_working_copy(monkeypatch)


# -- R6: the exact argument ------------------------------------------------------


def test_the_observed_argument_is_the_one_a_run_emits_and_the_baseline_is_the_floor_alone(tmp_path, monkeypatch, real_edge):
    """R6. RED: probe with a bare ``--disallowedTools Agent`` (the first design) and the equality fails."""
    fake = FakeCli(tmp_path, monkeypatch)
    config = tmp_path / "none.yaml"
    state = observe(observer_for(tmp_path, real_edge, config))
    assert state.state == cli_check.OBSERVED, state
    runs = fake.argvs()
    assert len(runs) == 3  # baseline, actual, floor parse
    values = [argv[argv.index("--disallowedTools") + 1] for argv in runs]
    run_value = translate.worker_job_options("claude-cli", None, floor.deny_tools())["disallowed_tools"]
    assert run_value == floor.disallowed_tools_value(floor.deny_tools())
    assert values[1] == values[2] == run_value  # byte-identical to what a run emits, as ONE argument
    assert values[0] == ",".join(floor.PATTERNS)  # the same floor with every tool-level name removed
    assert run_value.startswith(values[0]) and run_value[len(values[0]):] == ",Workflow,EnterWorktree,RemoteTrigger,CronCreate,ScheduleWakeup,Agent"
    # the only deviations from llm-kit's worker argv
    for argv in runs:
        assert argv[argv.index("--output-format") + 1] == "stream-json" and "--verbose" in argv
        assert argv[argv.index("--model") + 1] == "haiku"
        assert argv[argv.index("--setting-sources") + 1] == "local" and "--strict-mcp-config" in argv
        assert "bypassPermissions" in argv and argv[argv.index("--allowedTools") + 1] == ""


# -- R6b: the states ------------------------------------------------------------


@pytest.mark.parametrize("behavior,expected,floor_parse", [
    ({}, cli_check.OBSERVED, "denied"),
    ({"honor": False}, cli_check.IGNORED, "not-run"),
    ({"bash": "executed"}, cli_check.IGNORED, "executed"),
    ({"bash": "executed-clean"}, cli_check.IGNORED, "executed"),
    ({"bash": "none"}, cli_check.UNOBSERVED, "none"),
    ({"has_task_tool": False}, cli_check.VACUOUS, "not-run"),
    ({"no_init": True}, cli_check.UNOBSERVED, "not-run"),
])
def test_each_outcome_of_the_observation_is_its_own_state(tmp_path, monkeypatch, behavior, expected, floor_parse):
    FakeCli(tmp_path, monkeypatch, **behavior)
    edge = FakeEdge()
    edge.worker_argv = shaped_argv
    state = observe(observer_for(tmp_path, edge, tmp_path / "none.yaml"))
    assert (state.state, state.floor_parse) == (expected, floor_parse), state
    assert state.summary()["state"] == expected and state.summary()["binary"] in (None, state.summary()["binary"])
    if expected != cli_check.UNOBSERVED or behavior.get("bash") == "none":
        assert state.binary is not None and state.binary.version.startswith("9.9.9")
        assert len(state.binary.digest) == 64


def test_a_cli_that_cannot_start_a_session_says_why_in_the_observation_detail(tmp_path, monkeypatch):
    """H9. The CLI's own stderr line reaches the detail; before, a failed observation read only "(exited)".

    Red: restore ``stderr=subprocess.DEVNULL`` in ``run_stream``; the detail lacks the reason.
    """
    FakeCli(tmp_path, monkeypatch, no_init=True, stderr="Not logged in")
    edge = FakeEdge()
    edge.worker_argv = shaped_argv
    state = observe(observer_for(tmp_path, edge, tmp_path / "none.yaml"))
    assert state.state == cli_check.UNOBSERVED
    assert "exited; stderr: Not logged in" in state.detail, state.detail


def test_the_stderr_tail_is_the_last_line_redacted_and_bounded(tmp_path, monkeypatch):
    from task_system.dashboard import redact

    FakeCli(tmp_path, monkeypatch, no_init=True, stderr="first\n" + "x" * 500)
    fake = str(tmp_path / "fake-bin" / "claude")
    run = cli_check.run_stream([fake], "hi", tmp_path, until="init", timeout_s=5, env=dict(os.environ),
                               stderr_path=tmp_path / "stderr.log")
    assert run.problem == "exited" and run.stderr_tail == "x" * cli_check.STDERR_TAIL_CHARS
    assert (tmp_path / "stderr.log").read_text(encoding="utf-8").startswith("first")  # the whole stream is kept as evidence
    (tmp_path / "fake-bin" / "behavior.json").write_text(
        json.dumps({"no_init": True, "stderr": "auth failed for hunter2hunter2"}), encoding="utf-8")
    redact.register_secret("hunter2hunter2")
    try:
        run = cli_check.run_stream([fake], "hi", tmp_path, until="init", timeout_s=5, env=dict(os.environ),
                                   stderr_path=tmp_path / "stderr2.log")
    finally:
        redact.clear_secrets()
    assert "hunter2hunter2" not in run.stderr_tail and run.stderr_tail.startswith("auth failed")


def test_a_characterize_check_that_did_not_pass_carries_its_legs_stderr_line(tmp_path):
    """H9, characterize. Red: leave ``_explained`` out of ``evaluate``; the detail lacks the CLI's reason."""
    from task_system.dashboard import runfiles, subagents

    for leg in subagents.LEGS:
        (tmp_path / leg).mkdir()
        (tmp_path / leg / "events.jsonl").write_text("", encoding="utf-8")
        runfiles.write_json(tmp_path / leg / "leg.json", {"leg": leg, "problem": "exited", "stderr_tail": "Not logged in"})
    checks = subagents.evaluate(tmp_path)
    assert all("exited; stderr: Not logged in" in check.detail for check in checks.values() if check.verdict != subagents.PASS)
    assert checks["agent_callable"].verdict == subagents.INCONCLUSIVE
    assert "leg A ended early: exited; stderr: Not logged in" in checks["agent_callable"].detail


def test_a_probe_command_that_ran_and_failed_is_executed_not_unclear():
    """A command the CLI let through exits non-zero in the scratch directory: an error that is not a permission denial."""
    def events(content, is_error=True):
        return [{"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                                                              "input": {"command": "git commit --dry-run -m probe"}}]}},
                {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "is_error": is_error, "content": content}]}}]

    for text in ("Exit code 128\nfatal: not a git repository (or any of the parent directories): .git", "Exit code 1\nnothing to commit"):
        assert cli_check.floor_parse_verdict(events(text), {"permission_denials": []})[0] == "executed", text
    assert cli_check.floor_parse_verdict(events("ok", False), {"permission_denials": []})[0] == "executed"
    denied = events("Permission to use Bash with command git commit has been denied.")
    assert cli_check.floor_parse_verdict(denied, {"permission_denials": [{"tool_use_id": "t1"}]})[0] == "denied"
    assert cli_check.floor_parse_verdict(denied, {"permission_denials": []})[0] == "unclear"  # looks denied, CLI did not list it
    assert cli_check.floor_parse_verdict(events("x")[:1], {})[0] == "unclear"  # no tool_result at all


def test_a_binary_is_observed_once_per_key_whatever_asks(tmp_path, monkeypatch):
    fake = FakeCli(tmp_path, monkeypatch)
    edge = FakeEdge()
    edge.worker_argv = shaped_argv
    observer = observer_for(tmp_path, edge, tmp_path / "none.yaml")
    key = cli_check.claude_key()
    for _ in range(3):
        observer.request(key)
    assert wait_for(lambda: observer.state_for(key), 30)
    for _ in range(3):
        observer.request(key)
    time.sleep(0.3)
    assert len(fake.argvs()) == 3  # one baseline, one actual, one floor parse: nothing more
    # a different binary (the file changed) is another key and is observed again
    fake.path.write_text(fake.path.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
    other = cli_check.claude_key()
    assert other != key
    observer.request(other)
    assert wait_for(lambda: observer.state_for(other), 30)
    assert len(fake.argvs()) == 6


# -- the scheduler's use of the states -----------------------------------------------


class Site:
    def __init__(self, tmp_path, monkeypatch, behavior="done", cli=None, observer_kw=None, items=None):
        runner_environment(monkeypatch)
        monkeypatch.setattr(scheduler, "RUNNER_SLACK_S", 1.0)
        self.tmp_path = tmp_path
        self.cli = FakeCli(tmp_path, monkeypatch, **(cli or {}))
        self.devroot, self.project, self.folder = dashboard_world(tmp_path, items=items)
        self.edge = FakeEdge(behavior=behavior)
        self.edge.worker_argv = shaped_argv
        self.config = tmp_path / "none.yaml"
        self.observer = observer_for(tmp_path, self.edge, self.config, **(observer_kw or {}))
        self.srv = Running(tmp_path / "data", (self.devroot,), edge=self.edge, config_path=self.config, cli_observer=self.observer)
        sched = self.srv.scheduler
        assert wait_for(lambda: sched._thread is not None and sched._thread.is_alive(), 10)
        sched._stop.set()
        sched._wake.set()
        sched._thread.join(timeout=10)
        self.pid = project_id_for(self.project)

    @property
    def sched(self):
        return self.srv.scheduler

    def queue(self, item="do-it", expected=None):
        return self.sched.queue(project_root=str(self.project), task_ref="dev/tasks/alpha", item_id=item, body={"timeout_s": 60},
                                expected_revision=expected)

    def tick_until(self, run_id, states, timeout_s=60.0):
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            self.sched.tick()
            run = self.sched.db.get(run_id)
            if run.state in tuple(states):
                return run
            time.sleep(0.1)
        raise AssertionError(f"run {run_id} never reached {states}: {self.sched.db.get(run_id)}")

    def close(self):
        try:
            self.srv.stop()
        except AssertionError:
            pass
        for proc in processes_running(str(self.tmp_path)):
            try:
                proc.kill()
            except Exception:  # noqa: BLE001 -- best-effort cleanup of a test's own processes
                pass


@pytest.fixture
def site(tmp_path, monkeypatch):
    made = []

    def build(**kw):
        s = Site(tmp_path, monkeypatch, **kw)
        made.append(s)
        return s

    yield build
    for s in made:
        s.close()


def test_a_binary_that_ignores_a_listed_denial_refuses_claude_runs(site):
    """R6b. RED: treat ``ignored`` as ``unobserved`` and the run is queued and started."""
    s = site(cli={"honor": False})
    observe(s.observer)
    before = tree_digest(s.folder)
    with pytest.raises(QueueRefused) as refused:
        s.queue()
    exc = refused.value
    assert (exc.status, exc.code) == (422, "bad-options") and "cli-deny-ignored" in json.dumps(exc.reasons)
    assert tree_digest(s.folder) == before and s.sched.db.runs() == []  # the folder untouched, nothing queued
    assert s.sched.capabilities()["subagents"]["claude"]["cli_check"]["state"] == "ignored"


def test_an_entry_queued_before_the_observation_ended_fails_at_start_and_leaves_the_task_alone(site):
    s = site(cli={"honor": False})
    run = s.queue()  # queued while the binary was still unknown
    final = s.tick_until(run.id, runtime.TERMINAL_STATES)
    assert final.state == runtime.FAILED and final.code == "bad-options" and "cli-deny-ignored" in final.detail
    assert mutation.stored_status((s.folder / "task.yaml").read_text(encoding="utf-8")) == "active"
    assert not Path(s.sched.run_dir_for(s.pid, run.id)).exists()  # no run directory, no job file, no ledger


@pytest.mark.parametrize("behavior", [{}, {"bash": "none"}, {"no_init": True}, {"has_task_tool": False}])
def test_observed_unobserved_and_vacuous_binaries_all_start_runs_with_the_denials(site, behavior):
    s = site(cli=behavior)
    run = s.queue()
    final = s.tick_until(run.id, runtime.TERMINAL_STATES)
    assert final.state == runtime.ACCEPTED, final
    spec = json.loads((Path(final.run_dir) / "spec.json").read_text(encoding="utf-8"))
    assert spec["subagents"]["state"] == "off"
    assert spec["cli_check"]["state"] in (cli_check.OBSERVED, cli_check.UNOBSERVED, cli_check.VACUOUS)
    job = (Path(final.run_dir) / "job.yaml").read_text(encoding="utf-8")
    assert "Agent" in job and "Workflow" in job


def test_an_observation_in_flight_blocks_neither_a_cancel_nor_a_tick_and_the_entry_waits_cli_check(site):
    """R6c. RED: run the observation inside ``_start_queued`` and the tick (and so the Cancel behind its lock) waits for it."""
    items = [{"id": "do-it", "title": "Do it", "state": "available", "priority": "P1"},
             {"id": "later-one", "title": "Later one", "state": "available", "priority": "P2"}]
    s = site(cli={"init_delay": 3.0, "honor": True}, observer_kw={"init_timeout_s": 1.0, "result_timeout_s": 1.0}, items=items)
    first, second = s.queue("do-it"), s.queue("later-one")
    # the first tick asks for the observation; it must return at once, with both entries still queued
    began = time.monotonic()
    ticker = threading.Thread(target=s.sched.tick)
    ticker.start()
    time.sleep(0.15)
    cancel_began = time.monotonic()
    s.sched.cancel(second.id)  # an unrelated queued entry
    cancel_took = time.monotonic() - cancel_began
    ticker.join(timeout=30)
    tick_took = time.monotonic() - began
    assert cancel_took < 0.5, f"Cancel waited {cancel_took:.2f}s for the observation"
    assert tick_took < 1.0, f"the tick took {tick_took:.2f}s"
    assert s.sched.db.get(first.id).state == runtime.QUEUED
    assert s.sched.queue_view()[first.id][1]["code"] == "cli-check"
    row = [r for r in s.srv.json("GET", "/api/v1/dashboard")[2]["runs"] if r["id"] == first.id][0]
    assert row["waiting"]["code"] == "cli-check"
    final = s.tick_until(first.id, runtime.TERMINAL_STATES)  # once the observation ends (unobserved), the run starts
    assert final.state == runtime.ACCEPTED
    assert s.sched.capabilities()["subagents"]["claude"]["cli_check"]["state"] == cli_check.UNOBSERVED


def test_a_codex_run_never_waits_for_the_claude_check(site):
    s = site(cli={"init_delay": 30.0})
    key = cli_check.claude_key()
    from task_system.dashboard.runtime import Run  # noqa: F401 -- the type the gate reads

    run = s.sched.db.enqueue(run_id="cx", project_id=s.pid, project_root=str(s.project), task_ref="dev/tasks/alpha", item_id="do-it",
                             item_title="Do it", options={"worker": "codex"}, source_revision="x")
    assert s.sched._cli_gate(run, load_config(s.config)) == ("go", None)
    assert s.observer.state_for(key) is None and not s.observer.in_flight(key)  # nothing was asked for


# -- live ------------------------------------------------------------------------------


@pytest.mark.skipif(not LIVE, reason="spends a real haiku-sized Claude session; set TASK_KIT_LIVE_CLAUDE=1 to run")
def test_the_installed_claude_code_honors_the_denials_a_run_emits(tmp_path, monkeypatch):
    """Live: the real ``claude`` (real HOME, so it is logged in), the real llm-kit worker argv, the real denials.

    ``observed`` or the test fails. The session is a haiku call that runs one denied command.
    """
    real_claude = shutil.which("claude")
    assert real_claude, "TASK_KIT_LIVE_CLAUDE=1 needs the claude CLI on PATH"
    monkeypatch.delenv("CLAUDECODE", raising=False)  # a nested claude must not believe it runs inside another session
    real_edge = _edge_from_working_copy(monkeypatch)
    observer = cli_check.CliObserver(root=tmp_path / "dashboard", edge=real_edge, load_config=lambda: load_config(tmp_path / "none.yaml"),
                                     init_timeout_s=60.0, result_timeout_s=180.0)
    key = cli_check.claude_key()
    observer.request(key)
    assert wait_for(lambda: observer.state_for(key), 400), "the live observation never finished"
    state = observer.state_for(key)
    observer.close()
    print(json.dumps(state.summary(), indent=2))
    assert state.state == cli_check.OBSERVED, state.summary()
