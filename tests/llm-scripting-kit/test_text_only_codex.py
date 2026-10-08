"""codex-cli text-only mode: argv, patched model catalog, and record.

Codex tools come from three places -- the user config, feature flags, and the
model catalog -- so the mode ignores the config, turns the features off, and
passes a one-model catalog patched from ``codex debug models``. No real codex
runs: the runner seam answers both the catalog read and the exec call.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from llm_scripting_kit.completion.adapter_capabilities import (
    _CODEX_TEXT_ONLY_CATALOG_DROP,
    _CODEX_TEXT_ONLY_CATALOG_SET,
    _CODEX_TEXT_ONLY_CONFIG,
    CODEX_CAPABILITIES,
)
from llm_scripting_kit.completion.capabilities import (
    DENY,
    FILESYSTEM_WRITE,
    REQUEST,
    SHELL_EXEC,
    SUBAGENT_SPAWN,
    TEXT_ONLY_MODE,
    TEXT_ONLY_PARAMETER,
)
from llm_scripting_kit.completion.codex_backend import CodexCliBackend, CodexRunError
from llm_scripting_kit.completion.types import BackendOptions

#: A catalog entry shaped like codex-cli 0.160.0's ``codex debug models``.
_ENTRY = {
    "slug": "m",
    "display_name": "M",
    "tool_mode": "code_mode_only",
    "multi_agent_version": "v2",
    "experimental_supported_tools": ["clock"],
    "supports_search_tool": True,
    "node_repl_disabled": False,
    "apply_patch_tool_type": "freeform",
    "shell_type": "unified_exec",
}
_CATALOG = {"models": [dict(_ENTRY, slug="other"), _ENTRY]}


class _Runner:
    """Answers ``debug models`` with a catalog and the exec call with "ok".

    The catalog file named on the exec argv is read while the call is in
    flight, because the backend deletes it afterwards.
    """

    def __init__(self, catalog=_CATALOG, catalog_rc=0, catalog_stdout=None) -> None:
        self.calls: list = []
        self.catalog = catalog
        self.catalog_rc = catalog_rc
        self.catalog_stdout = catalog_stdout
        self.catalog_seen = None
        self.catalog_path = None

    def __call__(self, cmd, request, cwd, **kwargs):
        cmd = list(cmd)
        self.calls.append(cmd)
        if cmd[1:3] == ["debug", "models"]:
            out = self.catalog_stdout if self.catalog_stdout is not None else json.dumps(self.catalog)
            return out, "", self.catalog_rc
        pairs = _pairs(cmd)
        catalog = [p for p in pairs if p.startswith("model_catalog_json=")]
        if catalog:
            self.catalog_path = Path(catalog[0].split("=", 1)[1].strip("'"))
            self.catalog_seen = json.loads(self.catalog_path.read_text(encoding="ascii"))
        Path(cmd[cmd.index("-o") + 1]).write_text("ok", encoding="utf-8")
        return "", "tokens used: 10", 0


def _pairs(argv) -> list:
    return [argv[i + 1] for i in range(len(argv) - 1) if argv[i] == "-c"]


def _codex(text_only=False, runner=None):
    runner = runner or _Runner()
    return CodexCliBackend(runner=runner, argv_prefix=("codex",), text_only=text_only), runner


def _exec_argv(runner):
    return next(c for c in runner.calls if c[1] == "exec")


def test_text_only_argv(tmp_path):
    backend, runner = _codex(text_only=True)
    response = backend.complete("sys", "usr", model="m", options=BackendOptions(cwd=tmp_path))
    assert runner.calls[0] == ["codex", "debug", "models"]
    argv = _exec_argv(runner)
    assert argv[:3] == ["codex", "exec", "--ignore-user-config"]
    assert argv[argv.index("-s") + 1] == "read-only"
    assert not any("network_access" in part for part in argv)
    pairs = _pairs(argv)
    assert set(_CODEX_TEXT_ONLY_CONFIG) <= set(pairs)
    assert any(p.startswith("model_catalog_json='") and p.endswith("'") for p in pairs)
    assert "text-only-mode" in response.execution_controls_applied
    assert "network-enable" not in response.execution_controls_applied


def test_the_catalog_carries_only_the_patched_model(tmp_path):
    backend, runner = _codex(text_only=True)
    backend.complete("sys", "usr", model="m", options=BackendOptions(cwd=tmp_path))
    (entry,) = runner.catalog_seen["models"]
    assert entry["slug"] == "m"
    for key, value in _CODEX_TEXT_ONLY_CATALOG_SET.items():
        assert entry[key] == value
    for key in _CODEX_TEXT_ONLY_CATALOG_DROP:
        assert key not in entry
    # Everything else is the live entry, unchanged.
    assert entry["shell_type"] == "unified_exec" and entry["display_name"] == "M"
    assert not runner.catalog_path.exists(), "the temp catalog must be deleted"


def test_default_argv_is_unchanged(tmp_path):
    backend, runner = _codex()
    response = backend.complete("sys", "usr", model="m", options=BackendOptions(cwd=tmp_path))
    assert len(runner.calls) == 1, "no catalog read outside text-only mode"
    argv = runner.calls[0]
    assert "--ignore-user-config" not in argv
    assert argv[argv.index("-s") + 1] == "workspace-write"
    assert "sandbox_workspace_write.network_access=true" in _pairs(argv)
    assert not set(_CODEX_TEXT_ONLY_CONFIG) & set(_pairs(argv))
    assert "text-only-mode" not in response.execution_controls_applied


def test_an_explicit_read_only_sandbox_is_accepted(tmp_path):
    backend, runner = _codex(text_only=True)
    backend.complete(
        "sys", "usr", model="m",
        options=BackendOptions(cwd=tmp_path, extras={"sandbox": "read-only", "network": False}),
    )
    argv = _exec_argv(runner)
    assert argv[argv.index("-s") + 1] == "read-only"


@pytest.mark.parametrize(
    "extras", [{"sandbox": "workspace-write"}, {"network": True}], ids=["sandbox", "network"]
)
def test_a_conflicting_extra_is_refused_before_dispatch(tmp_path, extras):
    backend, runner = _codex(text_only=True)
    with pytest.raises(ValueError, match="text-only mode"):
        backend.complete("sys", "usr", model="m", options=BackendOptions(cwd=tmp_path, extras=extras))
    assert runner.calls == []


def test_no_model_id_is_refused_before_dispatch(tmp_path):
    backend, runner = _codex(text_only=True)
    with pytest.raises(ValueError, match="model id"):
        backend.complete("sys", "usr", model="", options=BackendOptions(cwd=tmp_path))
    assert runner.calls == []


@pytest.mark.parametrize(
    "runner,match",
    [
        (_Runner(catalog={"models": [dict(_ENTRY, slug="other")]}), "no entry for 'm'"),
        (_Runner(catalog_rc=2), "debug models failed"),
        (_Runner(catalog_stdout="not json"), "no JSON catalog"),
    ],
    ids=["model-missing", "read-failed", "not-json"],
)
def test_a_catalog_that_cannot_be_patched_stops_the_call(tmp_path, runner, match):
    backend, _ = _codex(text_only=True, runner=runner)
    with pytest.raises(CodexRunError, match=match):
        backend.complete("sys", "usr", model="m", options=BackendOptions(cwd=tmp_path))
    assert [c[1] for c in runner.calls] == ["debug"], "exec must not run"


def test_record_declares_the_three_guarantees():
    control = next(c for c in CODEX_CAPABILITIES.execution_controls if c.id == TEXT_ONLY_MODE)
    assert control.effect == DENY
    assert control.subjects == (FILESYSTEM_WRITE, SHELL_EXEC, SUBAGENT_SPAWN)
    assert control.source == REQUEST and control.parameter == TEXT_ONLY_PARAMETER
    assert control.emits.startswith("--ignore-user-config -s read-only ")
    for item in _CODEX_TEXT_ONLY_CONFIG:
        assert f"-c {item}" in control.emits
    assert "model_catalog_json=" in control.emits
    assert "request_user_input" in control.note


def test_the_builder_emits_ignore_user_config_only_when_asked(tmp_path):
    from bootstrap_lib.codex import build_codex_exec_argv

    plain = build_codex_exec_argv(root=tmp_path, argv_prefix=("codex",))
    asked = build_codex_exec_argv(root=tmp_path, argv_prefix=("codex",), ignore_user_config=True)
    assert "--ignore-user-config" not in plain
    assert asked[:3] == ["codex", "exec", "--ignore-user-config"]
    asked.remove("--ignore-user-config")
    assert asked == plain


def test_a_bootstrap_without_ignore_user_config_is_diagnosed_not_a_type_error(tmp_path, monkeypatch):
    import bootstrap_lib.codex as codex_mod

    real = codex_mod.build_codex_exec_argv

    def old_builder(*, root, output_file, argv_prefix, model=None, effort=None, sandbox=None,
                    network=None, extra_config=()):  # no ignore_user_config
        return real(root=root, output_file=output_file, argv_prefix=argv_prefix)

    monkeypatch.setattr(codex_mod, "build_codex_exec_argv", old_builder)
    backend, _ = _codex(text_only=True)
    with pytest.raises(RuntimeError, match=r"no ignore_user_config.*bootstrap >= 0\.146\.0.*plugin update"):
        backend.complete("sys", "usr", model="m", options=BackendOptions(cwd=tmp_path))
    # Without text-only the old builder is fine: the probe is for the keyword used.
    plain, runner = _codex(text_only=False)
    plain.complete("sys", "usr", model="m", options=BackendOptions(cwd=tmp_path))
    assert any(c[1] == "exec" for c in runner.calls)


def test_an_absent_bootstrap_codex_module_names_install_not_update(tmp_path, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "bootstrap_lib.codex", None)  # import raises ImportError
    backend, _ = _codex(text_only=False)
    with pytest.raises(RuntimeError, match=r"not importable.*plugin install bootstrap"):
        backend.complete("sys", "usr", model="m", options=BackendOptions(cwd=tmp_path))
