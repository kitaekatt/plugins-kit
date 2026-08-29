"""Tests for llm_scripting_kit.completion.backends -- both transports.

Hermetic: OpenRouterBackend goes through an injected fake client (no ``openai``,
no network); ClaudeCliBackend goes through a stub runner (no real ``claude``
spawn) with ``executable=`` set so no CLI need be on PATH.
"""
from __future__ import annotations

import json
import time as _time
from pathlib import Path
from types import SimpleNamespace

import pytest

from llm_scripting_kit.completion import backends as backends_mod
from llm_scripting_kit.completion import halt
from llm_scripting_kit.completion.backends import ClaudeCliBackend, OpenRouterBackend
from llm_scripting_kit.completion.claude_runner import AgentTimeoutError
from llm_scripting_kit.completion.types import BackendOptions, LLMBackend, LLMResponse


# ---------------------------------------------------------------------------
# ClaudeCliBackend (runner seam)
# ---------------------------------------------------------------------------


def _envelope(result: str = "hello", **extra) -> str:
    data = {"result": result, "is_error": False}
    data.update(extra)
    return json.dumps(data)


class _StubRunner:
    """Records calls; serves queued (stdout, stderr, returncode) tuples.

    Runner seam signature mirrors run_claude_streaming:
    ``(cmd, request, cwd, *, log_prefix, timeout_s)``.
    """

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    def __call__(self, cmd, request, cwd, *, log_prefix, timeout_s):
        self.calls.append(
            {"cmd": list(cmd), "request": request, "cwd": cwd,
             "log_prefix": log_prefix, "timeout_s": timeout_s}
        )
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def _cli(runner, **kw) -> ClaudeCliBackend:
    """A ClaudeCliBackend with the executable fixed so no CLI need be on PATH."""
    return ClaudeCliBackend(runner=runner, executable="claude", **kw)


