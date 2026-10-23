"""Protocol 2 of ``llm-kit completion complete --request-file -``: one call from the project model file.

The three completion backends run for real (``OpenRouterBackend``, ``ClaudeCliBackend``,
``CodexCliBackend``) with fake transports in place of the network and of the CLIs, so
the request each one builds is the request it would send. No paid call is made, no
harness is started, and every test runs under a throwaway HOME.

What each test pins, and what reverting it breaks, is stated where it is not obvious.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import sys
import types
from pathlib import Path
from typing import Any, Optional

import pytest

import llm_kit.cli as cli
from llm_kit.adapters import backends as backends_mod
from llm_kit.adapters import harness_auth
from llm_kit.completion.types import BackendOptions, LLMResponse
from llm_kit.dispatch import factory
from llm_kit.registry import consent as consent_mod
from llm_kit.registry import cost, project_models as pm
from llm_kit.registry.effort import HarnessAdapterError
from test_cli_goldens import _check, _form
from test_cli_harness import home, run, write  # noqa: F401 -- home is a fixture

_DOC = {
    "version": 1,
    "catalog": [
        {"id": "luna", "label": "Luna", "endpoint": "luna", "model": "gpt-6-luna"},
        {"id": "sonnet", "label": "Sonnet", "endpoint": "sonnet", "model": "claude-sonnet-5-5"},
        {
            "id": "deepseek", "label": "DeepSeek Flash", "endpoint": "openrouter",
            "model": "deepseek/deepseek-v4.1-flash", "efforts": ["none", "low", "high", "max"],
            "request_defaults": {"extras": {"provider": {"sort": "latency"}}},
        },
    ],
    "selections": {
        "draft.generate.primary": {"model": "luna", "effort": "high"},
        "art": {"model": "deepseek", "effort": "max"},
        "chat": {"model": "sonnet", "effort": None},
    },
}

_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}


# --- fake transports -------------------------------------------------------------------------


class World:
    """Everything a protocol 2 call can reach, replaced by a recorder."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.http: list[dict] = []
        self.claude: list[list] = []
        self.codex: list[list] = []
        self.codex_cwds: list[Path] = []
        self.codex_writes: dict[str, str] = {}
        self.codex_answer = "answer"
        self.http_model = "deepseek/deepseek-v4.1-flash"
        self.http_cost: Optional[float] = 0.0007
        self.claude_models: Optional[dict] = {"claude-sonnet-5-5": {"outputTokens": 5}}
        self.claude_cost: Optional[float] = 0.0123
        self.claude_answer = "answer"
        self.http_error: Optional[BaseException] = None
        self.claude_raw: Optional[str] = None

    # --- the OpenAI-SDK-shaped client
    def client(self) -> Any:
        world = self

        def create(**kwargs: Any) -> Any:
            world.http.append(kwargs)
            if world.http_error is not None:
                raise world.http_error
            usage = types.SimpleNamespace(
                prompt_tokens=11, completion_tokens=7, prompt_tokens_details={"cached_tokens": 4},
                **({"cost": world.http_cost} if world.http_cost is not None else {}),
            )
            message = types.SimpleNamespace(content="answer", reasoning_content=None)
            choice = types.SimpleNamespace(message=message, finish_reason="stop")
            return types.SimpleNamespace(choices=[choice], usage=usage, model=world.http_model)

        client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
        client.with_options = lambda **_: client
        return client

    # --- the claude CLI runner
    def claude_runner(self, cmd, request, cwd, **_kw):
        self.claude.append(list(cmd))
        if self.claude_raw is not None:
            return self.claude_raw, "", 0
        envelope: dict = {"result": self.claude_answer, "usage": {"input_tokens": 3, "output_tokens": 5, "cache_read_input_tokens": 1}}
        if self.claude_cost is not None:
            envelope["total_cost_usd"] = self.claude_cost
        if self.claude_models is not None:
            envelope["modelUsage"] = self.claude_models
        return json.dumps(envelope), "", 0

    # --- the codex CLI runner
    def codex_runner(self, cmd, request, cwd, **_kw):
        self.codex.append(list(cmd))
        self.codex_cwds.append(Path(cwd))
        if cmd[-2:] == ["debug", "models"]:
            slug = cmd[cmd.index("-m") + 1] if "-m" in cmd else "gpt-6-luna"
            return json.dumps({"models": [{"slug": "gpt-6-luna"}, {"slug": slug}]}), "", 0
        Path(cmd[cmd.index("-o") + 1]).write_text(self.codex_answer, encoding="utf-8")
        target = Path(cmd[cmd.index("-C") + 1])
        for name, text in self.codex_writes.items():
            (target / name).parent.mkdir(parents=True, exist_ok=True)
            (target / name).write_text(text, encoding="utf-8")
        return "", "tokens used\n1,234\n", 0

    @property
    def runner_calls(self) -> int:
        return len(self.http) + len(self.claude) + len(self.codex)


