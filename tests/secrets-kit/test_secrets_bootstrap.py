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

import bootstrap_lib.engine as engine
from secrets_kit.converge import Failure, Result

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


def _real_ctx(data_dir):
    """Build the REAL provider context (bootstrap_lib.engine._ScriptContext).

    Its ``log`` and ``log_ok`` write into separate lists this helper returns
    directly, so a test can assert against the same always-shown vs
    verbose-only channels the engine itself uses -- not a fake's paraphrase
    of that contract.
    """
    log_entries = []
    ok_entries = []
    ctx = engine._ScriptContext(
        config={},
        data_dir=str(data_dir),
        plugin_root=str(data_dir),
        log_entries=log_entries,
        ok_entries=ok_entries,
        prefix="",
        plugin_name="secrets-kit",
    )
    return ctx, log_entries, ok_entries


def _stub_result(*, skipped_reason=None, failures=None, notes=None):
    result = Result()
    result.skipped_reason = skipped_reason
    result.failures = failures or []
    result.notes = notes or []
    return result


def test_unconfigured_machine_logs_verbose_only_and_adds_no_failure(module, tmp_path, monkeypatch):
    """Nothing declared is the third-party default: quiet on the always-shown
    action channel; a verbose-only ok note is allowed."""
    monkeypatch.setattr(module, "CONFIG_PATH", tmp_path / "absent.json")
    monkeypatch.setattr(module, "ENV_PATH", tmp_path / "absent-env.json")
    ctx, log_entries, ok_entries = _real_ctx(tmp_path / "data")

    module.bootstrap(ctx)

    assert ctx.failures == []
    assert log_entries == []
    assert ok_entries == ["secrets: not configured"]


def test_no_profiles_for_this_host_is_also_quiet(module, tmp_path, monkeypatch):
    """A machine deliberately left out of every profile is opt-out, not a
    problem: manifest.py's Config.machine_key() docstring calls an unlisted
    machine 'not an error' -- 'subsetting by omission is how a machine opts
    out of holding secrets it has no business holding.'"""
    monkeypatch.setattr(
        module, "converge",
        lambda *a, **k: _stub_result(skipped_reason="no profiles for this host"),
    )
    ctx, log_entries, ok_entries = _real_ctx(tmp_path / "data")

    module.bootstrap(ctx)

    assert ctx.failures == []
    assert log_entries == []
    assert ok_entries == ["secrets: no profiles for this host"]


def test_performed_work_stays_on_the_visible_channel(module, tmp_path, monkeypatch):
    """A note is real action the user should see, even in the otherwise-quiet
    steady state -- unaffected by the quiet-skip classification above."""
    monkeypatch.setattr(
        module, "converge",
        lambda *a, **k: _stub_result(
            notes=["cloned git@example.com:acct/fleet-secrets.git"],
        ),
    )
    ctx, log_entries, ok_entries = _real_ctx(tmp_path / "data")

    module.bootstrap(ctx)

    assert ctx.failures == []
    assert log_entries == ["secrets: cloned git@example.com:acct/fleet-secrets.git"]


def test_real_failure_stays_on_the_visible_channel(module, tmp_path, monkeypatch):
    """A failure's summary line must stay on the always-shown channel, not
    fall into the verbose-only one alongside its failure record."""
    failure = Failure("secrets_locked", user_msg="u", agent_msg="a", ask_reason="info")
    monkeypatch.setattr(
        module, "converge",
        lambda *a, **k: _stub_result(
            skipped_reason="locked (awaiting one-time unlock)",
            failures=[failure],
        ),
    )
    ctx, log_entries, ok_entries = _real_ctx(tmp_path / "data")

    module.bootstrap(ctx)

    assert len(ctx.failures) == 1
    assert ctx.failures[0]["type"] == "secrets_locked"
    assert ctx.failures[0]["ask_reason"] == "info"
    assert log_entries
    assert ok_entries == []


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


@pytest.mark.parametrize('raw', [
    b'\xff', b'{ bad', b'null', b'[]', b'false', b'1', b'"text"',
    b'{}', b'{"machines": null}', b'{"machines": false}',
    b'{"machines": 0}', b'{"machines": ""}', b'{"machines": []}',
    b'{"machines": {}}', b'{"machines": {"testbox": ["metadata is ignored"]}}',
])
def test_optional_env_shape_or_decoding_does_not_bypass_actual_materialization(module, fleet, monkeypatch, raw):
    env = fleet.tmp / 'optional-env.json'
    env.write_bytes(raw)
    monkeypatch.setattr(module, 'CONFIG_PATH', fleet.config_path)
    monkeypatch.setattr(module, 'ENV_PATH', env)
    fleet.unlock()
    ctx = FakeCtx(fleet.data_dir)
    module.bootstrap(ctx)
    assert ctx.failures == []
    assert (fleet.dest_root / 'ha-token.txt').read_bytes() == b'token-value\n'
    assert ctx.oks == ['secrets: 0 ok, 1 written, 0 failed']


def test_malformed_optional_env_still_forwards_actual_secrets_declaration_failure(module, fleet, monkeypatch):
    env = fleet.tmp / 'optional-env.json'
    env.write_bytes(b'null')
    config = json.loads(fleet.config_path.read_text())
    config['repo'] = ['bad type']
    fleet.config_path.write_text(json.dumps(config), encoding='utf-8')
    monkeypatch.setattr(module, 'CONFIG_PATH', fleet.config_path)
    monkeypatch.setattr(module, 'ENV_PATH', env)
    ctx = FakeCtx(fleet.data_dir)
    module.bootstrap(ctx)
    assert len(ctx.failures) == 1
    key, detail = ctx.failures[0]
    assert key == 'secrets_config'
    assert 'secrets.json' in detail['user_msg'] and 'repo' in detail['user_msg']
    assert 'ask_reason' not in detail
    assert not (fleet.dest_root / 'ha-token.txt').exists()
    assert not (fleet.data_dir / 'state.json').exists()
    assert ctx.oks == []
