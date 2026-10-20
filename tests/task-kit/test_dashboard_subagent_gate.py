"""Native sub-agents: the characterization's judgement, its record, and the decision that turns them on.

A fake ``claude`` on PATH plays the CLI's stream-json telemetry as Claude Code 2.1.304 authors it (shapes taken from
a real session): ``system``/``init``, ``assistant`` and ``user`` events whose content blocks are ``tool_use`` and
``tool_result``, ``system``/``task_started`` events carrying ``spawn_depth``, child events marked by
``parent_tool_use_id``, and ``result`` events listing ``permission_denials``. It honors or breaks each limit as a
per-test setting and reads the two documented environment variables the characterization sets per leg. The live half
(real Claude Code) is ``test_dashboard_headless_subagents_live.py``.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from dashboard_support import (
    SCRIPTS_DIR,
    FakeEdge,
    Running,
    dashboard_world,
    runner_environment,
)

from task_system.dashboard import cli_check, config, floor, launcher, runtime, scheduler, snapshot, subagents
from task_system.dashboard.config import DashboardConfigError, SubagentsConfig, load_config
from task_system.dashboard.helper_cli import CHECK_MARKER
from task_system.dashboard.inventory import project_id_for

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the fake claude is a POSIX script")

FAKE_CLAUDE = '''#!{python}
import json, os, sys, time
from pathlib import Path
here = Path(__file__).resolve().parent
args = sys.argv[1:]
if args == ["--version"]:
    print("9.9.9 (Fake Claude Code)")
    raise SystemExit(0)
behavior = json.loads((here / "behavior.json").read_text(encoding="utf-8"))
if args == ["--help"]:
    print("Options:")
    if behavior.get("forward_flag", True):
        print("  --forward-subagent-text   Forward subagent text and thinking blocks")
    raise SystemExit(0)
env = {{k: os.environ.get(k) for k in ("CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH", "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS",
                                       "CLAUDE_CODE_DISABLE_WORKFLOWS")}}
with open(here / "argv.log", "a", encoding="utf-8") as log:
    log.write(json.dumps({{"args": args, "env": env, "cwd": os.getcwd()}}) + "\\n")
prompt = sys.stdin.read()
cwd = Path.cwd()
depth, conc = env["CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH"], env["CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS"]
denials = []
mid = [0]


def emit(event):
    print(json.dumps(event), flush=True)


def use(tool_id, name, tool_input, parent=None, message="m0"):
    emit({{"type": "assistant", "parent_tool_use_id": parent,
          "message": {{"id": message, "content": [{{"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}}]}}}})


def result(tool_id, content, error=False, parent=None):
    block = {{"type": "tool_result", "tool_use_id": tool_id, "content": content}}
    if error:
        block["is_error"] = True
    emit({{"type": "user", "parent_tool_use_id": parent, "message": {{"content": [block]}}}})


def started(tool_id, depth_value):
    emit({{"type": "system", "subtype": "task_started", "tool_use_id": tool_id, "spawn_depth": depth_value}})


def delete(command, tool_id, name, parent=None):
    use(tool_id, "Bash", {{"command": command}}, parent)
    if behavior.get("honor_floor", True):
        result(tool_id, "Permission to use Bash with command " + command + " has been denied.", error=True, parent=parent)
        denials.append({{"tool_name": "Bash", "tool_use_id": tool_id, "tool_input": {{"command": command}}}})
    else:
        (cwd / name).unlink()
        result(tool_id, "(Bash completed with no output)", parent=parent)


time.sleep(behavior.get("init_delay", 0))
emit({{"type": "system", "subtype": "init", "tools": ["Task", "Bash", "Write", "Edit", "Workflow"]}})
if "orch-control.txt" in prompt:
    use("u1", "Bash", {{"command": "touch orch-control.txt"}})
    if behavior.get("control_file", True):
        (cwd / "orch-control.txt").write_text("x")
    result("u1", "(Bash completed with no output)")
    delete("rm canary-orch.txt", "u2", "canary-orch.txt")
    helper = [line for line in prompt.splitlines() if "helper_cli.py" in line]
    if helper and behavior.get("helper", True):
        use("u3", "Bash", {{"command": helper[0].split(": ", 1)[1]}})
        result("u3", "{marker}\\n")
    if behavior.get("launch", True):
        use("a1", "Agent", {{"description": "child-a", "prompt": "child task"}}, message="m1")
        started("a1", 1)
        result("a1", "Async agent launched successfully.")
        emit({{"type": "result", "subtype": "success", "permission_denials": list(denials), "result": behavior.get("answer", "done")}})
        time.sleep(0.3)  # the sub-agent reports after the first result, as in Claude Code
        use("c1", "Write", {{"file_path": "child-a.txt", "content": "child"}}, "a1")
        (cwd / "child-a.txt").write_text("child")
        result("c1", "File created successfully", parent="a1")
        delete("rm canary-child.txt", "c2", "canary-child.txt", "a1")
        child_denials = denials[1:]
        if depth != "1" or behavior.get("ignore_depth"):
            use("g1", "Agent", {{"description": "grandchild", "prompt": "grandchild task"}}, "a1", message="m2")
            started("g1", 2)
            (cwd / "grandchild.txt").write_text("grandchild")
            result("g1", "launched", parent="a1")
        elif behavior.get("nesting_error"):
            use("g1", "Agent", {{"description": "grandchild", "prompt": "grandchild task"}}, "a1", message="m2")
            result("g1", "Subagent nesting limit reached. Do not retry.", error=True, parent="a1")
        emit({{"type": "result", "subtype": "success", "permission_denials": child_denials, "result": "child done"}})
    else:
        emit({{"type": "result", "subtype": "success", "permission_denials": list(denials), "result": behavior.get("answer", "done")}})
elif "par-1.txt" in prompt:
    use("p1", "Agent", {{"description": "par-1", "prompt": "write par-1"}}, message="m9")
    use("p2", "Agent", {{"description": "par-2", "prompt": "write par-2"}}, message="m9")
    started("p1", 1)
    (cwd / "par-1.txt").write_text("one")
    result("p1", "done")
    if conc == "1" and behavior.get("honor_concurrency", True):
        result("p2", "Concurrent subagent limit reached. You can run 1 subagents at once. Do not retry.", error=True)
    else:
        started("p2", 1)
        (cwd / "par-2.txt").write_text("two")
        result("p2", "done")
    emit({{"type": "result", "subtype": "success", "permission_denials": [], "result": behavior.get("answer", "done")}})
else:
    emit({{"type": "result", "subtype": "success", "permission_denials": [], "result": "ok"}})
'''


class FakeCli:
    def __init__(self, tmp_path: Path, monkeypatch, **behavior) -> None:
        self.dir = tmp_path / "fake-bin"
        self.dir.mkdir(exist_ok=True)
        self.path = self.dir / "claude"
        self.path.write_text(FAKE_CLAUDE.format(python=sys.executable, marker=CHECK_MARKER), encoding="utf-8")
        self.path.chmod(self.path.stat().st_mode | stat.S_IEXEC)
        self.set(**behavior)
        monkeypatch.setenv("PATH", f"{self.dir}{os.pathsep}{os.environ['PATH']}")

    def set(self, **behavior) -> None:
        (self.dir / "behavior.json").write_text(json.dumps(behavior), encoding="utf-8")

    def runs(self) -> list[dict]:
        log = self.dir / "argv.log"
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.is_file() else []

    def binary(self) -> cli_check.ClaudeBinary:
        return cli_check.identify_binary(cli_check.claude_key())


def shaped_argv(entry, *, effort, disallowed_tools, cwd) -> list[str]:
    """A worker argv shaped like llm-kit's (the fake edge has no llm-kit to ask)."""
    argv = [shutil.which("claude"), "-p", "--model", entry, "--append-system-prompt", "system", "--output-format", "json",
            "--no-session-persistence", "--permission-mode", "bypassPermissions", "--allowedTools", "",
            "--disallowedTools", disallowed_tools]
    if effort:
        argv += ["--effort", effort]
    return argv


