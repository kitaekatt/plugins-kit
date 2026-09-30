"""Provision the project-level ``ue-agent`` Claude Code subagent.

``<project>/.claude/agents/ue-agent.md`` is created from
``templates/ue-agent.md`` when it is missing, with its ``mcpServers`` block
rendered from the project's own ``.mcp.json`` ``unreal-engine`` entry. The
file is never overwritten: ``create`` opens with mode "x".

Split like ``bootstrap_lib.agent_skills_check``: ``check`` is side-effect free,
``render`` is pure, ``create`` is the only writer.
"""

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

SERVER_NAME = "unreal-engine"
OPTION_KEY = "provision_ue_agent"
SERVER_OPTION_KEY = "ue_agent_mcp_server"
PLACEHOLDER = "@@MCP_SERVERS@@"
TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "templates" / "ue-agent.md"

PRESENT = "present"
NO_SERVER = "no_server"
UNSUPPORTED = "unsupported"
MISSING = "missing"

_ALLOWED_KEYS = {"type", "command", "args", "env"}
_SAFE_COMMAND = re.compile(r"^[A-Za-z0-9_./\\-]+$")
_SAFE_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class Check:
    status: str
    detail: str = ""
    server: Optional[Dict[str, Any]] = None


def agent_path(project_dir) -> Path:
    return Path(project_dir) / ".claude" / "agents" / "ue-agent.md"


def read_option(config: Any) -> Tuple[bool, bool]:
    """Return (enabled, valid). Absent = on; a non-bool value is invalid."""
    if not hasattr(config, "get") or OPTION_KEY not in config:
        return True, True
    value = config[OPTION_KEY]
    if isinstance(value, bool):
        return value, True
    return False, False


_SAFE_SERVER_NAME = re.compile(r"^[A-Za-z0-9_-]+$")


def read_server_name(config: Any) -> Tuple[str, bool]:
    """Return (name, valid). Absent = "unreal-engine"; must be a plain name."""
    if not hasattr(config, "get") or SERVER_OPTION_KEY not in config:
        return SERVER_NAME, True
    value = config[SERVER_OPTION_KEY]
    if isinstance(value, str) and _SAFE_SERVER_NAME.match(value):
        return value, True
    return SERVER_NAME, False


def _validate_server(server: Any) -> str:
    """Return an empty string when supported, else the reason."""
    if not isinstance(server, dict):
        return "server entry is not an object"
    extra = sorted(set(server) - _ALLOWED_KEYS)
    if extra:
        return "unsupported key(s): " + ", ".join(str(k) for k in extra)
    if server.get("type", "stdio") != "stdio":
        return "server type is not stdio"
    command = server.get("command")
    if not isinstance(command, str) or not _SAFE_COMMAND.match(command):
        return "command is missing or not a plain string"
    args = server.get("args", [])
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        return "args are not all strings"
    env = server.get("env", {})
    if not isinstance(env, dict):
        return "env is not an object"
    for key, value in env.items():
        if not isinstance(key, str) or not _SAFE_ENV_KEY.match(key):
            return "env key is not a plain name"
        if not isinstance(value, str):
            return "env values are not all strings"
    return ""


def check(project_dir, server_name: str = SERVER_NAME) -> Check:
    """Classify the project. Reads only; never writes."""
    target = agent_path(project_dir)
    if os.path.lexists(target):
        return Check(PRESENT)
    mcp_path = Path(project_dir) / ".mcp.json"
    if not mcp_path.is_file():
        return Check(NO_SERVER, "no .mcp.json")
    try:
        data = json.loads(mcp_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return Check(UNSUPPORTED, f".mcp.json unreadable: {exc}")
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict) or server_name not in servers:
        return Check(NO_SERVER, f"no {server_name} server in .mcp.json")
    server = servers[server_name]
    reason = _validate_server(server)
    if reason:
        return Check(UNSUPPORTED, reason)
    return Check(MISSING, server=server)


def _mcp_block(server: Dict[str, Any], server_name: str) -> str:
    lines = [
        "mcpServers:",
        f"  - {server_name}:",
        "      type: stdio",
        f"      command: {server['command']}",
        f"      args: {json.dumps(server.get('args', []))}",
    ]
    env = server.get("env", {})
    if env:
        lines.append("      env:")
        for key, value in env.items():
            lines.append(f"        {key}: {json.dumps(value)}")
    return "\n".join(lines)


def render(
    server: Dict[str, Any],
    template_text: Optional[str] = None,
    server_name: str = SERVER_NAME,
) -> str:
    """Build the agent text (LF newlines) from the template and server entry."""
    if template_text is None:
        with open(TEMPLATE_PATH, "r", encoding="utf-8", newline="") as handle:
            template_text = handle.read()
    template_text = template_text.replace("\r\n", "\n")
    if template_text.count(PLACEHOLDER) != 1:
        raise ValueError("template must hold exactly one " + PLACEHOLDER)
    return template_text.replace(PLACEHOLDER, _mcp_block(server, server_name))


def create(project_dir, text: str) -> Path:
    """Write the agent file; raises FileExistsError rather than overwrite.

    Text mode, so the platform newline applies (CRLF on Windows).
    """
    target = agent_path(project_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "x", encoding="ascii") as handle:
        handle.write(text)
    return target
