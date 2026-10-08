"""A guarantees requirement admits an entry AND arms every control it relies on.

``requirements.arm_call`` switches claude-cli and codex-cli into text-only
mode, adds deny-list names for opencode-cli, and raises when a required
subject has no control it can arm. ``declaration.run`` applies it before every
call and refuses a response that does not report an armed control. No real
model runs: every backend is driven through its runner seam.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Optional

import pytest

from llm_scripting_kit import declaration as decl
from llm_scripting_kit.completion.adapter_capabilities import (
    _CLAUDE_TEXT_ONLY_ARGS,
    CLAUDE_CAPABILITIES,
    CODEX_CAPABILITIES,
    OPENCODE_CAPABILITIES,
    OPENROUTER_CAPABILITIES,
)
from llm_scripting_kit.completion.backends import ClaudeCliBackend
from llm_scripting_kit.completion.capabilities import (
    DENY_TOOL_NAMES,
    FILESYSTEM_WRITE,
    SHELL_EXEC,
    SUBAGENT_SPAWN,
    subjects_for_disallowed_tools,
)
from llm_scripting_kit.completion.codex_backend import CodexCliBackend
from llm_scripting_kit.completion.opencode_backend import OpencodeCliBackend
from llm_scripting_kit.completion.requirements import (
    arm_call,
    arm_requirements,
    match_capabilities,
    required_guarantees,
)
from llm_scripting_kit.completion.types import BackendOptions, LLMResponse
from llm_scripting_kit.model_endpoints import HARNESS_KIND, EndpointEntry
from llm_scripting_kit.reachability import STATUS_REACHABLE, Reachability

ALL_THREE = [FILESYSTEM_WRITE, SHELL_EXEC, SUBAGENT_SPAWN]
GUARANTEES = {"guarantees": ALL_THREE}
_CATALOG = {"models": [{"slug": "sol-model", "tool_mode": "code_mode_only",
                        "apply_patch_tool_type": "freeform"}]}


# -- seams -----------------------------------------------------------------


class _ClaudeRunner:
    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, cmd, request, cwd, **kwargs):
        self.calls.append(list(cmd))
        return json.dumps({"result": "ok", "is_error": False, "usage": {}}), "", 0


class _CodexRunner:
    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, cmd, request, cwd, **kwargs):
        self.calls.append(list(cmd))
        if list(cmd[1:3]) == ["debug", "models"]:
            return json.dumps(_CATALOG), "", 0
        Path(cmd[cmd.index("-o") + 1]).write_text("ok", encoding="utf-8")
        return "", "tokens used: 10", 0


class _OpencodeRunner:
    def __init__(self) -> None:
        self.envs: list = []

    def __call__(self, cmd, request, cwd, **kwargs):
        self.envs.append(kwargs.get("env") or {})
        return "ok", "", 0


def _claude():
    runner = _ClaudeRunner()
    return ClaudeCliBackend(runner=runner, executable="claude"), runner


def _codex():
    runner = _CodexRunner()
    return CodexCliBackend(runner=runner, argv_prefix=("codex",)), runner


def _opencode():
    runner = _OpencodeRunner()
    return OpencodeCliBackend(runner=runner, argv_prefix=("opencode-test",)), runner


def _contains_run(argv, run) -> bool:
    run = list(run)
    return any(argv[i : i + len(run)] == run for i in range(len(argv) - len(run) + 1))


# -- the vocabulary ---------------------------------------------------------


@pytest.mark.parametrize("subject", ALL_THREE)
def test_deny_tool_names_map_back_to_their_subject(subject):
    assert subjects_for_disallowed_tools(" ".join(DENY_TOOL_NAMES[subject])) == {subject}


@pytest.mark.parametrize(
    "requirements,expected",
    [
        (None, frozenset()),
        ({}, frozenset()),
        (["effort"], frozenset()),
        ({"guarantees": SHELL_EXEC}, frozenset({SHELL_EXEC})),
        ({"guarantees": ALL_THREE}, frozenset(ALL_THREE)),
        ({"denies": [SHELL_EXEC]}, frozenset({SHELL_EXEC})),
        ({"guarantees": {SHELL_EXEC: True, FILESYSTEM_WRITE: False}}, frozenset({SHELL_EXEC})),
    ],
)
def test_required_guarantees_reads_every_shape(requirements, expected):
    assert required_guarantees(requirements) == expected


# -- selection --------------------------------------------------------------


def test_the_requirement_admits_every_shipped_adapter():
    for record in (CLAUDE_CAPABILITIES, CODEX_CAPABILITIES, OPENCODE_CAPABILITIES,
                   OPENROUTER_CAPABILITIES):
        assert match_capabilities(record, GUARANTEES), record.adapter


# -- arm_call ---------------------------------------------------------------


@pytest.mark.parametrize("make", [_claude, _codex], ids=["claude", "codex"])
@pytest.mark.parametrize("subject", ALL_THREE)
def test_any_guarantee_arms_text_only_mode(make, subject):
    backend, _ = make()
    options = BackendOptions()
    armed = arm_call(backend, options, {"guarantees": [subject]})
    assert armed.backend is not backend and armed.backend.text_only is True
    assert backend.text_only is False, "the caller's backend is not mutated"
    assert armed.options is options and armed.controls == ("text-only-mode",)


def test_opencode_is_armed_through_its_deny_list():
    backend, _ = _opencode()
    armed = arm_call(backend, BackendOptions(disallowed_tools="WebFetch"), GUARANTEES)
    assert armed.backend is backend
    names = armed.options.disallowed_tools.split()
    assert names[0] == "WebFetch", "the caller's own names are kept, first"
    assert subjects_for_disallowed_tools(armed.options.disallowed_tools) == set(ALL_THREE)
    assert set(armed.controls) == {
        "permission-edit-deny", "permission-bash-deny", "permission-task-request-deny",
    }


def test_a_structural_guarantee_needs_no_arming():
    class _Transport:
        name = "openrouter"
        capabilities = OPENROUTER_CAPABILITIES

    backend, options = _Transport(), BackendOptions()
    armed = arm_call(backend, options, GUARANTEES)
    assert armed.backend is backend and armed.options is options and armed.controls == ()


@pytest.mark.parametrize("requirements", [None, {}, {"params": ["effort"]}])
def test_no_guarantee_leaves_the_call_untouched(requirements):
    backend, _ = _claude()
    options = BackendOptions()
    armed = arm_call(backend, options, requirements)
    assert armed.backend is backend and armed.options is options and armed.controls == ()


def test_a_control_nothing_can_arm_fails_loudly():
    """codex's sandbox-mode confines writes only through extras.sandbox, which
    arm_call does not set: a record admitting through it alone must raise."""
    record = {
        "adapter": "codex-cli",
        "execution_controls": [{
            "id": "sandbox-mode", "effect": "confine", "source": "request",
            "parameter": "extras.sandbox", "subjects": [FILESYSTEM_WRITE],
        }],
    }
    assert match_capabilities(record, {"guarantees": [FILESYSTEM_WRITE]})
    backend, _ = _codex()
    with pytest.raises(ValueError, match="advertises no control"):
        arm_call(backend, BackendOptions(), {"guarantees": [FILESYSTEM_WRITE]}, record)


def test_no_record_with_a_guarantee_fails_loudly():
    class _Bare:
        name = "mystery"

    with pytest.raises(ValueError, match="no capability record"):
        arm_call(_Bare(), BackendOptions(), GUARANTEES)


def test_a_record_that_arms_a_backend_without_the_field_fails_loudly():
    class _Fake:
        name = "claude-cli"

    with pytest.raises(TypeError, match="text_only"):
        arm_call(_Fake(), BackendOptions(), GUARANTEES, CLAUDE_CAPABILITIES)
    with pytest.raises(TypeError, match="text_only"):
        arm_requirements(_Fake(), GUARANTEES, CLAUDE_CAPABILITIES)


def test_arm_requirements_is_the_text_only_half():
    backend, _ = _claude()
    assert arm_requirements(backend, GUARANTEES).text_only is True
    assert arm_requirements(backend, None) is backend
    opencode, _ = _opencode()
    assert arm_requirements(opencode, GUARANTEES) is opencode


# -- declaration.run --------------------------------------------------------


@dataclass
class _Selection:
    endpoint: str
    kind: str
    backend: Any
    model: str
    effort: Optional[str] = None
    capabilities: Any = None


_HARNESS = {"sol": "codex", "opus": "claude", "oc": "opencode"}


def _run(names, backends, requirements, tmp_path, capabilities=None):
    entries = {
        name: EndpointEntry(id=name, base_url=None, model=f"{name}-model",
                            kind=HARNESS_KIND, harness=_HARNESS[name])
        for name in backends
    }
    return decl.run(
        names,
        decl.RunRequest(system="s", prompt="p", options=BackendOptions(cwd=tmp_path)),
        entries=entries,
        backend_factory=lambda name, **_kw: _Selection(
            name, HARNESS_KIND, backends[name], f"{name}-model"
        ),
        reachability_cache={
            name: Reachability(status=STATUS_REACHABLE, checked="cli-version", detail="ok")
            for name in backends
        },
        requirements=requirements,
        capabilities=capabilities,
    )


def test_run_dispatches_claude_in_text_only_mode(tmp_path):
    claude, runner = _claude()
    result = _run(["opus"], {"opus": claude}, GUARANTEES, tmp_path)
    assert result.status == decl.RUN_COMPLETED
    argv = runner.calls[0]
    assert _contains_run(argv, _CLAUDE_TEXT_ONLY_ARGS) and "bypassPermissions" not in argv
    assert "text-only-mode" in result.response.execution_controls_applied
    assert claude.text_only is False


def test_run_dispatches_codex_in_text_only_mode_for_a_write_guarantee(tmp_path):
    """The gap this closes: codex was admitted for filesystem-write and then
    run under the default workspace-write sandbox."""
    codex, runner = _codex()
    result = _run(["sol"], {"sol": codex}, {"guarantees": [FILESYSTEM_WRITE]}, tmp_path)
    assert result.status == decl.RUN_COMPLETED
    argv = next(c for c in runner.calls if c[1] == "exec")
    assert argv[argv.index("-s") + 1] == "read-only"
    assert "--ignore-user-config" in argv
    assert "text-only-mode" in result.response.execution_controls_applied


def test_run_arms_opencode_through_its_deny_list(tmp_path):
    opencode, runner = _opencode()
    result = _run(["oc"], {"oc": opencode}, GUARANTEES, tmp_path)
    assert result.status == decl.RUN_COMPLETED
    policy = json.loads(runner.envs[0]["OPENCODE_CONFIG_CONTENT"])
    assert policy["permission"]["edit"] == "deny"
    assert policy["permission"]["bash"] == "deny"
    assert policy["permission"]["task"] == "deny"


def test_run_refuses_an_entry_it_cannot_arm_before_any_call(tmp_path):
    codex, runner = _codex()
    record = {
        "adapter": "codex-cli",
        "execution_controls": [{
            "id": "sandbox-mode", "effect": "confine", "source": "request",
            "parameter": "extras.sandbox", "subjects": [FILESYSTEM_WRITE],
        }],
    }
    with pytest.raises(ValueError, match="advertises no control"):
        _run(["sol"], {"sol": codex}, {"guarantees": [FILESYSTEM_WRITE]}, tmp_path,
             capabilities={"codex-cli": record})
    assert runner.calls == []


def test_run_refuses_a_response_without_the_armed_control(tmp_path):
    @dataclass
    class _Drifted:
        """Accepts text_only but reports no control: adapter/record drift."""

        text_only: bool = False
        name: str = "claude-cli"
        capabilities = CLAUDE_CAPABILITIES

        def complete(self, system, user, *, model, options=None):
            return LLMResponse(text="ok", model=model)

        def classify_halt(self, exc):
            return None

    with pytest.raises(RuntimeError, match="text-only-mode"):
        _run(["opus"], {"opus": _Drifted()}, GUARANTEES, tmp_path)


def test_describe_offers_claude_and_codex_under_the_guarantees():
    entries = {
        "sol": EndpointEntry(id="sol", base_url=None, model="sol-model", kind=HARNESS_KIND, harness="codex"),
        "opus": EndpointEntry(id="opus", base_url=None, model="opus-model", kind=HARNESS_KIND, harness="claude"),
    }
    ranking = decl.describe(
        ["sol", "opus"], caller=decl.CALLER_PROCESS, requirements=GUARANTEES, entries=entries,
        backend_factory=lambda name, **_kw: _Selection(
            name, HARNESS_KIND, _codex()[0] if name == "sol" else _claude()[0], f"{name}-model"
        ),
        reachability_cache={
            name: Reachability(status=STATUS_REACHABLE, checked="cli-version", detail="ok")
            for name in entries
        },
    )
    assert ranking.default.id == "sol"
    assert not [d for d in ranking.dispositions
                if d.disposition == decl.DISPOSITION_REQUIREMENTS_MISMATCH]


@pytest.mark.parametrize("requirements", [None, {"params": ["effort"]}])
def test_run_without_a_guarantee_keeps_the_default_argv(tmp_path, requirements):
    claude, runner = _claude()
    result = _run(["opus"], {"opus": claude}, requirements, tmp_path)
    assert result.status == decl.RUN_COMPLETED
    assert "bypassPermissions" in runner.calls[0] and "--tools" not in runner.calls[0]
