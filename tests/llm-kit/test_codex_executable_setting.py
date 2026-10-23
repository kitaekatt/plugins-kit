"""LLM_KIT_CODEX_EXECUTABLE: one setting names the Codex build for every path.

The variable is honoured by detection, by argv building and by the completion
backend, with no harness, bootstrap or plugin involved. A value that cannot be
run raises an error naming the variable instead of falling back to ``codex`` on
PATH.
"""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from llm_kit.adapters import codex_command
from llm_kit.adapters.codex_backend import CodexCliBackend
from llm_kit.completion.types import BackendOptions

ENV = codex_command.CODEX_EXECUTABLE_ENV

posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="the fake CLI is a POSIX shell script"
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    codex_command.reset_detection_cache()
    yield
    codex_command.reset_detection_cache()


def _fake_cli(directory: Path, name: str = "codex-app", banner: str = "codex-cli 9.8.7") -> Path:
    path = directory / name
    path.write_text("#!/bin/sh\necho '%s'\n" % banner, encoding="ascii")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _root(tmp_path: Path) -> Path:
    return tmp_path.resolve()


def test_variable_name_is_the_documented_one():
    assert ENV == "LLM_KIT_CODEX_EXECUTABLE"


def test_unset_uses_codex_from_path(monkeypatch):
    seen = []

    def fake_which(name):
        seen.append(name)
        return "/usr/local/bin/codex"

    monkeypatch.setattr(codex_command.shutil, "which", fake_which)
    assert codex_command.resolve_codex_cli() == ("/usr/local/bin/codex",)
    assert seen == ["codex"]


@posix_only
def test_argv_uses_the_configured_absolute_path(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path)
    monkeypatch.setenv(ENV, str(cli))
    argv = codex_command.build_codex_exec_argv(root=_root(tmp_path))
    assert argv[0] == str(cli)
    assert argv[1] == "exec"


def test_argv_uses_a_configured_command_name_found_on_path(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, "codex-app")
    monkeypatch.setattr(
        codex_command.shutil, "which",
        lambda name: "/opt/app/codex-app" if name == "codex-app" else None,
    )
    argv = codex_command.build_codex_exec_argv(root=_root(tmp_path))
    assert argv[0] == "/opt/app/codex-app"


@posix_only
def test_detection_probes_the_configured_binary(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path, banner="codex-cli 9.8.7")
    monkeypatch.setenv(ENV, str(cli))
    detection = codex_command.detect_codex()
    assert detection.available is True
    assert detection.version == (9, 8, 7)
    assert detection.reason == "codex-cli 9.8.7"
    assert detection.argv_prefix == (str(cli),)


@posix_only
def test_detection_cache_follows_the_variable(tmp_path, monkeypatch):
    first = _fake_cli(tmp_path, "one", "codex-cli 1.0.0")
    second = _fake_cli(tmp_path, "two", "codex-cli 2.0.0")
    monkeypatch.setenv(ENV, str(first))
    assert codex_command.detect_codex().version == (1, 0, 0)
    monkeypatch.setenv(ENV, str(second))
    assert codex_command.detect_codex().version == (2, 0, 0)


def test_missing_path_fails_loudly_naming_the_variable(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, str(tmp_path / "no-such-codex"))
    with pytest.raises(codex_command.CodexExecutableError) as built:
        codex_command.build_codex_exec_argv(root=_root(tmp_path))
    assert ENV in str(built.value)
    assert "no-such-codex" in str(built.value)
    with pytest.raises(codex_command.CodexExecutableError) as detected:
        codex_command.detect_codex()
    assert ENV in str(detected.value)


@posix_only
def test_non_executable_file_fails_loudly(tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.write_text("not a program", encoding="ascii")
    plain.chmod(0o644)
    monkeypatch.setenv(ENV, str(plain))
    with pytest.raises(codex_command.CodexExecutableError, match=ENV):
        codex_command.build_codex_exec_argv(root=_root(tmp_path))


def test_directory_fails_loudly(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, str(tmp_path))
    with pytest.raises(codex_command.CodexExecutableError, match=ENV):
        codex_command.resolve_codex_cli()


def test_bare_name_that_is_not_on_path_fails_loudly(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, "no-such-codex-command")
    monkeypatch.setattr(codex_command.shutil, "which", lambda name: None)
    with pytest.raises(codex_command.CodexExecutableError, match=ENV):
        codex_command.resolve_codex_cli()


def test_relative_path_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, os.path.join("bin", "codex"))
    with pytest.raises(codex_command.CodexExecutableError, match="relative"):
        codex_command.resolve_codex_cli()


def test_empty_value_is_refused(monkeypatch):
    monkeypatch.setenv(ENV, "   ")
    with pytest.raises(codex_command.CodexExecutableError, match=ENV):
        codex_command.configured_codex_executable()


def test_error_is_a_runtime_error():
    assert issubclass(codex_command.CodexExecutableError, RuntimeError)


@posix_only
def test_explicit_argv_prefix_ignores_the_variable(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, str(tmp_path / "no-such-codex"))
    argv = codex_command.build_codex_exec_argv(
        root=_root(tmp_path), argv_prefix=("/x/codex",)
    )
    assert argv[0] == "/x/codex"


@posix_only
def test_completion_backend_dispatches_the_configured_binary(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path)
    monkeypatch.setenv(ENV, str(cli))
    calls = []

    def runner(argv, prompt, cwd, **kwargs):
        calls.append(list(argv))
        Path(argv[argv.index("-o") + 1]).write_text("answer", encoding="utf-8")
        return "", "", 0

    backend = CodexCliBackend(runner=runner)
    response = backend.complete(
        "s", "u", model="gpt-5.4-codex", options=BackendOptions(cwd=_root(tmp_path))
    )
    assert response.text == "answer"
    assert calls[0][0] == str(cli)


@posix_only
def test_text_only_catalog_read_uses_the_configured_binary(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path)
    monkeypatch.setenv(ENV, str(cli))
    calls = []

    def runner(argv, prompt, cwd, **kwargs):
        calls.append(list(argv))
        return json.dumps({"models": [{"slug": "m"}]}), "", 0

    backend = CodexCliBackend(runner=runner, text_only=True)
    path = backend._write_text_only_catalog(
        "m", _root(tmp_path), log_prefix="", timeout_s=5
    )
    try:
        assert calls[0][:3] == [str(cli), "debug", "models"]
    finally:
        path.unlink()
