"""A native-child floor must not disable the worker's ordinary tools."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sys
from typing import Any

import pytest

from llm_kit.adapters.backends import ClaudeCliBackend
from llm_kit.adapters.capabilities import CLAUDE_CAPABILITIES, CODEX_CAPABILITIES
from llm_kit.completion.capabilities import FILESYSTEM_WRITE, SHELL_EXEC, SUBAGENT_SPAWN
from llm_kit.completion.requirements import arm_call, match_capabilities, required_guarantees
from llm_kit.completion.types import BackendOptions
from llm_kit.completion.types import LLMResponse
from llm_kit.dispatch.factory import BackendSelection
from llm_kit.jobs import run as jobs
from llm_kit.jobs.model import Contract, Job, Prompt

FLOOR = "Bash(git push --force*),Bash(git reset --hard*),Agent,Task"


def job(root: Path) -> Job:
    return Job(id="proof", prompt=Prompt(user="Write the harmless artifact"), models=("proof-claude",),
               directory=root, contract=Contract(command=(sys.executable, "-I", "-c", "pass"), directory=root))

def test_child_only_guarantee_keeps_normal_claude_tools() -> None:
    calls: list[list[str]] = []
    def runner(argv: list[str], *_args: object, **_kwargs: object) -> tuple[str, str, int]:
        calls.append(argv)
        return json.dumps({"result": "done", "usage": {}}), "", 0
    backend = ClaudeCliBackend(runner=runner, executable="claude")
    original = "Bash(git push --force*),Agent,Task"
    armed = arm_call(backend, BackendOptions(disallowed_tools=original), {"guarantees": [SUBAGENT_SPAWN]})
    response = armed.backend.complete("system", "work", model="opus", options=armed.options)
    assert "--tools" not in calls[0]
    assert calls[0][calls[0].index("--disallowedTools") + 1] == original
    assert armed.backend.text_only is False
    assert armed.controls == ("disallowed-tools",)
    assert "disallowed-tools" in response.execution_controls_applied
    assert "text-only-mode" not in response.execution_controls_applied


def test_child_only_arming_adds_missing_names_without_rewriting_patterns() -> None:
    original = "Bash(git push --force*)"
    armed = arm_call(ClaudeCliBackend(executable="claude"), BackendOptions(disallowed_tools=original),
                     {"guarantees": [SUBAGENT_SPAWN]})
    assert armed.options.disallowed_tools.startswith(original)
    assert "Task" in armed.options.disallowed_tools and "Agent" in armed.options.disallowed_tools
    assert not armed.backend.text_only


def test_lowercase_alias_keeps_prefix_and_adds_exact_canonical_names() -> None:
    armed = arm_call(ClaudeCliBackend(executable="claude"), BackendOptions(disallowed_tools="task"),
                     {"guarantees": [SUBAGENT_SPAWN]})
    assert armed.options.disallowed_tools == "task Task Agent"


@pytest.mark.parametrize("subjects", [[FILESYSTEM_WRITE], [SHELL_EXEC], [FILESYSTEM_WRITE, SUBAGENT_SPAWN],
                                     [FILESYSTEM_WRITE, SHELL_EXEC, SUBAGENT_SPAWN]])
def test_broad_guarantees_still_arm_text_only(subjects: list[str]) -> None:
    armed = arm_call(ClaudeCliBackend(executable="claude"), BackendOptions(disallowed_tools=FLOOR),
                     {"guarantees": subjects})
    assert armed.backend.text_only
    assert "text-only-mode" in armed.controls


def test_explicit_text_only_backend_is_not_reenabled() -> None:
    armed = arm_call(ClaudeCliBackend(executable="claude", text_only=True), BackendOptions(),
                     {"guarantees": [SUBAGENT_SPAWN]})
    assert armed.backend.text_only


def test_literal_fallback_appends_without_rewriting_raw_scoped_policy() -> None:
    record = replace(CLAUDE_CAPABILITIES, execution_controls=tuple(
        control for control in CLAUDE_CAPABILITIES.execution_controls if control.id != "text-only-mode"))
    original = "Write,Bash(git  push *)"
    armed = arm_call(ClaudeCliBackend(executable="claude"), BackendOptions(disallowed_tools=original),
                     {"guarantees": [FILESYSTEM_WRITE]}, record)
    assert armed.options.disallowed_tools.startswith(original)
    assert all(name in armed.options.disallowed_tools for name in ("Write", "Edit", "MultiEdit", "NotebookEdit"))
    assert not armed.backend.text_only
    assert armed.controls == ("disallowed-tools",)


def test_scoped_shell_floor_preserves_only_unscoped_guarantees(tmp_path: Path) -> None:
    selected = jobs._require_floor_subjects(job(tmp_path), FLOOR)
    assert required_guarantees(selected.requirements) == {SUBAGENT_SPAWN}
    assert match_capabilities(CLAUDE_CAPABILITIES, selected.requirements)
    assert not match_capabilities(CODEX_CAPABILITIES, selected.requirements)


def test_mixed_scoped_floor_retains_whole_write_guarantee(tmp_path: Path) -> None:
    selected = jobs._require_floor_subjects(job(tmp_path), "Write,Bash(git push *),Agent,Task")
    assert required_guarantees(selected.requirements) == {FILESYSTEM_WRITE, SUBAGENT_SPAWN}
    armed = arm_call(ClaudeCliBackend(executable="claude"), BackendOptions(disallowed_tools=FLOOR), selected.requirements)
    assert armed.backend.text_only


def test_opaque_floor_needs_a_literal_controller(tmp_path: Path) -> None:
    selected = jobs._require_floor_subjects(job(tmp_path), "UnknownTool")
    assert match_capabilities(CLAUDE_CAPABILITIES, selected.requirements)
    assert not match_capabilities(CODEX_CAPABILITIES, selected.requirements)


@pytest.mark.parametrize("floor", ["Bash(git push *", "Bash(git *))", "Bash(git *)suffix"])
def test_malformed_scoped_floor_refuses_before_dispatch(tmp_path: Path, floor: str) -> None:
    with pytest.raises(ValueError, match="floor|scoped|clause"):
        jobs._require_floor_subjects(job(tmp_path), floor)


def test_balanced_nested_patterns_are_not_split_or_broadened(tmp_path: Path) -> None:
    selected = jobs._require_floor_subjects(job(tmp_path), "Bash(echo (a,b) && git push *),Task,Agent")
    assert required_guarantees(selected.requirements) == {SUBAGENT_SPAWN}
    assert not match_capabilities(CODEX_CAPABILITIES, selected.requirements)


@pytest.fixture
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import llm_kit.dispatch.declaration as declaration
    import llm_kit.dispatch.usage_budget as usage
    import llm_kit.registry.models as models
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("CLAUDE_BOOTSTRAP_DATA_ROOT", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_TEST", raising=False)
    monkeypatch.setattr(models, "discover_model_entries", lambda **_kwargs: {})
    monkeypatch.setattr(declaration, "check_many", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(usage, "record_observed_halt", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(jobs.BackpressurePolicy, "from_environment", staticmethod(lambda: jobs.BackpressurePolicy(cap_s=0.0)))


@pytest.mark.usefixtures("isolated")
@pytest.mark.parametrize("reported", [True, False])
@pytest.mark.parametrize("floor", [FLOOR, "Bash(git push *)", "UnknownTool"])
def test_real_jobs_argv_retains_tools_and_requires_literal_delivery(tmp_path: Path, reported: bool, floor: str) -> None:
    calls: list[list[str]] = []
    artifact = tmp_path / "result.txt"
    def runner(argv: list[str], *_args: object, **_kwargs: object) -> tuple[str, str, int]:
        calls.append(argv)
        if "--tools" not in argv:
            artifact.write_text("real task tools retained", encoding="utf-8")
        return json.dumps({"result": "done", "usage": {}}), "", 0
    class Backend(ClaudeCliBackend):
        def _applied_controls(self, options: BackendOptions) -> tuple[str, ...]:
            controls = super()._applied_controls(options)
            return controls if reported else tuple(control for control in controls if control != "disallowed-tools")
    backend = Backend(runner=runner, executable="claude")
    def factory(endpoint: str, **_kwargs: Any) -> BackendSelection:
        return BackendSelection(endpoint, "harness", backend, "opus")
    selected = replace(job(tmp_path), contract=Contract(command=(sys.executable, "-I", "-c",
                       "from pathlib import Path; assert Path('result.txt').read_text() == 'real task tools retained'"), directory=tmp_path))
    snapshot = jobs.run_jobs([selected], tmp_path / "ledger.sqlite3", run_id="targeted-proof", disallowed_tools=floor,
                             backend_factory=factory, capabilities_provider=lambda: {"claude-cli": CLAUDE_CAPABILITIES})
    assert len(calls) == 1
    assert "--tools" not in calls[0]
    assert calls[0][calls[0].index("--disallowedTools") + 1] == floor
    if reported:
        assert snapshot.jobs[0].state.value == "accepted"
        assert artifact.read_text() == "real task tools retained"
    else:
        assert snapshot.jobs[0].state.value == "failed"
        assert snapshot.attempts[0].error.code == "armed_control_missing"


@pytest.mark.usefixtures("isolated")
@pytest.mark.parametrize("floor", ["Bash(git push *)", "UnknownTool", FLOOR])
def test_scoped_and_opaque_floor_refuses_other_harness_before_call(tmp_path: Path, floor: str) -> None:
    calls = []
    class Backend:
        name = "codex-cli"
        def complete(self, *_args: object, **_kwargs: object) -> None:
            calls.append("unsafe dispatch")
            raise AssertionError("Literal policy reached an incapable adapter")
    def factory(endpoint: str, **_kwargs: Any) -> BackendSelection:
        return BackendSelection(endpoint, "harness", Backend(), "fake-model")
    snapshot = jobs.run_jobs([job(tmp_path)], tmp_path / "refused.sqlite3", run_id="refusal", disallowed_tools=floor,
                             backend_factory=factory, capabilities_provider=lambda: {"codex-cli": CODEX_CAPABILITIES})
    assert snapshot.jobs[0].state.value == "unroutable"
    assert not snapshot.attempts and not calls


@pytest.mark.usefixtures("isolated")
@pytest.mark.parametrize("invalid", [{"source": "fixed"}, {"parameter": None},
                                    {"parameter": "allowed_tools"}, {"effect": "allow"}])
def test_literal_floor_rejects_control_id_without_request_delivery(tmp_path: Path, invalid: dict[str, Any]) -> None:
    calls = []
    record = replace(CLAUDE_CAPABILITIES, execution_controls=tuple(
        replace(control, **invalid) if control.id == "disallowed-tools" else control
        for control in CLAUDE_CAPABILITIES.execution_controls))
    class Backend:
        name = "claude-cli"
        def complete(self, *_args: object, **_kwargs: object) -> LLMResponse:
            calls.append("dispatch despite missing request delivery")
            return LLMResponse(text="done", model="fake-model", input_tokens=0, output_tokens=0,
                               execution_controls_applied=("disallowed-tools",))
    def factory(endpoint: str, **_kwargs: Any) -> BackendSelection:
        return BackendSelection(endpoint, "harness", Backend(), "fake-model")
    snapshot = jobs.run_jobs([job(tmp_path)], tmp_path / "literal-refused.sqlite3", run_id="literal-refusal",
                             disallowed_tools="Bash(git push *)", backend_factory=factory,
                             capabilities_provider=lambda: {"claude-cli": record})
    assert snapshot.jobs[0].state.value == "unroutable"
    assert not snapshot.attempts and not calls
