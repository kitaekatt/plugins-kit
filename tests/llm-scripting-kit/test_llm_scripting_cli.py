import dataclasses
import io
import json

import pytest

from llm_scripting_kit import cli
from llm_scripting_kit.completion.factory import BackendSelection
from llm_scripting_kit.completion.types import LLMResponse
from llm_scripting_kit.reachability import (
    STATUS_REACHABLE,
    STATUS_UNKNOWN,
    STATUS_UNREACHABLE,
    Reachability,
)


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
    # dropped/forwarded do NOT depend on the outcome -- whether the adapter
    # reads or validates a param is a property of the adapter, not the result
    assert resp["dropped_params"] == []
    assert resp["forwarded_params"] == []


def test_complete_envelope_carries_forwarded_params(monkeypatch, capsys):
    """A param sent downstream unvalidated is reported apart from the dropped."""
    backend = FakeBackend(
        LLMResponse(
            text="answer",
            model="model-id",
            dropped_params=("extras.alpha",),
            forwarded_params=("extras.top_k",),
        )
    )
    monkeypatch.setattr(cli, "create_backend", lambda *_, **__: _selection(backend))

    assert cli.main(["complete", "--prompt", "hello"]) == cli.EXIT_OK
    resp = json.loads(capsys.readouterr().out)["response"]
    assert resp["dropped_params"] == ["extras.alpha"]
    assert resp["forwarded_params"] == ["extras.top_k"]


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


# ---------------------------------------------------------------------------
# The versioned request/result protocol
# ---------------------------------------------------------------------------


def _request(tmp_path, payload, name="req.json"):
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _protocol_error(capsys):
    """The stderr envelope of a refused request."""
    captured = capsys.readouterr()
    assert captured.out == "", "a protocol error must not write to the result channel"
    return json.loads(captured.err)


def test_result_envelope_carries_the_protocol_version(monkeypatch, capsys):
    backend = FakeBackend(LLMResponse(text="answer", model="model-id"))
    monkeypatch.setattr(cli, "create_backend", lambda *_, **__: _selection(backend))

    assert cli.main(["complete", "--prompt", "hi"]) == cli.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["protocol"] == cli.PROTOCOL_VERSION
    # the pre-version keys are untouched: adding a field cannot break a reader
    assert {"endpoint", "kind", "backend", "response"} <= set(payload)


def test_a_request_reaches_the_backend_with_its_options(monkeypatch, capsys, tmp_path):
    """The four params that had no CLI path before, all on one surface."""
    backend = FakeBackend(LLMResponse(text="ok", model="model-id"))
    monkeypatch.setattr(cli, "create_backend", lambda *_, **__: _selection(backend))
    path = _request(
        tmp_path,
        {
            "protocol": 1,
            "system": "instructions",
            "prompt": "question",
            "options": {
                "allowed_tools": "Read",
                "disallowed_tools": "Bash",
                "system_prompt_mode": "append",
                "extras": {"top_k": 40},
            },
        },
    )

    assert cli.main(["complete", "--request-file", path]) == cli.EXIT_OK
    system, user, model, options = backend.call
    assert (system, user, model) == ("instructions", "question", "model-id")
    assert options.allowed_tools == "Read"
    assert options.disallowed_tools == "Bash"
    assert options.system_prompt_mode == "append"
    assert options.extras == {"top_k": 40}
    json.loads(capsys.readouterr().out)


def test_a_request_reads_stdin_when_asked(monkeypatch, capsys):
    backend = FakeBackend(LLMResponse(text="ok", model="model-id"))
    monkeypatch.setattr(cli, "create_backend", lambda *_, **__: _selection(backend))
    monkeypatch.setattr(
        cli.sys, "stdin", io.StringIO(json.dumps({"protocol": 1, "prompt": "hi"}))
    )

    assert cli.main(["complete", "--request-file", "-"]) == cli.EXIT_OK
    assert backend.call[1] == "hi"
    json.loads(capsys.readouterr().out)