@pytest.fixture()
def world(home, tmp_path, monkeypatch):  # noqa: F811
    root = tmp_path / "proj"
    root.mkdir()
    pm.write_project_models(_DOC, project_root=str(root))
    w = World(root)
    real = factory.create_backend

    def build(endpoint=None, **kwargs):
        selection = real(endpoint, **kwargs)
        backend = selection.backend
        if backend.name == "claude-cli":
            backend = dataclasses.replace(backend, runner=w.claude_runner, executable="claude")
        elif backend.name == "codex-cli":
            backend = dataclasses.replace(backend, argv_prefix=("codex",), runner=w.codex_runner)
        elif backend.name == "openrouter":
            backend = dataclasses.replace(backend, client=w.client())
        else:  # pragma: no cover - a backend this suite does not fake must not start a process
            raise AssertionError(f"unexpected backend {backend.name}")
        return dataclasses.replace(selection, backend=backend)

    monkeypatch.setattr(factory, "create_backend", build)
    monkeypatch.setattr(
        harness_auth, "probe_harness_auth",
        lambda harnesses, env=None, *, cwd: {h.lower(): cost.HarnessAuth(cost.SUBSCRIPTION, "fake subscription") for h in harnesses},
    )
    w.monkeypatch = monkeypatch
    return w


def allow(*, subscription: bool = True, metered: bool = True) -> None:
    """Write the machine's consent file (the global policy a request without the consent field is held to)."""
    consent_mod.update_config({"subscription": subscription, "metered": metered})


def call(world: World, capsys, **request: Any):
    payload = {"protocol": 2, "project_root": str(world.root), "prompt": "do it", **request}
    out = run(cli.main, ["completion", "complete", "--request-file", "-"], capsys,
              stdin=json.dumps(payload), monkeypatch=world.monkeypatch)
    doc = json.loads(out.out) if out.out.strip() else None
    err = json.loads(out.err) if out.err.strip() else None
    return out.code, doc, err


# --- the three completion backends ---------------------------------------------------------------


def test_openrouter_completion_result_shape(world, capsys):
    allow()
    code, doc, err = call(world, capsys, selection="art")
    assert code == 0 and err is None, (doc, err)
    assert doc["protocol"] == 2 and doc["status"] == "completed" and doc["mode"] == "completion"
    assert doc["selection"] == {"id": "art", "model": "deepseek"}
    resolved = doc["resolved"]
    assert (resolved["endpoint"], resolved["adapter"]) == ("openrouter", "openrouter")
    assert resolved["requested_model"] == "deepseek/deepseek-v4.1-flash"
    assert resolved["reported_model"] == "deepseek/deepseek-v4.1-flash" and resolved["model_verified"] is True
    assert resolved["effort"] == {"requested": "max", "sent": "max", "source": "selection"}
    assert doc["output"] == {"text": "answer", "structured": None}
    assert doc["usage"]["input_tokens"] == 11 and doc["usage"]["output_tokens"] == 7
    assert doc["usage"]["cache_hit_tokens"] == 4 and isinstance(doc["usage"]["wall_ms"], int)
    # an unmarked endpoint's native usage.cost is an ESTIMATE; the authoritative amount is unknown, not zero
    assert doc["cost"] == {
        "class": "metered", "reason": "no registry entry: billing undeclared",
        "usd": None, "source": None,
        "estimated_usd": 0.0007, "estimated_source": "openrouter.usage.cost",
    }
    assert doc["consent"] == {"opted_in": False, "applied": False, "scope": None}
    assert "files" not in doc
    assert doc["params"]["forwarded"] == ["extras.provider"]  # the row's request default reached the request, unvalidated
    assert world.http[0]["extra_body"] == {"provider": {"sort": "latency"}, "reasoning_effort": "max"}


@pytest.mark.parametrize("effort", ["low", "high", "max", "none"])
def test_deepseek_effort_reaches_the_wire(world, capsys, effort):
    """Every effort of the measured DeepSeek menu is delivered in the request body.

    Red on revert: drop `effort_style` from the shipped `openrouter` endpoint
    (registry/models.py) and the call is refused as undeliverable before any request.
    """
    allow()
    code, doc, err = call(world, capsys, model="deepseek", effort=effort)
    assert code == 0, (doc, err)
    assert world.http[0]["extra_body"]["reasoning_effort"] == effort
    assert doc["resolved"]["effort"] == {"requested": effort, "sent": effort, "source": "request"}


