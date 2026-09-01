import io
import json

from llm_scripting_kit import cli
from llm_scripting_kit.completion.factory import BackendSelection
from llm_scripting_kit.completion.types import LLMResponse


class FakeBackend:
    name = "fake"

    def __init__(self, response=None, error=None, halt=None):
        self.response = response
        self.error = error
        self.halt = halt
        self.call = None

    def complete(self, system, user, *, model, options=None):
        self.call = (system, user, model, options)
        if self.error:
            raise self.error
        return self.response

    def classify_halt(self, exc):
        return self.halt


def _selection(backend):
    return BackendSelection("chosen", "harness", backend, "model-id", "high")


def test_complete_reads_prompt_from_stdin_and_emits_stable_json(monkeypatch, capsys):
    backend = FakeBackend(LLMResponse(text="answer", model="model-id", input_tokens=4))
    monkeypatch.setattr(cli, "create_backend", lambda *_, **__: _selection(backend))
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("question"))

    assert cli.main(["complete", "--system", "instructions"]) == cli.EXIT_OK

    payload = json.loads(capsys.readouterr().out)
    assert payload["endpoint"] == "chosen"
    assert payload["backend"] == "fake"
    assert payload["response"]["text"] == "answer"
    assert backend.call[:3] == ("instructions", "question", "model-id")
    assert backend.call[3].effort == "high"


def test_complete_reads_utf8_files(monkeypatch, tmp_path, capsys):
    backend = FakeBackend(LLMResponse(text="ok", model="model-id"))
    monkeypatch.setattr(cli, "create_backend", lambda *_, **__: _selection(backend))
    system = tmp_path / "system.txt"
    prompt = tmp_path / "prompt.txt"
    system.write_text("指示", encoding="utf-8")
    prompt.write_text("質問", encoding="utf-8")

    assert cli.main(["complete", "--system-file", str(system), "--prompt-file", str(prompt)]) == 0
    assert backend.call[:2] == ("指示", "質問")
    json.loads(capsys.readouterr().out)


def test_complete_classified_halt_has_distinct_exit(monkeypatch, capsys):
    backend = FakeBackend(error=RuntimeError("limited"), halt="rate_limit")
    monkeypatch.setattr(cli, "create_backend", lambda *_, **__: _selection(backend))

    assert cli.main(["complete", "--prompt", "hello"]) == cli.EXIT_HALT
    # error-as-data: a failure comes back in the SAME envelope as a success,
    # on stdout, with the halt classification as the error code
    payload = json.loads(capsys.readouterr().out)
    assert payload["response"]["status"] == "error"
    assert payload["response"]["error"] == {
        "code": "rate_limit", "message": "limited"
    }
    assert payload["response"]["text"] == ""
    assert payload["backend"] == "fake"


def test_complete_failure_envelope_does_not_lie_about_a_call_that_ran(
    monkeypatch, capsys
):
    """A failed call still ran, and the envelope must not claim otherwise.

    The defaults would report started_at/ended_at as None -- which the response
    type documents as meaning no live call happened -- and an EMPTY applied-
    controls list, which asserts that the request emitted none. Both are false
    for a call that reached the adapter and failed.
    """
    backend = FakeBackend(error=RuntimeError("boom"))
    monkeypatch.setattr(cli, "create_backend", lambda *_, **__: _selection(backend))

    assert cli.main(["complete", "--prompt", "hello"]) == cli.EXIT_FAILURE
    resp = json.loads(capsys.readouterr().out)["response"]
    assert resp["started_at"] is not None
    assert resp["ended_at"] is not None
    # the CLI catches an exception and cannot know what argv the adapter built,
    # so the key is ABSENT rather than an empty list asserting "none emitted"
    assert "execution_controls_applied" not in resp
    assert "structured" not in resp


def test_complete_timeout_is_a_result_not_a_traceback(monkeypatch, capsys):
    from llm_scripting_kit.completion import AgentTimeoutError

    backend = FakeBackend(
        error=AgentTimeoutError(
            "timed out", cmd=["claude"], elapsed_s=1.0, stdout="", stderr=""
        )
    )
    monkeypatch.setattr(cli, "create_backend", lambda *_, **__: _selection(backend))

    assert cli.main(["complete", "--prompt", "hello"]) == cli.EXIT_FAILURE
    payload = json.loads(capsys.readouterr().out)
    assert payload["response"]["status"] == "timeout"
    assert payload["response"]["error"]["code"] == "execution"


def test_complete_envelope_omits_error_on_success(monkeypatch, capsys):
    backend = FakeBackend(LLMResponse(text="ok", model="model-id"))
    monkeypatch.setattr(cli, "create_backend", lambda *_, **__: _selection(backend))

    assert cli.main(["complete", "--prompt", "hello"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["response"]["status"] == "completed"
    # a null error beside a completed status is noise a consumer branches on
    assert "error" not in payload["response"]


def test_complete_text_format_states_a_failure_on_stderr(monkeypatch, capsys):
    backend = FakeBackend(error=RuntimeError("limited"), halt="rate_limit")
    monkeypatch.setattr(cli, "create_backend", lambda *_, **__: _selection(backend))

    assert cli.main(
        ["complete", "--prompt", "hello", "--format", "text"]
    ) == cli.EXIT_HALT
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "rate_limit: limited" in captured.err


def test_resolve_emits_selection(monkeypatch, capsys):
    backend = FakeBackend()
    monkeypatch.setattr(cli, "create_backend", lambda *_, **__: _selection(backend))

    assert cli.main(["resolve", "--endpoint", "chosen"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "backend": "fake", "effort": "high", "endpoint": "chosen",
        "kind": "harness", "model": "model-id",
    }
