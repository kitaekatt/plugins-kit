"""Tests for llm_scripting_kit.completion.opencode_backend.

Hermetic throughout: the adapter and runner seams are injected or patched, so
the tests never require an OpenCode executable, a configured provider, or a
live model server.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from llm_scripting_kit.completion import halt
from llm_scripting_kit.completion.claude_runner import AgentTimeoutError
from llm_scripting_kit.harness_adapters import HarnessInvocation
from llm_scripting_kit.completion.opencode_backend import (
    DEFAULT_OPENCODE_TIMEOUT_S,
    OPENCODE_FILESYSTEM_POSTURE,
    OpencodeCliBackend,
    OpencodeRunError,
    PROMPT_SEPARATOR,
    compose_prompt,
)
from llm_scripting_kit.completion.types import BackendOptions, LLMBackend


class _StubRunner:
    """Return a chosen runner result while recording the complete call."""

    def __init__(self, result=("answer", "", 0), raises=None):
        self.result = result
        self.raises = raises
        self.calls = []

    def __call__(self, cmd, request, cwd, **kwargs):
        self.calls.append(
            {"cmd": list(cmd), "request": request, "cwd": cwd, **kwargs}
        )
        if self.raises is not None:
            raise self.raises
        return self.result


def _backend(runner, **kwargs) -> OpencodeCliBackend:
    return OpencodeCliBackend(
        runner=runner,
        argv_prefix=("opencode-test",),
        **kwargs,
    )


def test_name_and_protocol():
    backend = OpencodeCliBackend(argv_prefix=("opencode-test",))
    assert backend.name == "opencode-cli"
    assert isinstance(backend, LLMBackend)


def test_complete_delegates_argv_and_stdin_to_the_adapter(tmp_path, monkeypatch):
    """The backend must not become a second owner of OpenCode's argv grammar."""
    calls = []

    def fake_build_invocation(adapter, entry, cwd, **kwargs):
        calls.append({"entry": entry, "cwd": cwd, **kwargs})
        return HarnessInvocation(argv=("delegated", "argv"), stdin="delegated brief")

    from llm_scripting_kit.completion import opencode_backend

    monkeypatch.setattr(
        opencode_backend.OpencodeAdapter,
        "build_invocation",
        fake_build_invocation,
    )
    runner = _StubRunner(result=("answer", "", 0))
    root = tmp_path.resolve()

    response = _backend(runner).complete(
        "SYSTEM", "USER", model="provider/model",
        options=BackendOptions(cwd=root, effort="high"),
    )

    assert response.text == "answer"
    assert runner.calls[0]["cmd"] == ["delegated", "argv"]
    assert runner.calls[0]["request"] == "delegated brief"
    assert calls[0]["cwd"] == root
    assert calls[0]["prompt"] == "SYSTEM\n\n---\n\nUSER"
    assert calls[0]["effort"] == "high"
    assert calls[0]["entry"].model == "provider/model"
    assert calls[0]["entry"].harness == "opencode"


def test_complete_returns_stdout_without_json_or_result_file(tmp_path):
    runner = _StubRunner(result=("stdout answer\n", "diagnostic", 0))
    response = _backend(runner).complete(
        "s", "u", model="provider/model",
        options=BackendOptions(cwd=tmp_path.resolve()),
    )

    assert response.text == "stdout answer\n"
    assert response.model == "provider/model"
    assert response.wall_ms >= 0
    assert response.attempts == 1
    assert response.from_cache is False
    assert (
        response.input_tokens,
        response.output_tokens,
        response.cache_hit_tokens,
        response.total_tokens,
    ) == (0, 0, 0, 0)