def test_a_served_model_that_differs_from_the_request_is_reported_and_not_verified(world, capsys):
    """Red on revert: let the adapter echo the requested model as the reported one and this stays green-by-accident."""
    allow()
    world.http_model = "deepseek/some-other-model"
    _code, doc, _ = call(world, capsys, selection="art")
    assert doc["resolved"]["requested_model"] == "deepseek/deepseek-v4.1-flash"
    assert doc["resolved"]["reported_model"] == "deepseek/some-other-model"
    assert doc["resolved"]["model_verified"] is False
    world.http_model = ""  # the downstream stated nothing: nothing is invented
    _code, doc, _ = call(world, capsys, selection="art")
    assert doc["resolved"]["reported_model"] is None and doc["resolved"]["model_verified"] is False


def test_deepseek_effort_null_sends_none_and_medium_is_refused(world, capsys):
    allow()
    code, doc, _ = call(world, capsys, selection="art", effort=None)
    assert code == 0 and "reasoning_effort" not in world.http[0].get("extra_body", {})
    assert doc["resolved"]["effort"] == {"requested": None, "sent": None, "source": "request"}
    before = len(world.http)
    code, doc, err = call(world, capsys, model="deepseek", effort="medium")  # the vendor lists no medium
    assert code == 4 and doc is None and err["error"]["kind"] == "effort"
    assert len(world.http) == before


def test_the_shipped_openrouter_endpoint_declares_the_style():
    from llm_kit.registry.models import DEFAULT_MODEL_CONFIG

    assert DEFAULT_MODEL_CONFIG["endpoints"]["openrouter"]["effort_style"] == "top-level"


def test_claude_completion_result_and_estimated_cost(world, capsys):
    allow()
    code, doc, err = call(world, capsys, selection="chat")
    assert code == 0, (doc, err)
    resolved = doc["resolved"]
    assert resolved["adapter"] == "claude-cli" and resolved["requested_model"] == "claude-sonnet-5-5"
    assert resolved["reported_model"] == "claude-sonnet-5-5" and resolved["model_verified"] is True
    assert resolved["effort"] == {"requested": None, "sent": None, "source": "none"}
    # the CLI's total_cost_usd is not pricing: it is only ever an estimate with a named source
    assert doc["cost"] == {
        "class": "subscription", "reason": "claude on subscription auth: fake subscription",
        "usd": None, "source": None,
        "estimated_usd": 0.0123, "estimated_source": "claude-cli.total_cost_usd",
    }
    argv = world.claude[0]
    assert "--effort" not in argv
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"  # a completion arms text-only mode
    assert "bypassPermissions" not in argv


def test_claude_without_a_single_reported_model_is_not_verified(world, capsys):
    allow()
    world.claude_models = {"claude-sonnet-5-5": {}, "claude-haiku-5-5": {}}
    world.claude_cost = None
    _code, doc, _ = call(world, capsys, selection="chat")
    assert doc["resolved"]["reported_model"] is None and doc["resolved"]["model_verified"] is False
    assert doc["cost"]["estimated_usd"] is None and doc["cost"]["estimated_source"] is None


def test_codex_completion_arms_text_only_and_reports_no_served_model(world, capsys):
    allow()
    code, doc, err = call(world, capsys, selection="draft.generate.primary")
    assert code == 0, (doc, err)
    resolved = doc["resolved"]
    assert resolved["adapter"] == "codex-cli" and resolved["requested_model"] == "gpt-6-luna"
    assert resolved["reported_model"] is None and resolved["model_verified"] is False  # codex states none
    assert resolved["effort"] == {"requested": "high", "sent": "high", "source": "selection"}
    assert doc["usage"]["total_tokens"] == 1234
    assert doc["cost"]["class"] == "subscription" and doc["cost"]["usd"] is None
    assert world.codex[0][-2:] == ["debug", "models"]  # text-only mode read the catalogue first
    exec_argv = world.codex[1]
    assert "model_reasoning_effort=high" in exec_argv and "read-only" in exec_argv


def test_absent_codex_config_keeps_the_existing_workspace_argv(world, capsys):
    allow()
    (world.root / "work").mkdir()
    code, _doc, err = call(world, capsys, model="luna", mode="workspace", working_dir="work")
    argv = world.codex[-1]
    assert argv == [
        "codex", "exec", "-s", "workspace-write", "-c",
        "sandbox_workspace_write.network_access=true", "-m", "gpt-6-luna", "-C",
        str((world.root / "work").resolve()), "-o", argv[argv.index("-o") + 1],
        "--json", "--skip-git-repo-check", "--color", "never", "-",
    ]
    assert code == 0, err


@pytest.mark.parametrize("inherit", ["none", "core"])
def test_codex_config_emits_only_requested_allowlisted_overrides(world, capsys, inherit):
    allow()
    (world.root / "work").mkdir()
    document = {**_DOC, "catalog": [dict(row) for row in _DOC["catalog"]]}
    luna = next(row for row in document["catalog"] if row["id"] == "luna")
    opposite = "core" if inherit == "none" else "none"
    luna["request_defaults"] = {"extras": {"codex_config": {"shell_environment_policy.inherit": opposite}}}
    pm.write_project_models(document, project_root=str(world.root))
    code, _doc, err = call(
        world, capsys, model="luna", mode="workspace", working_dir="work",
        codex_config={"shell_environment_policy.inherit": inherit, "approval_policy": "never"},
    )
    assert code == 0, err
    argv = world.codex[-1]
    assert argv[argv.index("-c") + 1] == "sandbox_workspace_write.network_access=true"
    assert f'shell_environment_policy.inherit="{inherit}"' in argv
    assert 'approval_policy="never"' in argv
    assert f'shell_environment_policy.inherit="{opposite}"' not in argv