class TestClaudeCliBackend:
    def test_happy_path_cmd_and_response(self):
        runner = _StubRunner([(
            _envelope("hello", usage={"input_tokens": 10, "output_tokens": 5,
                                      "cache_read_input_tokens": 3}),
            "", 0,
        )])
        resp = _cli(runner).complete("SYS", "USER", model="claude-opus-4-8")

        assert resp.text == "hello"
        assert resp.model == "claude-opus-4-8"
        assert resp.input_tokens == 10
        assert resp.output_tokens == 5
        assert resp.cache_hit_tokens == 3
        assert resp.attempts == 1
        assert resp.from_cache is False

        cmd = runner.calls[0]["cmd"]
        assert cmd[0] == "claude" and cmd[1] == "-p"
        assert cmd[cmd.index("--model") + 1] == "claude-opus-4-8"
        assert cmd[cmd.index("--system-prompt") + 1] == "SYS"
        assert cmd[cmd.index("--output-format") + 1] == "json"
        assert "--no-session-persistence" in cmd
        assert cmd[cmd.index("--permission-mode") + 1] == "bypassPermissions"
        # Pure completion: no tools by default.
        assert cmd[cmd.index("--allowedTools") + 1] == ""
        assert "--effort" not in cmd
        # User prompt rides stdin (the request), not argv.
        assert runner.calls[0]["request"] == "USER"
        assert runner.calls[0]["timeout_s"] == 900.0

    def test_options_effort_tools_timeout(self):
        runner = _StubRunner([(_envelope(), "", 0)])
        _cli(runner).complete(
            "s", "u", model="opus",
            options=BackendOptions(effort="medium", allowed_tools="Read",
                                   timeout_s=300, log_prefix="[gallery]"),
        )
        call = runner.calls[0]
        cmd = call["cmd"]
        assert cmd[cmd.index("--effort") + 1] == "medium"
        assert cmd[cmd.index("--allowedTools") + 1] == "Read"
        assert call["timeout_s"] == 300
        assert call["log_prefix"] == "[gallery]"

    def test_name_and_protocol(self):
        backend = ClaudeCliBackend(executable="claude")
        assert backend.name == "claude-cli"
        assert isinstance(backend, LLMBackend)

    def test_hard_stop_429_raises_and_classifies(self):
        runner = _StubRunner([(
            json.dumps({"result": "You have hit your limit",
                        "api_error_status": 429}),
            "", 0,
        )])
        backend = _cli(runner)
        with pytest.raises(RuntimeError) as excinfo:
            backend.complete("s", "u", model="opus")
        assert backend.classify_halt(excinfo.value) == halt.HALT_RATE_LIMIT
        # No retry on hard stops.
        assert len(runner.calls) == 1

    @pytest.mark.parametrize(
        "status,body,expected",
        [
            (401, "invalid credentials", halt.HALT_AUTH),
            (429, "too many requests", halt.HALT_RATE_LIMIT),
        ],
    )
    def test_hard_stop_message_classifies_when_body_carries_no_marker(
        self, status, body, expected
    ):
        """The RAISED message must classify through the shared taxonomy.

        Regression: the raise used to emit ``(api_error_status":{status})`` --
        an opening paren where the taxonomy's marker requires a double quote --
        so ``classify_claude_exception`` returned None for this backend's OWN
        raise. ``call_llm`` then surfaced a plain RuntimeError instead of a
        HaltError and a bulk loop kept spending against a persistent failure.
        Both bodies here deliberately carry NO marker of their own, so the
        status marker in the message is the ONLY thing that can classify them.
        """
        runner = _StubRunner([
            (json.dumps({"result": body, "api_error_status": status}), "", 0),
        ])
        backend = _cli(runner)
        with pytest.raises(RuntimeError) as excinfo:
            backend.complete("s", "u", model="opus")
        assert halt.classify_halt_text(str(excinfo.value)) == expected
        assert backend.classify_halt(excinfo.value) == expected

    def test_is_error_envelope_raises(self):
        runner = _StubRunner([(
            json.dumps({"result": "something broke", "is_error": True}),
            "", 0,
        )])
        with pytest.raises(RuntimeError, match="returned error"):
            _cli(runner).complete("s", "u", model="opus")

    def test_nonzero_exit_raises(self):
        runner = _StubRunner([("", "boom", 1)])
        with pytest.raises(RuntimeError, match="exit 1"):
            _cli(runner).complete("s", "u", model="opus")

    def test_transient_500_retries_then_succeeds(self, monkeypatch):
        sleeps: list = []
        monkeypatch.setattr(_time, "sleep", lambda s: sleeps.append(s))
        runner = _StubRunner([
            (json.dumps({"result": "", "api_error_status": 500}), "", 0),
            (_envelope("recovered"), "", 0),
        ])
        resp = _cli(runner, retry_cooldown_s=60).complete("s", "u", model="opus")
        assert resp.text == "recovered"
        assert resp.attempts == 2
        assert len(runner.calls) == 2
        assert sleeps == [60]

    def test_transient_exhaustion_surfaces_last_envelope(self, monkeypatch):
        monkeypatch.setattr(_time, "sleep", lambda _s: None)
        bad = (json.dumps({"result": "", "api_error_status": 503,
                           "is_error": True}), "", 0)
        runner = _StubRunner([bad, bad, bad])
        with pytest.raises(RuntimeError, match="returned error"):
            _cli(runner, retry_max_attempts=3).complete("s", "u", model="opus")
        assert len(runner.calls) == 3

    def test_timeout_propagates_typed_and_classifies(self):
        exc = AgentTimeoutError(
            "timed out", cmd=["claude", "-p"], elapsed_s=901,
            stdout="partial", stderr="quiet",
        )
        runner = _StubRunner([exc])
        backend = _cli(runner)
        with pytest.raises(AgentTimeoutError) as excinfo:
            backend.complete("s", "u", model="opus")
        # Identity preserved -- orchestrator isinstance checks depend on it.
        assert excinfo.value is exc
        assert backend.classify_halt(excinfo.value) == halt.HALT_RATE_LIMIT

    def test_timeout_diagnostics_dump(self, tmp_path: Path):
        exc = AgentTimeoutError(
            "timed out",
            cmd=["claude", "-p", "--system-prompt", "S" * 100],
            elapsed_s=901, stdout="OUT", stderr="ERR",
        )
        runner = _StubRunner([exc])
        backend = _cli(runner, diagnostics_dir=tmp_path)
        with pytest.raises(AgentTimeoutError):
            backend.complete("s", "u", model="opus")

        dumps = list(tmp_path.glob("timeout_*.log"))
        assert len(dumps) == 1
        body = dumps[0].read_text(encoding="utf-8")
        assert "elapsed 901s" in body
        assert "OUT" in body and "ERR" in body
        # System prompt body redacted from the cmd line.
        assert "S" * 100 not in body
        assert "<100 chars>" in body

    def test_ignores_cache_salt_and_completion_knobs(self):
        """No response cache; a salted / temperature-bearing options object is
        accepted and ignored (documented behavior)."""
        runner = _StubRunner([(_envelope("hello"), "", 0)])
        resp = _cli(runner).complete(
            "SYS", "USER", model="claude-x",
            options=BackendOptions(cache_salt=3, temperature=0.9, max_tokens=1),
        )
        assert resp.text == "hello"
        cmd = runner.calls[0]["cmd"]
        assert "--temperature" not in cmd and "--max-tokens" not in cmd


