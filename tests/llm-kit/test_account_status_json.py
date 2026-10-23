"""``llm-kit account status --json``: the structured credential contract.

``plugins/llm-kit/custom_bootstrap.py`` reads this document instead of parsing
the prose ``status`` output or inferring a state from the exit code (which is 1
for a missing key, a rejected key and no credit alike). Each credential state is
a distinct ``state`` value, and the exit code is 0 whenever a document was
printed. The key never appears in the output; ``key_sha256`` identifies it.

Every test runs the real CLI in this process under a temporary HOME with a fake
transport (``llm_kit.adapters.account._get``) and a guard that fails any real
``urlopen``.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import llm_kit.cli as llm_kit_cli
from llm_kit.adapters import account as account_mod
from llm_kit.cli import account as account_cli
from llm_kit.registry import api_key as api_key_mod
from llm_kit.registry import constants
from llm_kit.registry.env_file import write_env_file

LIB = Path(__file__).resolve().parents[2] / "packages" / "katsoft-llm-kit" / "src"
KEY = "sk-or-v1-contract-key"
KEY_SHA = hashlib.sha256(KEY.encode("utf-8")).hexdigest()
DOCUMENT_FIELDS = {
    "schema", "state", "endpoint", "base_url", "key_env", "source", "source_path", "key_sha256",
    "label", "usage", "limit", "is_free_tier", "rate_limit", "error", "set_key_command",
    "user_env_file",
}


class FakeNetwork:
    def __init__(self):
        self.calls: list[str] = []
        self.reply = (json.dumps({"data": {"label": "fake-label", "usage": 1.5, "limit": 10,
                                           "is_free_tier": False,
                                           "rate_limit": {"requests": 200, "interval": "10s"}}}).encode(), None)

    def __call__(self, url, headers, timeout):
        self.calls.append(url)
        return self.reply

    def http_error(self, code):
        self.reply = (b"", urllib.error.HTTPError("u", code, "x", {}, None))


@pytest.fixture
def world(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_BOOTSTRAP_DATA_ROOT", raising=False)
    user_env = home / ".claude" / "plugins" / "data" / "plugins-kit" / "llm-scripting-kit" / ".env"
    for module in (constants, api_key_mod, account_cli):
        monkeypatch.setattr(module, "USER_ENV_FILE", user_env)
    # llm-kit reads and writes the user key file at $LLM_KIT_ENV_FILE when it is set.
    monkeypatch.setenv("LLM_KIT_ENV_FILE", str(user_env))
    network = FakeNetwork()
    monkeypatch.setattr(account_mod, "_get", network)

    def real_urlopen(*_a, **_k):
        raise AssertionError("a real network call was attempted")

    monkeypatch.setattr(urllib.request, "urlopen", real_urlopen)
    project = tmp_path / "project"
    project.mkdir()
    return {"user_env": user_env, "network": network, "project": project, "tmp": tmp_path}


def status(world, capsys, *flags):
    capsys.readouterr()
    code = llm_kit_cli.main(["account", "status", "--json", "--endpoint", "openrouter",
                             "--project-root", str(world["project"]), *flags])
    out = capsys.readouterr()
    return code, json.loads(out.out), out


def with_key(world):
    write_env_file(world["user_env"], {"OPENROUTER_API_KEY": KEY})


def test_missing_key(world, capsys):
    code, doc, _ = status(world, capsys)
    assert code == 0
    assert doc["state"] == "missing_key"
    assert doc["key_sha256"] is None and doc["source"] == "missing" and doc["source_path"] is None
    assert doc["set_key_command"] == f"{constants.cli_command()} account set-key"
    assert doc["user_env_file"] == str(world["user_env"])
    assert world["network"].calls == []


def test_key_present_without_validation_makes_no_request(world, capsys):
    with_key(world)
    code, doc, _ = status(world, capsys, "--no-validate")
    assert code == 0 and doc["state"] == "key_present"
    assert doc["key_sha256"] == KEY_SHA and doc["source"] == "user"
    assert doc["source_path"] == str(world["user_env"])
    assert world["network"].calls == []


def test_ok_carries_the_account_fields(world, capsys):
    with_key(world)
    code, doc, _ = status(world, capsys)
    assert code == 0 and doc["state"] == "ok"
    assert (doc["label"], doc["usage"], doc["limit"], doc["is_free_tier"]) == ("fake-label", 1.5, 10, False)
    assert doc["rate_limit"] == {"requests": 200, "interval": "10s"}
    assert doc["error"] is None and doc["key_sha256"] == KEY_SHA
    assert world["network"].calls == [f"{constants.BASE_URL}/auth/key"]


def test_rejected_key(world, capsys):
    with_key(world)
    world["network"].http_error(401)
    code, doc, _ = status(world, capsys)
    assert code == 0 and doc["state"] == "rejected_key"
    assert doc["key_sha256"] == KEY_SHA and doc["label"] is None


def test_no_credit(world, capsys):
    with_key(world)
    world["network"].http_error(402)
    code, doc, _ = status(world, capsys)
    assert code == 0 and doc["state"] == "no_credit"
    assert doc["key_sha256"] == KEY_SHA


@pytest.mark.parametrize("failure", [urllib.error.URLError("offline"), None])
def test_check_failed_names_the_error(world, capsys, failure):
    with_key(world)
    world["network"].reply = (b"", failure) if failure else (b"", urllib.error.HTTPError("u", 500, "boom", {}, None))
    code, doc, _ = status(world, capsys)
    assert code == 0 and doc["state"] == "check_failed"
    assert doc["error"] and doc["key_sha256"] == KEY_SHA


def test_not_validated_when_the_endpoint_declares_no_check(world, capsys, monkeypatch):
    with_key(world)
    monkeypatch.setattr(account_cli, "validate_endpoint", lambda ep, key: None)
    code, doc, _ = status(world, capsys)
    assert code == 0 and doc["state"] == "not_validated" and doc["key_sha256"] == KEY_SHA


def test_every_state_is_a_distinct_value_of_one_field(world, capsys):
    seen = {}
    seen["missing_key"] = status(world, capsys)[1]["state"]
    with_key(world)
    seen["key_present"] = status(world, capsys, "--no-validate")[1]["state"]
    seen["ok"] = status(world, capsys)[1]["state"]
    world["network"].http_error(401)
    seen["rejected_key"] = status(world, capsys)[1]["state"]
    world["network"].http_error(402)
    seen["no_credit"] = status(world, capsys)[1]["state"]
    world["network"].reply = (b"", urllib.error.URLError("offline"))
    seen["check_failed"] = status(world, capsys)[1]["state"]
    assert seen == {name: name for name in seen}
    assert set(seen) <= set(account_cli.STATES) and len(set(seen.values())) == 6


def test_the_document_shape_and_the_key_never_leaks(world, capsys):
    with_key(world)
    _, doc, out = status(world, capsys)
    assert set(doc) == DOCUMENT_FIELDS and doc["schema"] == account_cli.STATUS_SCHEMA
    assert KEY not in out.out and KEY not in out.err
    assert out.out.count("\n") == 1, "exactly one JSON line on stdout"


def test_an_unresolvable_endpoint_is_a_usage_error_without_a_document(world, capsys):
    capsys.readouterr()
    code = llm_kit_cli.main(["account", "status", "--json", "--endpoint", "no-such-endpoint",
                             "--project-root", str(world["project"])])
    out = capsys.readouterr()
    assert code == 2 and out.out == "" and "Endpoint error" in out.err


def test_the_prose_status_is_unchanged_without_the_flag(world, capsys):
    with_key(world)
    capsys.readouterr()
    code = llm_kit_cli.main(["account", "status", "--endpoint", "openrouter",
                             "--project-root", str(world["project"])])
    out = capsys.readouterr().out
    assert code == 0 and "Status: OK" in out and "Label: fake-label" in out
    world["network"].http_error(401)
    code = llm_kit_cli.main(["account", "status", "--endpoint", "openrouter",
                             "--project-root", str(world["project"])])
    assert code == 1


@pytest.mark.skipif(sys.platform == "win32", reason="the launched interpreter is the test's own, via PYTHONPATH")
def test_the_real_process_prints_the_document(tmp_path):
    """The command line the bootstrap script builds, run as a real subprocess (no network: no key)."""
    home = tmp_path / "home"
    home.mkdir()
    import site
    env = {k: v for k, v in os.environ.items() if k not in {"OPENROUTER_API_KEY", "CLAUDE_BOOTSTRAP_DATA_ROOT"}}
    env.update({
        "HOME": str(home), "USERPROFILE": str(home), "_BOOTSTRAP_GUARD_SKIP_REEXEC": "1",
        "PYTHONPATH": os.pathsep.join([str(LIB), *site.getsitepackages()]),
    })
    done = subprocess.run(
        [sys.executable, "-m", "llm_kit.cli", "account", "status", "--json", "--endpoint", "openrouter",
         "--project-root", str(tmp_path), "--no-validate"],
        capture_output=True, text=True, env=env, timeout=60)
    assert done.returncode == 0, done.stderr
    doc = json.loads(done.stdout)
    assert doc["state"] == "missing_key" and set(doc) == DOCUMENT_FIELDS