HELPER_CLI = SCRIPTS_DIR / "task_system" / "dashboard" / "helper_cli.py"


def run_characterize(tmp_path, monkeypatch, *, cfg_text: str | None = None, **behavior):
    """A whole characterization against the fake CLI: (cli, record's claude section, decision, root, output)."""
    import io

    cli = FakeCli(tmp_path, monkeypatch, **behavior)
    edge = FakeEdge()
    edge.worker_argv = shaped_argv
    root = tmp_path / "data" / "dashboard"
    cfg_file = tmp_path / "cfg.yaml"
    if cfg_text:
        cfg_file.write_text(cfg_text, encoding="utf-8")
    out = io.StringIO()
    claude, decision = subagents.characterize(
        root=root, edge=edge, config=load_config(cfg_file), python=sys.executable, helper_cli=HELPER_CLI, scripts_dir=SCRIPTS_DIR,
        entry="opus", out=out, leg_timeout_s=60.0)
    return cli, claude, decision, root, out.getvalue()


def verdicts(claude: dict) -> dict[str, str]:
    return {name: check["verdict"] for name, check in claude["checks"].items()}


ALL_PASS = {"agent_callable": "pass", "floor_orchestrator": "pass", "floor_inherited": "pass", "depth_cap": "pass",
            "concurrent_cap": "pass", "helper_command_allowed": "pass"}


# -- the characterization, end to end against the fake CLI ------------------------------