def test_resolve_claude_executable_errors_clearly(monkeypatch):
    monkeypatch.setattr(backends_mod.shutil, "which", lambda _n: None)
    with pytest.raises(RuntimeError, match="Could not locate the `claude` CLI"):
        backends_mod._resolve_claude_executable()


# ---------------------------------------------------------------------------
# OpenRouterBackend (injected fake client)
# ---------------------------------------------------------------------------


class _FakeUsage:
    def __init__(self):
        self.prompt_tokens = 100
        self.completion_tokens = 20
        self.prompt_tokens_details = {"cached_tokens": 40}


class _FakeMessage:
    content = "router-response"
    reasoning_content = None


class _FakeChoice:
    message = _FakeMessage()
    finish_reason = "stop"


class _FakeResponse:
    choices = [_FakeChoice()]
    usage = _FakeUsage()


class _FakeCompletions:
    def __init__(self, sink):
        self._sink = sink

    def create(self, **kwargs):
        self._sink.append(kwargs)
        return _FakeResponse()


class _FakeChat:
    def __init__(self, sink):
        self.completions = _FakeCompletions(sink)


class _FakeClient:
    def __init__(self):
        self.sink = []
        self.chat = _FakeChat(self.sink)


class TestOpenRouterBackend:
    def test_name_and_protocol(self):
        backend = OpenRouterBackend()
        assert backend.name == "openrouter"
        assert isinstance(backend, LLMBackend)

    def test_parses_response_and_cache_hit_tokens(self):
        client = _FakeClient()
        resp = OpenRouterBackend(client=client).complete("sys", "usr", model="test/slug")
        assert isinstance(resp, LLMResponse)
        assert resp.text == "router-response"
        assert resp.input_tokens == 100
        assert resp.output_tokens == 20
        assert resp.cache_hit_tokens == 40
        assert resp.model == "test/slug"  # concrete slug used verbatim

    def test_marks_system_cache_control(self):
        client = _FakeClient()
        OpenRouterBackend(client=client).complete("SYSTEM", "USER", model="test/slug")
        messages = client.sink[0]["messages"]
        system_msg = messages[0]
        assert system_msg["role"] == "system"
        assert isinstance(system_msg["content"], list)
        assert system_msg["content"][0]["cache_control"] == {"type": "ephemeral"}
        # Plain user message when no prefix.
        assert messages[1]["content"] == "USER"

    def test_user_prefix_two_part_content(self):
        client = _FakeClient()
        OpenRouterBackend(client=client).complete(
            "s", "u", model="test/slug",
            options=BackendOptions(user_cache_prefix="PREAMBLE"),
        )
        user_msg = client.sink[0]["messages"][1]
        assert isinstance(user_msg["content"], list)
        assert user_msg["content"][0]["text"] == "PREAMBLE"
        assert user_msg["content"][0]["cache_control"] == {"type": "ephemeral"}
        assert user_msg["content"][1]["text"] == "u"

    def test_timeout_option_threads_through(self):
        client = _FakeClient()
        OpenRouterBackend(client=client).complete(
            "s", "u", model="test/slug", options=BackendOptions(timeout_s=42),
        )
        assert client.sink[0]["timeout"] == 42

    def test_max_tokens_and_temperature_thread_through(self):
        client = _FakeClient()
        OpenRouterBackend(client=client).complete(
            "s", "u", model="test/slug",
            options=BackendOptions(max_tokens=256, temperature=0.7),
        )
        assert client.sink[0]["max_tokens"] == 256
        assert client.sink[0]["temperature"] == 0.7

    def test_reasoning_truncation_is_not_returned_as_empty_success(self):
        client = _FakeClient()
        client.chat.completions.create = lambda **_kwargs: SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(
                    content="",
                    reasoning_content="thinking without a final answer",
                ),
                finish_reason="length",
            )],
            usage=_FakeUsage(),
        )

        with pytest.raises(
            RuntimeError,
            match=r"exhausted max_tokens=4096.*finish_reason=length",
        ):
            OpenRouterBackend(client=client).complete(
                "s", "u", model="test/slug"
            )

    def test_classify_halt_uses_openai_taxonomy(self):
        assert OpenRouterBackend().classify_halt(ValueError("boom")) is None

    def test_classify_halt_builds_no_client(self, monkeypatch):
        """classify_halt must never trigger the lazy build.

        The platform calls it unguarded from inside an ``except`` block, so a
        build there would raise a SECOND exception while handling the first --
        and, on the failure this lock exists to prevent, would take another
        file descriptor at exactly the moment the process has run out.
        """
        from llm_scripting_kit import client as client_mod

        def _explode(*_a, **_kw):
            raise AssertionError("classify_halt must not build a client")

        monkeypatch.setattr(client_mod, "make_openai_client", _explode)
        assert OpenRouterBackend().classify_halt(ValueError("boom")) is None

    def test_concurrent_first_wave_builds_exactly_one_client(self, monkeypatch):
        """N threads racing the lazy build must produce exactly ONE client.

        ``_ensure_client`` was an unsynchronized check-then-assign: every
        thread in a concurrent first wave found ``self.client`` unset and built
        its own. Each build calls
        ``ssl.create_default_context(cafile=certifi.where())`` and takes a file
        descriptor, so the surplus clients are a real fd leak -- measured
        downstream at 509 distinct clients and 391 ``[Errno 24] Too many open
        files`` errors from 900 threads through a single shared transport, on
        Windows' 512-entry table.

        The sleep in the factory is what makes this test meaningful. A draft
        written downstream with an instantaneous stub PASSED against the broken
        code, because the race window closes if the build returns before the
        next thread is scheduled. A real build does key resolution off disk and
        SSL context setup -- tens of milliseconds -- so the delay models the
        production timing rather than defeating it.
        """
        import threading

        from llm_scripting_kit import client as client_mod

        n_threads = 24
        built = []
        built_lock = threading.Lock()

        def slow_factory(*_args, **_kwargs):
            _time.sleep(0.05)
            client = object()
            with built_lock:
                built.append(client)
            return client

        monkeypatch.setattr(client_mod, "make_openai_client", slow_factory)

        backend = OpenRouterBackend()
        gate = threading.Barrier(n_threads)
        seen = []
        seen_lock = threading.Lock()

        def worker() -> None:
            gate.wait()
            client = backend._ensure_client()
            with seen_lock:
                seen.append(client)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(built) == 1, (
            f"unsynchronized lazy build produced {len(built)} clients for "
            f"{n_threads} threads -- each one holds a file descriptor"
        )
        # Every thread must observe the SAME client (the gate opens on "client
        # exists", so a late thread never gets a different instance).
        assert len(seen) == n_threads
        assert all(c is built[0] for c in seen)
        assert backend.client is built[0]