def test_the_cli_still_owns_effort_fallback_and_log_prefix(monkeypatch, capsys, tmp_path):
    backend = FakeBackend(LLMResponse(text="ok", model="model-id"))
    monkeypatch.setattr(cli, "create_backend", lambda *_, **__: _selection(backend))
    path = _request(tmp_path, {"protocol": 1, "prompt": "hi"})

    assert cli.main(["complete", "--request-file", path]) == cli.EXIT_OK
    # the selection's effort fills in, exactly as it does on the flag surface
    assert backend.call[3].effort == "high"
    assert backend.call[3].log_prefix == "[chosen]"
    json.loads(capsys.readouterr().out)


def test_a_request_may_not_be_combined_with_call_flags(monkeypatch, capsys, tmp_path):
    """Refused rather than merged: a precedence rule is invisible at the call site."""
    monkeypatch.setattr(cli, "create_backend", lambda *_, **__: pytest.fail("no call"))
    path = _request(tmp_path, {"protocol": 1, "prompt": "hi"})

    assert (
        cli.main(["complete", "--request-file", path, "--effort", "high"])
        == cli.EXIT_PROTOCOL
    )
    assert "--effort" in _protocol_error(capsys)["error"]["message"]


@pytest.mark.parametrize(
    "payload, expected",
    [
        ({"prompt": "hi"}, "missing 'protocol'"),
        ({"protocol": 99, "prompt": "hi"}, "unsupported protocol"),
        ({"protocol": 1, "nonsense": 1}, "unknown request key"),
        ({"protocol": 1, "options": {"nonsense": 1}}, "unknown option"),
        ({"protocol": 1, "options": {"log_prefix": "[x]"}}, "derived by the CLI"),
        ({"protocol": 1, "options": {"max_tokens": "many"}}, "must be an integer"),
        ({"protocol": 1, "options": {"temperature": "hot"}}, "must be a number"),
        ({"protocol": 1, "options": {"extras": 5}}, "must be a JSON object"),
        ({"protocol": 1, "options": {"cwd": 5}}, "must be a string path"),
        ({"protocol": 1, "cheap": "yes"}, "cheap must be a boolean"),
        ({"protocol": 1, "prompt": 5}, "prompt must be a string"),
        ([1, 2], "request must be a JSON object"),
    ],
)
def test_a_malformed_request_is_a_protocol_error(
    monkeypatch, capsys, tmp_path, payload, expected
):
    """No call is attempted, so the failure is neither an endpoint error nor a result."""
    monkeypatch.setattr(cli, "create_backend", lambda *_, **__: pytest.fail("no call"))
    path = _request(tmp_path, payload)

    assert cli.main(["complete", "--request-file", path]) == cli.EXIT_PROTOCOL
    envelope = _protocol_error(capsys)
    assert envelope["protocol"] == cli.PROTOCOL_VERSION
    assert envelope["error"]["kind"] == "protocol"
    assert expected in envelope["error"]["message"]
    # there is no call to describe, so no response object is invented for one
    assert "response" not in envelope


def test_invalid_json_is_a_protocol_error(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cli, "create_backend", lambda *_, **__: pytest.fail("no call"))
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")

    assert cli.main(["complete", "--request-file", str(path)]) == cli.EXIT_PROTOCOL
    assert "not valid JSON" in _protocol_error(capsys)["error"]["message"]