def test_a_cli_that_honors_every_limit_passes_every_check_and_turns_sub_agents_on(tmp_path, monkeypatch):
    cli, claude, decision, root, out = run_characterize(tmp_path, monkeypatch)
    assert verdicts(claude) == ALL_PASS, claude["checks"]
    assert decision.on and decision.max_concurrent == 3 and decision.env == floor.subagent_env(3)
    # the record is bound to this binary's digest and version, and lands where the dashboard reads it
    read = subagents.read_record(root)
    binary = cli.binary()
    assert read.status == "ok" and read.claude["binary"] == {"realpath": binary.path, "digest": binary.digest, "version": "9.9.9 (Fake Claude Code)"}
    assert read.claude["probe_version"] == subagents.PROBE_VERSION and read.claude["entry"] == "opus"
    assert read.claude["task_kit_version"] and Path(read.claude["probe_dir"]).is_dir()
    assert "native sub-agents: ON" in out and "agent_callable" in out
    # each leg ran the exact worker argv with only the output format swapped, with the documented variables per leg
    legs = {run["cwd"].rsplit("/", 2)[-2]: run for run in cli.runs() if "-p" in run["args"]}
    assert set(legs) == {"A", "B", "control"}
    assert legs["A"]["env"]["CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH"] == "1" and legs["A"]["env"]["CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS"] is None
    assert legs["B"]["env"]["CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH"] == "1" and legs["B"]["env"]["CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS"] == "1"
    assert legs["control"]["env"]["CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH"] is None  # no limit: the control
    for run in legs.values():
        args = run["args"]
        assert run["env"]["CLAUDE_CODE_DISABLE_WORKFLOWS"] == "1"
        assert args[args.index("--output-format") + 1] == "stream-json" and "--verbose" in args and "--forward-subagent-text" in args
        denied = args[args.index("--disallowedTools") + 1].split(",")
        assert "Workflow" in denied and "Agent" not in denied  # the ON deny list: every denial but the sub-agent tool
        assert denied == list(floor.PATTERNS) + list(floor.TOOLS_DENIED_ALWAYS)
        assert args[args.index("--model") + 1] == "opus" and args[args.index("--effort") + 1] == "low"


def test_a_record_stops_matching_when_the_binary_it_was_made_on_changes(tmp_path, monkeypatch):
    """R20, stale digest on real bytes. RED: compare nothing about the binary and the changed file keeps sub-agents ON."""
    cli, _claude, decision, root, _out = run_characterize(tmp_path, monkeypatch)
    assert decision.on
    read = subagents.read_record(root)
    cfg = config.DashboardConfig()
    assert subagents.decide("claude", cfg, cli.binary(), read, None).on  # the same bytes still match
    cli.path.write_text(cli.path.read_text(encoding="utf-8") + "\n# an update\n", encoding="utf-8")  # the CLI was updated in place
    after = subagents.decide("claude", cfg, cli.binary(), read, None)
    assert not after.on and after.code == "stale-binary" and "task-kit characterize" in after.reason


def test_the_forward_flag_is_passed_only_when_the_cli_lists_it(tmp_path, monkeypatch):
    cli, _claude, _decision, _root, _out = run_characterize(tmp_path, monkeypatch, forward_flag=False)
    assert all("--forward-subagent-text" not in run["args"] for run in cli.runs())


@pytest.mark.parametrize("behavior,failing,verdict", [
    ({"honor_floor": False}, {"floor_orchestrator", "floor_inherited"}, "fail"),  # the denied rm took effect, the canaries are gone
    ({"ignore_depth": True}, {"depth_cap"}, "fail"),  # a grandchild launched under the limit
    ({"honor_concurrency": False}, {"concurrent_cap"}, "fail"),  # both par files written under a limit of 1
    ({"launch": False}, {"agent_callable", "floor_inherited", "depth_cap"}, "inconclusive"),  # the model never launched a sub-agent
    ({"control_file": False}, {"floor_orchestrator"}, "inconclusive"),  # no control file: a refusal proves nothing
    ({"helper": False}, set(), "pass"),  # informational only: the helper line is judged and never gates
])
def test_a_broken_limit_or_missing_evidence_turns_sub_agents_off(tmp_path, monkeypatch, behavior, failing, verdict):
    _cli, claude, decision, _root, _out = run_characterize(tmp_path, monkeypatch, **behavior)
    got = verdicts(claude)
    for name in failing:
        assert got[name] == verdict, (name, claude["checks"][name])
    if failing:
        assert not decision.on and decision.code == "check-failed" and "task-kit characterize" in decision.reason
    else:
        assert decision.on  # a helper step that was not run does not gate
        assert got["helper_command_allowed"] == "inconclusive"


