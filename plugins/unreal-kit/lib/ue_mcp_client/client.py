"""
Synchronous Python client for the Unreal MCP Automation Bridge.

Wraps the WebSocket protocol used by the MCP Automation Bridge plugin
running inside the Unreal Editor. Sends automation requests and receives
responses using request/response correlation over a single WebSocket
connection.

Protocol reference: Tools/Source/Unreal_mcp/src/automation/bridge.ts
"""

import json
import os
import time
import uuid
from dataclasses import dataclass, field

import websocket


# ---------------------------------------------------------------------------
# Constants (mirror constants.ts)
# ---------------------------------------------------------------------------

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8090
DEFAULT_TIMEOUT_S = 30.0
HANDSHAKE_TIMEOUT_S = 5.0
SUBPROTOCOL = "mcp-automation"

# Progress extension safeguards
PROGRESS_EXTENSION_S = 30.0
MAX_PROGRESS_EXTENSIONS = 10
PROGRESS_STALE_THRESHOLD = 3
ABSOLUTE_MAX_TIMEOUT_S = 300.0


# ---------------------------------------------------------------------------
# Exceptions
#
# Named with the Mcp prefix so they never shadow Python's builtin
# ConnectionError / TimeoutError (shadowing forced importers to rename at
# every import site and made `except ConnectionError` ambiguous).
# ---------------------------------------------------------------------------

class McpError(Exception):
    """Base exception for all MCP client errors."""


class McpConnectionError(McpError):
    """Failed to connect or lost connection to the Automation Bridge.

    Common causes:
    - Unreal Editor is not running
    - McpAutomationBridge plugin is not active
    - Wrong host/port (check MCP_AUTOMATION_WS_HOST / MCP_AUTOMATION_WS_PORT)
    """


class HandshakeError(McpError):
    """WebSocket connected but the bridge handshake failed.

    The server did not respond with bridge_ack within the timeout,
    or responded with an unexpected message type.
    """


class McpTimeoutError(McpError):
    """The server did not respond within the allowed time.

    The timeout may have been extended by progress_update messages.
    If the operation genuinely takes longer, pass a larger timeout_s
    to send_request().
    """


class ActionError(McpError):
    """The server processed the request but reported failure.

    Attributes:
        action: The action that was requested.
        message: Human-readable error from the server.
        error_code: Machine-readable error code (if any).
        result: The full result dict from the response.
    """

    def __init__(self, action, message, error_code=None, result=None):
        self.action = action
        self.message = message
        self.error_code = error_code
        self.result = result
        detail = f"{action}: {message}"
        if error_code:
            detail = f"{action} [{error_code}]: {message}"
        super().__init__(detail)


# ---------------------------------------------------------------------------
# Response dataclass
# ---------------------------------------------------------------------------

