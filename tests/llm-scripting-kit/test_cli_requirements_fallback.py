"""``--requirements`` on ``complete``/``resolve`` and ``complete`` fallback over ``--models``.

No model is called: entries, reachability and backends are injected. The real
``declaration.run``/``describe`` do the selection, so the kit's rule (not a
stub) decides which entry serves.
"""
from __future__ import annotations

import functools
import io
import json

import pytest

from llm_scripting_kit import cli
from llm_scripting_kit import declaration as decl
from llm_scripting_kit.completion.halt import HALT_AUTH
from llm_scripting_kit.completion.types import LLMResponse
from llm_scripting_kit.model_endpoints import HARNESS_KIND, TRANSPORT_KIND, EndpointEntry
from llm_scripting_kit.reachability import STATUS_REACHABLE, Reachability

from test_declaration_skill_context import _Backend, _Halt, _Selection


def _entries():
    return {
        "opus": EndpointEntry(id="opus", base_url=None, model="opus-m", kind=HARNESS_KIND,
                              harness="claude", tier=None, family=None, conserve_usage=None),
        "or-a": EndpointEntry(id="or-a", base_url="http://a.invalid/v1", model="a/slug"),
        "or-b": EndpointEntry(id="or-b", base_url="http://b.invalid/v1", model="b/slug"),
    }


_KINDS = {"opus": HARNESS_KIND, "or-a": TRANSPORT_KIND, "or-b": TRANSPORT_KIND}


def _wire(monkeypatch, backends):
    def factory(name, **_kw):
        return _Selection(name, _KINDS[name], backends[name], _entries()[name].model)

    monkeypatch.setattr(cli, "create_backend", factory)
    reach = {n: Reachability(status=STATUS_REACHABLE, checked="t", detail="ok") for n in _entries()}
    monkeypatch.setattr(
        cli, "declaration_run",
        functools.partial(decl.run, entries=_entries(), reachability_cache=reach),
    )
    monkeypatch.setattr(
        cli, "describe",
        functools.partial(decl.describe, entries=_entries(), reachability_cache=reach),
    )
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("question"))


def _ok(text="ok"):
    return LLMResponse(text=text, model="served")