def test_the_control_leg_is_what_makes_the_absence_of_a_grandchild_evidence(tmp_path, monkeypatch):
    """With the limit set and no CLI refusal to read, depth_cap rests on the control launching a grandchild without it."""
    probe = Path(run_characterize(tmp_path, monkeypatch)[1]["probe_dir"])
    assert subagents.evaluate(probe)["depth_cap"].verdict == subagents.PASS
    shutil.rmtree(probe / "control")  # without the control the same evidence is not conclusive
    assert subagents.evaluate(probe)["depth_cap"].verdict == subagents.INCONCLUSIVE
    (probe / "control").mkdir()
    (probe / "control" / "events.jsonl").write_text("", encoding="utf-8")  # a control that launched nothing proves nothing
    assert subagents.evaluate(probe)["depth_cap"].verdict == subagents.INCONCLUSIVE


def test_a_cli_refusal_of_the_nested_launch_is_evidence_on_its_own(tmp_path, monkeypatch):
    _cli, claude, decision, _root, _out = run_characterize(tmp_path, monkeypatch, nesting_error=True)
    assert claude["checks"]["depth_cap"]["verdict"] == "pass" and "nesting-limit error" in claude["checks"]["depth_cap"]["detail"]
    probe = Path(claude["probe_dir"])
    shutil.rmtree(probe / "control")
    assert subagents.evaluate(probe)["depth_cap"].verdict == subagents.PASS  # no control needed for a CLI-authored refusal


def test_the_answer_text_is_never_evidence(tmp_path, monkeypatch):
    """R19. RED: let ``_floor_check`` accept an answer that says it was refused and the stripped fixture passes."""
    probe = Path(run_characterize(tmp_path, monkeypatch, answer="The rm was denied; every limit held; all steps passed.")[1]["probe_dir"])
    assert {n: c.verdict for n, c in subagents.evaluate(probe).items()} == ALL_PASS
    # the same sessions with every tool_result and every refusal-bearing event removed, and the answer claiming the refusals
    for leg in subagents.LEGS:
        events_file = probe / leg / "events.jsonl"
        kept = []
        for line in events_file.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if event["type"] == "user":
                continue  # no tool_result at all
            if event["type"] == "result":
                event["permission_denials"] = []
                event["result"] = "Everything was refused exactly as expected; the rm was denied and the nesting limit held."
            kept.append(json.dumps(event))
        events_file.write_text("\n".join(kept) + "\n", encoding="utf-8")
    got = {n: c.verdict for n, c in subagents.evaluate(probe).items()}
    assert "pass" not in {got[n] for n in ("floor_orchestrator", "floor_inherited", "concurrent_cap", "agent_callable", "helper_command_allowed")}, got
    assert got["floor_orchestrator"] == got["floor_inherited"] == got["concurrent_cap"] == "inconclusive"


def test_a_denial_the_cli_did_not_list_is_not_a_pass(tmp_path, monkeypatch):
    probe = Path(run_characterize(tmp_path, monkeypatch)[1]["probe_dir"])
    for line_leg in ("A",):
        events_file = probe / line_leg / "events.jsonl"
        rewritten = []
        for line in events_file.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if event["type"] == "result":
                event["permission_denials"] = []
            rewritten.append(json.dumps(event))
        events_file.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
    got = subagents.evaluate(probe)
    assert got["floor_orchestrator"].verdict == got["floor_inherited"].verdict == subagents.INCONCLUSIVE
    assert "permission_denials" in got["floor_orchestrator"].detail


def test_a_marker_where_it_must_be_absent_is_a_fail_not_an_inconclusive(tmp_path, monkeypatch):
    probe = Path(run_characterize(tmp_path, monkeypatch)[1]["probe_dir"])
    (probe / "A" / "work" / "grandchild.txt").write_text("x")
    (probe / "A" / "work" / "canary-orch.txt").unlink()
    (probe / "B" / "work" / "par-2.txt").write_text("two")
    got = subagents.evaluate(probe)
    assert got["depth_cap"].verdict == got["floor_orchestrator"].verdict == got["concurrent_cap"].verdict == subagents.FAIL


# -- the record and the decision ------------------------------------------------------------


NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
BINARY = cli_check.ClaudeBinary(cli_check.ClaudeKey("/x/claude", 1, 2), "/x/claude", "d" * 64, "2.1.304 (Claude Code)")


def claude_section(**over) -> dict:
    claude = {
        "binary": {"realpath": "/x/claude", "digest": "d" * 64, "version": "2.1.304 (Claude Code)"},
        "probe_version": subagents.PROBE_VERSION,
        "checked_at": NOW.replace(day=19).isoformat(timespec="seconds"),
        "expires_at": (NOW.replace(day=19) + timedelta(days=7)).isoformat(timespec="seconds"),
        "entry": "opus", "task_kit_version": "0.11.0", "probe_dir": "/x/p",
        "checks": {name: {"verdict": "pass", "detail": "", "informational": name in subagents.INFORMATIONAL} for name in subagents.CHECKS},
    }
    claude.update(over)
    return claude