def test_a_protocol_error_is_distinct_from_a_failed_call(monkeypatch, capsys, tmp_path):
    """The distinction the separate exit code exists for.

    A malformed request never ran and retrying the same bytes cannot help; a
    failed call ran and may succeed on retry. Collapsing them would tell a
    caller to retry what can only fail, or to fix what was already correct.
    """
    monkeypatch.setattr(cli, "create_backend", lambda *_, **__: pytest.fail("no call"))
    path = _request(tmp_path, {"protocol": 1, "options": {"max_tokens": "many"}})
    assert cli.main(["complete", "--request-file", path]) == cli.EXIT_PROTOCOL
    capsys.readouterr()

    backend = FakeBackend(error=RuntimeError("boom"))
    monkeypatch.setattr(cli, "create_backend", lambda *_, **__: _selection(backend))
    assert cli.main(["complete", "--prompt", "hi"]) == cli.EXIT_FAILURE
    # the failed call DOES get a result envelope on stdout
    assert json.loads(capsys.readouterr().out)["response"]["status"] == "error"


def test_the_request_schema_is_derived_from_backend_options(capsys):
    """A hand-written schema would be a second source of truth."""
    from llm_scripting_kit.completion import BackendOptions

    assert cli.main(["request-schema"]) == cli.EXIT_OK
    schema = json.loads(capsys.readouterr().out)
    settable = {f.name for f in dataclasses.fields(BackendOptions)} - {"log_prefix"}
    assert set(schema["options"]) == settable
    assert schema["protocol"] == cli.PROTOCOL_VERSION
    assert "log_prefix" in schema["rejected_options"]


@pytest.mark.parametrize(
    "flag, value",
    [
        ("--max-tokens", "99"),
        ("--temperature", "0.9"),
        ("--timeout", "0"),
        ("--system", ""),
    ],
)
def test_no_call_flag_is_silently_discarded_beside_a_request(
    monkeypatch, capsys, tmp_path, flag, value
):
    """Every call flag must be REFUSED, never quietly ignored.

    The falsy cases are the ones that hide: `--max-tokens` and `--temperature`
    once carried non-None argparse defaults, so a named value was
    indistinguishable from silence; and `0 == False` in Python, so a falsy
    conflict test would wave `--timeout 0` through.
    """
    monkeypatch.setattr(cli, "create_backend", lambda *_, **__: pytest.fail("no call"))
    path = _request(tmp_path, {"protocol": 1, "prompt": "hi"})

    argv = ["complete", "--request-file", path, flag]
    if value != "":
        argv.append(value)
    else:
        argv.append("")
    assert cli.main(argv) == cli.EXIT_PROTOCOL
    assert flag in _protocol_error(capsys)["error"]["message"]


# ---------------------------------------------------------------------------
# `endpoints --verify` and `probe` -- reachability, never a completion
# ---------------------------------------------------------------------------


def _fake_entries(_project_root):
    """Stand-in for cli._collect_endpoint_entries, isolated from real config."""
    config = {"default_endpoint": "openrouter"}
    discovery = type("D", (), {"notes": []})()
    values = {
        "openrouter": {"kind": "transport", "base_url": "http://a/v1", "key_env": "K"},
        "sol": {"kind": "harness", "harness": "codex", "model": "gpt-5-codex"},
    }
    return config, discovery, values


def test_endpoints_without_verify_makes_no_reachability_call(monkeypatch, capsys):
    """Plain `endpoints` must stay instant and offline -- no network, no subprocess."""
    monkeypatch.setattr(cli, "_collect_endpoint_entries", _fake_entries)

    def _fail_if_called(*_a, **_kw):
        pytest.fail("check_many must not run without --verify")

    monkeypatch.setattr(cli, "check_many", _fail_if_called)

    assert cli.main(["endpoints"]) == cli.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert "reachability" not in payload["endpoints"]["openrouter"]
    assert "reachability" not in payload["endpoints"]["sol"]