@pytest.mark.parametrize(("key", "value"), [
    ("shell_environment_policy.inherit", "all"),
    ("shell_environment_policy.inherit", "on-request"),
    ("shell_environment_policy.inherit", "danger-full-access"),
    ("shell_environment_policy.inherit", "never\nmodel=bad"),
    ("approval_policy", "on-request"),
    ("approval_policy", "on-failure"),
    ("approval_policy", "untrusted"),
])
def test_protocol2_refuses_prohibited_codex_config_before_dispatch(world, capsys, key, value):
    (world.root / "work").mkdir()
    code, _doc, err = call(
        world, capsys, model="luna", mode="workspace", working_dir="work",
        codex_config={key: value},
    )
    assert code == 4 and err["error"]["kind"] == "configuration"
    assert world.runner_calls == 0


def test_protocol2_refuses_images_on_non_image_adapter_before_dispatch(world, capsys):
    (world.root / "picture.png").write_bytes(b"png")
    code, _doc, err = call(world, capsys, model="deepseek", images=["picture.png"])
    assert code == 4 and err["error"]["kind"] == "mode"
    assert world.runner_calls == 0


def test_codex_inherit_is_refused_in_text_only_mode(world, capsys):
    code, _doc, err = call(
        world, capsys, model="luna",
        codex_config={"shell_environment_policy.inherit": "core"},
    )
    assert code == 4 and err["error"]["kind"] == "configuration"
    assert world.runner_calls == 0


def test_codex_config_refuses_an_unsafe_effective_row_sandbox(world, capsys):
    document = {**_DOC, "catalog": [dict(row) for row in _DOC["catalog"]]}
    luna = next(row for row in document["catalog"] if row["id"] == "luna")
    luna["request_defaults"] = {"extras": {"sandbox": "danger-full-access"}}
    pm.write_project_models(document, project_root=str(world.root))
    code, _doc, err = call(
        world, capsys, model="luna", mode="workspace", working_dir="work",
        codex_config={"approval_policy": "never"},
    )
    assert code == 4 and err["error"]["kind"] == "configuration"
    assert world.runner_calls == 0


def test_codex_image_is_attached_and_watch_scope_is_reported(world, capsys):
    allow()
    work = world.root / "work"
    work.mkdir()
    (work / "input.png").write_bytes(b"png")
    (work / "unlisted.txt").write_text("keep", encoding="utf-8")
    world.codex_writes = {"result.txt": "done"}
    code, doc, err = call(
        world, capsys, model="luna", mode="workspace", working_dir="work",
        images=["input.png"], watch_files=["result.txt"],
    )
    assert code == 0, err
    argv = world.codex[-1]
    assert argv[argv.index("-i") + 1] == str(work / "input.png")
    assert doc["manifest"] == {"scope": "watch", "watched_paths": ["result.txt"]}
    assert [item["path"] for item in doc["files"]] == ["result.txt"]


def test_codex_structured_output_under_a_schema(world, capsys):
    allow()
    world.codex_answer = json.dumps({"summary": "done"})
    code, doc, err = call(world, capsys, model="luna", output_schema=_SCHEMA)
    assert code == 0, (doc, err)
    assert doc["output"]["structured"] == {"summary": "done"}


def test_a_schema_the_adapter_cannot_deliver_is_refused_before_the_runner(world, capsys):
    allow()
    loose = {"type": "object", "properties": {"summary": {"type": "string"}}}  # codex accepts strict schemas only
    code, doc, err = call(world, capsys, model="luna", output_schema=loose)
    assert code == 4 and err["error"]["kind"] == "contract" and world.runner_calls == 0
    code, doc, err = call(world, capsys, model="luna", output_schema={"type": "object", "not_a_keyword": 1})
    assert code == 4 and err["error"]["kind"] == "contract"


def test_the_response_carries_the_reported_model_and_estimate_fields():
    names = [f.name for f in dataclasses.fields(LLMResponse)]
    assert names[-6:] == ["output_contract", "skill_context", "reported_model", "estimated_cost_usd", "estimated_cost_source", "reasoning_tokens"]
    blank = LLMResponse(text="t", model="m")
    assert (blank.reported_model, blank.estimated_cost_usd, blank.estimated_cost_source) == (None, None, None)


# --- the Claude exact-model menu -----------------------------------------------------------------


