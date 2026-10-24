"""Delivered deny controls for a caller composing a bounded coding worker."""

from pathlib import Path

import pytest

from llm_kit.adapters.codex_backend import CodexCliBackend
from llm_kit.adapters.codex_command import build_codex_exec_argv
from llm_kit.completion.types import BackendOptions


def pairs(argv: list[str]) -> set[str]:
    return {argv[index + 1] for index, token in enumerate(argv[:-1]) if token == "-c"}


def test_network_false_overrides_an_enabled_user_configuration(tmp_path: Path) -> None:
    argv = build_codex_exec_argv(root=tmp_path.resolve(), network=False, argv_prefix=("codex",))
    assert "sandbox_workspace_write.network_access=false" in pairs(argv)
    assert "sandbox_workspace_write.network_access=true" not in pairs(argv)


def test_worker_controls_reach_the_actual_completion_argv(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    def runner(argv: list[str], request: str, cwd: Path, **kwargs: object) -> tuple[str, str, int]:
        calls.append(argv)
        Path(argv[argv.index("-o") + 1]).write_text("receipt", encoding="utf-8")
        return "", "", 0
    backend = CodexCliBackend(argv_prefix=("codex",), runner=runner)
    result = backend.complete("System", "Work", model="gpt-6-luna", options=BackendOptions(
        cwd=tmp_path.resolve(), extras={"sandbox": "workspace-write", "network": False, "native_subagents": "deny"},
    ))
    argv = calls[0]
    assert argv[argv.index("-s") + 1] == "workspace-write"
    assert {"sandbox_workspace_write.network_access=false", "agents.enabled=false", "features.multi_agent=false", "features.multi_agent_v2=false"} <= pairs(argv)
    assert "extras.native_subagents" not in result.dropped_params
    assert {"network-disable", "native-subagents-deny"} <= set(result.execution_controls_applied)


@pytest.mark.parametrize("value", [True, False, "allow", "", 1, {}, ["deny"]])
def test_unsupported_subagent_controls_refuse_before_dispatch(tmp_path: Path, value: object) -> None:
    calls: list[object] = []
    backend = CodexCliBackend(argv_prefix=("codex",), runner=lambda *args, **kwargs: calls.append(args))
    with pytest.raises(ValueError, match="native_subagents"):
        backend.complete("System", "Work", model="gpt-6-luna", options=BackendOptions(
            cwd=tmp_path.resolve(), extras={"native_subagents": value},
        ))
    assert calls == []


def test_public_builder_refuses_conflicting_override(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="conflict"):
        build_codex_exec_argv(root=tmp_path.resolve(), network=False, native_subagents="deny",
                             extra_config=("agents.enabled=true",), argv_prefix=("codex",))
