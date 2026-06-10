"""Tests for ue_mcp_client -- Unreal MCP Automation Bridge client."""

import json
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add lib/ to path so we can import ue_mcp_client
_LIB_DIR = Path(__file__).resolve().parent.parent.parent / "plugins" / "unreal-kit" / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from ue_mcp_client import (
    ActionError,
    HandshakeError,
    McpClient,
    McpConnectionError,
    McpResponse,
    McpTimeoutError,
)
from ue_mcp_client.client import (
    ABSOLUTE_MAX_TIMEOUT_S,
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_TIMEOUT_S,
    HANDSHAKE_TIMEOUT_S,
    MAX_PROGRESS_EXTENSIONS,
    PROGRESS_STALE_THRESHOLD,
    _parse_json,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class FakeWebSocket:
    """Minimal mock of websocket.WebSocketApp / create_connection result."""

    def __init__(self):
        self.connected = True
        self._send_log = []
        self._recv_queue = []
        self._timeout = None
        self.closed = False

    def send(self, data):
        self._send_log.append(json.loads(data))

    def recv(self):
        if not self._recv_queue:
            if self._timeout is not None and self._timeout > 0:
                import websocket as ws_mod
                raise ws_mod.WebSocketTimeoutException("timeout")
            # Block forever (shouldn't happen in tests)
            raise RuntimeError("recv called with empty queue and no timeout")
        return json.dumps(self._recv_queue.pop(0))

    def settimeout(self, val):
        self._timeout = val

    def close(self):
        self.connected = False
        self.closed = True

    def enqueue(self, *messages):
        """Helper: enqueue messages to be returned by recv()."""
        for msg in messages:
            self._recv_queue.append(msg)


@pytest.fixture
def fake_ws():
    return FakeWebSocket()


@pytest.fixture
def client_with_ws(fake_ws):
    """Return (McpClient, FakeWebSocket) with the WS already wired up."""
    client = McpClient()
    client._ws = fake_ws
    client._handshake_metadata = {"type": "bridge_ack"}
    return client, fake_ws


# ---------------------------------------------------------------------------
# _parse_json
# ---------------------------------------------------------------------------

class TestParseJson:
    def test_valid_json(self):
        assert _parse_json('{"type": "bridge_ack"}') == {"type": "bridge_ack"}

    def test_bytes_input(self):
        assert _parse_json(b'{"ok": true}') == {"ok": True}

    def test_invalid_json_returns_none(self):
        assert _parse_json("not json") is None

    def test_none_input_returns_none(self):
        assert _parse_json(None) is None


# ---------------------------------------------------------------------------
# McpResponse
# ---------------------------------------------------------------------------

class TestMcpResponse:
    def test_from_dict_success(self):
        data = {
            "requestId": "abc-123",
            "success": True,
            "action": "spawn",
            "message": "Actor spawned",
            "result": {"actorName": "MyLight"},
        }
        resp = McpResponse.from_dict(data)
        assert resp.request_id == "abc-123"
        assert resp.success is True
        assert resp.action == "spawn"
        assert resp.result == {"actorName": "MyLight"}

    def test_from_dict_failure(self):
        data = {
            "requestId": "abc-456",
            "success": False,
            "error": "NOT_FOUND",
            "message": "Actor not found",
        }
        resp = McpResponse.from_dict(data)
        assert resp.success is False
        assert resp.error == "NOT_FOUND"

    def test_from_dict_missing_fields(self):
        resp = McpResponse.from_dict({})
        assert resp.request_id == ""
        assert resp.success is False
        assert resp.result == {}

    def test_from_dict_non_dict_result(self):
        resp = McpResponse.from_dict({"result": "not a dict", "success": True})
        assert resp.result == {}


# ---------------------------------------------------------------------------
# McpClient -- Configuration
# ---------------------------------------------------------------------------

class TestClientConfig:
    def test_defaults(self):
        client = McpClient()
        assert client._host == DEFAULT_HOST
        assert client._port == DEFAULT_PORT
        assert client._timeout_s == DEFAULT_TIMEOUT_S

    def test_explicit_params(self):
        client = McpClient(host="10.0.0.1", port=9090, timeout_s=60)
        assert client._host == "10.0.0.1"
        assert client._port == 9090
        assert client._timeout_s == 60.0

    def test_env_vars(self, monkeypatch):
        monkeypatch.setenv("MCP_AUTOMATION_WS_HOST", "192.168.1.1")
        monkeypatch.setenv("MCP_AUTOMATION_WS_PORT", "9999")
        monkeypatch.setenv("MCP_REQUEST_TIMEOUT_MS", "5000")
        client = McpClient()
        assert client._host == "192.168.1.1"
        assert client._port == 9999
        assert client._timeout_s == 5.0

    def test_explicit_overrides_env(self, monkeypatch):
        monkeypatch.setenv("MCP_AUTOMATION_WS_PORT", "9999")
        client = McpClient(port=7777)
        assert client._port == 7777

    def test_host_fallback_to_MCP_AUTOMATION_HOST(self, monkeypatch):
        monkeypatch.setenv("MCP_AUTOMATION_HOST", "10.0.0.5")
        # MCP_AUTOMATION_WS_HOST not set -- should fall back
        monkeypatch.delenv("MCP_AUTOMATION_WS_HOST", raising=False)
        client = McpClient()
        assert client._host == "10.0.0.5"

    def test_url_property(self):
        client = McpClient(host="1.2.3.4", port=1234)
        assert client.url == "ws://1.2.3.4:1234"


# ---------------------------------------------------------------------------
# McpClient -- Handshake
# ---------------------------------------------------------------------------

class TestHandshake:
    def test_successful_handshake(self, fake_ws):
        fake_ws.enqueue({"type": "bridge_ack", "serverName": "test"})

        client = McpClient()
        client._ws = fake_ws
        client._handshake()

        # Should have sent bridge_hello
        assert fake_ws._send_log[0]["type"] == "bridge_hello"
        assert client._handshake_metadata["serverName"] == "test"

    def test_handshake_handles_ping_before_ack(self, fake_ws):
        fake_ws.enqueue(
            {"type": "bridge_ping"},
            {"type": "bridge_ack"},
        )

        client = McpClient()
        client._ws = fake_ws
        client._handshake()

        # Should have sent hello + pong
        types = [m["type"] for m in fake_ws._send_log]
        assert "bridge_hello" in types
        assert "bridge_pong" in types

    def test_handshake_timeout(self, fake_ws):
        # Empty queue + timeout will trigger WebSocketTimeoutException
        client = McpClient()
        client._ws = fake_ws

        with pytest.raises(HandshakeError, match="timed out"):
            client._handshake()

    def test_handshake_bridge_error(self, fake_ws):
        fake_ws.enqueue({"type": "bridge_error", "message": "auth failed"})

        client = McpClient()
        client._ws = fake_ws

        with pytest.raises(HandshakeError, match="auth failed"):
            client._handshake()


# ---------------------------------------------------------------------------
# McpClient -- send_request
# ---------------------------------------------------------------------------

class TestSendRequest:
    def test_basic_request_response(self, client_with_ws):
        client, ws = client_with_ws
        req_id = None

        # We need to intercept the request to get the UUID, then enqueue a response.
        original_send = ws.send

        def capturing_send(data):
            nonlocal req_id
            msg = json.loads(data)
            if msg.get("type") == "automation_request":
                req_id = msg["requestId"]
                ws.enqueue({
                    "type": "automation_response",
                    "requestId": req_id,
                    "success": True,
                    "action": "control_actor",
                    "result": {"actorName": "MyActor"},
                })
            original_send(data)

        ws.send = capturing_send

        resp = client.send_request("control_actor", {"action": "spawn", "classPath": "Actor"})
        assert resp.success is True
        assert resp.result == {"actorName": "MyActor"}

    def test_action_error_raised(self, client_with_ws):
        client, ws = client_with_ws

        original_send = ws.send

        def capturing_send(data):
            msg = json.loads(data)
            if msg.get("type") == "automation_request":
                ws.enqueue({
                    "type": "automation_response",
                    "requestId": msg["requestId"],
                    "success": False,
                    "error": "NOT_FOUND",
                    "message": "Actor class not found",
                })
            original_send(data)

        ws.send = capturing_send

        with pytest.raises(ActionError, match="Actor class not found") as exc_info:
            client.send_request("control_actor", {"action": "spawn", "classPath": "Nonexistent"})

        assert exc_info.value.error_code == "NOT_FOUND"
        assert exc_info.value.action == "control_actor"

    def test_not_connected_raises(self):
        client = McpClient()
        with pytest.raises(McpConnectionError, match="Not connected"):
            client.send_request("spawn", {})

    def test_skips_unrelated_response(self, client_with_ws):
        client, ws = client_with_ws

        original_send = ws.send

        def capturing_send(data):
            msg = json.loads(data)
            if msg.get("type") == "automation_request":
                # First: unrelated response, then the real one
                ws.enqueue(
                    {
                        "type": "automation_response",
                        "requestId": "some-other-id",
                        "success": True,
                    },
                    {
                        "type": "automation_response",
                        "requestId": msg["requestId"],
                        "success": True,
                        "result": {"found": True},
                    },
                )
            original_send(data)

        ws.send = capturing_send
        resp = client.send_request("control_actor", {"action": "find_by_class", "className": "Light"})
        assert resp.result == {"found": True}

    def test_ping_pong_during_recv(self, client_with_ws):
        client, ws = client_with_ws

        original_send = ws.send

        def capturing_send(data):
            msg = json.loads(data)
            if msg.get("type") == "automation_request":
                ws.enqueue(
                    {"type": "bridge_ping"},
                    {
                        "type": "automation_response",
                        "requestId": msg["requestId"],
                        "success": True,
                    },
                )
            original_send(data)

        ws.send = capturing_send

        resp = client.send_request("test_action", {})
        assert resp.success is True
        # Verify pong was sent
        pong_msgs = [m for m in ws._send_log if m.get("type") == "bridge_pong"]
        assert len(pong_msgs) >= 1


# ---------------------------------------------------------------------------
# McpClient -- Progress updates & timeout extension
# ---------------------------------------------------------------------------

class TestProgressUpdates:
    def test_progress_extends_timeout(self, client_with_ws):
        client, ws = client_with_ws

        original_send = ws.send

        def capturing_send(data):
            msg = json.loads(data)
            if msg.get("type") == "automation_request":
                rid = msg["requestId"]
                ws.enqueue(
                    {"type": "progress_update", "requestId": rid, "percent": 25},
                    {"type": "progress_update", "requestId": rid, "percent": 50},
                    {
                        "type": "automation_response",
                        "requestId": rid,
                        "success": True,
                        "result": {"done": True},
                    },
                )
            original_send(data)

        ws.send = capturing_send

        resp = client.send_request("long_action", {}, timeout_s=0.1)
        assert resp.success is True

    def test_stale_progress_raises(self, client_with_ws):
        client, ws = client_with_ws

        original_send = ws.send

        def capturing_send(data):
            msg = json.loads(data)
            if msg.get("type") == "automation_request":
                rid = msg["requestId"]
                # First update sets the baseline; next PROGRESS_STALE_THRESHOLD
                # updates at the same percent trigger stale detection.
                for _ in range(PROGRESS_STALE_THRESHOLD + 1):
                    ws.enqueue({"type": "progress_update", "requestId": rid, "percent": 42})
            original_send(data)

        ws.send = capturing_send

        with pytest.raises(McpTimeoutError, match="stalled at 42%"):
            client.send_request("stuck_action", {}, timeout_s=1)

    def test_max_extensions_raises(self, client_with_ws):
        client, ws = client_with_ws

        original_send = ws.send

        def capturing_send(data):
            msg = json.loads(data)
            if msg.get("type") == "automation_request":
                rid = msg["requestId"]
                # Each update has a different percent to avoid stale detection
                for i in range(MAX_PROGRESS_EXTENSIONS + 1):
                    ws.enqueue({"type": "progress_update", "requestId": rid, "percent": i})
            original_send(data)

        ws.send = capturing_send

        with pytest.raises(McpTimeoutError, match="max progress extensions"):
            client.send_request("endless_action", {}, timeout_s=1)

    def test_ignores_progress_for_other_request(self, client_with_ws):
        client, ws = client_with_ws

        original_send = ws.send

        def capturing_send(data):
            msg = json.loads(data)
            if msg.get("type") == "automation_request":
                rid = msg["requestId"]
                ws.enqueue(
                    {"type": "progress_update", "requestId": "other-id", "percent": 50},
                    {
                        "type": "automation_response",
                        "requestId": rid,
                        "success": True,
                    },
                )
            original_send(data)

        ws.send = capturing_send

        resp = client.send_request("test", {})
        assert resp.success is True


# ---------------------------------------------------------------------------
# McpClient -- Convenience methods
# ---------------------------------------------------------------------------

class TestConvenienceMethods:
    def _setup_auto_response(self, ws, result=None):
        """Wire ws.send to auto-enqueue a success response."""
        original_send = ws.send

        def capturing_send(data):
            msg = json.loads(data)
            if msg.get("type") == "automation_request":
                ws.enqueue({
                    "type": "automation_response",
                    "requestId": msg["requestId"],
                    "success": True,
                    "action": msg["action"],
                    "result": result or {},
                })
            original_send(data)

        ws.send = capturing_send

    def test_console_command(self, client_with_ws):
        client, ws = client_with_ws
        self._setup_auto_response(ws)
        resp = client.console_command("stat fps")
        assert resp.success is True
        req = [m for m in ws._send_log if m.get("type") == "automation_request"][0]
        assert req["action"] == "console_command"
        assert req["payload"] == {"command": "stat fps"}

    def test_screenshot(self, client_with_ws):
        client, ws = client_with_ws
        self._setup_auto_response(ws, {"filePath": "/tmp/shot.png"})
        resp = client.screenshot("my_shot")
        assert resp.result == {"filePath": "/tmp/shot.png"}
        req = [m for m in ws._send_log if m.get("type") == "automation_request"][0]
        assert req["action"] == "control_editor"
        assert req["payload"]["action"] == "screenshot"

    def test_spawn(self, client_with_ws):
        client, ws = client_with_ws
        self._setup_auto_response(ws)
        resp = client.spawn(
            "PointLight",
            actor_name="Light1",
            location={"x": 1, "y": 2, "z": 3},
        )
        assert resp.success is True
        req = [m for m in ws._send_log if m.get("type") == "automation_request"][0]
        assert req["action"] == "control_actor"
        assert req["payload"]["action"] == "spawn"
        assert req["payload"]["classPath"] == "PointLight"
        assert req["payload"]["actorName"] == "Light1"
        assert req["payload"]["location"] == {"x": 1, "y": 2, "z": 3}

    def test_spawn_minimal(self, client_with_ws):
        client, ws = client_with_ws
        self._setup_auto_response(ws)
        client.spawn("Camera")
        req = [m for m in ws._send_log if m.get("type") == "automation_request"][0]
        assert req["action"] == "control_actor"
        assert req["payload"]["action"] == "spawn"
        assert "actorName" not in req["payload"]
        assert "location" not in req["payload"]

    def test_find_by_class(self, client_with_ws):
        client, ws = client_with_ws
        self._setup_auto_response(ws, {"actors": ["Light1", "Light2"]})
        resp = client.find_by_class("PointLight")
        assert resp.result["actors"] == ["Light1", "Light2"]
        req = [m for m in ws._send_log if m.get("type") == "automation_request"][0]
        assert req["action"] == "control_actor"
        assert req["payload"]["action"] == "find_by_class"

    def test_inspect_object(self, client_with_ws):
        client, ws = client_with_ws
        self._setup_auto_response(ws, {"className": "PointLight"})
        resp = client.inspect_object("/Game/Maps/Main.Main:PersistentLevel.Light1")
        assert resp.result["className"] == "PointLight"
        req = [m for m in ws._send_log if m.get("type") == "automation_request"][0]
        assert req["action"] == "inspect"
        assert req["payload"]["action"] == "inspect_object"

    def test_save_all(self, client_with_ws):
        client, ws = client_with_ws
        self._setup_auto_response(ws, {"saved": True})
        resp = client.save_all()
        assert resp.result["saved"] is True
        req = [m for m in ws._send_log if m.get("type") == "automation_request"][0]
        assert req["action"] == "control_editor"
        assert req["payload"]["action"] == "save_all"


# ---------------------------------------------------------------------------
# McpClient -- batch_console_commands
# ---------------------------------------------------------------------------

class TestBatchCommands:
    def test_batch_all_succeed(self, client_with_ws):
        client, ws = client_with_ws

        original_send = ws.send

        def capturing_send(data):
            msg = json.loads(data)
            if msg.get("type") == "automation_request":
                ws.enqueue({
                    "type": "automation_response",
                    "requestId": msg["requestId"],
                    "success": True,
                    "result": {"command": msg["payload"]["command"]},
                })
            original_send(data)

        ws.send = capturing_send

        results = client.batch_console_commands(["cmd1", "cmd2", "cmd3"])
        assert len(results) == 3
        assert all(isinstance(r, McpResponse) for r in results)

    def test_batch_partial_failure(self, client_with_ws):
        client, ws = client_with_ws
        call_count = [0]

        original_send = ws.send

        def capturing_send(data):
            msg = json.loads(data)
            if msg.get("type") == "automation_request":
                call_count[0] += 1
                if call_count[0] == 2:
                    ws.enqueue({
                        "type": "automation_response",
                        "requestId": msg["requestId"],
                        "success": False,
                        "error": "FAILED",
                        "message": "Command failed",
                    })
                else:
                    ws.enqueue({
                        "type": "automation_response",
                        "requestId": msg["requestId"],
                        "success": True,
                    })
            original_send(data)

        ws.send = capturing_send

        results = client.batch_console_commands(["ok1", "fail", "ok2"])
        assert len(results) == 3
        assert isinstance(results[0], McpResponse)
        assert isinstance(results[1], ActionError)
        assert isinstance(results[2], McpResponse)

    def test_batch_empty(self, client_with_ws):
        client, _ = client_with_ws
        assert client.batch_console_commands([]) == []


# ---------------------------------------------------------------------------
# McpClient -- Context manager
# ---------------------------------------------------------------------------

class TestContextManager:
    @patch("ue_mcp_client.client.websocket")
    def test_context_manager_closes(self, mock_ws_mod):
        fake = FakeWebSocket()
        fake.enqueue({"type": "bridge_ack"})
        mock_ws_mod.create_connection.return_value = fake
        mock_ws_mod.WebSocketException = Exception
        mock_ws_mod.WebSocketTimeoutException = type("Timeout", (Exception,), {})

        with McpClient() as mcp:
            assert mcp.connected

        assert fake.closed

    @patch("ue_mcp_client.client.websocket")
    def test_context_manager_closes_on_exception(self, mock_ws_mod):
        fake = FakeWebSocket()
        fake.enqueue({"type": "bridge_ack"})
        mock_ws_mod.create_connection.return_value = fake
        mock_ws_mod.WebSocketException = Exception
        mock_ws_mod.WebSocketTimeoutException = type("Timeout", (Exception,), {})

        with pytest.raises(ValueError):
            with McpClient() as mcp:
                raise ValueError("boom")

        assert fake.closed


# ---------------------------------------------------------------------------
# McpClient -- Error hierarchy
# ---------------------------------------------------------------------------

class TestErrorHierarchy:
    def test_connection_error_is_mcp_error(self):
        from ue_mcp_client.client import McpError
        assert issubclass(McpConnectionError, McpError)

    def test_exceptions_do_not_shadow_builtins(self):
        """Regression (U14): the client must not export classes named after
        Python builtins ConnectionError/TimeoutError."""
        import ue_mcp_client
        assert "ConnectionError" not in ue_mcp_client.__all__
        assert "TimeoutError" not in ue_mcp_client.__all__

    def test_handshake_error_is_mcp_error(self):
        from ue_mcp_client.client import McpError
        assert issubclass(HandshakeError, McpError)

    def test_timeout_error_is_mcp_error(self):
        from ue_mcp_client.client import McpError
        assert issubclass(McpTimeoutError, McpError)

    def test_action_error_attributes(self):
        err = ActionError("spawn", "not found", error_code="E404", result={"x": 1})
        assert err.action == "spawn"
        assert err.message == "not found"
        assert err.error_code == "E404"
        assert err.result == {"x": 1}
        assert "spawn" in str(err)
        assert "E404" in str(err)

    def test_action_error_without_code(self):
        err = ActionError("test", "failed")
        assert err.error_code is None
        assert "test: failed" in str(err)


# ---------------------------------------------------------------------------
# McpClient -- close / goodbye
# ---------------------------------------------------------------------------

class TestClose:
    def test_close_sends_goodbye(self, client_with_ws):
        client, ws = client_with_ws
        client.close()
        goodbye_msgs = [m for m in ws._send_log if m.get("type") == "bridge_goodbye"]
        assert len(goodbye_msgs) == 1
        assert ws.closed

    def test_close_idempotent(self, client_with_ws):
        client, ws = client_with_ws
        client.close()
        client.close()  # Should not raise
        assert ws.closed

    def test_connected_property(self, client_with_ws):
        client, ws = client_with_ws
        assert client.connected is True
        client.close()
        assert client.connected is False
