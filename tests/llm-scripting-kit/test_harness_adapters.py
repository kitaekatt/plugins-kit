"""Tests for harness-backed command construction."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from llm_scripting_kit import harness_adapters
from llm_scripting_kit.harness_adapters import (
    CODEX_EFFORT_MENU,
    CodexAdapter,
    HarnessAdapterError,
    OpencodeAdapter,
    detect_opencode,
    resolve_harness_adapter,
)
from llm_scripting_kit.model_endpoints import HARNESS_KIND, EndpointEntry


def _entry(harness: str, *, model: str = "provider/model", effort=None):
    return EndpointEntry(
        id=f"{harness}-entry",
        base_url=None,
        model=model,
        kind=HARNESS_KIND,
        harness=harness,
        effort=effort,
    )


class TestSelection:
    def test_selection_uses_the_entry_harness(self):
        assert isinstance(resolve_harness_adapter(_entry("codex")), CodexAdapter)
        assert isinstance(resolve_harness_adapter(_entry("opencode")), OpencodeAdapter)

    def test_unknown_harness_names_the_value_and_known_set(self):
        with pytest.raises(HarnessAdapterError) as excinfo:
            resolve_harness_adapter(_entry("mystery"))
        message = str(excinfo.value)
        assert "mystery" in message
        assert "codex" in message
        assert "opencode" in message


class TestCodexAdapter:
    def test_delegates_argv_construction_to_bootstrap(self, monkeypatch, tmp_path):
        monkeypatch.syspath_prepend(
            str(Path(__file__).resolve().parents[2] / "plugins" / "bootstrap")
        )
        from bootstrap_lib import codex

        calls = []

        def fake_builder(**kwargs):
            calls.append(kwargs)
            return ["delegated", "argv"]

        monkeypatch.setattr(codex, "build_codex_exec_argv", fake_builder)
        root = tmp_path.resolve()
        output = (tmp_path / "answer.txt").resolve()
        result = CodexAdapter(argv_prefix=("codex-test",)).build_argv(
            _entry("codex", model="gpt-5.6-sol"),
            root,
            prompt="brief",
            output_file=output,
            effort="xhigh",
        )

        assert result == ["delegated", "argv"]
        assert calls == [
            {
                "root": root,
                "model": "gpt-5.6-sol",
                "effort": "xhigh",
                "output_file": output,
                "argv_prefix": ("codex-test",),
            }
        ]

    def test_effort_menu_keeps_xhigh_and_rejects_unknown_values(self):
        assert CodexAdapter().accepted_efforts() == CODEX_EFFORT_MENU
        assert CODEX_EFFORT_MENU == frozenset(
            ("low", "medium", "high", "xhigh", "max")
        )
        with pytest.raises(HarnessAdapterError, match="accepted efforts"):
            CodexAdapter(argv_prefix=("codex",)).build_argv(
                _entry("codex"),
                Path.cwd().resolve(),
                prompt="brief",
                effort="turbo",
            )

    def test_presence_delegates_to_bootstrap_detection(self, monkeypatch):
        monkeypatch.syspath_prepend(
            str(Path(__file__).resolve().parents[2] / "plugins" / "bootstrap")
        )
        from bootstrap_lib import codex

        class Detection:
            available = False

        calls = []

        def fake_detect():
            calls.append(True)
            return Detection()

        monkeypatch.setattr(codex, "detect_codex", fake_detect)
        assert CodexAdapter().is_available() is False
        assert calls == [True]


class TestOpenCodeAdapter:
    def test_argv_uses_stdin_run_contract_and_effort_override(self, tmp_path):
        root = tmp_path.resolve()
        argv = OpencodeAdapter(argv_prefix=("opencode",)).build_argv(
            _entry("opencode", model="qwen38-local/qwen3.8-27b", effort="medium"),
            root,
            prompt="brief",
            effort="high",
        )

        assert argv == [
            "opencode",
            "run",
            "--dir",
            str(root),
            "-m",
            "qwen38-local/qwen3.8-27b",
            "--variant",
            "high",
            "--auto",
        ]
        assert "--format" not in argv
        assert OpencodeAdapter().accepted_efforts() is None

    def test_provider_variant_is_passed_through_when_nonempty(self, tmp_path):
        argv = OpencodeAdapter(argv_prefix=("opencode",)).build_argv(
            _entry("opencode"),
            tmp_path.resolve(),
            prompt="brief",
            effort="provider-specific-variant",
        )
        assert argv[-3:] == ["--variant", "provider-specific-variant", "--auto"]

    def test_no_output_file_flag_is_rejected(self, tmp_path):
        with pytest.raises(HarnessAdapterError, match="no output-file flag"):
            OpencodeAdapter(argv_prefix=("opencode",)).build_argv(
                _entry("opencode"),
                tmp_path.resolve(),
                prompt="brief",
                output_file=(tmp_path / "answer.txt").resolve(),
            )

    def test_prompt_file_becomes_stdin_payload(self, tmp_path):
        brief = (tmp_path / "brief.txt").resolve()
        brief.write_text("brief from file", encoding="utf-8")
        invocation = OpencodeAdapter(argv_prefix=("opencode",)).build_invocation(
            _entry("opencode"),
            tmp_path.resolve(),
            prompt_file=brief,
        )
        assert invocation.stdin == "brief from file"
        assert invocation.argv[0] == "opencode"

    def test_absent_cli_is_reported_without_running_it(self, monkeypatch):
        monkeypatch.setattr(harness_adapters.shutil, "which", lambda name: None)
        assert detect_opencode() is False
        assert OpencodeAdapter().is_available() is False

    def test_windows_batch_resolution_wraps_cmd_c(self, monkeypatch):
        monkeypatch.setattr(harness_adapters.os, "name", "nt")
        monkeypatch.setattr(
            harness_adapters.shutil,
            "which",
            lambda name: "C:\\tools\\opencode.CMD",
        )
        assert harness_adapters.resolve_opencode_cli() == (
            "cmd",
            "/c",
            "C:\\tools\\opencode.CMD",
        )


def test_codex_module_imports_without_bootstrap_and_fails_only_on_dispatch(tmp_path):
    """The shared library is a dispatch dependency, not an import dependency."""
    lib_root = Path(__file__).resolve().parents[2] / "plugins" / "llm-scripting-kit" / "lib"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(lib_root)
    env["ADAPTER_TEST_CWD"] = str(tmp_path.resolve())
    code = """
import os
import sys

sys.modules["bootstrap_lib"] = None
from llm_scripting_kit.harness_adapters import CodexAdapter
from llm_scripting_kit.model_endpoints import EndpointEntry, HARNESS_KIND

print("import-ok")
entry = EndpointEntry(
    "codex-entry", None, "gpt-5.6-sol", kind=HARNESS_KIND, harness="codex"
)
try:
    CodexAdapter(argv_prefix=("codex",)).build_argv(
        entry, os.environ["ADAPTER_TEST_CWD"], prompt="brief"
    )
except ModuleNotFoundError:
    print("dispatch-failed")
else:
    raise SystemExit("Codex dispatch unexpectedly succeeded without bootstrap_lib")
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["import-ok", "dispatch-failed"]
