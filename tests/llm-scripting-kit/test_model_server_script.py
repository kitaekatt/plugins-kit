from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "plugins" / "llm-scripting-kit"


def _run(name: str, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(
        ["bash", str(PLUGIN / "bin" / name), *args],
        text=True,
        capture_output=True,
        check=False,
        env=merged,
    )


def test_qwen36_help_does_not_require_runtime() -> None:
    result = _run("qwen36-server", "--help")
    assert result.returncode == 0
    assert "qwen36-server" in result.stdout


def test_qwen38_help_does_not_require_runtime() -> None:
    result = _run("qwen38-server", "--help")
    assert result.returncode == 0
    assert "qwen38-server" in result.stdout


def test_path_symlink_resolves_back_to_plugin(tmp_path: Path) -> None:
    command = tmp_path / "qwen36-server"
    command.symlink_to(PLUGIN / "bin" / "qwen36-server")
    result = subprocess.run(
        ["bash", str(command), "--help"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert "qwen36-server" in result.stdout


def test_qwen36_prints_measured_mtp_profile(tmp_path: Path) -> None:
    server = tmp_path / "ninfer-serve"
    artifact = tmp_path / "model.ninfer"
    server.write_text("#!/bin/sh\n", encoding="utf-8")
    server.chmod(0o755)
    artifact.touch()
    result = _run(
        "qwen36-server",
        "--print-command",
        env={"NINFER_SERVE": str(server), "QWEN36_ARTIFACT": str(artifact)},
    )
    assert result.returncode == 0
    assert "--max-context 262144" in result.stdout
    assert "--kv-dtype int8" in result.stdout
    assert "--spec mtp" in result.stdout
    assert "--draft-tokens 3" in result.stdout
    assert "--lm-head-draft" in result.stdout


def test_qwen38_prints_full_context_gpu_profile(tmp_path: Path) -> None:
    prefix = tmp_path / "llama.cpp"
    server = prefix / "bin" / "llama-server"
    model = tmp_path / "model.gguf"
    server.parent.mkdir(parents=True)
    server.write_text("#!/bin/sh\n", encoding="utf-8")
    server.chmod(0o755)
    model.touch()
    result = _run(
        "qwen38-server",
        "--print-command",
        env={"LLAMA_CPP_PREFIX": str(prefix), "QWEN38_GGUF": str(model)},
    )
    assert result.returncode == 0
    assert "-ngl 99" in result.stdout
    assert "-c 262144" in result.stdout
    assert "--cache-type-k q8_0" in result.stdout
    assert "--host 127.0.0.1" in result.stdout
