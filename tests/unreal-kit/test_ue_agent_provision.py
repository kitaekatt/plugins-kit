"""ue-agent provisioning: creates the project subagent, never overwrites."""

import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

import pytest

_PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "unreal-kit"
sys.path.insert(0, str(_PLUGIN / "lib"))
import ue_agent_provision as prov  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "unreal_custom_bootstrap_provision", _PLUGIN / "custom_bootstrap.py"
)
custom_bootstrap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(custom_bootstrap)

# Fixture copy of a real consumer's .mcp.json.
MCP_JSON = {
    "mcpServers": {
        "unreal-engine": {
            "command": "node",
            "args": ["Tools/Source/Unreal_mcp/scripts/ensure-deps.cjs"],
            "env": {
                "UE_PROJECT_PATH": "SpiritCrossing/SpiritCrossing.uproject",
                "WASM_ENABLED": "false",
                "MCP_AUTOMATION_HOST": "127.0.0.1",
                "MCP_AUTOMATION_PORT": "8091",
                "MCP_AUTOMATION_CLIENT_PORT": "8091",
            },
        }
    }
}

# Hardcoded on purpose: deriving this from the template would let both move
# together.
GOLDEN = """---
name: ue-agent
description: Executes Unreal Editor operations via MCP tools. Use when you need to create/modify assets, spawn actors, author graphs, drive PIE, or take screenshots in the Editor.
mcpServers:
  - unreal-engine:
      type: stdio
      command: node
      args: ["Tools/Source/Unreal_mcp/scripts/ensure-deps.cjs"]
      env:
        UE_PROJECT_PATH: "SpiritCrossing/SpiritCrossing.uproject"
        WASM_ENABLED: "false"
        MCP_AUTOMATION_HOST: "127.0.0.1"
        MCP_AUTOMATION_PORT: "8091"
        MCP_AUTOMATION_CLIENT_PORT: "8091"
---

You are responsible for correctly executing tool calls to the Unreal MCP server. Prior to performing any tool calls you must perform the Initialization Sequence defined below. Failure to do this will result in errors and wasted context. Once the Initialization Sequence is complete, proceed to the Tool Assistance Sequence.

## Initialization Sequence

1. Invoke the `/unreal-kit:ue-mcp-server` skill. This loads tool selection guidance, critical patterns, and known limitations.
2. Do not make any MCP tool calls until step 1 is complete.

## Tool Assistance Sequence

Execute the task you were given. After every MCP call that creates or modifies an asset:
1. Call `control_editor` -> `save_all`
2. Verify the `.uasset` exists on disk (use Glob)
3. Only proceed after both steps succeed
"""


class Ctx:
    def __init__(self, project_dir, config=None):
        self.project_dir = None if project_dir is None else str(project_dir)
        self.config = config or {}
        self.outcomes = []

    def log(self, message):
        self.outcomes.append(("log", message))

    def log_ok(self, message):
        self.outcomes.append(("ok", message))

    def lines(self):
        return [(k, m) for k, m in self.outcomes if m.startswith("ue-agent:")]


def _project(tmp_path, mcp=MCP_JSON):
    project = tmp_path / "proj"
    project.mkdir()
    if mcp is not None:
        (project / ".mcp.json").write_text(json.dumps(mcp), encoding="utf-8")
    return project


def _agent(project):
    return project / ".claude" / "agents" / "ue-agent.md"


def _run(project, config=None):
    ctx = Ctx(project, config)
    custom_bootstrap.bootstrap(ctx)
    return ctx


def test_golden_render_and_platform_newline(tmp_path):
    project = _project(tmp_path)
    result = prov.check(project)
    assert result.status == prov.MISSING
    text = prov.render(result.server)
    assert text.replace("\r\n", "\n") == GOLDEN
    prov.create(project, text)
    raw = _agent(project).read_bytes()
    assert raw == GOLDEN.replace("\n", os.linesep).encode("ascii")


def test_bootstrap_creates_when_missing(tmp_path):
    project = _project(tmp_path)
    ctx = _run(project)
    assert _agent(project).read_bytes().decode("ascii").replace("\r\n", "\n") == GOLDEN
    (kind, message), = ctx.lines()
    assert kind == "log" and "created" in message and "/agents" in message


def test_existing_read_only_file_untouched(tmp_path):
    project = _project(tmp_path)
    target = _agent(project)
    target.parent.mkdir(parents=True)
    target.write_text("mine", encoding="ascii")
    os.chmod(target, stat.S_IREAD)
    try:
        ctx = _run(project)
    finally:
        os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
    assert target.read_text(encoding="ascii") == "mine"
    assert ctx.lines() == [("ok", "ue-agent: present")]


