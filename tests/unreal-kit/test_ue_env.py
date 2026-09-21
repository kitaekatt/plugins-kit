"""Deterministic budget tests for the UE environment readiness helpers."""

import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

_LIB_DIR = Path(__file__).resolve().parent.parent.parent / "plugins" / "unreal-kit" / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import ue_env


class FakeClock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeClient:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.connected = False
        self.close_calls = []
        type(self).instances.append(self)

    def connect(self):
        self.connected = True

    def close(self, **kwargs):
        self.close_calls.append(kwargs)
        self.connected = False


def test_readiness_uses_connection_budget_and_cleanup_remaining(monkeypatch):
    FakeClient.instances.clear()
    clock = FakeClock()
    monkeypatch.setattr(ue_env.time, "monotonic", clock)
    monkeypatch.setitem(
        sys.modules,
        "ue_mcp_client",
        types.SimpleNamespace(
            McpClient=FakeClient,
            HandshakeError=Exception,
            McpConnectionError=Exception,
            McpTimeoutError=Exception,
        ),
    )

    assert ue_env.is_mcp_ready(probe_timeout_s=5)
    client = FakeClient.instances[-1]
    assert client.kwargs["connection_timeout_s"] == 5
    assert "request_cap_s" not in client.kwargs
    assert client.close_calls
    assert client.close_calls[0]["timeout_s"] > 0


def test_zero_readiness_budget_does_not_probe(monkeypatch):
    FakeClient.instances.clear()
    with patch.object(ue_env, "_tcp_probe", side_effect=AssertionError("probe")):
        assert not ue_env.is_mcp_ready(probe_timeout_s=0)
    assert not FakeClient.instances


def test_wait_for_ready_does_not_start_probe_after_deadline(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(ue_env.time, "monotonic", clock)
    probes = []

    def probe(host, port, probe_timeout_s):
        probes.append(probe_timeout_s)
        clock.advance(probe_timeout_s)
        return False

    monkeypatch.setattr(ue_env, "is_mcp_ready", probe)
    monkeypatch.setattr(ue_env.time, "sleep", lambda seconds: clock.advance(seconds))

    assert not ue_env.wait_for_mcp_ready(total_timeout_s=2, poll_interval_s=10)
    assert probes
    assert all(remaining > 0 for remaining in probes)
    assert len(probes) == 1


@pytest.mark.parametrize("value", [-1, float("inf"), float("nan")])
def test_readiness_budget_must_be_finite_and_positive(value):
    with pytest.raises(ValueError, match="finite and positive"):
        ue_env.is_mcp_ready(probe_timeout_s=value)