def _claude(runner):
    return backends_mod.ClaudeCliBackend(runner=runner, executable="claude")


@pytest.mark.parametrize("model,effort", [
    ("claude-opus-4-6", "xhigh"),     # this model's table row omits xhigh
    ("claude-sonnet-5-5", "ultra"),   # not on the CLI's scale at all
    ("claude-sonnet-5-5", "none"),    # the CLI takes no explicit disable
    ("opus", "ultra"),                # an alias keeps the harness-wide menu
])
def test_claude_refuses_an_effort_outside_the_exact_model_menu(model, effort):
    """Red on revert: remove the check_effort call in ClaudeCliBackend.complete and the runner runs."""
    calls = []
    with pytest.raises(HarnessAdapterError, match="does not accept effort"):
        _claude(lambda *a, **k: calls.append(a) or ("", "", 0)).complete(
            "s", "u", model=model, options=BackendOptions(effort=effort)
        )
    assert calls == []  # refused before argv and before the runner


def test_claude_sends_an_effort_its_menu_lists_and_omits_null():
    calls = []

    def runner(cmd, request, cwd, **_k):
        calls.append(cmd)
        return json.dumps({"result": "ok", "usage": {}}), "", 0

    backend = _claude(runner)
    backend.complete("s", "u", model="claude-opus-4-8", options=BackendOptions(effort="xhigh"))
    assert calls[0][calls[0].index("--effort") + 1] == "xhigh"
    backend.complete("s", "u", model="claude-opus-4-8", options=BackendOptions())
    assert "--effort" not in calls[1]


def test_protocol_2_refuses_a_claude_effort_before_the_runner(world, capsys):
    allow()
    code, doc, err = call(world, capsys, model="sonnet", effort="ultra")
    assert code == 4 and doc is None and err["error"]["kind"] == "effort" and world.runner_calls == 0
    code, doc, err = call(world, capsys, model="sonnet", effort="max")
    assert code == 0 and world.claude[-1][world.claude[-1].index("--effort") + 1] == "max"


# --- workspace mode and the file manifest -------------------------------------------------------------


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def test_codex_workspace_returns_a_manifest_of_created_and_modified_files(world, capsys):
    allow()
    work = world.root / "work"
    (work / "sub").mkdir(parents=True)
    (work / "keep.txt").write_text("unchanged")
    (work / "notes.txt").write_text("old")
    world.codex_writes = {"poster.png": "PNGDATA", "notes.txt": "new notes", "sub/b.txt": "bb", "keep.txt": "unchanged"}
    code, doc, err = call(world, capsys, selection="draft.generate.primary", mode="workspace",
                          working_dir="work", expect_files=["poster.png"])
    assert code == 0, (doc, err)
    assert doc["mode"] == "workspace" and doc["status"] == "completed"
    # created or modified, never the file the run rewrote with identical bytes; by relative path, in order
    assert doc["files"] == [
        {"path": "notes.txt", "size": 9, "sha256": _sha("new notes")},
        {"path": "poster.png", "size": 7, "sha256": _sha("PNGDATA")},
        {"path": "sub/b.txt", "size": 2, "sha256": _sha("bb")},
    ]
    exec_argv = world.codex[-1]
    assert exec_argv[exec_argv.index("-C") + 1] == str((world.root / "work").resolve())  # the absolute directory
    assert world.codex[0][-2:] != ["debug", "models"]  # workspace mode is not text-only
    assert "workspace-write" in exec_argv


def test_a_missing_expected_file_fails_the_call_and_still_reports_the_files(world, capsys):
    """Red on revert: drop the check_expected branch in dispatch/invoke.py and the call reads as completed."""
    allow()
    world.codex_writes = {"other.txt": "x"}
    (world.root / "work" / "adir").mkdir(parents=True)
    code, doc, err = call(world, capsys, model="luna", mode="workspace", working_dir="work",
                          expect_files=["poster.png", "adir"])
    assert code == 1 and err is None
    assert doc["status"] == "failed" and doc["error"]["code"] == "expected-files-missing"
    assert "poster.png" in doc["error"]["message"] and "adir" in doc["error"]["message"]
    assert [f["path"] for f in doc["files"]] == ["other.txt"]
    assert doc["output"]["text"] == "answer"  # the model's text is still reported


def test_workspace_is_refused_where_the_adapter_has_no_such_mode(world, capsys):
    allow()
    (world.root / "work").mkdir()
    for model in ("sonnet", "deepseek"):
        code, doc, err = call(world, capsys, model=model, mode="workspace", working_dir="work")
        assert code == 4 and err["error"]["kind"] == "mode", model
        assert "does not support the workspace mode" in err["error"]["message"]
    assert world.runner_calls == 0