def test_existing_directory_left_untouched(tmp_path):
    project = _project(tmp_path)
    _agent(project).mkdir(parents=True)
    ctx = _run(project)
    assert _agent(project).is_dir()
    assert ctx.lines() == [("ok", "ue-agent: present")]


def test_opt_out_false_skips(tmp_path):
    project = _project(tmp_path)
    ctx = _run(project, {"provision_ue_agent": False})
    assert not _agent(project).exists()
    (kind, message), = ctx.lines()
    assert kind == "ok" and "provision_ue_agent" in message


@pytest.mark.parametrize("value", ["false", 0, None, "yes"])
def test_invalid_option_skips_and_displays(tmp_path, value):
    project = _project(tmp_path)
    ctx = _run(project, {"provision_ue_agent": value})
    assert not _agent(project).exists()
    (kind, message), = ctx.lines()
    assert kind == "log" and "invalid option" in message


def test_custom_server_name_finds_and_renders_that_server(tmp_path):
    entry = MCP_JSON["mcpServers"]["unreal-engine"]
    project = _project(tmp_path, {"mcpServers": {"my-ue": entry}})
    ctx = _run(project, {"ue_agent_mcp_server": "my-ue"})
    text = _agent(project).read_bytes().decode("ascii").replace("\r\n", "\n")
    assert text == GOLDEN.replace("  - unreal-engine:", "  - my-ue:")
    assert ctx.lines()[0][0] == "log" and "created" in ctx.lines()[0][1]


def test_custom_server_name_ignores_default_named_server(tmp_path):
    project = _project(tmp_path)
    ctx = _run(project, {"ue_agent_mcp_server": "my-ue"})
    assert not _agent(project).exists()
    (kind, message), = ctx.lines()
    assert kind == "ok" and "my-ue" in message


@pytest.mark.parametrize("value", ["", 5, None, ["a"], "has space", "a:b"])
def test_invalid_server_name_skips_and_displays(tmp_path, value):
    project = _project(tmp_path)
    ctx = _run(project, {"ue_agent_mcp_server": value})
    assert not _agent(project).exists()
    (kind, message), = ctx.lines()
    assert kind == "log"
    assert message.startswith("ue-agent: invalid option - ue_agent_mcp_server")


def test_explicit_true_creates(tmp_path):
    project = _project(tmp_path)
    _run(project, {"provision_ue_agent": True})
    assert _agent(project).exists()


def test_no_project_dir_skips(tmp_path):
    ctx = _run(None)
    (kind, message), = ctx.lines()
    assert kind == "ok" and "skipped" in message


@pytest.mark.parametrize("mcp", [
    None,
    {"mcpServers": {}},
    {"mcpServers": {"other": {"command": "x"}}},
])
def test_no_server_skips_quietly(tmp_path, mcp):
    project = _project(tmp_path, mcp)
    ctx = _run(project)
    assert not _agent(project).exists()
    (kind, message), = ctx.lines()
    assert kind == "ok" and "skipped" in message


def _server(**changes):
    server = json.loads(json.dumps(MCP_JSON["mcpServers"]["unreal-engine"]))
    server.update(changes)
    return {"mcpServers": {"unreal-engine": server}}


@pytest.mark.parametrize("mcp", [
    _server(cwd="sub"),
    _server(type="http"),
    _server(args=["a", 1]),
    _server(env={"A": 1}),
    _server(command=["node"]),
])
def test_unsupported_shapes_skip_and_display(tmp_path, mcp):
    project = _project(tmp_path, mcp)
    ctx = _run(project)
    assert not _agent(project).exists()
    (kind, message), = ctx.lines()
    assert kind == "log" and "unsupported" in message


def test_unreadable_mcp_json_is_unsupported(tmp_path):
    project = _project(tmp_path, None)
    (project / ".mcp.json").write_text("{not json", encoding="utf-8")
    ctx = _run(project)
    (kind, message), = ctx.lines()
    assert kind == "log" and "unsupported" in message


def test_write_failure_is_logged_not_raised(tmp_path):
    project = _project(tmp_path)
    (project / ".claude").write_text("a file, not a directory", encoding="ascii")
    ctx = _run(project)
    (kind, message), = ctx.lines()
    assert kind == "log" and "FAILED" in message


def test_create_never_overwrites(tmp_path):
    project = _project(tmp_path)
    target = _agent(project)
    target.parent.mkdir(parents=True)
    target.write_text("mine", encoding="ascii")
    with pytest.raises(FileExistsError):
        prov.create(project, "other")
    assert target.read_text(encoding="ascii") == "mine"


def test_second_pass_reports_present(tmp_path):
    project = _project(tmp_path)
    _run(project)
    ctx = _run(project)
    assert ctx.lines() == [("ok", "ue-agent: present")]