def _main(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_multi_model_falls_back_on_classified_halt(monkeypatch, capsys):
    backends = {"or-a": _Backend("openrouter", [_Halt(HALT_AUTH)]),
                "or-b": _Backend("openrouter", [_ok("from-b")])}
    _wire(monkeypatch, backends)
    code, out, _ = _main(capsys, "complete", "--models", "or-a,or-b")
    env = json.loads(out)
    assert code == 0
    assert env["entry"] == "or-b" and env["run_status"] == "completed"
    assert env["response"]["text"] == "from-b" and env["endpoint"] == "or-b"
    assert [a["entry"] for a in env["attempts"]] == ["or-a", "or-b"]
    assert env["attempts"][0]["outcome"] == "halted" and env["attempts"][0]["halt"] == HALT_AUTH
    assert env["attempts"][1]["outcome"] == "completed"
    assert env["declaration"] == ["or-a", "or-b"]
    assert env["protocol"]  # existing keys stay


def test_all_entries_halting_reports_attempts_and_halt_exit(monkeypatch, capsys):
    backends = {"or-a": _Backend("openrouter", [_Halt(HALT_AUTH)]),
                "or-b": _Backend("openrouter", [_Halt(HALT_AUTH)])}
    _wire(monkeypatch, backends)
    code, out, _ = _main(capsys, "complete", "--models", "or-a,or-b")
    env = json.loads(out)
    assert code == cli.EXIT_HALT
    assert env["response"]["status"] == "error"
    assert env["response"]["error"]["code"] == HALT_AUTH
    assert len(env["attempts"]) == 2 and env["run_status"] == "attempt-limit"


def test_exhausting_every_entry_below_the_attempt_limit_reports_the_floor(monkeypatch, capsys):
    backends = {"or-a": _Backend("openrouter", [_Halt(HALT_AUTH)]),
                "or-b": _Backend("openrouter", [_Halt(HALT_AUTH)])}
    _wire(monkeypatch, backends)
    code, out, _ = _main(capsys, "complete", "--models", "or-a,or-b", "--max-attempts", "5")
    env = json.loads(out)
    assert code == cli.EXIT_HALT and env["run_status"] == "no-usable-entry"
    assert len(env["attempts"]) == 2 and env["floor"]


def test_max_attempts_one_stops_after_first_halt(monkeypatch, capsys):
    backends = {"or-a": _Backend("openrouter", [_Halt(HALT_AUTH)]),
                "or-b": _Backend("openrouter", [_ok()])}
    _wire(monkeypatch, backends)
    code, out, _ = _main(capsys, "complete", "--models", "or-a,or-b", "--max-attempts", "1")
    env = json.loads(out)
    assert code == cli.EXIT_HALT and env["run_status"] == "attempt-limit"
    assert backends["or-b"].calls == 0


def test_task_error_does_not_fall_back(monkeypatch, capsys):
    backends = {"or-a": _Backend("openrouter", [RuntimeError("bad prompt")]),
                "or-b": _Backend("openrouter", [_ok()])}
    _wire(monkeypatch, backends)
    code, out, _ = _main(capsys, "complete", "--models", "or-a,or-b")
    env = json.loads(out)
    assert code == cli.EXIT_FAILURE and env["run_status"] == "failed"
    assert env["response"]["error"]["code"] == "execution"
    assert backends["or-b"].calls == 0


def test_requirements_skip_entries_that_fail_them(monkeypatch, capsys, tmp_path):
    backends = {"opus": _Backend("claude-cli", []), "or-a": _Backend("openrouter", [_ok("a")])}
    _wire(monkeypatch, backends)
    path = tmp_path / "req.json"
    path.write_text(json.dumps({"skill_context": True}), encoding="utf-8")
    code, out, _ = _main(
        capsys, "complete", "--models", "opus,or-a", "--requirements", str(path)
    )
    env = json.loads(out)
    assert code == 0 and env["entry"] == "or-a"
    assert backends["opus"].calls == 0


def test_inline_requirements_json_on_a_single_model_is_routed(monkeypatch, capsys):
    backends = {"or-a": _Backend("openrouter", [_ok("a")])}
    _wire(monkeypatch, backends)
    code, out, _ = _main(capsys, "complete", "--models", "or-a", "--requirements", "{}")
    env = json.loads(out)
    assert code == 0 and env["entry"] == "or-a" and len(env["attempts"]) == 1


def test_requirements_without_models_is_a_usage_error(monkeypatch, capsys):
    _wire(monkeypatch, {})
    code, _, err = _main(capsys, "complete", "--requirements", "{}")
    assert code == cli.EXIT_USAGE and "--models" in err


def test_requirements_not_json_is_a_usage_error(monkeypatch, capsys):
    _wire(monkeypatch, {})
    code, _, err = _main(capsys, "resolve", "--models", "or-a", "--requirements", "{nope")
    assert code == cli.EXIT_USAGE and "not valid JSON" in err


def test_single_model_without_requirements_keeps_the_original_shape(monkeypatch, capsys):
    backend = _Backend("openrouter", [_ok("solo")])
    _wire(monkeypatch, {"or-a": backend})
    monkeypatch.setattr(cli, "_first_usable", lambda ids, _root: ids[0])
    code, out, _ = _main(capsys, "complete", "--models", "or-a")
    env = json.loads(out)
    assert code == 0 and env["response"]["text"] == "solo"
    assert "attempts" not in env and "entry" not in env


def test_resolve_applies_requirements(monkeypatch, capsys):
    backends = {"opus": _Backend("claude-cli", []), "or-a": _Backend("openrouter", [])}
    _wire(monkeypatch, backends)
    code, out, _ = _main(
        capsys, "resolve", "--models", "opus,or-a", "--requirements",
        json.dumps({"skill_context": True}),
    )
    assert code == 0 and json.loads(out)["endpoint"] == "or-a"


def test_request_file_refuses_requirements_and_max_attempts(monkeypatch, capsys, tmp_path):
    _wire(monkeypatch, {})
    req = tmp_path / "r.json"
    req.write_text("{}", encoding="utf-8")
    code, _, err = _main(
        capsys, "complete", "--request-file", str(req), "--requirements", "{}", "--max-attempts", "2"
    )
    assert code == cli.EXIT_PROTOCOL and "--requirements" in err and "--max-attempts" in err


class _Resp:
    headers = {"retry-after": "3"}


class _Backpressure(Exception):
    kind = "backpressure"
    status_code = 429
    response = _Resp()

    def __init__(self):
        super().__init__("429 server overloaded, retry later")


def test_single_call_backpressure_halt_carries_retry_after(monkeypatch, capsys):
    backend = _Backend("openrouter", [_Backpressure()])
    _wire(monkeypatch, {"or-a": backend})
    code, out, _ = _main(capsys, "complete", "--models", "or-a")
    env = json.loads(out)
    assert code == cli.EXIT_HALT
    assert env["response"]["error"]["code"] == "backpressure"
    assert env["halt"] == {"kind": "backpressure", "retry_after_s": 3.0}


def test_fallback_backpressure_halt_carries_retry_after(monkeypatch, capsys):
    backends = {"or-a": _Backend("openrouter", [_Backpressure()]),
                "or-b": _Backend("openrouter", [_Backpressure()])}
    _wire(monkeypatch, backends)
    code, out, _ = _main(capsys, "complete", "--models", "or-a,or-b")
    env = json.loads(out)
    assert code == cli.EXIT_HALT
    assert env["attempts"][0]["halt"] == "backpressure"
    assert env["attempts"][0]["halt_payload"] == {"kind": "backpressure", "retry_after_s": 3.0}
    assert env["halt"] == {"kind": "backpressure", "retry_after_s": 3.0}


def test_fallback_non_backpressure_halt_payload_is_kind_only(monkeypatch, capsys):
    backends = {"or-a": _Backend("openrouter", [_Halt(HALT_AUTH)]),
                "or-b": _Backend("openrouter", [_ok()])}
    _wire(monkeypatch, backends)
    _, out, _ = _main(capsys, "complete", "--models", "or-a,or-b")
    env = json.loads(out)
    assert env["attempts"][0]["halt_payload"] == {"kind": HALT_AUTH}
    assert "halt" not in env


def test_max_attempts_on_a_single_call_is_refused_not_ignored(monkeypatch, capsys):
    backends = {"or-a": _Backend("openrouter", [_ok("never")])}
    _wire(monkeypatch, backends)
    code, out, err = _main(capsys, "complete", "--models", "or-a", "--max-attempts", "3")
    assert code == 2 and out == ""
    assert "--max-attempts" in json.loads(err)["error"]["message"]
    assert backends["or-a"].calls == 0
