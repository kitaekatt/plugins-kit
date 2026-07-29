"""The ctx adapter.

These tests guard the INTEGRATION SEAM as much as the behavior: the adapter
must touch only the documented ctx surface (add_failure / log / log_ok /
data_dir), because that is what makes folding this plugin into the bootstrap
engine a file move instead of a rewrite. A test that pins the surface is
cheaper than rediscovering the coupling later.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "secrets-kit"


@pytest.fixture
def module():
    spec = importlib.util.spec_from_file_location(
        "secrets_kit_custom_bootstrap", _PLUGIN / "custom_bootstrap.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeCtx:
    """Records exactly what the adapter asked of the engine."""

    def __init__(self, data_dir):
        self.data_dir = str(data_dir)
        self.project_dir = None
        self.failures = []
        self.logs = []
        self.oks = []

    def add_failure(self, key, **kwargs):
        self.failures.append((key, kwargs))

    def log(self, message):
        self.logs.append(message)

    def log_ok(self, message):
        self.oks.append(message)


def test_unconfigured_machine_logs_once_and_adds_no_failure(module, tmp_path, monkeypatch):
    """Installing the plugin without declaring anything must be silent."""
    monkeypatch.setattr(module, "CONFIG_PATH", tmp_path / "absent.json")
    monkeypatch.setattr(module, "ENV_PATH", tmp_path / "absent-env.json")
    ctx = FakeCtx(tmp_path / "data")

    module.bootstrap(ctx)

    assert ctx.failures == []
    assert ctx.oks == []
    assert ctx.logs == ["secrets: not configured"]


def test_locked_machine_forwards_the_ask_reason(module, fleet, monkeypatch):
    """ask_reason is what makes the engine present this as ASK, not AUTO."""
    monkeypatch.setattr(module, "CONFIG_PATH", fleet.config_path)
    monkeypatch.setattr(module, "ENV_PATH", fleet.tmp / "no-env.json")
    ctx = FakeCtx(fleet.data_dir)

    module.bootstrap(ctx)

    assert len(ctx.failures) == 1
    key, kwargs = ctx.failures[0]
    assert key == "secrets_locked"
    assert kwargs["ask_reason"] == "info"
    assert "user_msg" in kwargs and "agent_msg" in kwargs


def test_successful_pass_reports_via_log_ok(module, fleet, monkeypatch):
    monkeypatch.setattr(module, "CONFIG_PATH", fleet.config_path)
    monkeypatch.setattr(module, "ENV_PATH", fleet.tmp / "no-env.json")
    fleet.unlock()
    ctx = FakeCtx(fleet.data_dir)

    module.bootstrap(ctx)

    assert ctx.failures == []
    assert ctx.oks == ["secrets: 0 ok, 1 written, 0 failed"]


def test_env_registry_is_read_when_present(module, fleet, monkeypatch):
    """The cross-check exists so secrets.json cannot become a second machine list."""
    env_path = fleet.tmp / "env.json"
    env_path.write_text(
        json.dumps({"machines": {"someone-else": {"os": "macos"}}}), encoding="utf-8"
    )
    monkeypatch.setattr(module, "CONFIG_PATH", fleet.config_path)
    monkeypatch.setattr(module, "ENV_PATH", env_path)
    fleet.unlock()
    ctx = FakeCtx(fleet.data_dir)

    module.bootstrap(ctx)

    assert len(ctx.failures) == 1
    assert ctx.failures[0][0] == "secrets_config"
    assert ctx.failures[0][1]["ask_reason"] == "info"


def test_malformed_env_json_skips_the_crosscheck_rather_than_failing(
    module, fleet, monkeypatch
):
    """env.json is the ENGINE's manifest; a plugin must not fail a session over it."""
    env_path = fleet.tmp / "env.json"
    env_path.write_text("{ not json", encoding="utf-8")
    monkeypatch.setattr(module, "CONFIG_PATH", fleet.config_path)
    monkeypatch.setattr(module, "ENV_PATH", env_path)
    fleet.unlock()
    ctx = FakeCtx(fleet.data_dir)

    module.bootstrap(ctx)

    assert ctx.failures == []
    assert ctx.oks


def test_adapter_uses_only_the_documented_ctx_surface(module, fleet, monkeypatch):
    """Anything beyond this surface would break an in-process fold."""

    class StrictCtx(FakeCtx):
        def __getattr__(self, name):
            raise AssertionError(
                f"adapter reached for ctx.{name}, which is outside the seam"
            )

    monkeypatch.setattr(module, "CONFIG_PATH", fleet.config_path)
    monkeypatch.setattr(module, "ENV_PATH", fleet.tmp / "no-env.json")
    fleet.unlock()

    module.bootstrap(StrictCtx(fleet.data_dir))