def test_unknown_modes_and_misplaced_workspace_keys_are_refused(world, capsys):
    allow()
    for mode in ("image", "build", "Workspace"):
        code, _doc, err = call(world, capsys, model="luna", mode=mode)
        assert code == 4 and err["error"]["kind"] == "mode"
    code, _doc, err = call(world, capsys, model="luna", mode="workspace")  # no working_dir
    assert code == 4 and err["error"]["kind"] == "path"
    code, _doc, err = call(world, capsys, model="luna", working_dir="work")  # not a workspace call
    assert code == 4 and err["error"]["kind"] == "path"
    code, _doc, err = call(world, capsys, model="luna", expect_files=["a"])
    assert code == 4 and err["error"]["kind"] == "path"
    assert world.runner_calls == 0


@pytest.mark.parametrize("working_dir", ["../outside", "/", "nope", "link"])
def test_working_dir_is_confined_to_an_existing_directory_beneath_the_root(world, capsys, working_dir, tmp_path):
    allow()
    (tmp_path / "outside").mkdir()
    try:
        os.symlink(tmp_path / "outside", world.root / "link")
    except OSError:  # pragma: no cover - symlinks unavailable
        pytest.skip("no symlinks")
    code, _doc, err = call(world, capsys, model="luna", mode="workspace", working_dir=working_dir)
    assert code == 4 and err["error"]["kind"] == "path" and world.runner_calls == 0


@pytest.mark.parametrize("bad", ["../x", "/abs", "a\\b", "", "a/../../b", ".", 3])
def test_expect_files_must_be_relative_and_beneath_the_directory(world, capsys, bad):
    allow()
    (world.root / "work").mkdir()
    code, _doc, err = call(world, capsys, model="luna", mode="workspace", working_dir="work", expect_files=[bad])
    assert code == 4 and err["error"]["kind"] == "path" and world.runner_calls == 0


# --- consent --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "selection,cost_class", [("art", "metered"), ("chat", "subscription"), ("draft.generate.primary", "subscription")]
)
def test_a_request_without_the_consent_field_is_not_refused_for_its_cost_class(world, capsys, selection, cost_class):
    """No consent file and no `consent` field: the optional layer does not apply, so the call dispatches.

    Red on revert: restore a refusal for requests that did not opt in and the runner is never called.
    """
    code, doc, err = call(world, capsys, selection=selection)
    assert code == 0, (doc, err)
    assert doc["cost"]["class"] == cost_class
    assert doc["consent"]["opted_in"] is False and doc["consent"]["applied"] is False
    assert world.runner_calls >= 1  # a harness entry also runs an auth probe through the spied runner


def test_consent_is_per_cost_class(world, capsys):
    allow(subscription=True, metered=False)
    assert call(world, capsys, selection="chat", consent=True)[0] == 0
    assert call(world, capsys, selection="art", consent=True)[0] == 4
    allow(subscription=False, metered=True)
    assert call(world, capsys, selection="art", consent=True)[0] == 0
    assert call(world, capsys, selection="chat", consent=True)[0] == 4


def test_a_harness_on_metered_auth_is_a_metered_entry(world, capsys, monkeypatch):
    allow(subscription=True, metered=False)
    monkeypatch.setattr(
        harness_auth, "probe_harness_auth",
        lambda harnesses, env=None, *, cwd: {h.lower(): cost.HarnessAuth(cost.METERED, "ANTHROPIC_API_KEY is set") for h in harnesses},
    )
    code, _doc, err = call(world, capsys, selection="chat", consent=True)
    assert code == 4 and err["error"]["kind"] == "consent" and "metered" in err["error"]["message"]
    assert world.runner_calls == 0


def test_the_consent_override_file_is_honoured(world, capsys, monkeypatch, tmp_path):
    chosen = tmp_path / "my-consent.json"
    monkeypatch.setenv("LLM_KIT_CONSENT_FILE", str(chosen))
    assert call(world, capsys, selection="art", consent=True)[0] == 4
    consent_mod.update_config({"subscription": False, "metered": True})
    assert chosen.is_file() and call(world, capsys, selection="art", consent=True)[0] == 0


def test_a_legacy_consent_file_still_allows_the_call(world, home, capsys):  # noqa: F811
    legacy = home / ".claude/plugins/data/plugins-kit/llm-kit/consent.json"
    write(legacy, json.dumps({
        "schema": 2, "local": True, "subscription": True, "metered": False, "scopes": {}, "updated_at": "x",
    }))
    assert call(world, capsys, selection="chat", consent=True)[0] == 0
    assert call(world, capsys, selection="art", consent=True)[0] == 4  # the legacy file allows subscription only
    assert not (home / ".config").exists()  # reading it created no neutral file


# --- refusals and the request grammar -------------------------------------------------------------------