def read_of(tmp_path, claude: dict | None = None, raw: str | None = None) -> subagents.RecordRead:
    root = tmp_path / "rec"
    root.mkdir(exist_ok=True)
    (root / subagents.RECORD_FILE).unlink(missing_ok=True)
    if raw is not None:
        (root / subagents.RECORD_FILE).write_text(raw, encoding="utf-8")
    elif claude is not None:
        subagents.write_record(root, claude)
    return subagents.read_record(root)


def decide(tmp_path, *, worker="claude", cfg=None, binary=BINARY, claude=None, raw=None, obs=None, now=NOW, record=True):
    cfg = cfg or config.DashboardConfig()
    read = read_of(tmp_path, claude if (claude is not None or raw is not None) else (claude_section() if record else None), raw)
    return subagents.decide(worker, cfg, binary, read, obs, now=now)


def test_the_decision_table(tmp_path):
    """R20. Every row of the decision table; RED for the expiry row: drop the expiry check and the expired record turns ON."""
    on = decide(tmp_path)
    assert on.on and on.code == "on" and on.max_concurrent == 3 and on.env == floor.subagent_env(3)
    assert on.deny_tools == floor.TOOLS_DENIED_ALWAYS and "Agent" not in on.deny_tools and "Workflow" in on.deny_tools
    assert on.to_json()["limits"] == {"depth": 1, "concurrent": 3} and on.to_json()["checks"]["depth_cap"] == "pass"
    off = decide(tmp_path, worker="codex")
    assert not off.on and off.code == "worker" and "Agent" in off.deny_tools and off.env == {}
    assert decide(tmp_path, cfg=config.DashboardConfig(subagents=SubagentsConfig(enabled=False))).code == "disabled"
    ignored = cli_check.DenyObservation(BINARY, cli_check.IGNORED, detail="Task is still offered")
    assert decide(tmp_path, obs=ignored).code == "refused"  # the per-binary check outranks the record
    assert decide(tmp_path, record=False).code == "no-record"
    assert decide(tmp_path, binary=None).code == "no-binary"
    assert decide(tmp_path, binary=dataclasses.replace(BINARY, digest="e" * 64)).code == "stale-binary"  # the bytes changed
    stale = decide(tmp_path, binary=dataclasses.replace(BINARY, version="2.1.305 (Claude Code)"))
    assert stale.code == "stale-version" and "2.1.304" in stale.reason and "2.1.305" in stale.reason
    assert decide(tmp_path, claude=claude_section(probe_version=subagents.PROBE_VERSION + 1)).code == "probe-version"
    expired = decide(tmp_path, now=NOW + timedelta(days=7))
    assert expired.code == "expired" and not expired.on and "task-kit characterize" in expired.reason
    assert decide(tmp_path, now=NOW + timedelta(days=5)).on  # inside the window
    failed = claude_section()
    failed["checks"]["depth_cap"]["verdict"] = "inconclusive"
    result = decide(tmp_path, claude=failed)
    assert result.code == "check-failed" and "depth_cap" in result.reason and not result.on
    informational = claude_section()
    informational["checks"]["helper_command_allowed"]["verdict"] = "fail"
    assert decide(tmp_path, claude=informational).on  # the helper line never gates
    gone = claude_section()
    del gone["checks"]["concurrent_cap"]
    assert decide(tmp_path, claude=gone).code == "check-failed"  # a required check that was never recorded is not a pass


def test_recheck_days_can_shorten_a_record_but_not_extend_it(tmp_path):
    short = config.DashboardConfig(subagents=SubagentsConfig(recheck_days=2))
    assert not decide(tmp_path, cfg=short, now=NOW + timedelta(days=2)).on  # checked the 19th, two days
    long = config.DashboardConfig(subagents=SubagentsConfig(recheck_days=30))
    assert decide(tmp_path, cfg=long, now=NOW + timedelta(days=5)).on
    assert decide(tmp_path, cfg=long, now=NOW + timedelta(days=8)).code == "expired"  # the record's own expiry still stands


@pytest.mark.parametrize("raw,named", [
    ("{not json", "not valid JSON"),
    ("[1, 2]", "expected a JSON dict"),
    ('{"schema": 2, "claude": {}}', "schema is 2"),
    ('{"schema": 1}', "claude is not an object"),
    ('{"schema": 1, "claude": {"binary": {"digest": "x"}}}', "claude.binary needs"),
])
def test_a_record_that_exists_but_cannot_be_used_is_named_corrupt_never_absent(tmp_path, raw, named):
    result = decide(tmp_path, raw=raw)
    assert result.code == "corrupt" and not result.on and named in result.reason and "task-kit characterize" in result.reason
    assert result.code != "no-record"