def test_inapplicable_options_are_not_forwarded_to_the_adapter(
    tmp_path, monkeypatch
):
    calls = []

    def fake_build_invocation(adapter, entry, cwd, **kwargs):
        calls.append(kwargs)
        return HarnessInvocation(argv=("delegated",), stdin="brief")

    from llm_scripting_kit.completion import opencode_backend

    monkeypatch.setattr(
        opencode_backend.OpencodeAdapter,
        "build_invocation",
        fake_build_invocation,
    )
    runner = _StubRunner()
    _backend(runner).complete(
        "s", "u", model="provider/model",
        options=BackendOptions(
            cwd=tmp_path.resolve(),
            max_tokens=7,
            temperature=0.9,
            cache_salt=3,
            user_cache_prefix="prefix",
            allowed_tools="Read",
            extras={"not-an-opencode-flag": True},
        ),
    )

    # Only the adapter-owned variant is applicable; the union's other fields
    # do not leak into a hand-built command or prompt transformation.
    assert calls == [{"prompt": "s\n\n---\n\nu", "effort": None}]


def test_prompt_composition_matches_the_single_stdin_channel():
    assert compose_prompt("SYSTEM", "USER") == f"SYSTEM{PROMPT_SEPARATOR}USER"
    assert compose_prompt("", "USER") == "USER"
    assert compose_prompt("SYSTEM", "") == "SYSTEM"


def test_nonzero_exit_is_a_transport_error_and_keeps_channels_on_attributes(
    tmp_path: Path,
):
    stdout = "provider output mentioning a rate limit"
    stderr = "{\"name\":\"UnknownError\"}"
    runner = _StubRunner(result=(stdout, stderr, 1))
    backend = _backend(runner)

    with pytest.raises(OpencodeRunError) as excinfo:
        backend.complete(
            "s", "u", model="provider/model",
            options=BackendOptions(cwd=tmp_path.resolve()),
        )

    exc = excinfo.value
    assert exc.returncode == 1
    assert exc.stdout == stdout
    assert exc.stderr == stderr
    assert stdout not in str(exc)
    assert stderr not in str(exc)
    # A nonzero exit is a transport failure, not a persistent halt inferred
    # from arbitrary output text.
    assert backend.classify_halt(exc) is None


def test_timeout_is_a_transport_error_and_model_text_stays_out_of_message(
    tmp_path: Path,
):
    model_text = "The answer discusses a rate limit and unauthorized access."
    timeout = AgentTimeoutError(
        "opencode run exceeded 120s timeout\nstdout tail:\n" + model_text,
        cmd=["opencode", "run"],
        elapsed_s=120,
        stdout=model_text,
        stderr="quiet",
    )
    runner = _StubRunner(raises=timeout)
    backend = _backend(runner)

    with pytest.raises(AgentTimeoutError) as excinfo:
        backend.complete(
            "s", "u", model="provider/model",
            options=BackendOptions(cwd=tmp_path.resolve()),
        )

    # Preserve the runner's distinct timeout type and diagnostic attributes,
    # while sanitizing its historical inline channel tails for this transport.
    assert excinfo.value is timeout
    assert timeout.stdout == model_text
    assert model_text not in str(timeout)
    assert backend.classify_halt(timeout) is None


def test_zero_exit_with_failure_shaped_output_is_still_an_answer(tmp_path: Path):
    failure_shaped = '{"name":"UnknownError","message":"provider failed"}'
    runner = _StubRunner(result=(failure_shaped, "", 0))
    response = _backend(runner).complete(
        "s", "u", model="provider/model",
        options=BackendOptions(cwd=tmp_path.resolve()),
    )

    assert response.text == failure_shaped


def test_model_authored_text_never_reaches_nonzero_exception_message(
    tmp_path: Path,
):
    chatter = (
        "I can explain rate limits and unauthorized requests, but this answer "
        "is otherwise healthy."
    )
    runner = _StubRunner(result=(chatter, chatter, 1))

    with pytest.raises(OpencodeRunError) as excinfo:
        _backend(runner).complete(
            "s", "u", model="provider/model",
            options=BackendOptions(cwd=tmp_path.resolve()),
        )

    exc = excinfo.value
    assert chatter not in str(exc)
    assert exc.stdout == chatter and exc.stderr == chatter
    assert halt.classify_opencode_exception(exc) is None


