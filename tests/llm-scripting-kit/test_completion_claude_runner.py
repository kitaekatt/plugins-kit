"""Tests for llm_scripting_kit.completion.claude_runner -- the claude -p runner.

The real ``claude`` CLI is never spawned. Each test drives the runner against a
tiny ``python -c`` child that mimics one facet of the CLI's behavior: a hang, a
utf-8 echo, or a hard-stop marker on stderr.

Non-ASCII note: the CJK literal in the utf-8 round-trip test is the point of
that test -- it proves the pipes survive bytes the Windows cp1252 locale codec
cannot encode. Everything else stays ASCII.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from llm_scripting_kit.completion.claude_runner import (
    AgentTimeoutError,
    HARD_STOP_STDERR_MARKERS,
    looks_like_hard_stop,
    run_claude_streaming,
)


# ---- looks_like_hard_stop -------------------------------------------------

def test_hard_stop_detects_429_marker():
    assert looks_like_hard_stop('"api_error_status":429')
    assert looks_like_hard_stop("Hit your limit")  # case-insensitive


def test_hard_stop_detects_401_markers():
    assert looks_like_hard_stop("authentication_error")
    assert looks_like_hard_stop('"api_error_status":401')
    assert looks_like_hard_stop("Invalid Authentication Credentials")


def test_hard_stop_ignores_unrelated_text():
    assert not looks_like_hard_stop("everything is fine")
    assert not looks_like_hard_stop("")


def test_hard_stop_custom_marker_tuple():
    """A caller may pass its own marker tuple; the default is unchanged."""
    assert looks_like_hard_stop("custom-halt", markers=("custom-halt",))
    assert not looks_like_hard_stop("custom-halt")


def test_hard_stop_markers_tuple_is_the_canonical_five():
    """The shared marker tuple is the five markers the source pipelines used."""
    assert HARD_STOP_STDERR_MARKERS == (
        "hit your limit",
        '"api_error_status":429',
        '"api_error_status":401',
        "authentication_error",
        "invalid authentication credentials",
    )


# ---- run_claude_streaming: timeout ----------------------------------------

def test_times_out_on_hung_subprocess(tmp_path: Path):
    """A subprocess that never emits stdout and never exits is killed at the
    per-call timeout and raises the typed AgentTimeoutError."""
    cmd = [sys.executable, "-c", "import time; time.sleep(30)"]

    start = time.monotonic()
    with pytest.raises(AgentTimeoutError) as exc_info:
        run_claude_streaming(
            cmd, "ignored request", tmp_path,
            log_prefix="[test]", timeout_s=1,
        )
    elapsed = time.monotonic() - start

    msg = str(exc_info.value)
    assert "exceeded" in msg and "timeout" in msg
    # Bounded: the 30s child sleep must not be what ended the call.
    assert elapsed < 10


def test_timeout_error_carries_cmd_and_elapsed(tmp_path: Path):
    """AgentTimeoutError carries cmd / elapsed_s / stdout / stderr so a caller
    can dump diagnostics without re-deriving them."""
    cmd = [sys.executable, "-c", "import time; time.sleep(30)"]

    with pytest.raises(AgentTimeoutError) as exc_info:
        run_claude_streaming(
            cmd, "req", tmp_path, log_prefix="[test]", timeout_s=1,
        )

    exc = exc_info.value
    assert exc.cmd == cmd
    assert isinstance(exc.elapsed_s, int)
    assert exc.elapsed_s >= 1
    assert isinstance(exc.stdout, str)
    assert isinstance(exc.stderr, str)


# ---- run_claude_streaming: utf-8 round trip -------------------------------

def test_round_trips_cjk_through_utf8_pipes(tmp_path: Path):
    """CJK text survives the stdin -> child -> stdout round trip intact.

    On Windows the locale codec is cp1252; without encoding="utf-8" on the
    Popen, writing CJK forms to stdin raises UnicodeEncodeError (or mangles
    bytes). The child runs in UTF-8 mode (-X utf8) so its side is deterministic;
    the parent side is what this exercises.
    """
    request = "Locked atoms: Sakura -> 樱; sea -> 海\n"
    cmd = [
        sys.executable, "-X", "utf8", "-c",
        "import sys; sys.stdout.write(sys.stdin.read())",
    ]
    stdout, stderr, returncode = run_claude_streaming(
        cmd, request, tmp_path, log_prefix="[test]", timeout_s=30,
    )
    assert returncode == 0
    assert stdout == request


def test_popen_uses_utf8_encoding(tmp_path: Path, monkeypatch):
    """The Popen seam is invoked with encoding="utf-8" explicitly so the fix
    cannot regress into relying on the machine's locale codec."""
    captured: dict = {}
    real_popen = subprocess.Popen

    def spy_popen(*args, **kwargs):
        captured.update(kwargs)
        return real_popen(*args, **kwargs)

    import llm_scripting_kit.completion.claude_runner as runner_mod
    monkeypatch.setattr(runner_mod.subprocess, "Popen", spy_popen)

    cmd = [sys.executable, "-c", "import sys; sys.stdin.read()"]
    run_claude_streaming(
        cmd, "x", tmp_path, log_prefix="[test]", timeout_s=30,
    )
    assert captured.get("encoding") == "utf-8"
    assert captured.get("errors") == "replace"


# ---- run_claude_streaming: hard-stop on stderr ----------------------------

def test_hard_stop_marker_on_stderr_kills_and_raises(tmp_path: Path):
    """A child that emits a hard-stop marker on stderr is killed and the runner
    raises RuntimeError with the matched marker substring."""
    child = (
        "import sys, time; "
        "sys.stderr.write('boom: \"api_error_status\":429 hit your limit\\n'); "
        "sys.stderr.flush(); "
        "time.sleep(30)"
    )
    cmd = [sys.executable, "-c", child]

    start = time.monotonic()
    with pytest.raises(RuntimeError) as exc_info:
        run_claude_streaming(
            cmd, "req", tmp_path, log_prefix="[test]", timeout_s=30,
        )
    elapsed = time.monotonic() - start

    msg = str(exc_info.value)
    assert "hard-stop" in msg
    assert "429" in msg or "hit your limit" in msg.lower()
    # Killed promptly -- not via the 30s sleep nor the 30s timeout.
    assert elapsed < 15


def test_clean_exit_returns_streams_and_returncode(tmp_path: Path):
    """A child that echoes stdin and exits 0 returns its stdout intact."""
    cmd = [
        sys.executable, "-c",
        "import sys; sys.stdout.write('echo:' + sys.stdin.read())",
    ]
    stdout, stderr, returncode = run_claude_streaming(
        cmd, "payload", tmp_path, log_prefix="[test]", timeout_s=30,
    )
    assert returncode == 0
    assert stdout == "echo:payload"
