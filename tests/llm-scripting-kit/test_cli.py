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
    payload = json.loads(capsys.readouterr().err)
    assert payload["error"] == {"kind": "rate_limit", "message": "limited"}


def test_resolve_emits_selection(monkeypatch, capsys):
    backend = FakeBackend()
    monkeypatch.setattr(cli, "create_backend", lambda *_, **__: _selection(backend))

    assert cli.main(["resolve", "--endpoint", "chosen"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "backend": "fake", "effort": "high", "endpoint": "chosen",
        "kind": "harness", "model": "model-id",
    }