@pytest.mark.parametrize("request_,kind", [
    ({"selection": "nope"}, "selection"),
    ({"model": "nope"}, "selection"),
    ({"selection": "art", "model": "deepseek"}, "selection"),
    ({}, "selection"),
    ({"model": "luna", "extra": 1}, "protocol"),
    ({"model": "luna", "prompt": "  "}, "protocol"),
    ({"model": "luna", "effort": 3}, "effort"),
    ({"model": "luna", "effort": "bogus"}, "effort"),
    ({"model": "luna", "max_tokens": 0}, "protocol"),
    ({"model": "luna", "timeout_s": -1}, "protocol"),
    ({"model": "luna", "output_schema": []}, "contract"),
    ({"model": "luna", "file": "../x.yaml"}, "project-file"),
    ({"model": "luna", "file": "absent.yaml"}, "project-file"),
])
def test_refusals_happen_before_any_spawn(world, capsys, request_, kind):
    allow()
    code, doc, err = call(world, capsys, **request_)
    assert code == 4 and doc is None and err["protocol"] == 2
    assert err["error"]["kind"] == kind, err
    assert world.runner_calls == 0


def test_the_protocol_must_be_exactly_two_and_a_project_root_clash_is_refused(world, capsys):
    allow()
    for version in (2.0, "2", 3):
        out = run(cli.main, ["completion", "complete", "--request-file", "-"], capsys,
                  stdin=json.dumps({"protocol": version, "model": "luna", "prompt": "p"}), monkeypatch=world.monkeypatch)
        assert out.code == 4 and json.loads(out.err)["error"]["kind"] == "protocol"
    out = run(cli.main, ["completion", "complete", "--request-file", "-", "--project-root", str(world.root.parent)], capsys,
              stdin=json.dumps({"protocol": 2, "project_root": str(world.root), "model": "luna", "prompt": "p"}),
              monkeypatch=world.monkeypatch)
    assert out.code == 4 and json.loads(out.err)["error"]["kind"] == "path"
    assert world.runner_calls == 0


def test_flags_that_describe_the_call_are_still_refused_beside_a_request(world, capsys):
    out = run(cli.main, ["completion", "complete", "--request-file", "-", "--effort", "high"], capsys,
              stdin=json.dumps({"protocol": 2, "model": "luna", "prompt": "p"}), monkeypatch=world.monkeypatch)
    assert out.code == 4 and "cannot be combined" in json.loads(out.err)["error"]["message"]


def test_protocol_2_answers_in_json_only(world, capsys):
    out = run(cli.main, ["completion", "complete", "--request-file", "-", "--format", "text"], capsys,
              stdin=json.dumps({"protocol": 2, "model": "luna", "prompt": "p"}), monkeypatch=world.monkeypatch)
    assert out.code == 4 and "JSON only" in json.loads(out.err)["error"]["message"]


def test_request_schema_describes_either_protocol(capsys):
    one = json.loads(run(cli.main, ["completion", "request-schema"], capsys).out)
    two = json.loads(run(cli.main, ["completion", "request-schema", "--protocol", "2"], capsys).out)
    assert one["protocol"] == 1 and "options" in one["keys"]
    assert two["protocol"] == 2 and {"selection", "model", "mode", "working_dir", "expect_files"} <= set(two["keys"])
    assert two["exit_codes"]["4"].startswith("refused before any spawn")


# --- failures that ran ----------------------------------------------------------------------------------


def test_a_classified_halt_exits_3_with_the_halt_object(world, capsys):
    allow()
    world.claude_raw = json.dumps({"result": "You've hit your limit", "api_error_status": 429, "is_error": True})
    code, doc, err = call(world, capsys, selection="chat")
    assert code == 3 and err is None
    assert doc["status"] == "failed" and doc["error"]["code"] == "rate_limit"
    assert doc["halt"] == {"kind": "rate_limit"}
    assert doc["output"] is None and doc["usage"]["input_tokens"] is None  # nothing was reported, nothing is invented
    assert doc["cost"]["usd"] is None and doc["cost"]["class"] == "subscription"


def test_an_ordinary_failure_exits_1(world, capsys):
    allow()
    world.http_error = ValueError("the model exploded")
    code, doc, _ = call(world, capsys, selection="art")
    assert code == 1 and doc["status"] == "failed" and doc["error"] == {"code": "execution", "message": "the model exploded"}
    assert "halt" not in doc


# --- no harness, no ~/.claude --------------------------------------------------------------------------


class _Audit:
    """Records every path opened while armed (the interpreter's audit hook stays installed)."""

    opened: list = []
    armed = False

    @classmethod
    def hook(cls, event, args):
        if cls.armed and event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
            cls.opened.append(os.fsdecode(args[0]))


sys.addaudithook(_Audit.hook)