def test_timeout_is_passed_explicitly_and_default_exceeds_refusal_window(tmp_path):
    runner = _StubRunner()
    _backend(runner).complete(
        "s", "u", model="provider/model",
        options=BackendOptions(cwd=tmp_path.resolve()),
    )
    assert DEFAULT_OPENCODE_TIMEOUT_S == 120.0
    assert runner.calls[0]["timeout_s"] == DEFAULT_OPENCODE_TIMEOUT_S
    assert runner.calls[0]["label"] == "opencode run"
    assert runner.calls[0]["hard_stop_markers"] == ()

    runner2 = _StubRunner()
    _backend(runner2).complete(
        "s", "u", model="provider/model",
        options=BackendOptions(cwd=tmp_path.resolve(), timeout_s=7),
    )
    assert runner2.calls[0]["timeout_s"] == 7


def test_filesystem_posture_is_explicitly_unconfined(tmp_path, capsys):
    runner = _StubRunner()
    backend = _backend(runner)
    backend.complete(
        "s", "u", model="provider/model",
        options=BackendOptions(cwd=tmp_path.resolve(), log_prefix="[oc]"),
    )

    assert backend.filesystem_posture == OPENCODE_FILESYSTEM_POSTURE
    assert OPENCODE_FILESYSTEM_POSTURE == "unconfined"
    notice = capsys.readouterr().err
    assert "--auto bypasses permissions" in notice
    assert "does not confine writes" in notice


class TestHaltClassificationScansStderrOnly:
    """The channel asymmetry is probe-backed; these pin both halves of it.

    OpenCode puts the model's words on stdout and its own framing on stderr
    (observed 2026-08-26, opencode 1.18.23). So stderr may be scanned for halt
    vocabulary and stdout may never be: scanning stdout would let a healthy run
    that merely discusses a rate limit abort the caller's whole run.
    """

    def _error(self, *, stdout: str = "", stderr: str = ""):
        from llm_scripting_kit.completion.opencode_backend import OpencodeRunError

        return OpencodeRunError(
            "opencode run failed (exit 1)",
            stdout=stdout,
            stderr=stderr,
            returncode=1,
        )

    def test_an_observed_auth_failure_classifies_as_a_halt(self):
        # Read off a real run: an OpenRouter provider with an invalid key exits
        # 1 with empty stdout and stderr "Error: User not found."
        from llm_scripting_kit.completion.halt import (
            HALT_AUTH,
            classify_opencode_exception,
        )

        exc = self._error(stderr="\nError: User not found.\n")
        assert classify_opencode_exception(exc) == HALT_AUTH

    def test_a_standard_envelope_on_stderr_classifies(self):
        from llm_scripting_kit.completion.halt import (
            HALT_RATE_LIMIT,
            classify_opencode_exception,
        )

        exc = self._error(stderr='Error: {"api_error_status":429}')
        assert classify_opencode_exception(exc) == HALT_RATE_LIMIT

    def test_model_prose_on_stdout_cannot_forge_a_halt(self):
        # The whole reason stdout is excluded. A model asked to say these words
        # really does put them on stdout -- verified against a live run.
        from llm_scripting_kit.completion.halt import classify_opencode_exception

        exc = self._error(
            stdout="BANANA rate limit exceeded insufficient credit "
            'authentication_error "api_error_status":401',
            stderr="",
        )
        assert classify_opencode_exception(exc) is None

    def test_a_transport_only_failure_is_not_a_halt(self):
        from llm_scripting_kit.completion.halt import classify_opencode_exception

        exc = self._error(
            stderr="Error: Cannot connect to API: Unable to connect. "
            "Is the computer able to access the url?"
        )
        assert classify_opencode_exception(exc) is None
