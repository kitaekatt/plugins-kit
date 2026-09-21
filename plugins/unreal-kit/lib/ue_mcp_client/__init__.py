"""
ue_mcp_client -- Python client for the Unreal MCP Automation Bridge.

Provides a synchronous wrapper around the WebSocket protocol used by the
McpAutomationBridge plugin running inside the Unreal Editor.

Quick start::

    from ue_mcp_client import McpClient

    with McpClient() as mcp:
        # Run a console command
        mcp.console_command("stat fps")

        # Spawn an actor
        resp = mcp.spawn("PointLight", actor_name="MyLight",
                         location={"x": 0, "y": 0, "z": 200})
        print(resp.result)

        # Screenshot
        mcp.screenshot("my_screenshot")

        # Generic request -- tool is the domain, action goes in payload
        resp = mcp.send_request("control_actor", {
            "action": "find_by_class", "className": "PointLight"
        })
        print(resp.result)

        # Batch console commands
        results = mcp.batch_console_commands(["stat fps", "stat unit"])

Configuration (environment variables):

    MCP_AUTOMATION_WS_HOST  -- server host (default: 127.0.0.1)
    MCP_AUTOMATION_WS_PORT  -- server port (default: 8090)
    MCP_REQUEST_TIMEOUT_MS  -- default request timeout in ms (default: 30000)

The client also accepts ``connection_timeout_s`` (default 5 seconds) for the
connect/handshake/cleanup phase and ``request_cap_s`` (default 300 seconds)
for the total lifetime of one request, including progress extensions. Set
both ``timeout_s`` and ``request_cap_s`` when an action needs more time; a
batch applies its action budget separately to each command.
"""

from .client import (
    ActionError,
    HandshakeError,
    McpClient,
    McpConnectionError,
    McpError,
    McpResponse,
    McpTimeoutError,
)

__all__ = [
    "McpClient",
    "McpResponse",
    "McpError",
    "McpConnectionError",
    "HandshakeError",
    "McpTimeoutError",
    "ActionError",
]