def test_a_session_of_calls_under_an_empty_home_creates_and_opens_nothing_under_claude(world, home, capsys):  # noqa: F811
    """The libraries are usable by someone who is not using an AI harness.

    Red on revert: point the consent default back at ~/.claude and `consent set` creates it.
    """
    _Audit.opened.clear()
    _Audit.armed = True
    assert run(cli.main, ["consent", "set", "--subscription", "on", "--metered", "on"], capsys).code == 0
    (world.root / "work").mkdir()
    for request in (
        {"selection": "art"}, {"selection": "chat"}, {"selection": "draft.generate.primary"},
    ):
        code, doc, err = call(world, capsys, **request)
        assert code == 0, (request, doc, err)
    world.codex_writes = {"out.txt": "x"}  # only the workspace call may write, and only beneath its directory
    code, doc, err = call(world, capsys, model="luna", mode="workspace", working_dir="work", expect_files=["out.txt"])
    assert code == 0, (doc, err)
    assert (world.root / "work" / "out.txt").is_file()
    assert run(cli.main, ["models", "catalog", "--project-root", str(world.root)], capsys).code == 0
    _Audit.armed = False
    assert not [p for p in _Audit.opened if f"{os.sep}.claude" in p], _Audit.opened
    assert not (home / ".claude").exists()
    assert (home / ".config" / "llm-kit" / "consent.json").is_file()


def test_the_real_cli_in_a_subprocess_with_an_empty_home_touches_no_claude_path(tmp_path):
    """A fresh process, an empty HOME: every user-state command runs and nothing is created under .claude."""
    import subprocess

    empty = tmp_path / "empty-home"
    empty.mkdir()
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import sys, os\n"
        "seen = []\n"
        "sys.addaudithook(lambda e, a: seen.append(os.fsdecode(a[0])) if e == 'open' and isinstance(a[0], (str, bytes, os.PathLike)) else None)\n"
        "import llm_kit.cli as cli\n"
        "codes = [cli.main(a) for a in (['consent', 'set', '--subscription', 'on'], ['consent', 'show', '--json'],\n"
        "         ['account', 'set-key', '--key', 'k', '--no-validate'], ['account', 'which'])]\n"
        "print('CODES', codes)\n"
        "print('CLAUDE', [p for p in seen if '/.claude' in p])\n",
        encoding="utf-8",
    )
    src = Path(__file__).resolve().parents[2] / "packages" / "katsoft-llm-kit" / "src"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("LLM_KIT", "XDG_", "MODEL_ENDPOINTS", "OPENROUTER", "CLAUDE"))}
    env.update(HOME=str(empty), USERPROFILE=str(empty), PYTHONPATH=str(src), _BOOTSTRAP_GUARD_SKIP_REEXEC="1")
    done = subprocess.run([sys.executable, str(probe)], env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert "CODES [0, 0, 0, 0]" in done.stdout and "CLAUDE []" in done.stdout, done.stdout
    assert not (empty / ".claude").exists()
    assert sorted(p.name for p in (empty / ".config" / "llm-kit").iterdir()) == [".env", "consent.json"]


# --- goldens ---------------------------------------------------------------------------------------------


def _golden(world, name: str, capsys, tmp_path: Path, **request: Any) -> dict:
    payload = {"protocol": 2, "project_root": str(world.root), "prompt": "do it", **request}
    out = run(cli.main, ["completion", "complete", "--request-file", "-"], capsys,
              stdin=json.dumps(payload), monkeypatch=world.monkeypatch)
    shown = json.loads(json.dumps(payload).replace(str(world.root), "<T>/proj"))
    observed = {"request": shown, "exit": out.code, "stdout": _form(out.out, tmp_path), "stderr": _form(out.err, tmp_path)}
    _check(name, observed)
    return observed


def test_golden_completion_openrouter(world, capsys, tmp_path):
    allow()
    assert _golden(world, "invoke_completion_openrouter", capsys, tmp_path, selection="art")["exit"] == 0


def test_golden_workspace_codex(world, capsys, tmp_path):
    allow()
    (world.root / "work").mkdir()
    world.codex_writes = {"poster.png": "PNGDATA"}
    got = _golden(world, "invoke_workspace_codex", capsys, tmp_path, selection="draft.generate.primary",
                  mode="workspace", working_dir="work", expect_files=["poster.png"])
    assert got["exit"] == 0


def test_golden_expected_file_missing(world, capsys, tmp_path):
    allow()
    (world.root / "work").mkdir()
    got = _golden(world, "invoke_expect_files_missing", capsys, tmp_path, model="luna", mode="workspace",
                  working_dir="work", expect_files=["poster.png"])
    assert got["exit"] == 1


def test_golden_consent_refusal(world, capsys, tmp_path):
    got = _golden(world, "invoke_consent_refused", capsys, tmp_path, selection="art", consent=True)
    assert got["exit"] == 4


def test_golden_mode_refusal(world, capsys, tmp_path):
    allow()
    (world.root / "work").mkdir()
    got = _golden(world, "invoke_mode_refused", capsys, tmp_path, model="sonnet", mode="workspace", working_dir="work")
    assert got["exit"] == 4