def test_a_record_with_a_bad_check_or_date_is_corrupt(tmp_path):
    assert decide(tmp_path, claude=claude_section(checked_at="yesterday")).code == "corrupt"
    bad = claude_section()
    bad["checks"]["depth_cap"] = {"verdict": "maybe"}
    assert decide(tmp_path, claude=bad).code == "corrupt"


def test_a_missing_stale_or_failed_record_never_refuses_a_run_it_only_turns_the_tool_off(tmp_path):
    for case in (decide(tmp_path, record=False), decide(tmp_path, raw="{x"), decide(tmp_path, now=NOW + timedelta(days=30)),
                 decide(tmp_path, binary=dataclasses.replace(BINARY, digest="0" * 64))):
        assert not case.on and case.code != "refused" and "Agent" in case.deny_tools and case.env == {}


# -- configuration --------------------------------------------------------------------------


def test_the_subagents_section_defaults_and_validates(tmp_path):
    path = tmp_path / "task.local.yaml"
    assert load_config(path).subagents == SubagentsConfig(True, 3, 7)
    path.write_text("dashboard:\n  subagents:\n    enabled: false\n    max_concurrent: 2\n    recheck_days: 1\n", encoding="utf-8")
    assert load_config(path).subagents == SubagentsConfig(False, 2, 1)
    for body, why in [("max_concurrent: 0", "max_concurrent"), ("recheck_days: 0", "recheck_days"), ("enabled: yes please", "enabled"),
                      ("max_concurrent: 1.5", "max_concurrent"), ("max_concurrent: true", "max_concurrent"), ("depth: 2", "unknown keys")]:
        path.write_text(f"dashboard:\n  subagents:\n    {body}\n", encoding="utf-8")
        with pytest.raises(DashboardConfigError) as err:
            load_config(path)
        assert why in str(err.value) and str(path) in str(err.value)
    path.write_text("dashboard:\n  subagents: 3\n", encoding="utf-8")
    with pytest.raises(DashboardConfigError):
        load_config(path)


# -- an ON run: environment, deny list, prompt, capabilities ---------------------------------


class FixedObserver:
    """Stands in for the per-binary observer with a settled, honest observation of the fake claude."""

    def __init__(self, binary) -> None:
        self.obs = cli_check.DenyObservation(binary, cli_check.OBSERVED, detail="fixed", at="x")

    def state_for(self, key):
        return self.obs

    def in_flight(self, key):
        return False

    def request(self, key):
        raise AssertionError("an observed binary is never asked for again")

    def close(self):
        pass


class World:
    def __init__(self, tmp_path, monkeypatch, *, record="good", cfg_text=None, behavior="env-dump"):
        runner_environment(monkeypatch)
        monkeypatch.setattr(scheduler, "RUNNER_SLACK_S", 1.0)
        self.cli = FakeCli(tmp_path, monkeypatch)
        self.binary = self.cli.binary()
        self.devroot, self.project, self.folder = dashboard_world(tmp_path)
        self.edge = FakeEdge(behavior=behavior)
        self.config = tmp_path / "task.local.yaml"
        if cfg_text:
            self.config.write_text(cfg_text, encoding="utf-8")
        self.srv = Running(tmp_path / "data", (self.devroot,), edge=self.edge, config_path=self.config,
                           cli_observer=FixedObserver(self.binary))
        self.root = self.srv.server.root
        if record == "good":
            self.write_record(**{})
        self.pid = project_id_for(self.project)

    def write_record(self, **over):
        claude = claude_section(**{"binary": {"realpath": self.binary.path, "digest": self.binary.digest, "version": self.binary.version},
                                   "checked_at": subagents._stamp(datetime.now(timezone.utc)),
                                   "expires_at": subagents._stamp(datetime.now(timezone.utc) + timedelta(days=7)), **over})
        subagents.write_record(self.root, claude)

    def run(self):
        from task_system.dashboard import mutation

        status, _h, body = self.srv.post(
            f"/api/v1/items/{self.pid}/dev%2Ftasks%2Falpha/do-it/queue",
            {"expected_revision": mutation.folder_revision(self.folder), "options": {"timeout_s": 60}})
        assert status == 202, body
        run = self.srv.settle(body["run"]["id"])
        assert run.state == runtime.ACCEPTED, run
        run_dir = Path(run.run_dir)
        return (json.loads((run_dir / "env-dump.json").read_text(encoding="utf-8")),
                json.loads((run_dir / "spec.json").read_text(encoding="utf-8")))

    def close(self):
        self.srv.stop()


@pytest.fixture
def world(tmp_path, monkeypatch):
    made = []

    def build(**kw):
        w = World(tmp_path, monkeypatch, **kw)
        made.append(w)
        return w

    yield build
    for w in made:
        w.close()