@dataclass
class McpResponse:
    """Parsed automation_response from the bridge."""

    request_id: str
    success: bool
    action: str = ""
    message: str = ""
    error: str = ""
    result: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data):
        return cls(
            request_id=data.get("requestId", ""),
            success=bool(data.get("success")),
            action=data.get("action", ""),
            message=data.get("message", ""),
            error=data.get("error", ""),
            result=data.get("result") if isinstance(data.get("result"), dict) else {},
        )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class McpClient:
    """Synchronous client for the Unreal MCP Automation Bridge.

    Usage::

        with McpClient() as mcp:
            # Generic request -- tool is domain, action in payload
            resp = mcp.send_request("control_actor", {
                "action": "spawn",
                "classPath": "PointLight",
                "actorName": "MyLight",
                "location": {"x": 0, "y": 0, "z": 200},
            })

            # Convenience methods
            mcp.console_command("stat fps")
            mcp.screenshot("my_screenshot")
            actors = mcp.find_by_class("PointLight")

    Configuration via environment variables:

    - ``MCP_AUTOMATION_WS_HOST`` -- server host (default: 127.0.0.1)
    - ``MCP_AUTOMATION_WS_PORT`` -- server port (default: 8090)
    - ``MCP_REQUEST_TIMEOUT_MS``  -- default timeout in ms (default: 30000)
    """

    def __init__(self, host=None, port=None, timeout_s=None):
        self._host = host or os.environ.get(
            "MCP_AUTOMATION_WS_HOST",
            os.environ.get("MCP_AUTOMATION_HOST", DEFAULT_HOST),
        )
        self._port = int(
            port
            or os.environ.get("MCP_AUTOMATION_WS_PORT", DEFAULT_PORT)
        )
        env_timeout_ms = os.environ.get("MCP_REQUEST_TIMEOUT_MS")
        if timeout_s is not None:
            self._timeout_s = float(timeout_s)
        elif env_timeout_ms is not None:
            self._timeout_s = int(env_timeout_ms) / 1000.0
        else:
            self._timeout_s = DEFAULT_TIMEOUT_S
        self._ws = None
        self._handshake_metadata = None

    # -- Context manager ---------------------------------------------------

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    # -- Connection --------------------------------------------------------

    @property
    def url(self):
        return f"ws://{self._host}:{self._port}"

    def connect(self):
        """Open the WebSocket and complete the bridge handshake."""
        try:
            self._ws = websocket.create_connection(
                self.url,
                timeout=HANDSHAKE_TIMEOUT_S,
                subprotocols=[SUBPROTOCOL],
            )
        except (OSError, websocket.WebSocketException) as exc:
            raise McpConnectionError(
                f"Cannot connect to Unreal Editor at {self.url}. "
                f"Is the Editor running with the McpAutomationBridge plugin? ({exc})"
            ) from exc

        self._handshake()

    def close(self):
        """Send bridge_goodbye and close the socket."""
        if self._ws is not None:
            try:
                self._ws.send(json.dumps({
                    "type": "bridge_goodbye",
                    "reason": "client closing",
                }))
            except Exception:
                pass
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None

    @property
    def connected(self):
        return self._ws is not None and self._ws.connected

    # -- Handshake ---------------------------------------------------------

    def _handshake(self):
        """Send bridge_hello and wait for bridge_ack."""
        hello = {"type": "bridge_hello"}
        self._ws.send(json.dumps(hello))

        deadline = time.monotonic() + HANDSHAKE_TIMEOUT_S
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._ws.settimeout(remaining)
            try:
                raw = self._ws.recv()
            except websocket.WebSocketTimeoutException:
                break
            except websocket.WebSocketException as exc:
                raise McpConnectionError(
                    f"Connection lost during handshake: {exc}"
                ) from exc

            msg = _parse_json(raw)
            if msg is None:
                continue

            msg_type = msg.get("type")
            if msg_type == "bridge_ack":
                self._handshake_metadata = msg
                self._ws.settimeout(None)
                return
            if msg_type == "bridge_ping":
                self._send_pong()
                continue
            if msg_type == "bridge_error":
                raise HandshakeError(
                    f"Bridge rejected connection: {msg.get('message', msg.get('error', 'unknown'))}"
                )

        raise HandshakeError(
            f"Handshake timed out after {HANDSHAKE_TIMEOUT_S}s -- "
            f"no bridge_ack received from {self.url}"
        )

    # -- Request/response --------------------------------------------------

    def send_request(self, tool, payload=None, timeout_s=None):
        """Send an automation request and wait for the response.

        The bridge uses two-layer routing: ``tool`` is the domain name
        (e.g. ``"control_actor"``, ``"control_editor"``, ``"inspect"``),
        and ``payload`` contains an ``"action"`` key with the specific
        operation (e.g. ``{"action": "spawn", "classPath": "PointLight"}``).

        Some tools like ``"console_command"`` are their own domain and
        don't need an ``"action"`` key in the payload.

        Args:
            tool: Tool/domain name (e.g. "control_actor", "console_command").
            payload: Parameters dict, typically including an "action" key.
            timeout_s: Override default timeout for this request.

        Returns:
            McpResponse with the server's reply.

        Raises:
            ActionError: Server reported ``success: false``.
            McpTimeoutError: No response within the timeout window.
            McpConnectionError: Socket closed unexpectedly.
        """
        if not self.connected:
            raise McpConnectionError("Not connected. Call connect() or use a context manager.")

        request_id = str(uuid.uuid4())
        message = {
            "type": "automation_request",
            "requestId": request_id,
            "action": tool,
            "payload": payload or {},
        }
        try:
            self._ws.send(json.dumps(message))
        except websocket.WebSocketException as exc:
            raise McpConnectionError(f"Failed to send request: {exc}") from exc

        base_timeout = timeout_s if timeout_s is not None else self._timeout_s
        return self._recv_response(request_id, tool, base_timeout)

    def _recv_response(self, request_id, action, base_timeout):
        """Block until the matching automation_response arrives.

        Transparently handles ping/pong, progress_update (with timeout
        extension and deadlock safeguards), and other control messages.
        """
        deadline = time.monotonic() + base_timeout
        absolute_deadline = time.monotonic() + ABSOLUTE_MAX_TIMEOUT_S
        extension_count = 0
        last_progress_percent = None
        stale_count = 0

        while True:
            now = time.monotonic()
            remaining = min(deadline, absolute_deadline) - now
            if remaining <= 0:
                raise McpTimeoutError(
                    f"Request '{action}' timed out after {base_timeout}s "
                    f"(extensions={extension_count}). The Editor may be busy "
                    f"or the action may not exist."
                )

            self._ws.settimeout(remaining)
            try:
                raw = self._ws.recv()
            except websocket.WebSocketTimeoutException:
                raise McpTimeoutError(
                    f"Request '{action}' timed out after {base_timeout}s "
                    f"(extensions={extension_count}). The Editor may be busy "
                    f"or the action may not exist."
                )
            except websocket.WebSocketException as exc:
                raise McpConnectionError(
                    f"Connection lost while waiting for response: {exc}"
                ) from exc

            msg = _parse_json(raw)
            if msg is None:
                continue

            msg_type = msg.get("type")

            # -- Heartbeat --
            if msg_type == "bridge_ping":
                self._send_pong()
                continue

            # -- Progress update: extend timeout with deadlock safeguards --
            if msg_type == "progress_update":
                if msg.get("requestId") != request_id:
                    continue

                percent = msg.get("percent")

                # Safeguard 1: max extensions
                if extension_count >= MAX_PROGRESS_EXTENSIONS:
                    raise McpTimeoutError(
                        f"Request '{action}' exceeded max progress extensions "
                        f"({MAX_PROGRESS_EXTENSIONS}) -- possible deadlock."
                    )

                # Safeguard 2: stale detection
                if percent is not None and percent == last_progress_percent:
                    stale_count += 1
                    if stale_count >= PROGRESS_STALE_THRESHOLD:
                        raise McpTimeoutError(
                            f"Request '{action}' stalled at {percent}% for "
                            f"{PROGRESS_STALE_THRESHOLD} updates -- possible deadlock."
                        )
                else:
                    stale_count = 0

                last_progress_percent = percent
                extension_count += 1
                deadline = time.monotonic() + PROGRESS_EXTENSION_S
                continue

            # -- Ignorable control messages --
            if msg_type in ("bridge_ack", "bridge_pong", "bridge_goodbye"):
                continue

            # -- Bridge error --
            if msg_type == "bridge_error":
                raise McpError(
                    f"Bridge error: {msg.get('message', msg.get('error', 'unknown'))}"
                )

            # -- Automation response --
            if msg_type == "automation_response":
                if msg.get("requestId") != request_id:
                    # Response for a different request; ignore.
                    continue

                resp = McpResponse.from_dict(msg)
                if not resp.success:
                    raise ActionError(
                        action=action,
                        message=resp.message or resp.error or "Unknown error",
                        error_code=resp.error if resp.error != resp.message else None,
                        result=resp.result,
                    )
                return resp

            # Unknown message type -- skip silently.

    def _send_pong(self):
        try:
            self._ws.send(json.dumps({"type": "bridge_pong"}))
        except websocket.WebSocketException:
            pass  # Best effort; recv loop will catch the broken socket.

    # -- Convenience methods -----------------------------------------------

    def console_command(self, command, timeout_s=None):
        """Run a console command in the Editor (or PIE when active).

        Args:
            command: The console command string.
            timeout_s: Override default timeout.

        Returns:
            McpResponse.
        """
        return self.send_request(
            "console_command", {"command": command}, timeout_s=timeout_s,
        )

    def screenshot(self, filename, timeout_s=None):
        """Capture a screenshot from the active viewport.

        Args:
            filename: Output filename (without extension).
            timeout_s: Override default timeout.

        Returns:
            McpResponse with file path in result.
        """
        return self.send_request(
            "control_editor",
            {"action": "screenshot", "filename": filename},
            timeout_s=timeout_s,
        )

    def spawn(self, class_path, actor_name=None, location=None, rotation=None, timeout_s=None):
        """Spawn an actor in the level.

        Args:
            class_path: Actor class or alias (e.g. "PointLight", "StaticMeshActor").
            actor_name: Optional name for the spawned actor.
            location: Optional dict with x, y, z keys.
            rotation: Optional dict with pitch, yaw, roll keys.
            timeout_s: Override default timeout.

        Returns:
            McpResponse.
        """
        payload = {"action": "spawn", "classPath": class_path}
        if actor_name is not None:
            payload["actorName"] = actor_name
        if location is not None:
            payload["location"] = location
        if rotation is not None:
            payload["rotation"] = rotation
        return self.send_request("control_actor", payload, timeout_s=timeout_s)

    def find_by_class(self, class_name, timeout_s=None):
        """Find all actors of a given class in the current level.

        Args:
            class_name: Unreal class name (e.g. "PointLight").
            timeout_s: Override default timeout.

        Returns:
            McpResponse with actors list in result.
        """
        return self.send_request(
            "control_actor",
            {"action": "find_by_class", "className": class_name},
            timeout_s=timeout_s,
        )

    def inspect_object(self, object_path, timeout_s=None):
        """Inspect an object's properties.

        Args:
            object_path: Full Unreal object path.
            timeout_s: Override default timeout.

        Returns:
            McpResponse with properties in result.
        """
        return self.send_request(
            "inspect",
            {"action": "inspect_object", "objectPath": object_path},
            timeout_s=timeout_s,
        )

    def save_all(self, timeout_s=None):
        """Save all modified assets in the Editor."""
        return self.send_request(
            "control_editor", {"action": "save_all"}, timeout_s=timeout_s,
        )

    def batch_console_commands(self, commands, timeout_s=None):
        """Run multiple console commands sequentially.

        Args:
            commands: List of console command strings.
            timeout_s: Per-command timeout override.

        Returns:
            List of McpResponse, one per command. If a command fails with
            ActionError, the error is captured as the response entry (the
            remaining commands still execute).
        """
        results = []
        for cmd in commands:
            try:
                resp = self.console_command(cmd, timeout_s=timeout_s)
                results.append(resp)
            except ActionError as exc:
                results.append(exc)
        return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_json(raw):
    """Parse a WebSocket text frame as JSON, returning None on failure."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
