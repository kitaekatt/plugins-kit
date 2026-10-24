"""codex exec with network off must say so: the user's config.toml must not win.

``-s workspace-write`` reads ``sandbox_workspace_write.network_access`` from the
user's ``$CODEX_HOME/config.toml`` when no ``-c`` overrides it, so a user value of
true silently re-enables network for a request that asked for it off. The builder
therefore emits an explicit ``=false`` wherever the workspace-write sandbox applies.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from llm_kit.adapters import codex_command
from llm_kit.adapters.capabilities import CODEX_CAPABILITIES
from llm_kit.adapters.codex_backend import CodexCliBackend

TRUE = "sandbox_workspace_write.network_access=true"
FALSE = "sandbox_workspace_write.network_access=false"


@pytest.fixture(autouse=True)
def _posix_argv(monkeypatch):
    # No Windows pair, so the exact argv is the same on every host.
    monkeypatch.setattr(codex_command.os, "name", "posix")


def _argv(tmp_path: Path, **kwargs):
    return codex_command.build_codex_exec_argv(
        root=tmp_path, argv_prefix=("codex",), output_file=tmp_path / "o.txt", **kwargs
    )


def _tail(tmp_path: Path):
    return ["-C", str(tmp_path.resolve()), "-o", str((tmp_path / "o.txt").resolve()),
            "--skip-git-repo-check", "--color", "never", "-"]


def test_network_true_exact_argv(tmp_path):
    argv = _argv(tmp_path, network=True)
    assert argv == ["codex", "exec", "-s", "workspace-write", "-c", TRUE] + _tail(tmp_path)


def test_network_false_exact_argv(tmp_path):
    argv = _argv(tmp_path, network=False)
    assert argv == ["codex", "exec", "-s", "workspace-write", "-c", FALSE] + _tail(tmp_path)


def test_network_false_with_ignore_user_config_still_explicit(tmp_path):
    argv = _argv(tmp_path, network=False, ignore_user_config=True)
    assert argv[:7] == ["codex", "exec", "--ignore-user-config", "-s", "workspace-write", "-c", FALSE]


@pytest.mark.parametrize("sandbox", ["read-only", "danger-full-access"])
def test_no_network_pair_outside_workspace_write(tmp_path, sandbox):
    # The key lives in the workspace-write table; other modes never read it, and
    # text-only mode (read-only) keeps its documented "no network flag" argv.
    argv = _argv(tmp_path, network=False, sandbox=sandbox)
    assert not any("network_access" in part for part in argv)


def test_network_disable_control_is_advertised():
    ids = {c.id for c in CODEX_CAPABILITIES.execution_controls}
    assert {"network-enable", "network-disable"} <= ids


def _applied(tmp_path, **kwargs):
    backend = CodexCliBackend.__new__(CodexCliBackend)
    return backend._applied_controls(_argv(tmp_path, **kwargs))


def test_applied_controls_follow_the_emitted_pair(tmp_path):
    on = _applied(tmp_path, network=True)
    off = _applied(tmp_path, network=False)
    assert "network-enable" in on and "network-disable" not in on
    assert "network-disable" in off and "network-enable" not in off
    # read-only emits neither pair, so neither control is reported
    ro = _applied(tmp_path, network=False, sandbox="read-only")
    assert "network-enable" not in ro and "network-disable" not in ro