def test_an_on_run_carries_the_documented_limits_the_workflow_switch_and_no_agent_denial(world):
    """R21. RED: drop the depth variable from ``floor.subagent_env``; separately drop DISABLE_WORKFLOWS from the run env."""
    dump, spec = world().run()
    env = dump["env"]
    assert env["CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH"] == "1" and env["CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS"] == "3"
    assert env["CLAUDE_CODE_DISABLE_WORKFLOWS"] == "1" and env["TASK_KIT_AGENT_DEPTH"] == "0"  # workflows (and ultracode) stay off
    denied = dump["options"]["disallowed_tools"].split(",")
    assert "Workflow" in denied and "Agent" not in denied
    for tool in floor.TOOLS_DENIED_ALWAYS:
        assert tool in denied
    assert all(pattern in denied for pattern in floor.PATTERNS)  # the Bash floor applies to sub-agents exactly as to the orchestrator
    assert spec["runner"]["env"] == {**floor.RUN_ENV, **floor.subagent_env(3)}
    assert spec["subagents"]["state"] == "on" and spec["subagents"]["limits"] == {"depth": 1, "concurrent": 3}
    assert "Agent" not in spec["subagents"]["deny_tools"]


def test_the_concurrency_limit_follows_the_configuration(world):
    dump, spec = world(cfg_text="dashboard:\n  subagents:\n    max_concurrent: 2\n").run()
    assert dump["env"]["CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS"] == "2" and "at most 2 running at once" in dump["system"]
    assert spec["subagents"]["limits"] == {"depth": 1, "concurrent": 2}


@pytest.mark.parametrize("setup,code", [
    ({"record": "none"}, "no-record"),
    ({"cfg_text": "dashboard:\n  subagents:\n    enabled: false\n"}, "disabled"),
])
def test_an_off_run_carries_both_denials_the_workflow_switch_and_neither_limit(world, setup, code):
    w = world(**setup)
    dump, spec = w.run()
    env = dump["env"]
    assert env["CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH"] is None and env["CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS"] is None
    assert env["CLAUDE_CODE_DISABLE_WORKFLOWS"] == "1"
    denied = dump["options"]["disallowed_tools"].split(",")
    assert "Agent" in denied and "Workflow" in denied
    assert spec["runner"]["env"] == floor.RUN_ENV and spec["subagents"]["state"] == "off" and spec["subagents"]["code"] == code


def test_a_stale_record_leaves_the_run_going_with_the_tool_denied(world):
    w = world()
    w.write_record(binary={"realpath": w.binary.path, "digest": "f" * 64, "version": w.binary.version})  # another binary's record
    dump, spec = w.run()
    assert "Agent" in dump["options"]["disallowed_tools"].split(",") and spec["subagents"]["code"] == "stale-binary"


@pytest.mark.parametrize("on", [True, False])
def test_the_prompt_says_what_the_decision_is(world, on):
    """R23. RED: print the ON paragraph whatever the decision is and the OFF run is told it may launch sub-agents."""
    dump, _ = world(record="good" if on else "none").run()
    system, denied = dump["system"], dump["options"]["disallowed_tools"].split(",")
    if on:
        assert "Native sub-agents: you may use the Agent tool for bounded sub-work in this checkout: at most 3 running at once" in system
        assert "a sub-agent cannot start another" in system and "You alone report the task_result." in system
        assert "the Agent and Workflow tools are denied" not in system and "the Workflow tool is denied in this run" in system
        assert "Agent" not in denied
    else:
        assert "Work alone: the Agent and Workflow tools are denied in this run." in system
        assert "Native sub-agents:" not in system and "Agent" in denied


def test_capabilities_and_the_run_row_name_the_decision_and_its_residuals(world):
    """R24. RED: drop ``RESIDUALS`` from the decision's JSON and the capabilities lose the three residuals."""
    w = world()
    status, _h, body = w.srv.json("GET", "/api/v1/dashboard")
    sub = body["capabilities"]["subagents"]["claude"]
    assert sub["state"] == "on" and sub["limits"] == {"depth": 1, "concurrent": 3} and sub["binary"]["version"] == w.binary.version
    assert sub["checks"] == ALL_PASS and sub["residuals"] == list(subagents.RESIDUALS)
    text = " ".join(sub["residuals"])
    assert "resumed" in text and "no total limit" in text and "servers" in text  # the resume, no-total-limit and server-flag residuals
    assert any("Workflows and ultracode are off" in line for line in sub["closed"])
    assert sub["cli_check"]["state"] == "observed"
    w.run()
    status, _h, after = w.srv.json("GET", "/api/v1/dashboard")
    rows = [r for r in after["recent_runs"] + after["runs"] if r["state"] == "accepted"]
    assert rows and rows[0]["subagents"] == {"state": "on", "reason": "characterized on this Claude Code", "limits": {"depth": 1, "concurrent": 3}}


