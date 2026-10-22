"""U1b: harness authentication probes (checks C7, C8a-C8g, C9 of the cost-class plan).

Every probe test injects a runner; no real claude or codex process runs here, except
two tests that put fake executables on a private PATH to prove the default
``subprocess.run`` call wiring.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from llm_kit.adapters import harness_auth
from llm_kit.adapters.harness_auth import (
    CODEX_AUTH_ENV,
    claude_auth,
    clear_codex_auth_cache,
    codex_auth,
    probe_harness_auth,
)
from llm_kit.registry.cost import BILLING_DIVERTING_VARS, METERED, SUBSCRIPTION, UNKNOWN

TOKEN = "sk-FIXTURE-TOKEN-0123456789abcdef"


@pytest.fixture(autouse=True)
def _fresh_cache():
    clear_codex_auth_cache()
    yield
    clear_codex_auth_cache()


class Spy:
    """A runner that records every call and answers with a fixed result or raises."""

    def __init__(self, *, stdout="", returncode=0, raises=None):
        self.stdout, self.returncode, self.raises = stdout, returncode, raises
        self.calls: list = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(argv, self.returncode, stdout=self.stdout, stderr="")


def claude_status(**fields) -> str:
    doc = {
        "loggedIn": True,
        "authMethod": "claude.ai",
        "apiProvider": "firstParty",
        "email": "someone@example.invalid",
        "orgId": "org-fixture-id",
        "subscriptionType": "max",
    }
    doc.update(fields)
    return json.dumps(doc)


def doctor(*, provider="openai", mode="chatgpt", stored_key="false", cl_status="ok",
           ac_status="ok", schema=1, version="0.162.0", extra=None) -> str:
    cl_details = {"model provider": provider, "cwd": "/somewhere", "CODEX_HOME": "/home/x/.codex"}
    ac_details = {"stored auth mode": mode, "stored API key": stored_key,
                  "stored ChatGPT tokens": "true", "auth file": "/home/x/.codex/auth.json"}
    if provider is None:
        del cl_details["model provider"]
    doc = {
        "schemaVersion": schema,
        "codexVersion": version,
        "overallStatus": "warning",
        "checks": {
            "config.load": {"id": "config.load", "status": cl_status, "details": cl_details},
            "auth.credentials": {"id": "auth.credentials", "status": ac_status, "details": ac_details},
            "state.rollout_db_parity": {"status": "warning", "details": {}},
        },
    }
    if extra:
        doc["checks"]["config.load"]["details"].update(extra)
    return json.dumps(doc)


# ---------------------------------------------------------------------------
# C7: claude_auth
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", BILLING_DIVERTING_VARS)
def test_claude_divert_var(name):
    spy = Spy(stdout=claude_status())
    auth = claude_auth({name: "x"}, runner=spy)
    assert auth.state == METERED
    assert name in auth.reason
    assert spy.calls == []


def test_claude_empty_var_ignored():
    spy = Spy(stdout=claude_status())
    auth = claude_auth({name: "" for name in BILLING_DIVERTING_VARS}, runner=spy)
    assert auth.state == SUBSCRIPTION
    assert len(spy.calls) == 1


def test_claude_status_subscription_and_call_shape():
    spy = Spy(stdout=claude_status())
    env = {"PATH": "/bin", "HOME": "/h"}
    auth = claude_auth(env, runner=spy, timeout_s=7)
    assert auth.state == SUBSCRIPTION
    argv, kwargs = spy.calls[0]
    assert argv == ["claude", "auth", "status"]
    assert kwargs["env"] == env and kwargs["timeout"] == 7


def test_claude_env_defaults_to_os_environ(monkeypatch):
    for name in BILLING_DIVERTING_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://proxy.invalid")
    spy = Spy(stdout=claude_status())
    assert claude_auth(runner=spy).state == METERED
    assert spy.calls == []


@pytest.mark.parametrize(
    "fields",
    [
        {"authMethod": "console"},
        {"authMethod": "api_key", "apiProvider": "firstParty"},
        {"apiProvider": "bedrock"},
        {"apiProvider": None},
    ],
)
def test_claude_status_console_is_metered(fields):
    auth = claude_auth({}, runner=Spy(stdout=claude_status(**fields)))
    assert auth.state == METERED


@pytest.mark.parametrize(
    "spy",
    [
        Spy(raises=FileNotFoundError("claude")),
        Spy(raises=subprocess.TimeoutExpired(["claude"], 10)),
        Spy(raises=PermissionError("nope")),
        Spy(stdout=claude_status(), returncode=1),
        Spy(stdout="not json"),
        Spy(stdout="[]"),
        Spy(stdout=json.dumps({"loggedIn": False})),
        Spy(stdout=json.dumps({"authMethod": 7})),
    ],
    ids=["missing", "timeout", "oserror", "exit1", "nonjson", "list", "no-authMethod", "bad-authMethod"],
)
def test_claude_status_failure_unknown(spy):
    assert claude_auth({}, runner=spy).state == UNKNOWN


# ---------------------------------------------------------------------------
# C8: codex_auth through `codex doctor --json`
# ---------------------------------------------------------------------------


def codex_home(tmp_path: Path, *, config: str = 'model_provider = "openai"\n') -> dict:
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "config.toml").write_text(config)
    (home / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt", "OPENAI_API_KEY": None,
                                                "tokens": {"access_token": TOKEN}}))
    return {"CODEX_HOME": str(home), "PATH": "/bin"}


@pytest.mark.parametrize("name", ["CODEX_API_KEY", "OPENAI_API_KEY"])
def test_codex_env_key_metered_without_doctor(tmp_path, name):
    spy = Spy(stdout=doctor())
    auth = codex_auth({**codex_home(tmp_path), name: "x"}, cwd=tmp_path, runner=spy)
    assert auth.state == METERED and name in auth.reason
    assert spy.calls == []


def test_codex_env_access_token_unknown_without_doctor(tmp_path):
    spy = Spy(stdout=doctor())
    auth = codex_auth({"CODEX_ACCESS_TOKEN": "x"}, cwd=tmp_path, runner=spy)
    assert auth.state == UNKNOWN and "CODEX_ACCESS_TOKEN" in auth.reason
    assert spy.calls == []


def test_codex_env_empty_values_fall_through_to_doctor(tmp_path):
    spy = Spy(stdout=doctor())
    auth = codex_auth({name: "" for name in CODEX_AUTH_ENV}, cwd=tmp_path, runner=spy)
    assert auth.state == SUBSCRIPTION
    assert len(spy.calls) == 1


def test_codex_doctor_openai_chatgpt_is_subscription(tmp_path):
    spy = Spy(stdout=doctor())
    auth = codex_auth(codex_home(tmp_path), cwd=tmp_path, runner=spy)
    assert auth.state == SUBSCRIPTION
    assert "0.162.0" in auth.reason
    assert spy.calls[0][0] == ["codex", "doctor", "--json"]


def test_codex_managed_provider_metered(tmp_path):
    # User files say openai + chatgpt; a managed/system/cloud layer made the effective provider azure.
    env = codex_home(tmp_path)
    assert 'model_provider = "openai"' in (Path(env["CODEX_HOME"]) / "config.toml").read_text()
    auth = codex_auth(env, cwd=tmp_path, runner=Spy(stdout=doctor(provider="azure")))
    assert auth.state == METERED
    assert "azure" in auth.reason


@pytest.mark.parametrize(
    "fields",
    [{"mode": "apiKey"}, {"stored_key": "true"}, {"mode": "amazonBedrock"}],
    ids=["mode-apikey", "stored-key-true", "mode-bedrock"],
)
def test_codex_doctor_apikey_auth_metered(tmp_path, fields):
    auth = codex_auth({}, cwd=tmp_path, runner=Spy(stdout=doctor(**fields)))
    assert auth.state == METERED


@pytest.mark.parametrize(
    "spy",
    [
        Spy(raises=FileNotFoundError("codex")),
        Spy(raises=subprocess.TimeoutExpired(["codex"], 30)),
        Spy(stdout=doctor(), returncode=1),
        Spy(stdout="not json"),
        Spy(stdout="[]"),
        Spy(stdout=doctor(schema=2)),
        Spy(stdout=doctor(schema=True)),
        Spy(stdout=doctor(provider=None)),
        Spy(stdout=doctor(cl_status="warn")),
        Spy(stdout=doctor(ac_status="warning")),
        Spy(stdout=json.dumps({"schemaVersion": 1, "checks": {}})),
    ],
    ids=["missing", "timeout", "exit1", "nonjson", "list", "schema2", "schema-bool",
         "no-provider", "config-warn", "auth-warn", "no-checks"],
)
def test_codex_doctor_failure_unknown(tmp_path, spy):
    assert codex_auth({}, cwd=tmp_path, runner=spy).state == UNKNOWN


def test_codex_unknown_is_not_cached(tmp_path):
    failing, working = Spy(raises=subprocess.TimeoutExpired(["codex"], 30)), Spy(stdout=doctor())
    assert codex_auth({}, cwd=tmp_path, runner=failing).state == UNKNOWN
    assert codex_auth({}, cwd=tmp_path, runner=working).state == SUBSCRIPTION


def test_codex_doctor_uses_run_cwd_and_env(tmp_path):
    spy = Spy(stdout=doctor())
    workspace = tmp_path / "ws"
    workspace.mkdir()
    env = {"PATH": "/bin", "HOME": "/h", "CODEX_HOME": "/c"}
    codex_auth(env, cwd=workspace, runner=spy, timeout_s=12)
    _, kwargs = spy.calls[0]
    assert kwargs["cwd"] == str(workspace)
    assert kwargs["env"] == env
    assert kwargs["timeout"] == 12


def test_probe_passes_cwd_and_env_through(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(harness_auth, "codex_auth",
                        lambda env=None, *, cwd, **kw: seen.setdefault("codex", (env, cwd)) and None
                        or harness_auth.HarnessAuth(SUBSCRIPTION, "c"))
    monkeypatch.setattr(harness_auth, "claude_auth",
                        lambda env=None, **kw: seen.setdefault("claude", env) and None
                        or harness_auth.HarnessAuth(SUBSCRIPTION, "k"))
    env = {"A": "1"}
    result = probe_harness_auth(["Codex", "CLAUDE", "opencode", "codex"], env, cwd=tmp_path)
    assert set(result) == {"claude", "codex"}
    assert seen["codex"] == (env, tmp_path)
    assert seen["claude"] == env
    assert probe_harness_auth(["opencode"], env, cwd=tmp_path) == {}
    assert probe_harness_auth([], env, cwd=tmp_path) == {}


def test_codex_cache_key_includes_cwd(tmp_path):
    spy = Spy(stdout=doctor())
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    env = {"CODEX_HOME": "/c"}
    codex_auth(env, cwd=a, runner=spy)
    codex_auth(env, cwd=a, runner=spy)
    assert len(spy.calls) == 1
    codex_auth(env, cwd=b, runner=spy)
    assert len(spy.calls) == 2


def test_codex_cache_key_includes_codex_home(tmp_path):
    spy = Spy(stdout=doctor())
    codex_auth({"CODEX_HOME": "/one"}, cwd=tmp_path, runner=spy)
    codex_auth({"CODEX_HOME": "/two"}, cwd=tmp_path, runner=spy)
    assert len(spy.calls) == 2


# ---------------------------------------------------------------------------
# C9: no secret value in any reason
# ---------------------------------------------------------------------------


def test_reasons_hold_no_secret(tmp_path):
    def fresh_codex(*args, **kwargs):
        # Same key every time: without a clear, the first definitive result would answer all.
        clear_codex_auth_cache()
        return codex_auth(*args, **kwargs)

    env = codex_home(tmp_path)
    leaky = {"auth file": TOKEN, "access_token": TOKEN, "model provider detail": TOKEN}
    reasons = [
        claude_auth({"ANTHROPIC_API_KEY": TOKEN}, runner=Spy()).reason,
        claude_auth({}, runner=Spy(stdout=claude_status(authMethod="console", orgId=TOKEN,
                                                         email=TOKEN))).reason,
        claude_auth({}, runner=Spy(stdout=claude_status(email=TOKEN, orgId=TOKEN))).reason,
        claude_auth({}, runner=Spy(stdout=claude_status(authMethod=TOKEN))).reason,
        claude_auth({}, runner=Spy(stdout=TOKEN, returncode=0)).reason,
        fresh_codex({**env, "OPENAI_API_KEY": TOKEN}, cwd=tmp_path, runner=Spy()).reason,
        fresh_codex({**env, "CODEX_API_KEY": TOKEN}, cwd=tmp_path, runner=Spy()).reason,
        fresh_codex({**env, "CODEX_ACCESS_TOKEN": TOKEN}, cwd=tmp_path, runner=Spy()).reason,
        fresh_codex(env, cwd=tmp_path, runner=Spy(stdout=doctor(extra=leaky))).reason,
        fresh_codex(env, cwd=tmp_path, runner=Spy(stdout=doctor(provider=TOKEN, extra=leaky))).reason,
        fresh_codex(env, cwd=tmp_path, runner=Spy(stdout=doctor(mode=TOKEN, extra=leaky))).reason,
        fresh_codex(env, cwd=tmp_path, runner=Spy(stdout=doctor(version=TOKEN, extra=leaky))).reason,
        fresh_codex(env, cwd=tmp_path, runner=Spy(stdout=TOKEN)).reason,
        fresh_codex(env, cwd=tmp_path, runner=Spy(raises=OSError(TOKEN))).reason,
    ]
    assert all(reasons)
    assert not [r for r in reasons if TOKEN in r]


# ---------------------------------------------------------------------------
# Real subprocess wiring: fake executables on a private PATH
# ---------------------------------------------------------------------------

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="shell-script fakes")


def _fake(bin_dir: Path, name: str, stdout: str, record: Path) -> None:
    script = bin_dir / name
    script.write_text(f"#!/bin/sh\npwd > '{record}'\nprintf '%s' '{stdout}'\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)


@posix_only
def test_real_subprocess_claude(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake(bin_dir, "claude", claude_status(), tmp_path / "rec")
    env = {"PATH": f"{bin_dir}{os.pathsep}/bin{os.pathsep}/usr/bin"}
    assert claude_auth(env).state == SUBSCRIPTION


@posix_only
def test_real_subprocess_codex_runs_in_cwd(tmp_path):
    bin_dir, ws = tmp_path / "bin", tmp_path / "ws"
    bin_dir.mkdir(), ws.mkdir()
    record = tmp_path / "rec"
    _fake(bin_dir, "codex", doctor(), record)
    env = {"PATH": f"{bin_dir}{os.pathsep}/bin{os.pathsep}/usr/bin"}
    assert codex_auth(env, cwd=ws).state == SUBSCRIPTION
    assert Path(record.read_text().strip()).resolve() == ws.resolve()