# ---------------------------------------------------------------------------
# extras -> extra_body forwarding, and the keyless client build
# ---------------------------------------------------------------------------


class TestOpenRouterExtras:
    def test_extras_arrive_as_extra_body(self):
        client = _FakeClient()
        OpenRouterBackend(client=client).complete(
            "s", "u", model="test/slug",
            options=BackendOptions(extras={"reasoning_effort": "medium"}),
        )
        assert client.sink[0]["extra_body"] == {"reasoning_effort": "medium"}

    def test_extra_body_is_a_copy_not_the_callers_mapping(self):
        client = _FakeClient()
        extras = {"reasoning_effort": "high"}
        OpenRouterBackend(client=client).complete(
            "s", "u", model="test/slug", options=BackendOptions(extras=extras),
        )
        assert client.sink[0]["extra_body"] is not extras

    def test_empty_extras_add_no_extra_body_key(self):
        client = _FakeClient()
        OpenRouterBackend(client=client).complete("s", "u", model="test/slug")
        assert "extra_body" not in client.sink[0]


class TestKeylessClientBuild:
    def test_keyless_endpoint_builds_without_a_key_in_the_environment(
        self, monkeypatch
    ):
        """A keyless endpoint must not consult key resolution at all."""
        from llm_scripting_kit import client as client_mod

        cfg = {
            "endpoints": {
                "keyless-local": {
                    "base_url": "http://localhost:8080/v1",
                    "key_env": None,
                },
            },
            "models": {},
        }
        monkeypatch.setattr(
            "llm_scripting_kit.models.load_model_config", lambda **kw: cfg
        )
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

        def _boom(*a, **kw):  # pragma: no cover - asserted not to run
            raise AssertionError("get_api_key must not be called for a keyless endpoint")

        monkeypatch.setattr(client_mod, "get_api_key", _boom)

        captured = {}

        class _FakeOpenAI:
            def __init__(self, *, api_key, base_url):
                captured["api_key"] = api_key
                captured["base_url"] = base_url

        import sys
        import types

        fake_openai = types.ModuleType("openai")
        fake_openai.OpenAI = _FakeOpenAI
        monkeypatch.setitem(sys.modules, "openai", fake_openai)

        client_mod.make_openai_client(endpoint="keyless-local")
        assert captured["base_url"] == "http://localhost:8080/v1"
        assert captured["api_key"] == client_mod.KEYLESS_API_KEY
        assert captured["api_key"]  # the SDK requires a truthy key