def test_an_unreadable_spec_is_said_in_the_run_row_not_hidden(tmp_path):
    run = dataclasses.replace(_a_run(), run_dir=str(tmp_path))
    (tmp_path / "spec.json").write_text("{nope", encoding="utf-8")
    assert snapshot.recorded_subagents(run)["state"] == "unreadable"
    (tmp_path / "spec.json").write_text('{"schema": 1}', encoding="utf-8")
    other = dataclasses.replace(_a_run(), id="other", run_dir=str(tmp_path))
    assert snapshot.recorded_subagents(other) is None  # a spec from before the decision was recorded: nothing to show


def _a_run():
    return runtime.Run(seq=1, id="r1", project_id="p", project_root="x", task_ref="dev/tasks/a", item_id="i", item_title="I",
                       state="running", code=None, detail=None, options={}, source_revision="r", queued_at="t", started_at="t",
                       ended_at=None, run_dir=None, runner_pid=None, runner_ctime=None, retry_of=None)


# -- the command -----------------------------------------------------------------------------


def _real_edge_or_skip(monkeypatch, tmp_path):
    import importlib

    from dashboard_support import REPO_ROOT

    monkeypatch.delenv("CLAUDE_BOOTSTRAP_DATA_ROOT", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_TEST", raising=False)
    monkeypatch.syspath_prepend(str(REPO_ROOT / "plugins" / "llm-kit" / "lib"))
    importlib.invalidate_caches()
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(home))
    from task_system.dashboard.jobs_edge import JobsEdge

    if not JobsEdge(ttl_s=0.0).status().available:
        pytest.skip("llm-kit is not importable here")


def test_the_characterize_command_writes_the_record_and_exits_zero_whatever_the_verdict(tmp_path, monkeypatch, capsys):
    _real_edge_or_skip(monkeypatch, tmp_path)
    monkeypatch.delenv("TASK_KIT_AGENT_DEPTH", raising=False)
    monkeypatch.setattr(launcher, "load_config", lambda: load_config(tmp_path / "none.yaml"))
    FakeCli(tmp_path, monkeypatch, honor_concurrency=False)  # one limit broken: the record says so, the command still exits 0
    data = tmp_path / "data"
    code = launcher.main(["characterize", "--entry", "sonnet", "--data-dir", str(data)], scripts_dir=SCRIPTS_DIR, entrypoint=Path("x"))
    out = capsys.readouterr().out
    assert code == launcher.EXIT_OK, out
    read = subagents.read_record(data / "dashboard")
    assert read.status == "ok" and read.claude["entry"] == "sonnet" and read.claude["checks"]["concurrent_cap"]["verdict"] == "fail"
    assert "concurrent_cap" in out and "OFF -- the check concurrent_cap did not pass" in out


def test_the_characterize_command_refuses_codex_and_a_machine_without_claude(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("TASK_KIT_AGENT_DEPTH", raising=False)
    code = launcher.main(["characterize", "--worker", "codex", "--data-dir", str(tmp_path)], scripts_dir=SCRIPTS_DIR, entrypoint=Path("x"))
    assert code == launcher.EXIT_USAGE and "Codex workers are refused" in capsys.readouterr().err
    _real_edge_or_skip(monkeypatch, tmp_path)
    monkeypatch.setattr(launcher, "load_config", lambda: load_config(tmp_path / "none.yaml"))
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    code = launcher.main(["characterize", "--data-dir", str(tmp_path / "d")], scripts_dir=SCRIPTS_DIR, entrypoint=Path("x"))
    assert code == launcher.EXIT_ERROR and "no claude executable is on PATH" in capsys.readouterr().err
    assert not (tmp_path / "d" / "dashboard" / subagents.RECORD_FILE).exists()  # nothing recorded when nothing ran


def test_characterize_is_refused_inside_a_dashboard_run(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TASK_KIT_AGENT_DEPTH", "0")
    code = launcher.main(["characterize", "--data-dir", str(tmp_path)], scripts_dir=SCRIPTS_DIR, entrypoint=Path("x"))
    assert code == launcher.EXIT_NESTED and "characterize the CLI" in capsys.readouterr().err
    assert not (tmp_path / "dashboard").exists()


@pytest.mark.parametrize("command", [None, "status", "stop", "restart", "serve"])
@pytest.mark.parametrize("option", [["--worker", "codex"], ["--entry", "haiku"]])
def test_worker_and_entry_are_refused_loudly_outside_characterize(tmp_path, monkeypatch, capsys, command, option):
    monkeypatch.delenv("TASK_KIT_AGENT_DEPTH", raising=False)
    argv = ([command] if command else []) + option + ["--data-dir", str(tmp_path)]
    with pytest.raises(SystemExit) as exit_:
        launcher.main(argv, scripts_dir=SCRIPTS_DIR, entrypoint=Path("x"))
    assert exit_.value.code == launcher.EXIT_USAGE
    assert "apply only to the characterize command" in capsys.readouterr().err