def test_endpoints_verify_adds_a_reachability_field_per_entry(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_collect_endpoint_entries", _fake_entries)
    calls = []

    def _fake_check_many(values, *, timeout, project_root):
        calls.append((set(values), timeout, project_root))
        return {
            "openrouter": Reachability(status=STATUS_REACHABLE, checked="models-endpoint", detail="ok"),
            "sol": Reachability(status=STATUS_UNREACHABLE, checked="cli-version", detail="`codex` not found on PATH"),
        }

    monkeypatch.setattr(cli, "check_many", _fake_check_many)

    assert cli.main(["endpoints", "--verify", "--timeout", "3"]) == cli.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["endpoints"]["openrouter"]["reachability"] == {
        "status": "reachable", "checked": "models-endpoint", "detail": "ok",
    }
    assert payload["endpoints"]["sol"]["reachability"]["status"] == "unreachable"
    # the existing shape (kind, base_url, harness, ...) is untouched
    assert payload["endpoints"]["openrouter"]["base_url"] == "http://a/v1"
    assert payload["endpoints"]["sol"]["harness"] == "codex"
    assert calls == [({"openrouter", "sol"}, 3.0, None)]


def test_probe_exit_code_is_the_answer_when_reachable(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_collect_endpoint_entries", _fake_entries)
    monkeypatch.setattr(
        cli, "check_entry",
        lambda entry, name, *, timeout, project_root: Reachability(
            status=STATUS_REACHABLE, checked="models-endpoint", detail="ok"
        ),
    )

    assert cli.main(["probe", "--endpoint", "openrouter"]) == cli.EXIT_OK
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["endpoint"] == "openrouter"
    assert payload["reachability"] == {"status": "reachable", "checked": "models-endpoint", "detail": "ok"}
    assert captured.err == ""


def test_probe_exit_code_is_failure_when_unreachable(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_collect_endpoint_entries", _fake_entries)
    monkeypatch.setattr(
        cli, "check_entry",
        lambda entry, name, *, timeout, project_root: Reachability(
            status=STATUS_UNREACHABLE, checked="cli-version", detail="`codex` not found on PATH"
        ),
    )

    assert cli.main(["probe", "--endpoint", "sol"]) == cli.EXIT_FAILURE
    captured = capsys.readouterr()
    assert "codex` not found on PATH" in captured.err
    payload = json.loads(captured.out)
    assert payload["reachability"]["status"] == "unreachable"


def test_probe_exit_code_is_indeterminate_when_the_check_could_not_run(monkeypatch, capsys):
    """DEFECT 1 regression coverage at the CLI surface: a check that could not
    run must exit a THIRD, distinguishable code -- never EXIT_OK (0) and never
    EXIT_FAILURE (1), the code an "unreachable" verdict returns. A caller
    branching only on `== 0` is safe either way, but one branching on
    `!= 0 -> down` must be able to tell 1 and 5 apart, which is the entire
    point of this exit code.
    """
    monkeypatch.setattr(cli, "_collect_endpoint_entries", _fake_entries)
    monkeypatch.setattr(
        cli, "check_entry",
        lambda entry, name, *, timeout, project_root: Reachability(
            status=STATUS_UNKNOWN, checked="cli-version",
            detail="no reachability check registered for harness 'bogus'",
        ),
    )

    exit_code = cli.main(["probe", "--endpoint", "sol"])
    assert exit_code == cli.EXIT_INDETERMINATE
    assert exit_code not in (cli.EXIT_OK, cli.EXIT_FAILURE, cli.EXIT_USAGE)
    captured = capsys.readouterr()
    assert "no reachability check registered" in captured.err
    payload = json.loads(captured.out)
    assert payload["reachability"]["status"] == "unknown"


def test_probe_defaults_to_the_default_endpoint(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_collect_endpoint_entries", _fake_entries)
    seen = []
    monkeypatch.setattr(
        cli, "check_entry",
        lambda entry, name, *, timeout, project_root: (
            seen.append(name),
            Reachability(status=STATUS_REACHABLE, checked="models-endpoint", detail="ok"),
        )[1],
    )

    assert cli.main(["probe"]) == cli.EXIT_OK
    assert seen == ["openrouter"]


def test_probe_unknown_endpoint_name_is_still_exit_usage_not_indeterminate(monkeypatch, capsys):
    """A NAME that does not resolve to configuration (EXIT_USAGE, 2) must stay
    distinct from a resolved endpoint whose CHECK could not run
    (EXIT_INDETERMINATE, 5) -- two different failure axes, decided at two
    different points (before vs. after a check is even attempted).
    """
    monkeypatch.setattr(cli, "_collect_endpoint_entries", _fake_entries)

    assert cli.main(["probe", "--endpoint", "no-such-endpoint"]) == cli.EXIT_USAGE
    envelope = json.loads(capsys.readouterr().err)
    assert envelope["error"]["kind"] == "configuration"
    assert "no-such-endpoint" in envelope["error"]["message"]


def test_probe_never_makes_a_completion_call(monkeypatch, capsys):
    """The exit-code answer must come from reachability, never `create_backend`."""
    monkeypatch.setattr(cli, "_collect_endpoint_entries", _fake_entries)
    monkeypatch.setattr(
        cli, "check_entry",
        lambda entry, name, *, timeout, project_root: Reachability(
            status=STATUS_REACHABLE, checked="models-endpoint", detail="ok"
        ),
    )
    monkeypatch.setattr(cli, "create_backend", lambda *_a, **_kw: pytest.fail("no completion call"))

    assert cli.main(["probe", "--endpoint", "openrouter"]) == cli.EXIT_OK


def test_the_flag_surface_leaves_temperature_unset(monkeypatch, capsys):
    """A bare call inherits the server/model temperature default."""
    backend = FakeBackend(LLMResponse(text="ok", model="model-id"))
    monkeypatch.setattr(cli, "create_backend", lambda *_, **__: _selection(backend))

    assert cli.main(["complete", "--prompt", "hi"]) == cli.EXIT_OK
    options = backend.call[3]
    assert options.max_tokens == 4096
    assert options.temperature is None
    assert backend.call[0] == ""
    json.loads(capsys.readouterr().out)


def test_which_with_a_malformed_registry_gets_the_configuration_envelope_not_a_traceback(
    monkeypatch, capsys
):
    """status/set-key/which used to return BEFORE main()'s try/except that maps
    (EndpointResolveError, ModelResolveError, EndpointRegistryError, OSError,
    ValueError) to the configuration envelope + EXIT_USAGE.
    _resolve_endpoint_or_exit only ever catches EndpointResolveError, so an
    EndpointRegistryError (a malformed registry file) escaped `which` as a raw
    traceback instead of the documented exit-2 envelope.
    """

    def _raise(*_a, **_kw):
        raise cli.EndpointRegistryError("malformed registry: bad yaml")

    monkeypatch.setattr(cli, "resolve_endpoint", _raise)

    assert cli.main(["which"]) == cli.EXIT_USAGE
    envelope = json.loads(capsys.readouterr().err)
    assert envelope["error"]["kind"] == "configuration"
    assert "malformed registry" in envelope["error"]["message"]


def test_set_key_with_an_unwritable_target_gets_the_configuration_envelope_not_a_traceback(
    monkeypatch, capsys, tmp_path
):
    """Same gap as `which`, for `set-key`: an OSError from write_env_file (e.g.
    an unwritable USER_ENV_FILE) escaped as a raw traceback instead of the
    documented exit-2 configuration envelope.
    """
    # Redirect USER_ENV_FILE to a tmp path so the read_env_file() call that
    # precedes the write never touches this host's real credential file.
    monkeypatch.setattr(cli, "USER_ENV_FILE", tmp_path / ".env")

    def _raise(*_a, **_kw):
        raise OSError("[Errno 13] Permission denied: '.env'")

    monkeypatch.setattr(cli, "write_env_file", _raise)

    assert cli.main(["set-key", "--key", "sk-or-v1-whatever", "--no-validate"]) == cli.EXIT_USAGE
    envelope = json.loads(capsys.readouterr().err)
    assert envelope["error"]["kind"] == "configuration"
    assert "Permission denied" in envelope["error"]["message"]
